"""Run pyannote Community-1 in its isolated runtime.

Install ``requirements-community-1.txt`` only in
``work/model-runtime/pyannote-community-1/venv``. The model is gated: this
script requires an accepted Hugging Face token from the local token store and
never falls back to the ECAPA adapter.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import wave
from pathlib import Path
from typing import Any


PIPELINE_ID = "pyannote/speaker-diarization-community-1"
SPEAKER_EMBEDDING_MODEL_ID = f"{PIPELINE_ID}/embedding"
DEFAULT_DEVICE = "cpu"
RUNTIME_CACHE = Path(__file__).resolve().parents[1] / "work" / "model-runtime" / "pyannote-community-1"
os.environ.setdefault("HF_HUB_CACHE", str(RUNTIME_CACHE / "hub"))
os.environ.setdefault("MPLCONFIGDIR", str(RUNTIME_CACHE / "matplotlib"))
os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "0")


class ModelAccessBlocked(RuntimeError):
    pass


def _get_token() -> str | None:
    try:
        from huggingface_hub import get_token
    except ImportError:
        return None
    return get_token()


def status(token_present: bool | None = None, device: str = DEFAULT_DEVICE) -> dict[str, Any]:
    if token_present is None:
        try:
            import huggingface_hub  # noqa: F401
        except ImportError:
            return {
                "status": "missing",
                "code": "COMMUNITY1_DEPENDENCIES_MISSING",
                "detail": "Community-1 依赖尚未安装完整，暂时无法检查本机令牌。",
            }
        token_present = bool(_get_token())
    if not token_present:
        return {
            "status": "blocked",
            "code": "HF_TOKEN_REQUIRED",
            "detail": "Accept the Community-1 model conditions and configure a local Hugging Face token.",
        }
    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        return {
            "status": "missing",
            "code": "PYANNOTE_AUDIO_MISSING",
            "detail": "Install scripts/requirements-community-1.txt in the dedicated Community-1 runtime.",
        }
    return {
        "status": "ready",
        "model": PIPELINE_ID,
        "embedding_model": SPEAKER_EMBEDDING_MODEL_ID,
        "device_requested": device,
    }


def _is_access_error(exc: Exception) -> bool:
    error_name = exc.__class__.__name__.lower()
    if "gated" in error_name or "unauthorized" in error_name or "forbidden" in error_name:
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in {401, 403}


def serialize_output(output: Any, device: str = DEFAULT_DEVICE) -> dict[str, Any]:
    diarization = output.speaker_diarization
    exclusive = output.exclusive_speaker_diarization
    if exclusive is None:
        raise ValueError("Community-1 returned no exclusive speaker diarization")

    labels = [str(label) for label in diarization.labels()]
    raw_embeddings = output.speaker_embeddings
    if raw_embeddings is None:
        raise ValueError("Community-1 returned no speaker embeddings")
    if hasattr(raw_embeddings, "detach"):
        raw_embeddings = raw_embeddings.detach().cpu().numpy()
    if hasattr(raw_embeddings, "tolist"):
        raw_embeddings = raw_embeddings.tolist()
    if not isinstance(raw_embeddings, (list, tuple)):
        raise ValueError("Community-1 speaker embeddings are not a matrix")
    if len(labels) == 1 and raw_embeddings and isinstance(raw_embeddings[0], (int, float)):
        raw_embeddings = [raw_embeddings]
    if len(raw_embeddings) != len(labels):
        raise ValueError("Community-1 speaker labels and embedding rows do not align")

    speaker_embeddings: dict[str, list[float]] = {}
    dimension: int | None = None
    for label, raw_vector in zip(labels, raw_embeddings):
        if not isinstance(raw_vector, (list, tuple)):
            raise ValueError(f"Community-1 embedding row for {label} is invalid")
        vector = [float(value) for value in raw_vector]
        if not vector or not all(math.isfinite(value) for value in vector):
            raise ValueError(f"Community-1 embedding row for {label} is empty or non-finite")
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise ValueError("Community-1 speaker embedding dimensions do not match")
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 1e-8:
            raise ValueError(f"Community-1 embedding row for {label} is a zero vector")
        speaker_embeddings[label] = [round(value / norm, 8) for value in vector]

    segments: list[dict[str, Any]] = []
    for turn, _, raw_speaker in exclusive.itertracks(yield_label=True):
        speaker = str(raw_speaker)
        if speaker not in speaker_embeddings:
            raise ValueError(f"Community-1 exclusive turn has no matching speaker embedding: {speaker}")
        start, end = float(turn.start), float(turn.end)
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            continue
        segments.append({"start": start, "end": end, "speaker": speaker})

    return {
        "status": "ok",
        "backend": "pyannote-community-1",
        "model": PIPELINE_ID,
        "embedding_model": SPEAKER_EMBEDDING_MODEL_ID,
        "device": device,
        "segments": segments,
        "speaker_embeddings": speaker_embeddings,
    }


def load_pcm_wave(input_path: Path):
    """Read the pipeline's PCM16 WAV without TorchCodec's Windows DLLs."""
    import numpy as np

    with wave.open(str(input_path), "rb") as source:
        if source.getsampwidth() != 2 or source.getcomptype() != "NONE":
            raise ValueError("Community-1 requires a PCM16 WAV input")
        channels, sample_rate = source.getnchannels(), source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    waveform = samples.reshape(-1, channels).T.astype(np.float32) / 32768.0
    return waveform, sample_rate


def run(input_path: Path, device: str) -> dict[str, Any]:
    token = _get_token()
    if not token:
        raise ModelAccessBlocked("HF_TOKEN_REQUIRED")
    try:
        import torch
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise RuntimeError("PYANNOTE_AUDIO_MISSING") from exc

    pipeline = Pipeline.from_pretrained(PIPELINE_ID, token=token)
    pipeline.to(torch.device(device))
    waveform, sample_rate = load_pcm_wave(input_path)
    return serialize_output(pipeline({"waveform": torch.from_numpy(waveform), "sample_rate": sample_rate}), device)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local pyannote Community-1 speaker diarization")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--device", default=os.getenv("DIARIZATION_DEVICE", DEFAULT_DEVICE))
    args = parser.parse_args()

    if args.status:
        print(json.dumps(status(device=args.device), ensure_ascii=False))
        return 0
    if not args.input or not args.output:
        parser.error("分析需要 --input 和 --output")

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = run(Path(args.input).resolve(), args.device)
    except ModelAccessBlocked:
        payload = {
            "status": "blocked",
            "code": "HF_TOKEN_REQUIRED",
            "detail": "Accept the Community-1 model conditions and configure a local Hugging Face token.",
        }
    except Exception as exc:
        if _is_access_error(exc):
            payload = {
                "status": "blocked",
                "code": "COMMUNITY1_ACCESS_DENIED",
                "detail": "Community-1 access was denied. Accept its model conditions for this account and check the local token.",
            }
        else:
            payload = {"status": "error", "code": exc.__class__.__name__}

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if payload["status"] == "error":
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({key: value for key, value in payload.items() if key != "speaker_embeddings"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
