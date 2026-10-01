"""Detect sung-song intervals with the public, lightweight YAMNet model.

YAMNet is the 521-class AudioSet event model documented by TensorFlow and
implemented here with the small PyTorch conversion from ``torch_audioset``.
This bridge deliberately requires a vocal/song event *and* a music-context
event.  A generic ``Music`` prediction by itself is not enough to replace the
generated mix with the original audio.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import torch
from scipy.signal import resample_poly


MODEL_NAME = "YAMNet"
MODEL_SOURCE = "https://www.tensorflow.org/hub/tutorials/yamnet"
WEIGHTS_SOURCE = "https://github.com/w-hc/torch_audioset/releases/download/v0.1/yamnet.pth"
TARGET_SAMPLE_RATE = 16000
WINDOW_SECONDS = 0.96
HOP_SECONDS = 0.48
DEFAULT_BATCH_SIZE = 64
DEFAULT_VOCAL_THRESHOLD = 0.40
DEFAULT_MUSIC_THRESHOLD = 0.25
DEFAULT_MIN_DURATION = 1.5

# These are intentionally narrower than the full AudioSet music ontology.  A
# soundtrack/instrumental-only window remains background music, not a song.
VOCAL_CLASS_NAMES = {
    "Singing",
    "Choir",
    "Child singing",
    "Synthetic singing",
    "Vocal music",
    "Song",
}
MUSIC_CLASS_NAMES = {
    "Music",
    "Musical instrument",
    "Pop music",
    "Hip hop music",
    "Rock music",
    "Soul music",
    "Swing music",
    "Folk music",
    "Classical music",
    "Electronic music",
    "House music",
    "Electronic dance music",
    "Ambient music",
    "Trance music",
    "Music of Latin America",
    "Music for children",
    "New-age music",
    "Vocal music",
    "Music of Africa",
    "Gospel music",
    "Music of Asia",
    "Traditional music",
    "Independent music",
    "Song",
    "Background music",
    "Theme music",
    "Jingle (music)",
    "Soundtrack music",
    "Video game music",
    "Christmas music",
    "Dance music",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _weights_path() -> Path:
    value = os.getenv("YAMNET_WEIGHTS", "").strip()
    if value:
        return Path(value).expanduser().resolve()
    return _project_root() / "work" / "model-runtime" / "torch-cache" / "hub" / "checkpoints" / "yamnet.pth"


def _read_audio(path: Path) -> tuple[np.ndarray, int]:
    try:
        samples, sample_rate = sf.read(str(path), always_2d=True, dtype="float32")
    except Exception as exc:
        raise RuntimeError(f"无法读取歌曲检测输入 WAV：{path}") from exc
    if samples.size == 0 or sample_rate <= 0:
        raise RuntimeError("歌曲检测输入音频为空")
    mono = np.asarray(samples, dtype=np.float32).mean(axis=1)
    if not np.isfinite(mono).all():
        raise RuntimeError("歌曲检测输入音频含 NaN/Inf")
    if int(sample_rate) != TARGET_SAMPLE_RATE:
        gcd = np.gcd(int(sample_rate), TARGET_SAMPLE_RATE)
        mono = resample_poly(mono, TARGET_SAMPLE_RATE // gcd, int(sample_rate) // gcd).astype(np.float32)
    return np.ascontiguousarray(mono), TARGET_SAMPLE_RATE


def _load_model(device: str, weights_path: Path):
    try:
        from torch_audioset.yamnet.model import YAMNet
    except ImportError as exc:
        raise RuntimeError("未安装 torch_audioset；无法进行真实歌曲事件检测") from exc
    if not weights_path.is_file() or weights_path.stat().st_size < 1_000_000:
        raise RuntimeError(f"YAMNet 权重不存在或过小：{weights_path}")
    model = YAMNet()
    try:
        state = torch.load(str(weights_path), map_location=device, weights_only=True)
        model.load_state_dict(state)
    except Exception as exc:
        raise RuntimeError(f"YAMNet 权重加载失败：{exc.__class__.__name__}") from exc
    model.to(device).eval()
    return model


def _preprocess(audio: np.ndarray, sample_rate: int) -> torch.Tensor:
    try:
        from torch_audioset.data.torch_input_processing import WaveformToInput
        from torch_audioset.params import YAMNetParams
    except ImportError as exc:
        raise RuntimeError("torch_audioset 的 YAMNet 预处理不可用") from exc
    # The upstream conversion defaults to a one-second hop for batch labeling;
    # event localization uses the official YAMNet 0.48 s patch hop instead.
    YAMNetParams.PATCH_HOP_SECONDS = HOP_SECONDS
    transform = WaveformToInput()
    waveform = torch.from_numpy(audio).unsqueeze(0)
    patches, _ = transform.wavform_to_log_mel(waveform, sample_rate)
    if patches.ndim != 4 or patches.shape[0] == 0:
        raise RuntimeError("YAMNet 预处理未产生有效窗口")
    return patches.contiguous()


def _categories() -> list[str]:
    try:
        from torch_audioset.yamnet.model import yamnet_category_metadata
        values = yamnet_category_metadata()
    except Exception as exc:
        raise RuntimeError("无法读取 YAMNet AudioSet 类别表") from exc
    names = [str(item.get("name", "")) for item in values if isinstance(item, dict)]
    if len(names) != 521:
        raise RuntimeError(f"YAMNet 类别表数量异常：{len(names)}")
    return names


def _merge(indices: list[int], *, frame_count: int, duration: float, min_duration: float) -> list[dict[str, float]]:
    if not indices:
        return []
    active = np.zeros(frame_count, dtype=bool)
    active[indices] = True
    # Permit one missed 0.48 s patch, but do not bridge long non-song gaps.
    max_gap = max(1, int(round(1.0 / HOP_SECONDS)))
    runs: list[tuple[int, int]] = []
    start: int | None = None
    gap = 0
    for index, value in enumerate(active.tolist() + [False]):
        if value:
            if start is None:
                start = index
            gap = 0
            continue
        if start is None:
            continue
        gap += 1
        if gap <= max_gap and index < frame_count:
            continue
        end = index - gap + 1
        if end > start:
            runs.append((start, end))
        start = None
        gap = 0
    intervals: list[dict[str, float]] = []
    for first, last in runs:
        begin = max(0.0, first * HOP_SECONDS)
        end = min(duration, last * HOP_SECONDS + WINDOW_SECONDS)
        if end - begin >= min_duration:
            intervals.append({"start": round(begin, 6), "end": round(end, 6)})
    return intervals


def detect(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    weights_path = _weights_path()
    audio, sample_rate = _read_audio(input_path)
    duration = audio.size / sample_rate
    started = time.perf_counter()
    names = _categories()
    vocal_indices = [index for index, name in enumerate(names) if name in VOCAL_CLASS_NAMES]
    music_indices = [index for index, name in enumerate(names) if name in MUSIC_CLASS_NAMES]
    if not vocal_indices or not music_indices:
        raise RuntimeError("YAMNet 类别表缺少歌曲/音乐类别")
    patches = _preprocess(audio, sample_rate)
    model = _load_model(args.device, weights_path)
    vocal_scores: list[np.ndarray] = []
    music_scores: list[np.ndarray] = []
    top_frames: list[dict[str, Any]] = []
    with torch.inference_mode():
        for start in range(0, patches.shape[0], args.batch_size):
            batch = patches[start:start + args.batch_size].to(args.device)
            scores = model(batch, to_prob=True).detach().cpu().numpy()
            vocal_scores.append(np.max(scores[:, vocal_indices], axis=1))
            music_scores.append(np.max(scores[:, music_indices], axis=1))
            for offset, row in enumerate(scores):
                frame = start + offset
                best = np.argsort(row)[-3:][::-1]
                top_frames.append({
                    "frame": frame,
                    "time": round(frame * HOP_SECONDS, 6),
                    "top": [{"label": names[int(index)], "score": round(float(row[index]), 4)} for index in best],
                })
    vocal = np.concatenate(vocal_scores)
    music = np.concatenate(music_scores)
    candidate = np.flatnonzero((vocal >= args.vocal_threshold) & (music >= args.music_threshold)).tolist()
    intervals = _merge(candidate, frame_count=len(vocal), duration=duration, min_duration=args.min_duration)
    elapsed = time.perf_counter() - started
    peak = sorted(top_frames, key=lambda item: max(x["score"] for x in item["top"]), reverse=True)[:5]
    max_vocal = float(np.max(vocal)) if len(vocal) else 0.0
    max_music = float(np.max(music)) if len(music) else 0.0
    if intervals:
        status = "detected"
        reason = "检测到达到 vocal/song 与 music 双重阈值的片段"
    elif max_vocal >= args.vocal_threshold * 0.5:
        status = "low_confidence"
        reason = "检测到接近阈值的歌唱事件，歌曲边界仍需复核；普通背景音乐本身不构成歌曲证据"
    else:
        status = "no_song"
        reason = "未检测到达到双重阈值的歌曲片段"
    result: dict[str, Any] = {
        "backend": "yamnet-audioset",
        "model": MODEL_NAME,
        "model_source": MODEL_SOURCE,
        "weights_source": WEIGHTS_SOURCE,
        "weights_path": str(weights_path),
        "weights_sha256": hashlib.sha256(weights_path.read_bytes()).hexdigest(),
        "device": args.device,
        "sample_rate": sample_rate,
        "duration_sec": round(duration, 6),
        "window_seconds": WINDOW_SECONDS,
        "hop_seconds": HOP_SECONDS,
        "frames": len(vocal),
        "thresholds": {"vocal": args.vocal_threshold, "music": args.music_threshold, "min_duration": args.min_duration},
        "max_vocal_score": round(max_vocal, 6),
        "max_music_score": round(max_music, 6),
        "status": status,
        "reason": reason,
        "intervals": intervals,
        "song_intervals": intervals,
        "peak_frames": peak,
        "elapsed_sec": round(elapsed, 3),
        "realtime_factor": round(duration / elapsed, 3) if elapsed else None,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def status(args: argparse.Namespace) -> dict[str, Any]:
    weights = _weights_path()
    try:
        import torch_audioset  # noqa: F401
        package_ready = True
    except ImportError:
        package_ready = False
    return {
        "status": "ready" if package_ready and weights.is_file() and weights.stat().st_size >= 1_000_000 else "missing_model",
        "backend": "yamnet-audioset",
        "model": MODEL_NAME,
        "model_source": MODEL_SOURCE,
        "weights_source": WEIGHTS_SOURCE,
        "weights_path": str(weights),
        "weights_size": weights.stat().st_size if weights.is_file() else 0,
        "package_ready": package_ready,
        "token_required": False,
        "device_requested": args.device,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="YAMNet song interval detection")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--device", default=os.getenv("SONG_DETECTION_DEVICE", "cpu"))
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("SONG_DETECTION_BATCH_SIZE", DEFAULT_BATCH_SIZE)))
    parser.add_argument("--vocal-threshold", type=float, default=float(os.getenv("SONG_DETECTION_VOCAL_THRESHOLD", DEFAULT_VOCAL_THRESHOLD)))
    parser.add_argument("--music-threshold", type=float, default=float(os.getenv("SONG_DETECTION_MUSIC_THRESHOLD", DEFAULT_MUSIC_THRESHOLD)))
    parser.add_argument("--min-duration", type=float, default=float(os.getenv("SONG_DETECTION_MIN_DURATION", DEFAULT_MIN_DURATION)))
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status(args), ensure_ascii=False))
        return 0
    if not args.input or not args.output:
        parser.error("检测需要 --input 和 --output")
    if args.batch_size < 1:
        parser.error("--batch-size 必须为正数")
    print(json.dumps(detect(args), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
