from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..config import Settings
from .base import RemoteServiceError, ServiceStatus


class FasterWhisperAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def status(self) -> ServiceStatus:
        try:
            response = httpx.get(f"{self.settings.faster_whisper_url}/health", timeout=5)
            if response.status_code >= 400:
                return ServiceStatus("error", f"HTTP {response.status_code}", self.settings.faster_whisper_url, self.settings.faster_whisper_model)
            return ServiceStatus("ok", "Speaches health 正常", self.settings.faster_whisper_url, self.settings.faster_whisper_model)
        except httpx.HTTPError as exc:
            return ServiceStatus("blocked", f"服务不可达：{exc.__class__.__name__}", self.settings.faster_whisper_url, self.settings.faster_whisper_model)

    def ensure_model_loaded(self) -> None:
        model_id = quote(self.settings.faster_whisper_model, safe="")
        try:
            response = httpx.get(f"{self.settings.faster_whisper_url}/api/ps", timeout=5)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and self.settings.faster_whisper_model in payload.get("models", []):
                    return
        except (httpx.HTTPError, ValueError):
            pass
        try:
            # Speaches loads the model synchronously; the first cold load can
            # outlast 30 seconds and raise a misleading connection timeout.
            response = httpx.post(f"{self.settings.faster_whisper_url}/api/ps/{model_id}", timeout=300)
        except httpx.HTTPError as exc:
            raise RemoteServiceError("FASTER_WHISPER_UNAVAILABLE", "faster-whisper 模型生命周期接口不可达", "检查 8001 服务", {"error": exc.__class__.__name__}) from exc
        already_loaded = False
        if response.status_code == 409:
            already_loaded = response.text.strip() == "Model already loaded"
            if not already_loaded:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                already_loaded = isinstance(payload, dict) and payload.get("detail") == "Model already loaded"
        if response.status_code not in {200, 201, 202, 204, 404} and not already_loaded:
            raise RemoteServiceError("FASTER_WHISPER_MODEL_LOAD_FAILED", f"faster-whisper 模型加载失败 HTTP {response.status_code}", "检查模型缓存与 GPU 显存", {"detail": response.text[-1000:]})

    def transcribe(self, audio_path: Path, language: str | None = None) -> dict[str, Any]:
        self.ensure_model_loaded()
        # Speaches' multipart parser expects a mapping here.  Passing the
        # repeated fields as a list of tuples makes the request reach the
        # server but raises a TypeError while normalizing form values.  A
        # single segment granularity is sufficient for this product's
        # sentence-level timeline and avoids the broken word-field path.
        data: dict[str, Any] = {
            "model": self.settings.faster_whisper_model,
            "timestamp_granularities[]": ["segment"],
            "response_format": "verbose_json",
        }
        if language and language != "auto":
            # Speaches/faster-whisper expects the short Whisper language code,
            # while the product stores BCP-47 values such as zh-CN/en-US.
            normalized_language = language.split("-", 1)[0].split("_", 1)[0].lower()
            data["language"] = normalized_language
        try:
            with audio_path.open("rb") as handle:
                response = httpx.post(
                    f"{self.settings.faster_whisper_url}/v1/audio/transcriptions",
                    files={"file": (audio_path.name, handle, "audio/wav")},
                    data=data,
                    timeout=1800,
                )
        except (OSError, httpx.HTTPError) as exc:
            raise RemoteServiceError("FASTER_WHISPER_TRANSCRIBE_FAILED", "faster-whisper 转写请求失败", "检查服务、音频和模型状态", {"error": exc.__class__.__name__}) from exc
        if response.status_code >= 400:
            raise RemoteServiceError("FASTER_WHISPER_TRANSCRIBE_FAILED", f"faster-whisper 返回 HTTP {response.status_code}", "检查请求字段与模型", {"detail": response.text[-1000:]})
        try:
            payload = response.json()
        except ValueError as exc:
            raise RemoteServiceError("FASTER_WHISPER_INVALID_RESPONSE", "faster-whisper 返回不是 JSON", "检查 response_format=verbose_json") from exc
        raw_segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(raw_segments, list):
            raise RemoteServiceError("FASTER_WHISPER_NO_SEGMENTS", "faster-whisper 未返回句级 segments", "确认服务支持 verbose_json 与 timestamp_granularities")
        segments: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_segments):
            try:
                start = float(raw["start"])
                end = float(raw["end"])
                text = str(raw.get("text", "")).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start or not text:
                continue
            segments.append({"index": index, "start": start, "end": end, "text": text, "words": raw.get("words") or []})
        if not segments:
            raise RemoteServiceError("FASTER_WHISPER_EMPTY", "faster-whisper 返回了空对白", "检查片段是否包含清晰人声")
        return {"language": payload.get("language"), "text": payload.get("text", ""), "segments": segments}
