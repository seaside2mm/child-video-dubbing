from __future__ import annotations

import base64
import json
import mimetypes
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from .base import AdapterError, BlockedError, RemoteServiceError, ServiceStatus


class OmniVoiceAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._openapi: dict[str, Any] | None = None

    def status(self) -> ServiceStatus:
        try:
            response = httpx.get(f"{self.settings.omnivoice_url}/tts/ping", timeout=8)
            if response.status_code >= 400:
                return ServiceStatus("error", f"/tts/ping HTTP {response.status_code}", self.settings.omnivoice_url, self.settings.omnivoice_model)
            profile = self.profile_creation_available()
            detail = "OmniVoice /tts/ping 正常"
            if not profile:
                detail += "；Gradio 自动建档接口未发现"
            else:
                detail += "；Gradio 自动建档接口可发现"
            return ServiceStatus("ok", detail, self.settings.omnivoice_url, self.settings.omnivoice_model)
        except httpx.HTTPError as exc:
            return ServiceStatus("blocked", f"服务不可达：{exc.__class__.__name__}", self.settings.omnivoice_url, self.settings.omnivoice_model)

    def _get_openapi(self) -> dict[str, Any]:
        if self._openapi is None:
            try:
                response = httpx.get(f"{self.settings.omnivoice_url}/gradio_api/openapi.json", timeout=15)
            except httpx.HTTPError as exc:
                raise RemoteServiceError("OMNIVOICE_OPENAPI_UNAVAILABLE", "无法读取 OmniVoice Gradio OpenAPI", "确认 7861 服务与 /gradio_api/openapi.json", {"error": exc.__class__.__name__}) from exc
            if response.status_code >= 400:
                raise RemoteServiceError("OMNIVOICE_OPENAPI_FAILED", f"OmniVoice Gradio OpenAPI HTTP {response.status_code}")
            try:
                self._openapi = response.json()
            except ValueError as exc:
                raise RemoteServiceError("OMNIVOICE_OPENAPI_INVALID", "OmniVoice Gradio OpenAPI 不是 JSON") from exc
        return self._openapi

    def profile_creation_available(self) -> bool:
        try:
            paths = self._get_openapi().get("paths", {})
        except AdapterError:
            return False
        has_upload = any(path.rstrip("/").endswith("/upload") and "post" in operations for path, operations in paths.items())
        has_profile = any(
            "save_openai_voice_profile_from_ui" in str(path)
            and "post" in operations
            for path, operations in paths.items()
        )
        return has_upload and has_profile

    def _profile_endpoints(self) -> tuple[str, str]:
        """Resolve the POST and SSE GET paths from the running Gradio schema.

        Gradio versions have used both ``call/<fn>`` and
        ``call/v2/<fn>`` for the POST while keeping the event GET at
        ``call/<fn>/{event_id}``.  Hard-coding one of those variants makes
        profile creation look available but fail at runtime.
        """
        paths = self._get_openapi().get("paths", {})
        post_path = next(
            (
                path
                for path, operations in paths.items()
                if "save_openai_voice_profile_from_ui" in str(path)
                and "post" in operations
            ),
            None,
        )
        if not post_path:
            raise BlockedError("OMNIVOICE_PROFILE_UNSUPPORTED", "当前 OmniVoice 没有可发现的 Gradio 自动建档接口")
        function_name = "save_openai_voice_profile_from_ui"
        get_path = next(
            (
                path
                for path, operations in paths.items()
                if function_name in str(path)
                and "get" in operations
                and ("{event_id}" in str(path) or str(path).rstrip("/").endswith(function_name))
            ),
            None,
        )
        if not get_path:
            # The published schema has occasionally omitted the GET path.
            # The non-v2 form is the only documented Gradio SSE route.
            get_path = f"/gradio_api/call/{function_name}/{{event_id}}"
        return str(post_path), str(get_path)

    @staticmethod
    def _schema_for_operation(operation: dict[str, Any]) -> dict[str, Any]:
        body = (operation.get("requestBody") or {}).get("content") or {}
        content = body.get("application/json") or next(iter(body.values()), {})
        schema = content.get("schema") or {}
        return schema if isinstance(schema, dict) else {}

    def _profile_request_body(self, post_path: str, name: str, file_data: dict[str, Any], ref_text: str, language: str, seed: int) -> dict[str, Any]:
        operation = (self._get_openapi().get("paths", {}).get(post_path) or {}).get("post") or {}
        schema = self._schema_for_operation(operation)
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if properties and "data" not in properties:
            values: dict[str, Any] = {
                "name": name,
                "audio_path": file_data,
                "ref_text": ref_text,
                "language": language,
                "seed": seed,
                "randomize_seed": False,
            }
            return {key: values[key] for key in properties if key in values}
        # Older Gradio call endpoints accept positional data. Keep this as a
        # schema-derived fallback rather than assuming it is always valid.
        return {"data": [name, file_data, ref_text, language, seed, False]}

    def create_voice_profile(self, name: str, audio_path: Path, ref_text: str, *, language: str = "zh", seed: int = 240901) -> str:
        if not self.profile_creation_available():
            raise BlockedError("OMNIVOICE_PROFILE_UNSUPPORTED", "当前 OmniVoice 没有可发现的 Gradio 自动建档接口", "在 7861 的 Voices 页面建立 profile，或升级到提供 save_openai_voice_profile_from_ui 的服务")
        mime = mimetypes.guess_type(audio_path.name)[0] or "audio/wav"
        try:
            with audio_path.open("rb") as handle:
                upload = httpx.post(
                    f"{self.settings.omnivoice_url}/gradio_api/upload",
                    files={"files": (audio_path.name, handle, mime)},
                    timeout=180,
                )
        except (OSError, httpx.HTTPError) as exc:
            raise RemoteServiceError("OMNIVOICE_PROFILE_UPLOAD_FAILED", "OmniVoice 参考音频上传失败", "检查音频与服务", {"error": exc.__class__.__name__}) from exc
        if upload.status_code >= 400:
            raise RemoteServiceError("OMNIVOICE_PROFILE_UPLOAD_FAILED", f"OmniVoice 上传 HTTP {upload.status_code}", "检查 Gradio upload 接口", {"detail": upload.text[-1000:]})
        try:
            uploaded = upload.json()
            uploaded_path = uploaded[0] if isinstance(uploaded, list) else uploaded.get("path")
        except (ValueError, TypeError, AttributeError, IndexError) as exc:
            raise RemoteServiceError("OMNIVOICE_PROFILE_UPLOAD_INVALID", "OmniVoice 上传返回缺少文件路径") from exc
        file_data = {"path": uploaded_path, "orig_name": audio_path.name, "mime_type": mime, "meta": {"_type": "gradio.FileData"}}
        path, event_path = self._profile_endpoints()
        profile_language = {
            "zh": "chinese",
            "zh-cn": "chinese",
            "zh-tw": "chinese",
            "en": "english",
            "en-us": "english",
            "en-gb": "english",
            "auto": "Auto",
        }.get(language.lower(), language)
        body = self._profile_request_body(path, name, file_data, ref_text, profile_language, seed)
        try:
            response = httpx.post(f"{self.settings.omnivoice_url}{path}", json=body, timeout=60)
        except httpx.HTTPError as exc:
            raise RemoteServiceError("OMNIVOICE_PROFILE_CREATE_FAILED", "OmniVoice 建档请求失败", "检查 Gradio schema 与服务日志", {"error": exc.__class__.__name__}) from exc
        # A few Gradio builds publish a direct named schema but still expose
        # the legacy positional endpoint. Retry only a validation response;
        # never submit a second profile after an accepted event.
        if response.status_code in {400, 404, 405, 422} and "data" not in body:
            try:
                response = httpx.post(
                    f"{self.settings.omnivoice_url}{path}",
                    json={"data": [name, file_data, ref_text, profile_language, seed, False]},
                    timeout=60,
                )
            except httpx.HTTPError as exc:
                raise RemoteServiceError("OMNIVOICE_PROFILE_CREATE_FAILED", "OmniVoice 建档请求失败", "检查 Gradio schema 与服务日志", {"error": exc.__class__.__name__}) from exc
        if response.status_code >= 400:
            raise RemoteServiceError("OMNIVOICE_PROFILE_CREATE_FAILED", f"OmniVoice 建档 HTTP {response.status_code}", "检查建档参数", {"detail": response.text[-1200:]})
        try:
            event_id = response.json().get("event_id")
        except (ValueError, AttributeError) as exc:
            raise RemoteServiceError("OMNIVOICE_PROFILE_CREATE_INVALID", "OmniVoice 建档没有返回 event_id") from exc
        if not event_id:
            raise RemoteServiceError("OMNIVOICE_PROFILE_CREATE_INVALID", "OmniVoice 建档 event_id 为空")
        return self._wait_gradio_event(event_path, str(event_id), fallback=name)

    def _wait_gradio_event(self, call_path: str, event_id: str, *, fallback: str) -> str:
        event_url = call_path.replace("{event_id}", event_id) if "{event_id}" in call_path else f"{call_path.rstrip('/')}/{event_id}"
        try:
            with httpx.stream("GET", f"{self.settings.omnivoice_url}{event_url}", timeout=300) as response:
                if response.status_code >= 400:
                    raise RemoteServiceError("OMNIVOICE_PROFILE_EVENT_FAILED", f"OmniVoice 建档事件 HTTP {response.status_code}")
                final: Any = None
                event_error: str | None = None
                for line in response.iter_lines():
                    if line.startswith("event:") and line[6:].strip().lower() in {"error", "failed"}:
                        event_error = "Gradio 建档事件报告失败"
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw in {"", "null"}:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = raw
                    final = payload
        except httpx.HTTPError as exc:
            raise RemoteServiceError("OMNIVOICE_PROFILE_EVENT_FAILED", "读取 OmniVoice 建档事件失败", "检查 Gradio SSE", {"error": exc.__class__.__name__}) from exc
        if event_error:
            raise RemoteServiceError("OMNIVOICE_PROFILE_EVENT_FAILED", event_error)
        if final is None:
            raise RemoteServiceError("OMNIVOICE_PROFILE_EVENT_EMPTY", "OmniVoice 建档事件没有完成结果")
        profile = self._profile_id_from_result(final, fallback)
        if profile:
            self._verify_profile_exists(profile)
            return profile
        raise RemoteServiceError("OMNIVOICE_PROFILE_EVENT_INVALID", "OmniVoice 建档结果没有明确返回 voice profile", "检查建档事件输出；不会把任意状态字符串当作音色 ID")

    def _verify_profile_exists(self, profile: str) -> None:
        """Confirm the service persisted the returned profile before using it."""
        last_ids: set[str] = set()
        for _ in range(3):
            try:
                response = httpx.get(f"{self.settings.omnivoice_url}/v1/audio/voices", timeout=15)
            except httpx.HTTPError as exc:
                raise RemoteServiceError("OMNIVOICE_PROFILE_VERIFY_FAILED", "无法验证 OmniVoice 音色列表", "检查 /v1/audio/voices", {"error": exc.__class__.__name__}) from exc
            if response.status_code >= 400:
                raise RemoteServiceError("OMNIVOICE_PROFILE_VERIFY_FAILED", f"OmniVoice 音色列表 HTTP {response.status_code}", "检查 /v1/audio/voices")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RemoteServiceError("OMNIVOICE_PROFILE_VERIFY_FAILED", "OmniVoice 音色列表不是 JSON") from exc
            last_ids = self._profile_ids(payload)
            if profile in last_ids:
                return
            time.sleep(0.5)
        raise RemoteServiceError(
            "OMNIVOICE_PROFILE_NOT_FOUND",
            f"OmniVoice 建档事件返回的音色未出现在实际列表：{profile}",
            "检查建档任务是否完成；不会使用未验证的音色 ID",
            {"profile": profile, "available_count": len(last_ids)},
        )

    @staticmethod
    def _profile_ids(value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key in ("id", "name", "voice_profile", "profile"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    found.add(item.strip())
            for child in value.values():
                found.update(OmniVoiceAdapter._profile_ids(child))
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, str) and child.strip():
                    found.add(child.strip())
                found.update(OmniVoiceAdapter._profile_ids(child))
        return found

    @staticmethod
    def _profile_id_from_result(value: Any, fallback: str) -> str | None:
        if not fallback:
            return None

        success_status = False
        failure_status = False
        selected_value: str | None = None
        failure_words = ("error", "failed", "failure", "错误", "失败")
        success_words = ("saved", "created", "保存成功", "创建成功", "已保存", "已创建", "建立成功")

        def visit(item: Any) -> None:
            nonlocal failure_status, success_status, selected_value
            if isinstance(item, str):
                text = item.strip()
                lowered = text.lower()
                if any(word in lowered for word in failure_words):
                    failure_status = True
                elif any(word in lowered for word in success_words):
                    success_status = True
                return
            if isinstance(item, list):
                for child in item:
                    visit(child)
                return
            if not isinstance(item, dict):
                return
            # Gradio returns an update object for the profile dropdown. Only
            # its selected value is authoritative; choices and the saved
            # profile table may contain unrelated existing voices.
            candidate = item.get("value")
            if isinstance(candidate, str) and candidate.strip():
                selected_value = candidate.strip()
            for key in ("voice_profile", "profile", "profile_name"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    selected_value = candidate.strip()
            for key, child in item.items():
                if key not in {"choices", "table", "data", "value", "voice_profile", "profile", "profile_name"}:
                    visit(child)

        visit(value)
        if selected_value == fallback and success_status and not failure_status:
            return fallback
        return None

    def generate(
        self,
        text: str,
        output_path: Path,
        *,
        voice_profile: str | None,
        ref_audio: Path | None,
        ref_text: str | None,
        speed: float,
        language: str | None = None,
        seed: int = 240901,
        randomize_seed: bool = False,
        instruct: str | None = None,
    ) -> Path:
        if not text.strip():
            raise BlockedError("OMNIVOICE_EMPTY_TEXT", "目标文本为空，拒绝生成静音配音")
        body: dict[str, Any] = {
            self.settings.omnivoice_text_field: text,
            self.settings.omnivoice_format_field: "wav",
            self.settings.omnivoice_speed_field: speed,
            "seed": seed,
            "randomize_seed": randomize_seed,
        }
        if language and language != "auto":
            body["language"] = language.split("-", 1)[0].split("_", 1)[0].lower()
        if voice_profile:
            body[self.settings.omnivoice_voice_field] = voice_profile
        if ref_audio:
            body[self.settings.omnivoice_ref_audio_field] = str(ref_audio)
        if ref_text:
            body[self.settings.omnivoice_ref_text_field] = ref_text
        if instruct:
            body["instruct"] = instruct
        try:
            response = httpx.post(f"{self.settings.omnivoice_url}{self.settings.omnivoice_tts_path}", json=body, timeout=600)
        except httpx.HTTPError as exc:
            raise RemoteServiceError("OMNIVOICE_TTS_FAILED", "OmniVoice 配音请求失败", "检查服务与 voice profile", {"error": exc.__class__.__name__}) from exc
        if response.status_code >= 400:
            raise RemoteServiceError("OMNIVOICE_TTS_FAILED", f"OmniVoice 返回 HTTP {response.status_code}", "检查真实 voice_profile/ref_audio 参数", {"detail": response.text[-1200:]})
        output_path.parent.mkdir(parents=True, exist_ok=True)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("audio/") or response.content[:4] == b"RIFF":
            output_path.write_bytes(response.content)
        else:
            try:
                payload = response.json()
            except ValueError as exc:
                raise RemoteServiceError("OMNIVOICE_TTS_INVALID", "OmniVoice 返回既不是音频也不是 JSON") from exc
            self._write_json_audio(payload, output_path)
        if not output_path.is_file() or output_path.stat().st_size < 128:
            raise RemoteServiceError("OMNIVOICE_TTS_EMPTY", "OmniVoice 没有返回有效音频")
        return output_path

    @staticmethod
    def _write_json_audio(payload: Any, output_path: Path) -> None:
        candidates = [payload]
        if isinstance(payload, dict):
            candidates += [payload.get(key) for key in ("audio", "audio_base64", "data", "path", "url")]
            if isinstance(payload.get("result"), dict):
                candidates += [payload["result"].get(key) for key in ("audio", "path", "url")]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.startswith("data:audio") and "," in candidate:
                output_path.write_bytes(base64.b64decode(candidate.split(",", 1)[1]))
                return
            if isinstance(candidate, str):
                path = Path(candidate)
                if path.is_file():
                    output_path.write_bytes(path.read_bytes())
                    return
                if candidate.startswith(("http://", "https://")):
                    try:
                        remote = httpx.get(candidate, timeout=120)
                    except httpx.HTTPError as exc:
                        raise RemoteServiceError("OMNIVOICE_TTS_AUDIO_FETCH_FAILED", "无法读取 OmniVoice 音频 URL") from exc
                    if remote.status_code < 400 and remote.content:
                        output_path.write_bytes(remote.content)
                        return
            if isinstance(candidate, dict) and isinstance(candidate.get("data"), str):
                try:
                    output_path.write_bytes(base64.b64decode(candidate["data"]))
                    return
                except (ValueError, TypeError):
                    pass
        raise RemoteServiceError("OMNIVOICE_TTS_INVALID", "OmniVoice JSON 没有可写入的音频字段")
