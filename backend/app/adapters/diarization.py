from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..config import Settings
from .base import BlockedError, ServiceStatus
from .media import _run


COMMUNITY1_MODEL_ID = "pyannote/speaker-diarization-community-1"
COMMUNITY1_EMBEDDING_MODEL_ID = f"{COMMUNITY1_MODEL_ID}/embedding"
ECAPA_EMBEDDING_MODEL_ID = "speechbrain/spkrec-ecapa-voxceleb"


class DiarizationAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _local_command(self) -> list[str] | None:
        script = self.settings.root_dir / "scripts" / "model_diarization.py"
        runtime = self.settings.root_dir / "work" / "model-runtime" / "venv" / "Scripts" / "python.exe"
        if not script.is_file():
            return None
        if not runtime.is_file():
            runtime = Path(shutil.which("python") or "")
        if not runtime or not runtime.is_file():
            return None
        return [str(runtime), str(script)]

    def _community_command(self) -> list[str] | None:
        script = self.settings.root_dir / "scripts" / "model_community_diarization.py"
        runtime = self.settings.root_dir / "work" / "model-runtime" / "pyannote-community-1" / "venv" / "Scripts" / "python.exe"
        if not script.is_file() or not runtime.is_file():
            return None
        return [str(runtime), str(script)]

    def _community_status(self) -> tuple[list[str] | None, dict[str, Any]]:
        command = self._community_command()
        if not command:
            return None, {
                "status": "blocked",
                "code": "COMMUNITY1_RUNTIME_REQUIRED",
                "detail": "Community-1 隔离 runtime 尚未安装；不会回退到 ECAPA。",
            }
        try:
            result = subprocess.run(
                [*command, "--status", "--device", os.getenv("DIARIZATION_DEVICE", "cpu")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
            payload = json.loads(result.stdout) if result.stdout.strip() else {}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            return command, {
                "status": "error",
                "code": "COMMUNITY1_STATUS_FAILED",
                "detail": f"Community-1 隔离 runtime 状态检查失败：{exc.__class__.__name__}",
            }
        if result.returncode and not payload:
            return command, {
                "status": "error",
                "code": "COMMUNITY1_STATUS_FAILED",
                "detail": "Community-1 隔离 runtime 状态检查失败。",
            }
        return command, payload

    def status(self) -> ServiceStatus:
        if self.settings.diarization_command:
            try:
                executable = shlex.split(self.settings.diarization_command, posix=False)[0]
            except (IndexError, ValueError):
                return ServiceStatus("invalid", "DIARIZATION_COMMAND 无法解析")
            if not (Path(executable).exists() or shutil.which(executable)):
                return ServiceStatus("missing", f"找不到 diarization 命令：{executable}")
            return ServiceStatus("configured", "已配置真实 diarization 命令")
        if self.settings.pyannote_model == COMMUNITY1_MODEL_ID:
            _, payload = self._community_status()
            state = payload.get("status")
            if state == "ready":
                return ServiceStatus(
                    "configured",
                    f"{payload.get('model', COMMUNITY1_MODEL_ID)} 已就绪，{payload.get('embedding_model', COMMUNITY1_EMBEDDING_MODEL_ID)}",
                    model=COMMUNITY1_MODEL_ID,
                )
            return ServiceStatus(
                "error" if state == "error" else "blocked",
                str(payload.get("detail") or "Community-1 尚未授权或未安装。"),
                model=COMMUNITY1_MODEL_ID,
            )
        if self.settings.pyannote_model:
            try:
                import pyannote.audio  # noqa: F401
            except ImportError:
                return ServiceStatus("missing", "已配置 PYANNOTE_MODEL，但当前 Python 未安装 pyannote.audio")
            return ServiceStatus("configured", "已配置 pyannote Pipeline")
        local = self._local_command()
        if not local:
            return ServiceStatus("missing", "未配置 pyannote 模型、diarization 命令或项目隔离 speaker runtime")
        try:
            result = subprocess.run(
                [*local, "--status", "--device", os.getenv("DIARIZATION_DEVICE", "cpu")],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            payload = json.loads(result.stdout) if result.stdout.strip() else {}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            return ServiceStatus("error", f"speaker runtime 状态检查失败：{exc.__class__.__name__}")
        if result.returncode:
            return ServiceStatus("error", (result.stderr or result.stdout or "speaker 状态命令失败")[-1000:])
        if payload.get("status") == "ready":
            return ServiceStatus("configured", f"{payload.get('model', 'speaker embedding')} 已就绪，默认设备 {payload.get('device_requested', 'cpu')}")
        return ServiceStatus("missing", f"speaker runtime 已安装但 embedding 模型不完整：{payload.get('model_dir', '未知路径')}")

    def diarize(self, audio_path: Path, output_path: Path) -> list[dict[str, Any]]:
        if self.settings.diarization_command:
            command = shlex.split(self.settings.diarization_command, posix=False)
            command = [part.format(input=str(audio_path), output=str(output_path)) for part in command]
            _run(command, timeout=1800)
            if not output_path.is_file():
                raise BlockedError("DIARIZATION_NO_OUTPUT", "diarization 命令没有留下 JSON 输出")
            try:
                data = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BlockedError("DIARIZATION_INVALID_OUTPUT", "diarization JSON 无法解析") from exc
        elif self.settings.pyannote_model == COMMUNITY1_MODEL_ID:
            command, payload = self._community_status()
            if payload.get("status") != "ready" or not command:
                code = str(payload.get("code") or "COMMUNITY1_BLOCKED")
                detail = str(payload.get("detail") or "Community-1 尚未就绪；不会回退到 ECAPA。")
                raise BlockedError(code, detail, "安装隔离 runtime，并在本机接受模型条件、配置 Hugging Face token。")
            command = [
                *command,
                "--input", str(audio_path),
                "--output", str(output_path),
                "--device", os.getenv("DIARIZATION_DEVICE", "cpu"),
            ]
            _run(command, timeout=1800)
            try:
                data = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BlockedError("COMMUNITY1_INVALID_OUTPUT", "Community-1 JSON 输出无法解析") from exc
            if isinstance(data, dict) and data.get("status") == "blocked":
                raise BlockedError(
                    str(data.get("code") or "COMMUNITY1_BLOCKED"),
                    str(data.get("detail") or "Community-1 模型访问被阻止。"),
                    "确认本机 Hugging Face 账号已接受该模型条件且 token 有访问权限。",
                )
            if isinstance(data, dict) and data.get("status") == "error":
                raise BlockedError("COMMUNITY1_INFERENCE_FAILED", "Community-1 推理失败；未回退到其他声纹模型。", details={"code": data.get("code")})
        elif self.settings.pyannote_model:
            data = self._run_pyannote(audio_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            local = self._local_command()
            if not local:
                raise BlockedError("DIARIZATION_NOT_CONFIGURED", "没有真实说话人分段服务", "安装项目隔离 speaker runtime，或配置 DIARIZATION_COMMAND/PYANNOTE_MODEL；不能用每集随机 SPEAKER_00 代替跨集身份")
            command = [
                *local,
                "--input", str(audio_path),
                "--output", str(output_path),
                "--device", os.getenv("DIARIZATION_DEVICE", "cpu"),
            ]
            transcript_path = audio_path.with_name("transcript.json")
            if transcript_path.is_file():
                command.extend(["--transcript", str(transcript_path)])
            _run(command, timeout=1800)
            try:
                data = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BlockedError("DIARIZATION_INVALID_OUTPUT", "speaker runtime JSON 无法解析") from exc
        if isinstance(data, dict):
            root = data
            data = data.get("segments") or data.get("speakers") or []
        else:
            root = {}
        if not isinstance(data, list) or not data:
            raise BlockedError("DIARIZATION_EMPTY", "说话人分段为空")
        community_selected = self.settings.pyannote_model == COMMUNITY1_MODEL_ID
        embedding_model = root.get("embedding_model")
        if root.get("backend") == "speechbrain-ecapa-voxceleb":
            embedding_model = embedding_model or root.get("model") or ECAPA_EMBEDDING_MODEL_ID
        if community_selected:
            if root.get("backend") != "pyannote-community-1" or root.get("model") != COMMUNITY1_MODEL_ID:
                raise BlockedError("COMMUNITY1_MODEL_MISMATCH", "Community-1 输出缺少预期模型标识；拒绝复用其他模型声纹。")
            if embedding_model != COMMUNITY1_EMBEDDING_MODEL_ID:
                raise BlockedError("COMMUNITY1_EMBEDDING_MODEL_MISMATCH", "Community-1 输出声纹模型标识不匹配；拒绝复用其他模型声纹。")
        cluster_embeddings = root.get("cluster_embeddings") or {}
        speaker_embeddings = root.get("speaker_embeddings") or {}
        normalized: list[dict[str, Any]] = []
        for item in data:
            try:
                raw_speaker = str(item["speaker"])
                # Cross-episode matching belongs to the series character table
                # in Pipeline, not a second global, continually drifting store.
                if community_selected:
                    embedding = speaker_embeddings.get(raw_speaker)
                    if not isinstance(embedding, list) or not embedding:
                        raise BlockedError("COMMUNITY1_EMBEDDING_MISSING", f"Community-1 speaker {raw_speaker} 缺少有效声纹；拒绝静默降级。")
                else:
                    embedding = cluster_embeddings.get(raw_speaker) or item.get("embedding")
                normalized_item = {
                    "start": float(item["start"]),
                    "end": float(item["end"]),
                    "speaker": raw_speaker,
                    "embedding": embedding,
                    "embedding_model": embedding_model,
                    "cluster_id": item.get("cluster_id"),
                }
                if "transcript_index" in item:
                    normalized_item["transcript_index"] = item["transcript_index"]
                normalized.append(normalized_item)
            except BlockedError:
                raise
            except (KeyError, TypeError, ValueError):
                continue
        if not normalized:
            raise BlockedError("DIARIZATION_EMPTY", "说话人分段没有有效时间段")
        return normalized

    def _run_pyannote(self, audio_path: Path) -> list[dict[str, Any]]:
        try:
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise BlockedError("PYANNOTE_MISSING", "当前 Python 未安装 pyannote.audio", "安装与本机 CUDA/Python 兼容的 pyannote 依赖") from exc
        token = os.getenv("HF_TOKEN", "").strip()
        pipeline = Pipeline.from_pretrained(self.settings.pyannote_model, token=token or None)
        result = pipeline(str(audio_path))
        segments: list[dict[str, Any]] = []
        for turn, _, speaker in result.itertracks(yield_label=True):
            segments.append({"start": float(turn.start), "end": float(turn.end), "speaker": str(speaker)})
        return segments

    @staticmethod
    def assign(segments: list[dict[str, Any]], diarization: list[dict[str, Any]], project_key: str) -> list[dict[str, Any]]:
        for segment in segments:
            start = float(segment["start_sec"] if "start_sec" in segment else segment["start"])
            end = float(segment["end_sec"] if "end_sec" in segment else segment["end"])
            best = None
            best_overlap = 0.0
            for turn in diarization:
                overlap = max(0.0, min(end, turn["end"]) - max(start, turn["start"]))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best = turn
            if best:
                raw = best["speaker"]
                # A profile_id is an embedding-backed cross-episode identity.
                # Only external/legacy labels remain project-scoped.
                profile_id = best.get("profile_id") or best.get("speaker_key")
                if profile_id:
                    segment["speaker_key"] = str(profile_id)
                elif best.get("embedding_model") == COMMUNITY1_EMBEDDING_MODEL_ID:
                    segment["speaker_key"] = f"{project_key}:community1:{raw}"
                else:
                    segment["speaker_key"] = f"{project_key}:{raw}"
                segment["speaker_raw"] = raw
                segment["speaker_embedding"] = best.get("embedding")
                segment["speaker_embedding_model"] = best.get("embedding_model")
            else:
                segment["speaker_key"] = None
        return segments
