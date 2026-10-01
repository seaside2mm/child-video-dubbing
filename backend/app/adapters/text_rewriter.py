from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..config import Settings
from .base import BlockedError, RemoteServiceError, ServiceStatus


LEVEL_RULES = {
    "L1": "只保留最常用的短句、基础动作和高频词；尽量 3-8 个汉字或 2-6 个英文词。",
    "L2": "使用常见生活词和简单连接词；避免复杂从句；尽量不超过原句信息量。",
    "L3": "允许常用叙事句和因果关系；保持儿童可理解，避免生硬直译。",
    "L4": "允许较丰富的词汇与复合句，但不添加原文没有的剧情信息。",
    "L5": "保留自然叙事细节与语气，仅在超出时间窗口时做最小压缩。",
}


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RemoteServiceError("REWRITER_INVALID_JSON", "改写服务返回不是有效 JSON", "检查模型是否遵守结构化输出要求") from exc
    if not isinstance(value, dict):
        raise RemoteServiceError("REWRITER_INVALID_SHAPE", "改写服务返回必须是对象")
    return value


class TextRewriterAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def status(self) -> ServiceStatus:
        if not (self.settings.text_base_url and self.settings.text_api_key and self.settings.text_model):
            return ServiceStatus("missing", "未配置文本 API 地址、模型或密钥", self.settings.text_base_url or None, self.settings.text_model or None)
        return ServiceStatus("configured", "已配置 OpenAI 兼容文本服务", self.settings.text_base_url, self.settings.text_model)

    def rewrite(
        self,
        segments: list[dict[str, Any]],
        *,
        source_language: str,
        target_language: str,
        level: str,
        speed: float,
        measured_duration: float | None = None,
        current_target_text: str | None = None,
        retry_index: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self.settings.text_base_url or not self.settings.text_api_key or not self.settings.text_model:
            raise BlockedError("TEXT_API_NOT_CONFIGURED", "未配置真实文本 API", "配置 DUBBING_TEXT_BASE_URL、DUBBING_TEXT_MODEL 和 DUBBING_TEXT_API_KEY（或对应 OPENAI_* 变量）")
        if not segments:
            return []
        rules = LEVEL_RULES[level]
        source = []
        for item in segments:
            window = max(0.01, float(item["end"]) - float(item["start"]))
            budget_window = window
            if measured_duration is not None and measured_duration > window:
                ratio = window / max(float(measured_duration), 0.01)
                budget_window = max(0.1, window * ratio * 0.92)
            value = {
                "id": str(item["id"]),
                "start": round(float(item["start"]), 3),
                "end": round(float(item["end"]), 3),
                "speaker": item.get("speaker_name") or item.get("speaker_key") or "角色",
                "text": item.get("source_text", ""),
                "time_window_seconds": round(window, 3),
                "max_target_units": self._budget(budget_window, speed, target_language),
            }
            if measured_duration is not None:
                value["measured_audio_duration"] = round(float(measured_duration), 3)
                value["current_target_text"] = current_target_text or ""
                value["rewrite_attempt"] = retry_index or 1
            source.append(value)
        system = (
            "你是儿童视频配音文本编辑。只处理对白，不处理歌曲。保持剧情事实、角色关系、关键动作与语气。"
            "不得补写原文没有的信息，不得为了填满时间而编造台词；可以自然留白。输出必须是 JSON 对象，"
            "格式为 {\"segments\":[{\"id\":string,\"target_text\":string}]}，id 必须逐一对应且不增不减。"
        )
        if measured_duration is not None:
            system += (
                "这是超出时间窗后的候选重写：必须保持同一等级规则和原文意义，参考 measured_audio_duration，"
                "在 time_window_seconds 内进一步压缩 current_target_text；不要删掉关键动作、人物关系或否定含义。"
            )
        user = {
            "source_language": source_language,
            "target_language": target_language,
            "level": level,
            "level_rule": rules,
            "speed": speed,
            "segments": source,
        }
        payload = {
            "model": self.settings.text_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        try:
            response = httpx.post(
                f"{self.settings.text_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.text_api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=300,
            )
        except httpx.HTTPError as exc:
            raise RemoteServiceError("TEXT_API_UNAVAILABLE", "文本 API 请求失败", "检查地址、密钥和服务日志", {"error": exc.__class__.__name__}) from exc
        if response.status_code in {404, 405}:
            # Some OpenAI-compatible gateways expose Responses only. Use it
            # only when Chat Completions is genuinely unavailable; a 401/429
            # must remain an honest authentication/rate-limit failure.
            response = self._responses_request(system, user)
        if response.status_code >= 400:
            raise RemoteServiceError("TEXT_API_FAILED", f"文本 API 返回 HTTP {response.status_code}", "检查模型、额度或结构化输出支持", {"http_status": response.status_code})
        try:
            outer = response.json()
            content = self._response_content(outer)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RemoteServiceError("TEXT_API_INVALID_RESPONSE", "文本 API 返回缺少可解析的文本内容") from exc
        value = _extract_json(str(content))
        result = value.get("segments")
        if not isinstance(result, list) or len(result) != len(source):
            raise RemoteServiceError("REWRITER_COUNT_MISMATCH", "改写结果条数与输入不一致", "局部重试该场景")
        expected = [str(item["id"]) for item in source]
        seen = [str(item.get("id")) for item in result]
        if seen != expected:
            raise RemoteServiceError("REWRITER_ID_MISMATCH", "改写结果 ID 顺序或集合不一致", "局部重试该场景")
        validated: list[dict[str, Any]] = []
        for raw, original in zip(result, source):
            text = str(raw.get("target_text", "")).strip()
            if not text:
                raise RemoteServiceError("REWRITER_EMPTY_TEXT", f"片段 {original['id']} 的目标文本为空")
            budget = int(original["max_target_units"])
            units = self._units(text, target_language)
            if units > max(budget * 2, budget + 4):
                raise RemoteServiceError("REWRITER_OVER_BUDGET", f"片段 {original['id']} 超出时长字数预算", "降低级别或编辑目标文本后重试", {"units": units, "budget": budget})
            validated.append({"id": str(raw["id"]), "target_text": text, "units": units, "budget": budget})
        return validated

    def _responses_request(self, system: str, user: dict[str, Any]) -> httpx.Response:
        payload = {
            "model": self.settings.text_model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": json.dumps(user, ensure_ascii=False)}]},
            ],
            "temperature": 0.2,
            "text": {"format": {"type": "json_object"}},
        }
        try:
            return httpx.post(
                f"{self.settings.text_base_url}/responses",
                headers={"Authorization": f"Bearer {self.settings.text_api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=300,
            )
        except httpx.HTTPError as exc:
            raise RemoteServiceError("TEXT_API_UNAVAILABLE", "文本 API Responses 请求失败", "检查地址、密钥和服务日志", {"error": exc.__class__.__name__}) from exc

    @staticmethod
    def _response_content(outer: dict[str, Any]) -> str:
        if isinstance(outer.get("output_text"), str):
            return outer["output_text"]
        choices = outer.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
                if texts:
                    return "".join(texts)
        output = outer.get("output")
        if isinstance(output, list):
            texts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                for content in item.get("content") or []:
                    if isinstance(content, dict) and isinstance(content.get("text"), str):
                        texts.append(content["text"])
            if texts:
                return "".join(texts)
        raise KeyError("content")

    @staticmethod
    def _units(text: str, language: str) -> int:
        if language.lower().startswith(("zh", "ja")):
            return len(re.findall(r"[\u3400-\u9fff]", text)) + len(re.findall(r"[A-Za-z0-9]+", text))
        return len(re.findall(r"\b[\w']+\b", text))

    @classmethod
    def _budget(cls, duration: float, speed: float, language: str) -> int:
        # Conservative readable rate; generated audio is measured again later.
        units_per_second = 4.5 if language.lower().startswith(("zh", "ja")) else 2.1
        return max(2, int(duration * units_per_second * max(0.55, min(speed, 1.15))))
