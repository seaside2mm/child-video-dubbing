from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_EXTERNAL_ENV = ROOT_DIR / ".env"


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read only simple KEY=VALUE entries; never print the values."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        if key and key not in os.environ:
            result[key] = value
    return result


def _env(name: str, dotenv: dict[str, str], default: str = "") -> str:
    return os.getenv(name, dotenv.get(name, default)).strip()


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    work_dir: Path
    db_path: Path
    env_file: Path | None
    faster_whisper_url: str
    faster_whisper_model: str
    omnivoice_url: str
    omnivoice_model: str
    text_base_url: str
    text_api_key: str
    text_model: str
    ffmpeg: str
    ffprobe: str
    bandit_command: str
    diarization_command: str
    pyannote_model: str
    omnivoice_tts_path: str
    omnivoice_profile_create_path: str
    omnivoice_request_mode: str
    omnivoice_text_field: str
    omnivoice_voice_field: str
    omnivoice_ref_audio_field: str
    omnivoice_ref_text_field: str
    omnivoice_speed_field: str
    omnivoice_format_field: str

    @classmethod
    def load(cls, root_dir: Path = ROOT_DIR) -> "Settings":
        local_env = root_dir / ".env"
        env_hint = os.getenv("DUBBING_ENV_FILE", _read_dotenv(local_env).get("DUBBING_ENV_FILE", "")).strip()
        env_file = Path(env_hint) if env_hint else local_env
        dotenv = _read_dotenv(env_file)
        work_dir = Path(_env("DUBBING_WORK_DIR", dotenv, str(root_dir / "work"))).resolve()
        db_path = Path(_env("DUBBING_DB_PATH", dotenv, str(work_dir / "dubbing.sqlite3"))).resolve()
        text_base_url = _env("DUBBING_TEXT_BASE_URL", dotenv) or _env("OPENAI_BASE_URL", dotenv)
        text_api_key = _env("DUBBING_TEXT_API_KEY", dotenv) or _env("OPENAI_API_KEY", dotenv)
        text_model = (
            _env("DUBBING_TEXT_MODEL", dotenv)
            or _env("OPENAI_MODEL", dotenv)
            or _env("OPENAI_VISION_MODEL", dotenv)
        )
        ffmpeg = _env("FFMPEG_BIN", dotenv) or shutil.which("ffmpeg") or "ffmpeg"
        ffprobe = _env("FFPROBE_BIN", dotenv) or shutil.which("ffprobe") or "ffprobe"
        return cls(
            root_dir=root_dir,
            work_dir=work_dir,
            db_path=db_path,
            env_file=env_file if env_file.is_file() else None,
            faster_whisper_url=_env("FASTER_WHISPER_URL", dotenv, "http://127.0.0.1:8001").rstrip("/"),
            faster_whisper_model=_env("FASTER_WHISPER_MODEL", dotenv, "Systran/faster-whisper-large-v3"),
            omnivoice_url=_env("OMNIVOICE_URL", dotenv, "http://127.0.0.1:7861").rstrip("/"),
            omnivoice_model=_env("OMNIVOICE_MODEL", dotenv, "k2-fsa/OmniVoice"),
            text_base_url=text_base_url.rstrip("/"),
            text_api_key=text_api_key,
            text_model=text_model,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            bandit_command=_env("BANDIT_COMMAND", dotenv),
            diarization_command=_env("DIARIZATION_COMMAND", dotenv),
            pyannote_model=_env("PYANNOTE_MODEL", dotenv, "pyannote/speaker-diarization-community-1"),
            omnivoice_tts_path=_env("OMNIVOICE_TTS_PATH", dotenv, "/tts/generate"),
            omnivoice_profile_create_path=_env("OMNIVOICE_PROFILE_CREATE_PATH", dotenv),
            omnivoice_request_mode=_env("OMNIVOICE_REQUEST_MODE", dotenv, "json"),
            omnivoice_text_field=_env("OMNIVOICE_TEXT_FIELD", dotenv, "text"),
            omnivoice_voice_field=_env("OMNIVOICE_VOICE_FIELD", dotenv, "voice_profile"),
            omnivoice_ref_audio_field=_env("OMNIVOICE_REF_AUDIO_FIELD", dotenv, "ref_audio"),
            omnivoice_ref_text_field=_env("OMNIVOICE_REF_TEXT_FIELD", dotenv, "ref_text"),
            omnivoice_speed_field=_env("OMNIVOICE_SPEED_FIELD", dotenv, "speed"),
            omnivoice_format_field=_env("OMNIVOICE_FORMAT_FIELD", dotenv, "format"),
        )

    def safe(self) -> dict[str, Any]:
        return {
            "work_dir": str(self.work_dir),
            "database": str(self.db_path),
            "env_file_configured": bool(self.env_file),
            "faster_whisper_url": self.faster_whisper_url,
            "faster_whisper_model": self.faster_whisper_model,
            "omnivoice_url": self.omnivoice_url,
            "omnivoice_model": self.omnivoice_model,
            "text_api_configured": bool(self.text_base_url and self.text_api_key and self.text_model),
            "text_base_url": self.text_base_url,
            "text_model": self.text_model,
            "bandit_configured": bool(self.bandit_command),
            "diarization_configured": bool(self.diarization_command or self.pyannote_model),
        }


settings = Settings.load()
