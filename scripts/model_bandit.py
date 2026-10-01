"""Run the verified BandIt checkpoint and write real cinematic stems.

This is an inference-only bridge for the local application.  It deliberately
uses the separately installed ``bandit-infer`` runtime and its official
Zenodo checkpoint registry; it never treats the input mix as a background
stem.  The default device is CPU so a health check or a first pilot cannot
silently compete with OmniVoice/Speaches for the 3080.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import torch

from bandit_infer import BanditSession


DEFAULT_MODEL = "v1-mus64-l1snr"
DEFAULT_CHUNK_SECONDS = 30.0
DEFAULT_OVERLAP_SECONDS = 1.0


def _path(value: str | None, default: Path) -> Path:
    return Path(value).expanduser().resolve() if value else default.resolve()


def _weights_default() -> Path:
    # The script lives at <project>/scripts/model_bandit.py.
    return Path(__file__).resolve().parents[1] / "work" / "models"


def _manifest_path(value: str | None, output_dir: Path) -> Path:
    return Path(value).resolve() if value else output_dir / "bandit.manifest.json"


def _read_audio(path: Path) -> tuple[np.ndarray, int]:
    try:
        samples, sample_rate = sf.read(str(path), always_2d=True, dtype="float32")
    except Exception as exc:  # soundfile reports several backend-specific errors.
        raise RuntimeError(f"无法读取 BandIt 输入 WAV：{path}") from exc
    if samples.size == 0 or sample_rate <= 0:
        raise RuntimeError("BandIt 输入音频为空")
    samples = np.asarray(samples, dtype=np.float32).T
    if samples.shape[0] > 2:
        samples = samples[:2]
    if not np.isfinite(samples).all():
        raise RuntimeError("BandIt 输入音频含 NaN/Inf")
    return np.ascontiguousarray(samples), int(sample_rate)


def _fade(length: int, left: int, right: int) -> np.ndarray:
    weights = np.ones(length, dtype=np.float32)
    if left:
        count = min(left, length)
        weights[:count] = np.linspace(1.0 / max(count, 1), 1.0, count, dtype=np.float32)
    if right:
        count = min(right, length)
        weights[-count:] = np.minimum(
            weights[-count:],
            np.linspace(1.0, 1.0 / max(count, 1), count, dtype=np.float32),
        )
    return weights


def _separate(
    audio: np.ndarray,
    sample_rate: int,
    *,
    model: str,
    device: str,
    weights_dir: Path,
    chunk_seconds: float,
    overlap_seconds: float,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    if sample_rate != 44100:
        raise RuntimeError(
            f"BandIt v1 {model} 要求原生 44100 Hz，当前为 {sample_rate} Hz；由适配器先行重采样"
        )
    total = audio.shape[1]
    chunk_size = max(1, int(round(chunk_seconds * sample_rate)))
    overlap = max(0, min(int(round(overlap_seconds * sample_rate)), chunk_size // 3))
    step = max(1, chunk_size - overlap)
    output_sum: dict[str, np.ndarray] = {}
    weight_sum = np.zeros(total, dtype=np.float32)
    chunk_count = 0
    session = BanditSession(model, device=device, weights_dir=weights_dir)
    started = time.perf_counter()
    with session:
        for start in range(0, total, step):
            end = min(total, start + chunk_size)
            current = audio[:, start:end]
            stems = session.infer(current, sample_rate=sample_rate)
            actual = min(
                [current.shape[1]]
                + [int(np.asarray(value).shape[-1]) for value in stems.values()]
            )
            if actual <= 0:
                raise RuntimeError(f"BandIt 第 {chunk_count} 个窗口没有输出")
            end = start + actual
            left = overlap if start > 0 else 0
            right = overlap if end < total else 0
            weights = _fade(actual, left, right)
            weight_sum[start:end] += weights
            for stem, value in stems.items():
                value = np.asarray(value, dtype=np.float32)
                if value.ndim == 1:
                    value = value[None, :]
                value = value[:, :actual]
                if value.shape[0] != audio.shape[0]:
                    raise RuntimeError(f"BandIt 输出声道数异常：{stem}={value.shape}")
                if stem not in output_sum:
                    output_sum[stem] = np.zeros((audio.shape[0], total), dtype=np.float32)
                output_sum[stem][:, start:end] += value * weights[None, :]
            chunk_count += 1
            if end >= total:
                break
    if not output_sum or np.any(weight_sum <= 0):
        raise RuntimeError("BandIt 分离没有覆盖完整输入时间轴")
    stems = {stem: value / np.maximum(weight_sum[None, :], 1e-8) for stem, value in output_sum.items()}
    elapsed = time.perf_counter() - started
    runtime_device = str(torch.device(device if device != "auto" else ("cuda" if torch.cuda.is_available() else "cpu")))
    telemetry: dict[str, Any] = {
        "device": runtime_device,
        "elapsed_sec": round(elapsed, 3),
        "realtime_factor": round((total / sample_rate) / elapsed, 3) if elapsed else None,
        "chunks": chunk_count,
        "chunk_seconds": chunk_seconds,
        "overlap_seconds": overlap_seconds,
    }
    if torch.cuda.is_available() and runtime_device.startswith("cuda"):
        telemetry["cuda_peak_allocated_mib"] = round(torch.cuda.max_memory_allocated() / 1024**2, 1)
        telemetry["cuda_peak_reserved_mib"] = round(torch.cuda.max_memory_reserved() / 1024**2, 1)
    return stems, telemetry


def _status(model: str, device: str, weights_dir: Path) -> dict[str, Any]:
    session = BanditSession(model, device=device, weights_dir=weights_dir)
    info = session.cache_info()
    return {
        "status": "ready" if info["verified"] else "missing_weights",
        "backend": "bandit-infer",
        "model": model,
        "family": session.spec.family,
        "device_requested": device,
        "weights_path": str(info["path"]),
        "weights_verified": bool(info["verified"]),
        "weights_size": session.spec.size,
        "weights_sha256": session.spec.sha256,
        "sample_rate": session.spec.sample_rate,
        "stems": list(session.spec.stems),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    weights_dir = _path(args.weights_dir, _weights_default())
    weights_dir.mkdir(parents=True, exist_ok=True)
    audio, sample_rate = _read_audio(input_path)
    stems, telemetry = _separate(
        audio,
        sample_rate,
        model=args.model,
        device=args.device,
        weights_dir=weights_dir,
        chunk_seconds=args.chunk_seconds,
        overlap_seconds=args.overlap_seconds,
    )
    stem_paths: dict[str, str] = {}
    for name, value in stems.items():
        filename = "effects.wav" if name == "sfx" else f"{name}.wav"
        path = output_dir / filename
        sf.write(str(path), np.clip(value.T, -1.0, 1.0), sample_rate, subtype="PCM_16")
        if not path.is_file() or path.stat().st_size < 128:
            raise RuntimeError(f"BandIt 输出为空：{path}")
        stem_paths[name] = filename
    spec = BanditSession(args.model, device=args.device, weights_dir=weights_dir).spec
    manifest: dict[str, Any] = {
        "backend": "bandit-infer",
        "model": args.model,
        "family": spec.family,
        "checkpoint": str(weights_dir / spec.filename),
        "checkpoint_sha256": spec.sha256,
        "input": str(input_path),
        "sample_rate": sample_rate,
        "channels": int(audio.shape[0]),
        "duration_sec": round(audio.shape[1] / sample_rate, 6),
        "stems": stem_paths,
        "song_intervals": [],
        "song_detection": {
            "status": "unavailable",
            "reason": "未检测歌曲区间，不能证明片段中没有歌曲或歌曲边界；music/effects 仍完整混入 background，不静音、不伪造歌词区间",
        },
        "telemetry": telemetry,
    }
    path = _manifest_path(args.manifest, output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Verified local BandIt inference")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--output-dir")
    parser.add_argument("--manifest")
    parser.add_argument("--model", default=os.getenv("BANDIT_MODEL", DEFAULT_MODEL))
    parser.add_argument("--device", default=os.getenv("BANDIT_DEVICE", "cpu"))
    parser.add_argument("--weights-dir", default=os.getenv("BANDIT_INFER_WEIGHTS"))
    parser.add_argument("--chunk-seconds", type=float, default=float(os.getenv("BANDIT_CHUNK_SECONDS", DEFAULT_CHUNK_SECONDS)))
    parser.add_argument("--overlap-seconds", type=float, default=float(os.getenv("BANDIT_OVERLAP_SECONDS", DEFAULT_OVERLAP_SECONDS)))
    args = parser.parse_args()
    weights_dir = _path(args.weights_dir, _weights_default())
    if args.status:
        print(json.dumps(_status(args.model, args.device, weights_dir), ensure_ascii=False))
        return 0
    if not args.input or not args.output_dir:
        parser.error("分离需要 --input 和 --output-dir")
    manifest = run(args)
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
