"""Local speaker segmentation with a real public ECAPA speaker embedding model.

The model is ``speechbrain/spkrec-ecapa-voxceleb``.  It is public and does not
require an HF token.  When an ASR transcript is available, its sentence bounds
define embedding windows and energy VAD only trims silence inside each sentence.
Clusters remain unlabeled; cross-episode identity belongs to the series table.
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
from scipy.signal import resample_poly
from sklearn.cluster import AgglomerativeClustering


MODEL_ID = "speechbrain/spkrec-ecapa-voxceleb"
EMBEDDING_RATE = 16000
DEFAULT_DEVICE = "cpu"
DEFAULT_WINDOW_SECONDS = 2.5
DEFAULT_HOP_SECONDS = 1.25
DEFAULT_CLUSTER_DISTANCE = 0.32
MIN_ASR_WINDOW_SECONDS = 0.8
MAX_ASR_WINDOW_SECONDS = 8.0


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _model_dir() -> Path:
    value = os.getenv("SPEAKER_MODEL_DIR", "").strip()
    return Path(value).expanduser().resolve() if value else _project_root() / "work" / "model-runtime" / "speechbrain-spkrec-ecapa-voxceleb"


def _hf_cache() -> Path:
    value = os.getenv("HF_HOME", "").strip()
    return Path(value).expanduser().resolve() if value else _project_root() / "work" / "model-runtime" / "hf-cache"


def _read_audio(path: Path) -> tuple[np.ndarray, int]:
    try:
        samples, sample_rate = sf.read(str(path), always_2d=True, dtype="float32")
    except Exception as exc:
        raise RuntimeError(f"无法读取说话人分析输入 WAV：{path}") from exc
    if samples.size == 0 or sample_rate <= 0:
        raise RuntimeError("说话人分析输入音频为空")
    mono = np.asarray(samples, dtype=np.float32).mean(axis=1)
    if not np.isfinite(mono).all():
        raise RuntimeError("说话人分析输入音频含 NaN/Inf")
    if sample_rate != EMBEDDING_RATE:
        gcd = np.gcd(sample_rate, EMBEDDING_RATE)
        mono = resample_poly(mono, EMBEDDING_RATE // gcd, sample_rate // gcd).astype(np.float32)
    return np.ascontiguousarray(mono), EMBEDDING_RATE


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    values = np.asarray(mask, dtype=bool)
    if not values.any():
        return []
    padded = np.pad(values.astype(np.int8), (1, 1))
    starts = np.flatnonzero(np.diff(padded) == 1)
    ends = np.flatnonzero(np.diff(padded) == -1)
    return [(int(start), int(end)) for start, end in zip(starts, ends)]


def _voice_runs(audio: np.ndarray, sample_rate: int) -> list[tuple[int, int]]:
    frame = max(1, int(round(0.025 * sample_rate)))
    hop = max(1, int(round(0.010 * sample_rate)))
    if audio.size < frame:
        return []
    count = 1 + (audio.size - frame) // hop
    rms = np.asarray(
        [np.sqrt(np.mean(audio[index * hop:index * hop + frame] ** 2) + 1e-10) for index in range(count)],
        dtype=np.float32,
    )
    db = 20.0 * np.log10(np.maximum(rms, 1e-5))
    if float(np.max(db)) < -52.0:
        return []
    floor = float(np.percentile(db, 30.0))
    # Keep the threshold below the observed peak.  This matters for a quiet
    # cartoon dialogue track where the 30th percentile can sit close to the
    # loudest frame.
    threshold = min(max(-45.0, floor + 8.0), float(np.max(db)) - 1.0)
    active = db >= threshold
    runs = _runs(active)
    # Close small pauses, then reject very short bursts that are usually
    # residual music/noise rather than a usable speaker sample.
    max_gap = max(1, int(round(0.25 / 0.010)))
    merged: list[tuple[int, int]] = []
    for start, end in runs:
        if merged and start - merged[-1][1] <= max_gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    min_frames = max(1, int(round(0.30 / 0.010)))
    expanded: list[tuple[int, int]] = []
    pad = int(round(0.08 / 0.010))
    for start, end in merged:
        if end - start < min_frames:
            continue
        expanded.append((max(0, start - pad), min(count, end + pad)))
    return [(start * hop, min(audio.size, end * hop + frame)) for start, end in expanded]


def _windows(runs: list[tuple[int, int]], sample_rate: int, window_seconds: float, hop_seconds: float) -> list[tuple[int, int]]:
    window = max(1, int(round(window_seconds * sample_rate)))
    hop = max(1, int(round(hop_seconds * sample_rate)))
    result: list[tuple[int, int]] = []
    for run_start, run_end in runs:
        length = run_end - run_start
        if length <= window:
            result.append((run_start, run_end))
            continue
        start = run_start
        while start < run_end:
            end = min(run_end, start + window)
            if end - start >= int(0.55 * sample_rate):
                result.append((start, end))
            if end >= run_end:
                break
            start += hop
    return result


def _transcript_windows(
    transcript_segments: Any,
    runs: list[tuple[int, int]],
    sample_rate: int,
    audio_size: int,
    max_window_seconds: float = MAX_ASR_WINDOW_SECONDS,
) -> list[dict[str, int]]:
    if not isinstance(transcript_segments, list):
        return []
    minimum = max(1, int(round(MIN_ASR_WINDOW_SECONDS * sample_rate)))
    maximum = max(minimum, int(round(max_window_seconds * sample_rate)))
    pause = max(1, int(round(0.6 * sample_rate)))
    windows: list[dict[str, int]] = []
    for index, segment in enumerate(transcript_segments):
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str) or not segment["text"].strip():
            continue
        try:
            start_seconds = float(segment["start"])
            end_seconds = float(segment["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not np.isfinite(start_seconds) or not np.isfinite(end_seconds):
            continue
        start = max(0, int(round(start_seconds * sample_rate)))
        end = min(audio_size, int(round(end_seconds * sample_rate)))
        if end <= start:
            continue

        voiced: list[tuple[int, int]] = []
        for run_start, run_end in runs:
            left, right = max(start, run_start), min(end, run_end)
            if right <= left:
                continue
            if voiced and left - voiced[-1][1] <= pause:
                voiced[-1] = (voiced[-1][0], right)
            else:
                voiced.append((left, right))

        for voiced_start, voiced_end in voiced:
            if voiced_end - voiced_start < minimum:
                continue
            chunks: list[tuple[int, int]] = []
            chunk_start = voiced_start
            while chunk_start < voiced_end:
                chunk_end = min(voiced_end, chunk_start + maximum)
                chunks.append((chunk_start, chunk_end))
                chunk_start = chunk_end
            if len(chunks) > 1 and chunks[-1][1] - chunks[-1][0] < minimum:
                chunks[-2] = (chunks[-2][0], chunks[-1][1])
                chunks.pop()
            windows.extend(
                {"start": chunk_start, "end": chunk_end, "transcript_index": index}
                for chunk_start, chunk_end in chunks
                if chunk_end - chunk_start >= minimum
            )
    return windows


def _load_encoder(device: str, model_dir: Path):
    os.environ.setdefault("HF_HOME", str(_hf_cache()))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    model_dir.mkdir(parents=True, exist_ok=True)
    try:
        from speechbrain.inference.speaker import EncoderClassifier
        from speechbrain.utils.fetching import LocalStrategy
    except ImportError as exc:
        raise RuntimeError("未安装 speechbrain；无法进行真实 speaker embedding") from exc
    try:
        return EncoderClassifier.from_hparams(
            source=MODEL_ID,
            savedir=str(model_dir),
            run_opts={"device": device},
            local_strategy=LocalStrategy.COPY,
        )
    except Exception as exc:
        raise RuntimeError(f"speaker embedding 模型加载失败：{exc.__class__.__name__}") from exc


def _embed(encoder: Any, samples: np.ndarray) -> np.ndarray:
    waveform = torch.from_numpy(np.asarray(samples, dtype=np.float32)).unsqueeze(0)
    with torch.inference_mode():
        value = encoder.encode_batch(waveform)
    vector = value.detach().cpu().numpy().reshape(-1).astype(np.float32)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-8:
        raise RuntimeError("speaker embedding 输出为零向量")
    return vector / norm


def _cluster(vectors: np.ndarray, distance: float) -> np.ndarray:
    if len(vectors) <= 1:
        return np.zeros(len(vectors), dtype=np.int64)
    try:
        model = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=distance,
            metric="cosine",
            linkage="average",
        )
    except TypeError:  # Compatibility with older sklearn if the runtime is replaced.
        model = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=distance,
            affinity="cosine",
            linkage="average",
        )
    return np.asarray(model.fit_predict(vectors), dtype=np.int64)


def _merge_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for turn in turns:
        if merged and merged[-1]["speaker"] == turn["speaker"] and turn["start"] - merged[-1]["end"] <= 0.35:
            merged[-1]["end"] = turn["end"]
            continue
        merged.append(dict(turn))
    return merged


def run(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    audio, sample_rate = _read_audio(input_path)
    runs = _voice_runs(audio, sample_rate)
    if not runs:
        raise RuntimeError("未检测到可用于 speaker embedding 的清晰人声")
    transcript_path = Path(args.transcript).resolve() if args.transcript else input_path.with_name("transcript.json")
    transcript_windows: list[dict[str, int]] = []
    if transcript_path.is_file():
        try:
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            transcript = {}
        transcript_windows = _transcript_windows(
            transcript.get("segments") if isinstance(transcript, dict) else None,
            runs,
            sample_rate,
            audio.size,
        )
    windows = (
        [(item["start"], item["end"]) for item in transcript_windows]
        if transcript_windows
        else _windows(runs, sample_rate, args.window_seconds, args.hop_seconds)
    )
    if not windows:
        raise RuntimeError("VAD 有结果但没有足够长的 speaker embedding 窗口")
    started = time.perf_counter()
    encoder = _load_encoder(args.device, _model_dir())
    vectors: list[np.ndarray] = []
    for start, end in windows:
        sample = audio[start:end]
        if not transcript_windows and len(sample) < int(0.8 * sample_rate):
            sample = np.pad(sample, (0, int(0.8 * sample_rate) - len(sample)))
        vectors.append(_embed(encoder, sample))
    matrix = np.stack(vectors)
    labels = _cluster(matrix, args.cluster_distance)
    turns: list[dict[str, Any]] = []
    if transcript_windows:
        for item, label in zip(transcript_windows, labels):
            turns.append({
                "start": round(item["start"] / sample_rate, 6),
                "end": round(item["end"] / sample_rate, 6),
                "speaker": f"cluster-{int(label)}",
                "cluster_id": int(label),
                "transcript_index": item["transcript_index"],
            })
    else:
        # Energy-only fallback windows overlap; export midpoint-bounded turns.
        group_start = 0
        for group_end in range(1, len(windows) + 1):
            boundary = group_end == len(windows) or windows[group_end][0] >= windows[group_end - 1][1]
            if not boundary:
                continue
            group = windows[group_start:group_end]
            centers = [(start + end) / 2.0 for start, end in group]
            for offset, ((start, end), label) in enumerate(zip(group, labels[group_start:group_end])):
                left = start if offset == 0 else int(round((centers[offset - 1] + centers[offset]) / 2.0))
                right = end if offset == len(group) - 1 else int(round((centers[offset] + centers[offset + 1]) / 2.0))
                turns.append({
                    "start": round(left / sample_rate, 6),
                    "end": round(right / sample_rate, 6),
                    "speaker": f"cluster-{int(label)}",
                    "cluster_id": int(label),
                })
            group_start = group_end
        turns = _merge_turns(turns)
    centroids: dict[str, list[float]] = {}
    for label in sorted(set(labels.tolist())):
        centroid = matrix[labels == label].mean(axis=0)
        centroid /= max(float(np.linalg.norm(centroid)), 1e-8)
        centroids[f"cluster-{int(label)}"] = centroid.round(8).tolist()
    elapsed = time.perf_counter() - started
    manifest: dict[str, Any] = {
        "backend": "speechbrain-ecapa-voxceleb",
        "model": MODEL_ID,
        "device": args.device,
        "sample_rate": sample_rate,
        "duration_sec": round(audio.size / sample_rate, 6),
        "vad_runs": len(runs),
        "embedding_windows": len(windows),
        "segmentation_method": "asr_sentence_vad_clip" if transcript_windows else "energy_vad_windows",
        "transcript_segments_used": len({item["transcript_index"] for item in transcript_windows}),
        "cluster_count": len(centroids),
        "cluster_distance": args.cluster_distance,
        "elapsed_sec": round(elapsed, 3),
        "realtime_factor": round((audio.size / sample_rate) / elapsed, 3) if elapsed else None,
        "cross_episode_identity": "cross-episode mapping belongs to the series character table",
        "segments": turns,
        "cluster_embeddings": centroids,
    }
    output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def status(args: argparse.Namespace) -> dict[str, Any]:
    directory = _model_dir()
    required = ["hyperparams.yaml", "embedding_model.ckpt", "classifier.ckpt", "mean_var_norm_emb.ckpt"]
    present = {name: (directory / name).is_file() for name in required}
    return {
        "status": "ready" if all(present.values()) else "missing_model",
        "backend": "speechbrain",
        "model": MODEL_ID,
        "model_dir": str(directory),
        "files": present,
        "device_requested": args.device,
        "token_required": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Local ECAPA speaker diarization")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--transcript")
    parser.add_argument("--device", default=os.getenv("DIARIZATION_DEVICE", DEFAULT_DEVICE))
    parser.add_argument("--window-seconds", type=float, default=float(os.getenv("DIARIZATION_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS)))
    parser.add_argument("--hop-seconds", type=float, default=float(os.getenv("DIARIZATION_HOP_SECONDS", DEFAULT_HOP_SECONDS)))
    parser.add_argument("--cluster-distance", type=float, default=float(os.getenv("DIARIZATION_CLUSTER_DISTANCE", DEFAULT_CLUSTER_DISTANCE)))
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status(args), ensure_ascii=False))
        return 0
    if not args.input or not args.output:
        parser.error("分析需要 --input 和 --output")
    print(json.dumps(run(args), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
