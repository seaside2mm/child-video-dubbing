from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from .base import AdapterError, BlockedError, ServiceStatus
from .media import _run


@dataclass
class SeparationResult:
    speech_path: Path
    background_path: Path
    music_path: Path | None
    effects_path: Path | None
    song_intervals: list[dict[str, float]]
    manifest_path: Path


class SeparationAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _local_command(self) -> list[str] | None:
        script = self.settings.root_dir / "scripts" / "model_bandit.py"
        runtime = self.settings.root_dir / "work" / "model-runtime" / "venv" / "Scripts" / "python.exe"
        if not script.is_file():
            return None
        if not runtime.is_file():
            runtime = Path(shutil.which("python") or "")
        if not runtime or not runtime.is_file():
            return None
        return [str(runtime), str(script)]

    def _local_model(self) -> str:
        return os.getenv("BANDIT_MODEL", "v1-mus64-l1snr").strip() or "v1-mus64-l1snr"

    def status(self) -> ServiceStatus:
        command = self.settings.bandit_command
        if not command:
            local = self._local_command()
            if not local:
                return ServiceStatus("missing", "未配置 BandIt 命令，且项目隔离模型运行时不存在；不能把原混音冒充背景轨")
            try:
                result = subprocess.run(
                    [*local, "--status", "--model", self._local_model(), "--weights-dir", str(self.settings.root_dir / "work" / "models")],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                payload = json.loads(result.stdout) if result.stdout.strip() else {}
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                return ServiceStatus("error", f"BandIt 本地运行时状态检查失败：{exc.__class__.__name__}")
            if result.returncode:
                return ServiceStatus("error", (result.stderr or result.stdout or "BandIt 状态命令失败")[-1000:])
            state = payload.get("status")
            if state == "ready":
                return ServiceStatus("configured", f"BandIt {payload.get('model', self._local_model())} 已核验权重，默认设备 {payload.get('device_requested', 'cpu')}")
            return ServiceStatus("missing", f"BandIt 运行时已安装但权重未核验：{payload.get('weights_path', '未知路径')}")
        try:
            executable = shlex.split(command, posix=False)[0]
        except (IndexError, ValueError):
            return ServiceStatus("invalid", "BANDIT_COMMAND 无法解析")
        if not (Path(executable).exists() or shutil.which(executable)):
            return ServiceStatus("missing", f"找不到 BandIt 可执行文件：{executable}")
        return ServiceStatus("configured", "已配置真实分离命令；尚未运行长任务")

    def separate(self, source: Path, work_dir: Path) -> SeparationResult:
        work_dir.mkdir(parents=True, exist_ok=True)
        manifest = work_dir / "separation.manifest.json"
        command_template = self.settings.bandit_command
        local_command = self._local_command() if not command_template else None
        if local_command:
            # BandIt v1 is native 44.1 kHz. Keep this extracted input in the
            # project cache and never alter the read-only source video.
            model_input = work_dir / "bandit-input-44100.wav"
            _run([
                self.settings.ffmpeg,
                "-y",
                "-i", str(source),
                "-vn",
                "-ac", "2",
                "-ar", "44100",
                "-c:a", "pcm_s16le",
                str(model_input),
            ])
            command = [
                *local_command,
                "--input", str(model_input),
                "--output-dir", str(work_dir),
                "--manifest", str(manifest),
                "--model", self._local_model(),
                "--weights-dir", str(self.settings.root_dir / "work" / "models"),
                "--device", os.getenv("BANDIT_DEVICE", "cpu"),
            ]
        else:
            if not command_template:
                raise BlockedError("BANDIT_NOT_CONFIGURED", "未配置 BandIt 影视分离模型/命令，且项目隔离运行时不存在", "安装项目模型依赖或配置 BANDIT_COMMAND；也可在项目中导入已分离的 speech/music/effects 轨")
            try:
                command = shlex.split(command_template, posix=False)
            except ValueError as exc:
                raise BlockedError("BANDIT_COMMAND_INVALID", "BANDIT_COMMAND 无法解析", "修正配置") from exc
            command = [part.format(input=str(source), output_dir=str(work_dir), manifest=str(manifest)) for part in command]
        _run(command, timeout=1800)
        if not manifest.is_file():
            # A command may write the conventional files but omit a manifest. We still
            # require all tracks and write a local manifest from observed files.
            manifest_data = self._discover_files(work_dir)
            if not manifest_data:
                raise AdapterError("BANDIT_NO_OUTPUT", "BandIt 没有留下可验证的分离轨或 manifest", "检查命令输出目录")
            manifest.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
        data = self._read_manifest(manifest, work_dir)
        speech = self._required_track(data, "speech", work_dir)
        background = self._optional_track(data, "background", work_dir)
        music = self._optional_track(data, "music", work_dir)
        effects = self._optional_track(data, "effects", work_dir)
        if background is None and (music is None or effects is None):
            raise BlockedError("BANDIT_BACKGROUND_MISSING", "没有可验证的音乐/音效背景轨", "重新运行分离或导入 speech、music、effects 三条轨；不能使用原混音")
        if background is None:
            background = self._mix_background(music, effects, work_dir / "background.wav")
        song_intervals = data.get("song_intervals") or data.get("songs") or []
        normalized = []
        for item in song_intervals:
            try:
                normalized.append({"start": float(item["start"]), "end": float(item["end"])})
            except (KeyError, TypeError, ValueError):
                continue
        return SeparationResult(speech, background, music, effects, normalized, manifest)

    def _read_manifest(self, path: Path, work_dir: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AdapterError("BANDIT_MANIFEST_INVALID", "BandIt manifest 不能解析") from exc
        if not isinstance(data, dict):
            raise AdapterError("BANDIT_MANIFEST_INVALID", "BandIt manifest 必须是对象")
        data["_work_dir"] = str(work_dir)
        return data

    def _discover_files(self, work_dir: Path) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for name in ("speech", "dialogue", "vocals"):
            match = next((p for p in work_dir.glob(f"{name}.*") if p.suffix.lower() in {".wav", ".flac", ".mp3", ".m4a"}), None)
            if match:
                found["speech"] = match.name
                break
        for key, names in {"music": ("music", "instrumental"), "effects": ("effects", "sfx"), "background": ("background", "bg")}.items():
            for name in names:
                match = next((p for p in work_dir.glob(f"{name}.*") if p.suffix.lower() in {".wav", ".flac", ".mp3", ".m4a"}), None)
                if match:
                    found[key] = match.name
                    break
        return found if "speech" in found else {}

    @staticmethod
    def _safe_path(raw: Any, work_dir: Path) -> Path | None:
        if not raw:
            return None
        path = Path(str(raw))
        if not path.is_absolute():
            path = work_dir / path
        path = path.resolve()
        try:
            path.relative_to(work_dir.resolve())
        except ValueError as exc:
            raise BlockedError("BANDIT_PATH_OUTSIDE_WORK", "分离输出路径不在项目缓存目录内", "让 BandIt 写入 output_dir") from exc
        return path

    def _required_track(self, data: dict[str, Any], key: str, work_dir: Path) -> Path:
        path = self._optional_track(data, key, work_dir)
        if not path or not path.is_file() or path.stat().st_size < 128:
            raise BlockedError("BANDIT_SPEECH_MISSING", "没有可验证的 speech/dialogue 分离轨", "检查 BandIt 输出")
        return path

    def _optional_track(self, data: dict[str, Any], key: str, work_dir: Path) -> Path | None:
        aliases = {"speech": ("speech", "dialogue", "vocals"), "background": ("background", "bg"), "music": ("music", "instrumental"), "effects": ("effects", "sfx")}
        stem_map = data.get("stems") if isinstance(data.get("stems"), dict) else {}
        for name in aliases.get(key, (key,)):
            for raw in (data.get(name), stem_map.get(name)):
                path = self._safe_path(raw, work_dir)
                if path and path.is_file() and path.stat().st_size >= 128:
                    return path
        return None

    def _mix_background(self, music: Path, effects: Path, output: Path) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        # The BandIt stems are 44.1 kHz stereo. Keep that fidelity when
        # rebuilding the background and disable amix's implicit attenuation;
        # the limiter is the explicit anti-clipping safety net instead.
        _run([
            self.settings.ffmpeg,
            "-y",
            "-i", str(music),
            "-i", str(effects),
            "-filter_complex",
            "[0:a]aresample=44100[m];[1:a]aresample=44100[e];"
            "[m][e]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.97:level_in=1:level_out=0.97:attack=5:release=50:asc=0:level=0[a]",
            "-map", "[a]",
            "-ac", "2",
            "-ar", "44100",
            "-c:a", "pcm_s16le",
            str(output),
        ])
        if not output.is_file() or output.stat().st_size < 128:
            raise AdapterError("BANDIT_BACKGROUND_EMPTY", "音乐/音效混音背景轨为空")
        return output
