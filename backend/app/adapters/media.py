from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .base import AdapterError


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AdapterError("DEPENDENCY_MISSING", f"找不到外部程序：{command[0]}", "安装 FFmpeg 并将其加入 PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdapterError("MEDIA_TIMEOUT", f"外部程序超时：{command[0]}", "检查媒体文件或降低试片长度") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise AdapterError("MEDIA_COMMAND_FAILED", f"外部程序失败：{command[0]}", "检查媒体格式与 FFmpeg 安装", {"returncode": result.returncode, "detail": detail})
    return result


def probe_media(ffprobe: str, path: Path) -> dict[str, Any]:
    result = _run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError("MEDIA_PROBE_INVALID", "ffprobe 返回了不可解析结果") from exc
    streams = value.get("streams") or []
    format_data = value.get("format") or {}
    try:
        duration = float(format_data.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "duration": duration,
        "format": format_data.get("format_name", ""),
        "video_streams": sum(1 for s in streams if s.get("codec_type") == "video"),
        "audio_streams": sum(1 for s in streams if s.get("codec_type") == "audio"),
        "streams": streams,
    }


def trim_edge_silence(
    ffmpeg: str,
    path: Path,
    duration: float,
    *,
    threshold_db: float = -45.0,
    minimum_silence: float = 0.08,
    padding: float = 0.04,
) -> bool:
    """Remove only detected leading/trailing silence, keeping a short audio margin."""
    duration = float(duration)
    if duration <= 0:
        return False
    result = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={threshold_db}dB:d={minimum_silence}",
            "-f",
            "null",
            "-",
        ]
    )
    events: list[tuple[float, float]] = []
    pending_start: float | None = None
    for line in result.stderr.splitlines():
        start_match = re.search(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)", line)
        if start_match:
            pending_start = float(start_match.group(1))
            continue
        end_match = re.search(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)", line)
        if end_match and pending_start is not None:
            events.append((pending_start, float(end_match.group(1))))
            pending_start = None
    if pending_start is not None:
        events.append((pending_start, duration))
    if not events:
        return False

    trim_start = 0.0
    trim_end = duration
    if events[0][0] <= 0.02:
        trim_start = max(0.0, events[0][1] - padding)
    if events[-1][1] >= duration - 0.02:
        trim_end = min(duration, events[-1][0] + padding)
    if trim_start <= 0 and trim_end >= duration:
        return False
    if trim_end - trim_start < 0.2:
        return False

    temporary = path.with_name(f"{path.stem}.edge-trimmed{path.suffix}")
    try:
        _run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(path),
                "-af",
                f"atrim=start={trim_start:.6f}:end={trim_end:.6f},asetpts=PTS-STARTPTS",
                "-c:a",
                "pcm_s16le",
                str(temporary),
            ]
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def extract_audio(
    ffmpeg: str,
    source: Path,
    output: Path,
    *,
    start: float | None = None,
    duration: float | None = None,
    mono: bool = False,
    sample_rate: int | None = None,
) -> Path:
    """Extract an audio file without silently destroying the source layout.

    The media used as the replacement background must retain its channels and
    sample rate.  Callers doing ASR or speaker embedding can explicitly request
    the cheaper, deterministic mono/24 kHz representation.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-i", str(source)]
    if start is not None:
        command[2:2] = ["-ss", f"{start:.3f}"]
    if duration is not None:
        command[2:2] = ["-t", f"{duration:.3f}"]
    command += ["-vn"]
    if mono:
        command += ["-ac", "1"]
    if sample_rate:
        command += ["-ar", str(sample_rate)]
    command += ["-c:a", "pcm_s16le", str(output)]
    _run(command)
    if not output.is_file() or output.stat().st_size < 128:
        raise AdapterError("MEDIA_OUTPUT_EMPTY", f"没有生成有效音频：{output.name}")
    return output


def write_srt(segments: Iterable[dict[str, Any]], output: Path) -> Path:
    def timestamp(value: float) -> str:
        value = max(0.0, value)
        millis = int(round(value * 1000))
        hours, millis = divmod(millis, 3_600_000)
        minutes, millis = divmod(millis, 60_000)
        seconds, millis = divmod(millis, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    lines: list[str] = []
    index = 1
    for segment in segments:
        if segment.get("kind") != "dialogue" or not (segment.get("target_text") or "").strip():
            continue
        lines += [
            str(index),
            f"{timestamp(float(segment['start_sec']))} --> {timestamp(float(segment['end_sec']))}",
            str(segment["target_text"]).strip(),
            "",
        ]
        index += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8-sig")
    return output


def combine_dialogue(ffmpeg: str, segment_audio: list[tuple[Path, float]], duration: float, output: Path) -> Path:
    """Build a timeline of generated line audio using true segment offsets."""
    if not segment_audio:
        raise AdapterError("NO_SYNTHESIS_AUDIO", "没有可用于混音的真实配音音频")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y"]
    for path, _ in segment_audio:
        command += ["-i", str(path)]
    filters: list[str] = []
    labels: list[str] = []
    for index, (_, start) in enumerate(segment_audio):
        label = f"a{index}"
        delay = max(0, int(round(start * 1000)))
        filters.append(f"[{index}:a]aresample=24000,adelay={delay}:all=1,apad=pad_dur={max(0.0, duration - start):.3f}[{label}]")
        labels.append(f"[{label}]")
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
        + "alimiter=limit=0.95:attack=5:release=50,"
        + f"atrim=duration={duration:.3f},aresample=24000[mix]"
    )
    command += ["-filter_complex", ";".join(filters), "-map", "[mix]", "-c:a", "pcm_s16le", str(output)]
    _run(command)
    if not output.is_file() or output.stat().st_size < 128:
        raise AdapterError("MEDIA_OUTPUT_EMPTY", "配音混音结果为空")
    return output


def mix_background_and_dialogue(ffmpeg: str, background: Path, dialogue: Path, duration: float, output: Path) -> Path:
    """Mix generated dialogue over the separated music/effects track.

    Both inputs are padded/trimmed to the probed source duration before the
    mix.  This keeps the video timeline intact and avoids ``-shortest`` style
    truncation when a service returns a slightly short audio stream.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    length = max(0.01, float(duration))
    filter_graph = (
        f"[0:a]aresample=48000,apad,atrim=duration={length:.3f}[bg];"
        f"[1:a]aresample=48000,apad,atrim=duration={length:.3f}[dlg];"
        f"[bg][dlg]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.95:attack=5:release=50,atrim=duration={length:.3f}[mix]"
    )
    _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(background),
            "-i",
            str(dialogue),
            "-filter_complex",
            filter_graph,
            "-map",
            "[mix]",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        timeout=600,
    )
    if not output.is_file() or output.stat().st_size < 128:
        raise AdapterError("MEDIA_OUTPUT_EMPTY", "背景与配音混音结果为空")
    return output


def restore_song_intervals(
    ffmpeg: str,
    mixed: Path,
    source: Path,
    intervals: list[dict[str, float]],
    duration: float,
    output: Path,
) -> Path:
    """Replace marked song windows with the original mixed source audio.

    The separated background may omit vocals, so simply mixing generated
    dialogue over it can destroy songs.  The two masked tracks below retain
    the generated/background result outside song windows and the untouched
    source audio inside them, with no invented lyrics or subtitles.
    """
    valid = []
    for item in intervals:
        try:
            start, end = max(0.0, float(item["start"])), min(float(duration), float(item["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            valid.append((start, end))
    if not valid:
        return mixed
    valid.sort()
    merged: list[tuple[float, float]] = []
    for start, end in valid:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    expression = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in merged)
    song_mask = f"gt({expression},0)"
    length = max(0.01, float(duration))
    graph = (
        f"[0:a]aresample=48000,volume=0:enable='{song_mask}',apad,atrim=duration={length:.3f}[clean];"
        f"[1:a]aresample=48000,volume=0:enable='not({song_mask})',apad,atrim=duration={length:.3f}[song];"
        f"[clean][song]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.95:attack=5:release=50,atrim=duration={length:.3f}[mix]"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(mixed),
            "-i",
            str(source),
            "-filter_complex",
            graph,
            "-map",
            "[mix]",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        timeout=600,
    )
    if not output.is_file() or output.stat().st_size < 128:
        raise AdapterError("MEDIA_OUTPUT_EMPTY", "歌曲区间恢复后的混音为空")
    return output


def mux_candidate(
    ffmpeg: str,
    source: Path,
    background: Path,
    subtitles: Path,
    output: Path,
    *,
    mask_intervals: list[dict[str, float]] | None = None,
) -> Path:
    """Mux original video and a new audio track, burning only target-language subtitles."""
    output.parent.mkdir(parents=True, exist_ok=True)
    # The subtitle path is created under the project work directory. Escape the characters
    # understood by FFmpeg's subtitles filter while keeping Chinese filenames valid.
    subtitle_arg = str(subtitles).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    filters: list[str] = []
    valid_intervals = sorted(
        (
            max(0.0, float(item["start"])),
            float(item["end"]),
        )
        for item in (mask_intervals or [])
        if float(item["end"]) > max(0.0, float(item["start"]))
    )
    if valid_intervals:
        enabled = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in valid_intervals)
        filters.append(
            "drawbox=x=iw*0.22:y=ih*0.84:w=iw*0.56:h=ih*0.10:color=black:t=fill:"
            f"enable='{enabled}'"
        )
    filters.append(f"subtitles='{subtitle_arg}':force_style='MarginV=25,BorderStyle=1,Outline=2,Shadow=0'")
    command = [
        ffmpeg,
        "-y",
        "-i", str(source),
        "-i", str(background),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-vf", ",".join(filters),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output),
    ]
    _run(command, timeout=600)
    if not output.is_file() or output.stat().st_size < 1024:
        raise AdapterError("EXPORT_EMPTY", "候选成片为空")
    return output
