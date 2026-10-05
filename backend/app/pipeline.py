from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable

from .adapters.base import AdapterError, BlockedError
from .adapters.diarization import DiarizationAdapter
from .adapters.faster_whisper import FasterWhisperAdapter
from .adapters.media import (
    _run,
    combine_dialogue,
    extract_audio,
    mix_background_and_dialogue,
    mux_candidate,
    probe_media,
    restore_song_intervals,
    sha256_file,
    write_srt,
)
from .adapters.omnivoice import OmniVoiceAdapter
from .adapters.separation import SeparationAdapter
from .adapters.text_rewriter import TextRewriterAdapter
from .config import Settings
from .db import Database, dump, load, new_id, now_iso


STAGES = ("probe", "separate", "transcribe", "diarize", "characters", "rewrite", "synthesize", "mix", "subtitle", "export")
DIARIZATION_PREROLL_SEC = 0.08
MAX_DIALOGUE_TAIL_SEC = 1.0
SUBTITLE_MASK_PREROLL_SEC = 0.3
SUBTITLE_MASK_TAIL_SEC = 0.8

# A successful stage has just re-validated its own artifacts. Keep this list
# narrow so persistent warnings (songs, unverified cross-episode voices) stay
# visible while stale blocking errors can recover on retry.
RECOVERABLE_STAGE_ANOMALIES: dict[str, set[str]] = {
    "probe": {"SOURCE_NOT_FOUND", "SOURCE_INVALID"},
    "separate": {"SPEECH_TRACK_MISSING", "BANDIT_NOT_CONFIGURED", "BANDIT_COMMAND_INVALID", "BANDIT_NO_OUTPUT", "BANDIT_BACKGROUND_MISSING", "BANDIT_MANIFEST_INVALID", "BANDIT_PATH_OUTSIDE_WORK", "BANDIT_SPEECH_MISSING", "BANDIT_BACKGROUND_EMPTY"},
    "transcribe": {"SPEECH_TRACK_MISSING", "FASTER_WHISPER_UNAVAILABLE", "FASTER_WHISPER_MODEL_LOAD_FAILED", "FASTER_WHISPER_TRANSCRIBE_FAILED", "FASTER_WHISPER_INVALID_RESPONSE", "FASTER_WHISPER_NO_SEGMENTS", "FASTER_WHISPER_EMPTY"},
    "diarize": {"DIARIZATION_NO_OUTPUT", "DIARIZATION_INVALID_OUTPUT", "DIARIZATION_NOT_CONFIGURED", "DIARIZATION_EMPTY", "PYANNOTE_MISSING", "NO_SPEAKER_SEGMENTS"},
    "characters": {"NO_SPEAKER_SEGMENTS"},
    "rewrite": {"TEXT_API_NOT_CONFIGURED", "TEXT_API_UNAVAILABLE", "TEXT_API_FAILED", "TEXT_API_INVALID_RESPONSE", "REWRITER_INVALID_JSON", "REWRITER_INVALID_SHAPE", "REWRITER_COUNT_MISMATCH", "REWRITER_ID_MISMATCH", "REWRITER_EMPTY_TEXT", "REWRITER_OVER_BUDGET"},
    "synthesize": {"VOICE_PROFILE_MISSING", "OMNIVOICE_OPENAPI_UNAVAILABLE", "OMNIVOICE_OPENAPI_FAILED", "OMNIVOICE_OPENAPI_INVALID", "OMNIVOICE_PROFILE_UNSUPPORTED", "OMNIVOICE_PROFILE_UPLOAD_FAILED", "OMNIVOICE_PROFILE_UPLOAD_INVALID", "OMNIVOICE_PROFILE_CREATE_FAILED", "OMNIVOICE_PROFILE_CREATE_INVALID", "OMNIVOICE_PROFILE_EVENT_FAILED", "OMNIVOICE_PROFILE_EVENT_EMPTY", "OMNIVOICE_PROFILE_EVENT_INVALID", "OMNIVOICE_PROFILE_VERIFY_FAILED", "OMNIVOICE_EMPTY_TEXT", "OMNIVOICE_TTS_FAILED", "OMNIVOICE_TTS_INVALID", "OMNIVOICE_TTS_EMPTY", "OMNIVOICE_TTS_AUDIO_FETCH_FAILED", "TTS_OVER_DURATION", "TTS_NO_OUTPUT"},
    "mix": {"BACKGROUND_TRACK_MISSING", "SEGMENT_AUDIO_MISSING", "SEGMENT_AUDIO_OVERLAP", "NO_DIALOGUE_AUDIO", "NO_SYNTHESIS_AUDIO", "MEDIA_OUTPUT_EMPTY"},
    "export": {"EXPORT_INPUT_MISSING", "EXPORT_TRUNCATED", "EXPORT_EMPTY"},
}


class PipelineCancelled(RuntimeError):
    pass


class Pipeline:
    """A conservative, checkpointed media pipeline.

    Every stage writes a verifiable artifact before its checkpoint is marked
    complete. Missing models/services therefore leave a blocked job instead of
    producing a demo result that looks finished.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        progress: Callable[[str, float, str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ):
        self.settings = settings
        self.db = db
        self.progress = progress or (lambda _stage, _value, _message: None)
        self.cancelled = cancelled or (lambda: False)
        self.separation = SeparationAdapter(settings)
        self.transcriber = FasterWhisperAdapter(settings)
        self.diarizer = DiarizationAdapter(settings)
        self.rewriter = TextRewriterAdapter(settings)
        self.tts = OmniVoiceAdapter(settings)

    def run(self, job_id: str) -> None:
        job = self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job:
            return
        project = self.db.fetchone("SELECT * FROM projects WHERE id = ?", (job["project_id"],))
        if not project:
            self._fail_job(job, "PROJECT_NOT_FOUND", "任务关联的项目不存在", blocked=True)
            return
        try:
            if job["kind"] == "segment":
                self._run_segment_job(job, project)
            else:
                self._run_full_job(job, project)
        except PipelineCancelled:
            self._set_job(job["id"], status="cancelled", message="任务已取消", completed_at=now_iso())
            self._set_project(project["id"], status="paused", status_message="任务已取消；已保留已有缓存")
        except BlockedError as exc:
            self._record_anomaly(project["id"], None, exc.code, "blocking", True, exc.message, exc.action, exc.details)
            self._fail_job(job, exc.code, exc.message, blocked=True)
        except AdapterError as exc:
            self._record_anomaly(project["id"], None, exc.code, "blocking", True, exc.message, exc.action, exc.details)
            self._fail_job(job, exc.code, exc.message, blocked=True)
        except Exception as exc:  # keep a worker crash from leaving running forever
            self._record_anomaly(project["id"], None, "PIPELINE_UNEXPECTED", "blocking", True, "处理流程发生未预期错误", "查看后端日志并重试", {"type": exc.__class__.__name__, "stage": job.get("stage")})
            self._fail_job(job, "PIPELINE_UNEXPECTED", "处理流程发生未预期错误", blocked=False)

    def _run_full_job(self, job: dict[str, Any], project: dict[str, Any]) -> None:
        checkpoint = load(project.get("checkpoint_json"), {})
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        completed = set(checkpoint.get("completed_stages") or [])
        from_stage = str(job.get("from_stage") or "auto")
        if int(job.get("force") or 0) or from_stage != "auto":
            start = 0 if from_stage in {"auto", "", "probe"} and int(job.get("force") or 0) else self._stage_index(from_stage)
            self.invalidate(project["id"], STAGES[start])
            completed = {stage for stage in completed if self._stage_index(stage) < start}
            checkpoint["completed_stages"] = sorted(completed, key=self._stage_index)
        # Do not retain a downstream checkpoint when an earlier artifact is
        # missing or invalid after a crash/restart.
        metadata = load(project.get("metadata_json"), {})
        first_invalid = next(
            (
                index
                for index, stage in enumerate(STAGES)
                if stage in completed and not self._stage_artifact_valid(stage, {"project": project, "metadata": metadata})
            ),
            None,
        )
        if first_invalid is not None:
            self.invalidate(project["id"], STAGES[first_invalid])
            project = self.db.fetchone("SELECT * FROM projects WHERE id = ?", (project["id"],)) or project
            completed = {stage for stage in completed if self._stage_index(stage) < first_invalid}
            checkpoint["completed_stages"] = sorted(completed, key=self._stage_index)
        self._set_project(project["id"], status="processing", status_message="任务已开始")
        self._set_job(job["id"], status="running", stage=STAGES[min(len(completed), len(STAGES) - 1)], message="正在恢复可用缓存")
        context = {"project": project, "metadata": load(project.get("metadata_json"), {})}
        for index, stage in enumerate(STAGES):
            self._check_cancel()
            if stage in completed and self._stage_artifact_valid(stage, context):
                continue
            stage_message = self._stage_message(stage)
            self._set_job(job["id"], stage=stage, progress=index / len(STAGES), message=stage_message)
            self._set_project(project["id"], current_stage=stage, progress=index / len(STAGES), status="processing", status_message=stage_message)
            self.progress(stage, index / len(STAGES), stage_message)
            self._run_stage(stage, context)
            self._resolve_stage_anomalies(project["id"], stage)
            completed.add(stage)
            checkpoint["completed_stages"] = sorted(completed, key=self._stage_index)
            context["metadata"] = context.get("metadata") or {}
            self._persist_context(project["id"], context["metadata"], checkpoint)
            self._set_project(project["id"], current_stage=stage, progress=(index + 1) / len(STAGES), status="processing", status_message=f"{stage} 已完成")
            self._set_job(job["id"], checkpoint_json=dump(checkpoint), progress=(index + 1) / len(STAGES), message=f"{stage} 已完成")
        blocking = self.db.fetchone("SELECT COUNT(*) AS count FROM anomalies WHERE project_id = ? AND blocking = 1 AND resolved = 0", (project["id"],))
        warning_count = self.db.fetchone("SELECT COUNT(*) AS count FROM anomalies WHERE project_id = ? AND resolved = 0", (project["id"],))
        if int((blocking or {}).get("count") or 0):
            raise BlockedError("PROJECT_BLOCKED", "项目仍有未解决的阻断异常", "先处理项目异常后再导出")
        status = "completed_with_warnings" if int((warning_count or {}).get("count") or 0) else "completed"
        self._set_project(project["id"], status=status, current_stage="done", progress=1, status_message="候选成片已生成" if status == "completed" else "候选成片已生成，但仍有警告")
        self._set_job(job["id"], status=status, stage="done", progress=1, message="处理完成", completed_at=now_iso(), checkpoint_json=dump(checkpoint))

    def _run_segment_job(self, job: dict[str, Any], project: dict[str, Any]) -> None:
        segment_id = str(load(job.get("checkpoint_json"), {}).get("segment_id") or "")
        if not segment_id:
            raise BlockedError("SEGMENT_NOT_SPECIFIED", "句级重生成任务缺少 segment_id")
        segment = self.db.fetchone("SELECT * FROM segments WHERE id = ? AND project_id = ?", (segment_id, project["id"]))
        if not segment:
            raise BlockedError("SEGMENT_NOT_FOUND", "句级重生成目标不存在")
        self._set_project(project["id"], status="processing", status_message="正在重新生成句级配音")
        self._set_job(job["id"], status="running", stage="synthesize", progress=0.1, message="正在生成句级音频")
        context = {"project": project, "metadata": load(project.get("metadata_json"), {})}
        self._synthesize_segments(project, [segment])
        self._mix_export_dependencies(project, context)
        blocking = self.db.fetchone("SELECT COUNT(*) AS count FROM anomalies WHERE project_id = ? AND blocking = 1 AND resolved = 0", (project["id"],))
        if int((blocking or {}).get("count") or 0):
            raise BlockedError("PROJECT_BLOCKED", "项目仍有未解决的阻断异常", "先处理项目异常后再导出")
        self._set_job(job["id"], status="completed_with_warnings", stage="done", progress=1, message="句级配音及相关成片缓存已更新", completed_at=now_iso())
        self._set_project(project["id"], status="completed_with_warnings", current_stage="done", progress=1, status_message="句级配音已更新；请检查候选成片")

    def _run_stage(self, stage: str, context: dict[str, Any]) -> None:
        project = context["project"]
        if stage == "probe":
            source = self._source(project)
            if not source.is_file():
                raise BlockedError("SOURCE_NOT_FOUND", "源视频不存在或当前会话无法访问", "恢复源路径后重试")
            info = probe_media(self.settings.ffprobe, source)
            if info["video_streams"] < 1 or info["duration"] <= 0:
                raise BlockedError("SOURCE_INVALID", "源文件没有可用视频流或时长", "检查源视频格式")
            context["metadata"]["probe"] = {"duration": info["duration"], "format": info["format"], "audio_streams": info["audio_streams"], "video_streams": info["video_streams"]}
            self._set_project(project["id"], duration=info["duration"], current_stage="probe")
            return
        if stage == "separate":
            result = self.separation.separate(self._source(project), self._work(project))
            context["metadata"]["separation"] = {
                "speech": str(result.speech_path),
                "background": str(result.background_path),
                "music": str(result.music_path) if result.music_path else None,
                "effects": str(result.effects_path) if result.effects_path else None,
                "manifest": str(result.manifest_path),
                "song_intervals": result.song_intervals,
            }
            return
        if stage == "transcribe":
            self._transcribe(context)
            return
        if stage == "diarize":
            self._diarize(context)
            return
        if stage == "characters":
            self._characters(context)
            return
        if stage == "rewrite":
            self._rewrite(context)
            return
        if stage == "synthesize":
            self._synthesize_segments(project, self.db.fetchall("SELECT * FROM segments WHERE project_id = ? ORDER BY segment_index", (project["id"],)))
            return
        if stage == "mix":
            self._mix(context)
            return
        if stage == "subtitle":
            write_srt(self.db.fetchall("SELECT * FROM segments WHERE project_id = ? ORDER BY segment_index", (project["id"],)), self._work(project) / "target.srt")
            context["metadata"]["subtitle_path"] = str(self._work(project) / "target.srt")
            return
        if stage == "export":
            self._export(context)
            return
        raise BlockedError("STAGE_UNKNOWN", f"未知处理阶段：{stage}")

    def _transcribe(self, context: dict[str, Any]) -> None:
        project = context["project"]
        separation = context["metadata"].get("separation") or {}
        speech = Path(str(separation.get("speech") or ""))
        if not speech.is_file():
            raise BlockedError("SPEECH_TRACK_MISSING", "没有可验证的对白分离轨")
        self._detect_songs(context)
        series = self.db.fetchone("SELECT * FROM series WHERE id = ?", (project["series_id"],)) or {}
        result = self.transcriber.transcribe(speech, series.get("source_language"))
        transcript_path = self._work(project) / "transcript.json"
        transcript_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self.db.execute("DELETE FROM segments WHERE project_id = ?", (project["id"],))
        intervals = separation.get("song_intervals") or []
        manual_intervals = separation.get("song_intervals_override") or []
        now = now_iso()
        values = []
        for index, item in enumerate(result.get("segments") or []):
            start, end = float(item["start"]), float(item["end"])
            overlap = max((min(end, float(song["end"])) - max(start, float(song["start"]))) for song in intervals) if intervals else 0
            manual_start = any(float(song["start"]) <= start < float(song["end"]) for song in manual_intervals)
            kind = "song" if overlap > max(0.1, (end - start) * 0.35) or manual_start else "dialogue"
            segment_metadata = {"words": item.get("words") or []}
            if manual_start:
                segment_metadata["song_interval_override"] = True
            values.append((new_id("segment"), project["id"], index, start, end, None, None, kind, str(item["text"]), None, series.get("target_language"), series.get("speed"), None, None, None, None, "pending", 1, 1, None, dump(segment_metadata), now, now))
        if values:
            self.db.executemany(
                "INSERT INTO segments (id, project_id, segment_index, start_sec, end_sec, speaker_key, speaker_name, kind, source_text, target_text, target_language, speed, duration_delta, voice_profile, audio_path, audio_sha256, status, source_revision, target_revision, error_message, metadata_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        context["metadata"]["transcript"] = str(transcript_path)

    def _detect_songs(self, context: dict[str, Any]) -> None:
        project = context["project"]
        work = self._work(project)
        output = work / "songs.json"
        runtime = self.settings.root_dir / "work/model-runtime/venv/Scripts/python.exe"
        script = self.settings.root_dir / "scripts/model_song_detection.py"
        if not runtime.is_file() or not script.is_file():
            raise BlockedError("SONG_DETECTOR_MISSING", "缺少歌曲检测运行时，不能保证原歌曲被保留")
        # Use the original mix, not a stem where singing may already be removed.
        original_audio = work / "song-detection-input.wav"
        if not original_audio.is_file():
            extract_audio(self.settings.ffmpeg, self._source(project), original_audio, mono=True, sample_rate=16000)
        _run([str(runtime), str(script), "--input", str(original_audio), "--output", str(output)], timeout=300)
        detection = json.loads(output.read_text(encoding="utf-8"))
        separation = context["metadata"]["separation"]
        detected_intervals = detection["song_intervals"]
        manual_intervals = separation.get("song_intervals_override") or []
        separation["song_intervals_detected"] = detected_intervals
        separation["song_intervals"] = self._merge_song_intervals([*detected_intervals, *manual_intervals])
        separation["song_detection"] = {"status": detection["status"], "model": detection["model"], "evidence": str(output), "manual_override_count": len(manual_intervals)}
        if manual_intervals:
            self.db.execute(
                "UPDATE anomalies SET resolved = 1, updated_at = ? WHERE project_id = ? AND kind = 'SONG_DETECTION_UNCERTAIN' AND resolved = 0",
                (now_iso(), project["id"]),
            )
        elif detection["status"] in {"low_confidence", "no_song"}:
            message = (
                "歌曲检测置信度较低；请复核歌曲边界"
                if detection["status"] == "low_confidence"
                else "自动检测未找到歌曲；请确认原片中是否有需保留的歌曲"
            )
            self._record_anomaly(project["id"], None, "SONG_DETECTION_UNCERTAIN", "warning", False,
                                 message, "试听原片；如有歌曲请标记区间，无歌曲则确认此告警", {"evidence": str(output)})

    @staticmethod
    def _merge_song_intervals(intervals: list[dict[str, Any]]) -> list[dict[str, float]]:
        normalized: list[dict[str, float]] = []
        for item in intervals:
            start, end = float(item["start"]), float(item["end"])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise BlockedError("SONG_INTERVAL_INVALID", "歌曲区间存在无效时间，拒绝混音")
            normalized.append({"start": start, "end": end})
        merged: list[dict[str, float]] = []
        for item in sorted(normalized, key=lambda interval: interval["start"]):
            if merged and item["start"] <= merged[-1]["end"]:
                merged[-1]["end"] = max(merged[-1]["end"], item["end"])
            else:
                merged.append(item)
        return merged

    def _diarize(self, context: dict[str, Any]) -> None:
        project = context["project"]
        separation = context["metadata"].get("separation") or {}
        speech = Path(str(separation.get("speech") or ""))
        output = self._work(project) / "diarization.json"
        diarization = self.diarizer.diarize(speech, output)
        rows = self.db.fetchall("SELECT * FROM segments WHERE project_id = ? ORDER BY segment_index", (project["id"],))
        mapped = self.diarizer.assign([dict(row) for row in rows], diarization, project["id"])
        dialogue_rows = [row for row in mapped if row.get("kind") == "dialogue" and row.get("speaker_key")]
        cluster_count = len({str(row["speaker_key"]) for row in dialogue_rows})
        window_count = len(dialogue_rows)
        if cluster_count > 8 and window_count and cluster_count / window_count > 0.5:
            details = {"cluster_count": cluster_count, "window_count": window_count, "cluster_window_ratio": round(cluster_count / window_count, 3)}
            message = f"说话人聚类过度碎片化：{cluster_count} 个簇对应 {window_count} 个对白窗口"
            action = "先人工复核或调整 diarization 分段后重试；不会把低证据簇自动合并成少数角色"
            self._record_anomaly(project["id"], None, "DIARIZATION_FRAGMENTED", "blocking", True, message, action, details)
            raise BlockedError("DIARIZATION_FRAGMENTED", message, action, details)
        ordered = sorted(mapped, key=lambda row: int(row["segment_index"]))
        duration = float(project.get("duration") or 0)
        for row in ordered:
            metadata = load(row.get("metadata_json"), {})
            metadata = {**metadata, "speaker_raw": row.get("speaker_raw"), "speaker_embedding": row.get("speaker_embedding"), "speaker_embedding_model": row.get("speaker_embedding_model")}
            row["metadata_json"] = dump(metadata)
            if row.get("kind") == "dialogue" and row.get("voice_activity_start_sec") is not None and row.get("voice_activity_end_sec") is not None:
                asr_start, asr_end = float(row["start_sec"]), float(row["end_sec"])
                voice_start = float(row["voice_activity_start_sec"])
                voice_end = float(row["voice_activity_end_sec"])
                aligned_start = max(asr_start, voice_start - DIARIZATION_PREROLL_SEC)
                row["start_sec"] = aligned_start
                row["_timing_alignment"] = {
                    "asr_start_sec": asr_start,
                    "asr_end_sec": asr_end,
                    "voice_start_sec": voice_start,
                    "voice_end_sec": voice_end,
                    "start_sec": aligned_start,
                }
        for index, row in enumerate(ordered):
            alignment = row.pop("_timing_alignment", None)
            if alignment:
                available_end = min(duration or float("inf"), max(alignment["asr_end_sec"], alignment["voice_end_sec"] + MAX_DIALOGUE_TAIL_SEC))
                next_row = next((candidate for candidate in ordered[index + 1:] if candidate.get("kind") in {"dialogue", "song"}), None)
                if next_row:
                    next_start = float(next_row.get("start_sec", next_row.get("start", 0)))
                    if next_start > alignment["start_sec"]:
                        available_end = min(available_end, next_start)
                if available_end <= alignment["start_sec"]:
                    available_end = min(duration or float("inf"), alignment["start_sec"] + 0.2)
                row["end_sec"] = available_end
                metadata = load(row.get("metadata_json"), {})
                metadata["timing_alignment"] = {**alignment, "available_end_sec": available_end}
                row["metadata_json"] = dump(metadata)
        for row in ordered:
            values = {"speaker_key": row.get("speaker_key"), "metadata_json": row.get("metadata_json")}
            if row.get("kind") == "dialogue" and row.get("metadata_json"):
                if (load(row["metadata_json"], {}).get("timing_alignment") or {}).get("available_end_sec") is not None:
                    values.update({"start_sec": row["start_sec"], "end_sec": row["end_sec"]})
            self.db.update("segments", values, "id = ?", (row["id"],))
        context["metadata"]["diarization"] = str(output)

    def _characters(self, context: dict[str, Any]) -> None:
        project = context["project"]
        rows = self.db.fetchall("SELECT * FROM segments WHERE project_id = ? AND kind = 'dialogue' AND speaker_key IS NOT NULL ORDER BY segment_index", (project["id"],))
        if not rows:
            raise BlockedError("NO_SPEAKER_SEGMENTS", "没有可用于角色建档的对白说话人分段")
        by_speaker: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_speaker.setdefault(str(row["speaker_key"]), []).append(row)
        series = self.db.fetchone("SELECT * FROM series WHERE id = ?", (project["series_id"],)) or {}
        chars_dir = self._work(project) / "characters"
        used_character_names: set[str] = set()
        used_character_ids: set[str] = set()
        for speaker_key, speaker_rows in by_speaker.items():
            character = self.db.fetchone("SELECT * FROM characters WHERE series_id = ? AND speaker_key = ?", (project["series_id"], speaker_key))
            embedding = next((self._embedding(load(item.get("metadata_json"), {}).get("speaker_embedding")) for item in speaker_rows if self._embedding(load(item.get("metadata_json"), {}).get("speaker_embedding"))), None)
            embedding_model = next((load(item.get("metadata_json"), {}).get("speaker_embedding_model") for item in speaker_rows if self._embedding(load(item.get("metadata_json"), {}).get("speaker_embedding"))), None)
            if character and embedding_model and load(character.get("metadata_json"), {}).get("embedding_model") != embedding_model:
                raise BlockedError("VOICE_EMBEDDING_MODEL_CHANGED", "已有角色声纹来自不同或未知模型，拒绝复用旧音色绑定", "使用新模型命名空间重新分角色；原音色保留")
            if not character and embedding:
                # Different clusters from this episode must not be collapsed
                # again by the cross-episode library matcher.
                best_character = None
                best_similarity = 0.78
                for candidate in self.db.fetchall(
                    "SELECT c.* FROM characters c "
                    "WHERE c.series_id = ? AND c.embedding_path IS NOT NULL AND c.source_project_id != ? "
                    "AND EXISTS (SELECT 1 FROM segments s WHERE s.project_id = c.source_project_id "
                    "AND s.kind = 'dialogue' AND s.speaker_key = c.speaker_key) ORDER BY c.id",
                    (project["series_id"], project["id"]),
                ):
                    if candidate["id"] in used_character_ids:
                        continue
                    if not self._same_embedding_model(embedding_model, load(candidate.get("metadata_json"), {}).get("embedding_model")):
                        continue
                    candidate_embedding = self._embedding_file(candidate.get("embedding_path"))
                    similarity = self._cosine(embedding, candidate_embedding) if candidate_embedding else -1.0
                    if similarity >= best_similarity and (best_character is None or similarity > best_similarity):
                        best_character = candidate
                        best_similarity = similarity
                if best_character:
                    character = best_character
            if not character:
                character_id = new_id("character")
                now = now_iso()
                role_number = 1
                while f"角色 {role_number}" in used_character_names:
                    role_number += 1
                character_name = f"角色 {role_number}"
                self.db.insert("characters", {"id": character_id, "series_id": project["series_id"], "speaker_key": speaker_key, "name": character_name, "status": "candidate", "source_project_id": project["id"], "metadata_json": dump({"embedding_verified": any(load(x.get("metadata_json"), {}).get("speaker_embedding") for x in speaker_rows)}), "created_at": now, "updated_at": now})
                character = self.db.fetchone("SELECT * FROM characters WHERE id = ?", (character_id,))
            if not character:
                continue
            used_character_ids.add(str(character["id"]))
            used_character_names.add(str(character.get("name") or ""))
            def sample_bounds(item: dict[str, Any]) -> tuple[float, float]:
                alignment = load(item.get("metadata_json"), {}).get("timing_alignment") or {}
                start = float(alignment.get("voice_start_sec", item["start_sec"]))
                end = float(alignment.get("voice_end_sec", item["end_sec"]))
                return max(0.0, start - 0.05), end + 0.05

            speaker_rows = sorted(speaker_rows, key=lambda item: sample_bounds(item)[1] - sample_bounds(item)[0], reverse=True)
            sample_dir = chars_dir / character["id"]
            sample_dir.mkdir(parents=True, exist_ok=True)
            selected: list[dict[str, Any]] = []
            for candidate in speaker_rows:
                candidate_start, candidate_end = sample_bounds(candidate)
                if all(min(candidate_end, float(item["sample_end_sec"])) - max(candidate_start, float(item["sample_start_sec"])) <= 0.05 for item in selected):
                    selected.append({**candidate, "sample_start_sec": candidate_start, "sample_end_sec": candidate_end})
                if len(selected) == 2:
                    break
            if not selected:
                candidate = speaker_rows[0]
                candidate_start, candidate_end = sample_bounds(candidate)
                selected = [{**candidate, "sample_start_sec": candidate_start, "sample_end_sec": candidate_end}]
            if not character.get("main_sample_path"):
                main = extract_audio(self.settings.ffmpeg, Path(str(context["metadata"]["separation"]["speech"])), sample_dir / "main.wav", start=float(selected[0]["sample_start_sec"]), duration=float(selected[0]["sample_end_sec"]) - float(selected[0]["sample_start_sec"]), mono=True, sample_rate=24000)
                backup = None
                if len(selected) > 1:
                    try:
                        backup = extract_audio(self.settings.ffmpeg, Path(str(context["metadata"]["separation"]["speech"])), sample_dir / "backup.wav", start=float(selected[1]["sample_start_sec"]), duration=float(selected[1]["sample_end_sec"]) - float(selected[1]["sample_start_sec"]), mono=True, sample_rate=24000)
                    except AdapterError:
                        backup = None
                self.db.update("characters", {"main_sample_path": str(main), "backup_sample_path": str(backup) if backup else None, "updated_at": now_iso()}, "id = ?", (character["id"],))
                character["main_sample_path"] = str(main)
            if embedding and not character.get("embedding_path"):
                embedding_path = sample_dir / "embedding.json"
                embedding_path.write_text(json.dumps(embedding), encoding="utf-8")
                self.db.update("characters", {"embedding_path": str(embedding_path), "metadata_json": dump({**load(character.get("metadata_json"), {}), "embedding_verified": True, "embedding_model": embedding_model}), "updated_at": now_iso()}, "id = ?", (character["id"],))
                character["embedding_path"] = str(embedding_path)
            if not character.get("voice_profile"):
                sample_text = str(selected[0]["source_text"])
                # The display name (e.g. "角色 1") is only series-local and
                # collides in OmniVoice across series. Use persistent IDs for
                # the service-facing profile name while keeping the editable
                # character name unchanged in the database/UI.
                series_token = str(project["series_id"]).rsplit("-", 1)[-1][:16]
                character_token = str(character["id"]).rsplit("-", 1)[-1][:16]
                profile_name = f"cv_{series_token}_{character_token}"
                profile = self.tts.create_voice_profile(profile_name, Path(str(character["main_sample_path"])), sample_text, language=series.get("source_language") or "Auto")
                self.db.update("characters", {"voice_profile": profile, "status": "verified", "updated_at": now_iso()}, "id = ?", (character["id"],))
                character["voice_profile"] = profile
            self.db.execute("UPDATE segments SET speaker_key = ?, speaker_name = ?, voice_profile = ?, updated_at = ? WHERE project_id = ? AND speaker_key = ?", (character["speaker_key"], character["name"], character.get("voice_profile"), now_iso(), project["id"], speaker_key))
        # Without an embedding, a project-scoped key is intentionally not
        # reused in a later episode. Keep that uncertainty visible.
        if any(not load(row.get("metadata_json"), {}).get("speaker_embedding") for row in rows):
            self._record_anomaly(project["id"], None, "CROSS_EPISODE_VOICE_UNVERIFIED", "warning", False, "部分角色没有可验证 embedding；本集角色映射不会冒充跨集身份", "提供真实 embedding/声纹模型后再合并系列角色", {})

    def _rewrite(self, context: dict[str, Any]) -> None:
        project = context["project"]
        series = self.db.fetchone("SELECT * FROM series WHERE id = ?", (project["series_id"],)) or {}
        rows = self.db.fetchall("SELECT * FROM segments WHERE project_id = ? AND kind = 'dialogue' ORDER BY segment_index", (project["id"],))
        pending = [row for row in rows if not (row.get("target_text") or "").strip() or row.get("status") in {"pending", "failed", "blocked"}]
        if pending:
            rewritten = self.rewriter.rewrite([{"id": row["id"], "start": row["start_sec"], "end": row["end_sec"], "speaker_name": row.get("speaker_name"), "speaker_key": row.get("speaker_key"), "source_text": row["source_text"]} for row in pending], source_language=series.get("source_language") or "auto", target_language=series.get("target_language") or "zh-CN", level=series.get("level") or "L1", speed=float(series.get("speed") or 0.85))
            by_id = {item["id"]: item for item in rewritten}
            for row in pending:
                item = by_id[row["id"]]
                self.db.update("segments", {"target_text": item["target_text"], "target_language": series.get("target_language"), "speed": series.get("speed"), "status": "rewritten", "error_message": None, "updated_at": now_iso()}, "id = ?", (row["id"],))

    def _synthesize_segments(self, project: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        series = self.db.fetchone("SELECT * FROM series WHERE id = ?", (project["series_id"],)) or {}
        for row in rows:
            self._check_cancel()
            if row.get("kind") != "dialogue" or not (row.get("target_text") or "").strip():
                continue
            character = self.db.fetchone("SELECT * FROM characters WHERE series_id = ? AND speaker_key = ?", (project["series_id"], row.get("speaker_key"))) if row.get("speaker_key") else None
            if not character or not character.get("voice_profile"):
                raise BlockedError("VOICE_PROFILE_MISSING", f"片段 {row['id']} 没有已验证 voice profile", "先完成角色建档")
            text = str(row["target_text"]).strip()
            alignment = load(row.get("metadata_json"), {}).get("timing_alignment") or {}
            start_sec = float(alignment.get("start_sec", row["start_sec"]))
            available_end = float(alignment.get("available_end_sec", row["end_sec"]))
            limit = max(0.01, available_end - start_sec)
            if limit < 0.2:
                message = f"片段 {row['id']} 的原时间窗仅 {limit:.3f} 秒，短于真实配音可验证下限"
                details = {"window_seconds": limit, "minimum_seconds": 0.2, "source_text": row.get("source_text"), "target_text": text}
                self.db.update("segments", {"status": "blocked", "duration_delta": None, "error_message": message, "updated_at": now_iso()}, "id = ?", (row["id"],))
                self._record_anomaly(project["id"], row["id"], "SEGMENT_WINDOW_TOO_SHORT", "blocking", True, message, "合并相邻 ASR 窗口或人工调整时间边界后再配音", details)
                raise BlockedError("SEGMENT_WINDOW_TOO_SHORT", message, "合并相邻 ASR 窗口或人工调整时间边界后再配音", details)
            target_revision = int(row.get("target_revision") or 1)
            output = self._work(project) / "segments" / f"{row['id']}-r{target_revision}.wav"
            if output.is_file() and output.stat().st_size >= 128 and row.get("audio_path") == str(output):
                continue
            current_text = text
            generated: Path | None = None
            measured_duration = 0.0
            for attempt in range(3):
                self._check_cancel()
                output = self._work(project) / "segments" / f"{row['id']}-r{target_revision}.wav"
                generated = self.tts.generate(current_text, output, voice_profile=character.get("voice_profile"), ref_audio=None, ref_text=None, speed=float(row.get("speed") or series.get("speed") or 0.85), language=series.get("target_language") or "zh-CN", seed=240901, randomize_seed=False)
                info = probe_media(self.settings.ffprobe, generated)
                measured_duration = float(info.get("duration") or 0)
                if measured_duration <= 0:
                    raise BlockedError("TTS_NO_OUTPUT", f"片段 {row['id']} 没有可验证的音频时长")
                if measured_duration <= limit + 0.06:
                    break
                if attempt == 2:
                    message = f"片段 {row['id']} 在 2 次候选重写后仍超出原时间窗口：{measured_duration:.3f} 秒 > {limit:.3f} 秒"
                    details = {"window_seconds": limit, "measured_seconds": measured_duration, "rewrite_attempts": 2, "target_text": current_text}
                    self.db.update("segments", {"target_text": current_text, "target_revision": target_revision, "duration_delta": measured_duration - limit, "status": "blocked", "error_message": message, "updated_at": now_iso()}, "id = ?", (row["id"],))
                    raise BlockedError("TTS_OVER_DURATION", message, "人工缩短目标台词或调整时间边界后重试；不会截断或变速", details)
                rewritten = self.rewriter.rewrite(
                    [{"id": row["id"], "start": row["start_sec"], "end": row["end_sec"], "speaker_name": row.get("speaker_name"), "speaker_key": row.get("speaker_key"), "source_text": row["source_text"]}],
                    source_language=series.get("source_language") or "auto",
                    target_language=series.get("target_language") or "zh-CN",
                    level=series.get("level") or "L1",
                    speed=float(row.get("speed") or series.get("speed") or 0.85),
                    measured_duration=measured_duration,
                    current_target_text=current_text,
                    retry_index=attempt + 1,
                )
                current_text = str(rewritten[0]["target_text"]).strip()
                target_revision += 1
                self.db.update("segments", {"target_text": current_text, "target_revision": target_revision, "audio_path": None, "audio_sha256": None, "duration_delta": None, "status": "rewritten", "error_message": None, "updated_at": now_iso()}, "id = ?", (row["id"],))
            if not generated:
                raise BlockedError("TTS_NO_OUTPUT", f"片段 {row['id']} 没有生成音频")
            final_end = min(available_end, start_sec + measured_duration)
            self.db.update("segments", {"target_text": current_text, "target_revision": target_revision, "end_sec": final_end, "audio_path": str(generated), "audio_sha256": sha256_file(generated), "duration_delta": measured_duration - limit, "voice_profile": character.get("voice_profile"), "status": "synthesized", "error_message": None, "updated_at": now_iso()}, "id = ?", (row["id"],))

    def _mix(self, context: dict[str, Any]) -> None:
        project = context["project"]
        separation = context["metadata"].get("separation") or {}
        background = Path(str(separation.get("background") or ""))
        if not background.is_file():
            raise BlockedError("BACKGROUND_TRACK_MISSING", "没有可验证的音乐/音效背景轨")
        rows = self.db.fetchall("SELECT * FROM segments WHERE project_id = ? ORDER BY segment_index", (project["id"],))
        audio: list[tuple[Path, float]] = []
        for row in rows:
            if row.get("kind") != "dialogue":
                continue
            path = Path(str(row.get("audio_path") or ""))
            if not path.is_file() or row.get("status") != "synthesized":
                raise BlockedError("SEGMENT_AUDIO_MISSING", f"片段 {row['id']} 没有真实配音音频")
            duration = float(probe_media(self.settings.ffprobe, path).get("duration") or 0)
            window = float(row["end_sec"]) - float(row["start_sec"])
            if duration > window + 0.06:
                raise BlockedError("SEGMENT_AUDIO_OVERLAP", f"片段 {row['id']} 配音超出时间窗口，拒绝截断混音")
            audio.append((path, float(row["start_sec"])))
        if not audio:
            raise BlockedError("NO_DIALOGUE_AUDIO", "没有可用于混音的真实对白音频")
        duration = float(project.get("duration") or context["metadata"].get("probe", {}).get("duration") or 0)
        dialogue = combine_dialogue(self.settings.ffmpeg, audio, duration, self._work(project) / "dialogue.timeline.wav")
        mixed = mix_background_and_dialogue(self.settings.ffmpeg, background, dialogue, duration, self._work(project) / "mixed.wav")
        intervals = separation.get("song_intervals") or []
        if intervals:
            mixed = restore_song_intervals(self.settings.ffmpeg, mixed, self._source(project), intervals, duration, self._work(project) / "mixed.song-safe.wav")
        context["metadata"]["dialogue_mix"] = str(dialogue)
        context["metadata"]["mixed"] = str(mixed)

    def _export(self, context: dict[str, Any]) -> None:
        project = context["project"]
        mixed = Path(str(context["metadata"].get("mixed") or ""))
        subtitle = Path(str(context["metadata"].get("subtitle_path") or self._work(project) / "target.srt"))
        if not mixed.is_file() or not subtitle.is_file():
            raise BlockedError("EXPORT_INPUT_MISSING", "混音或目标字幕不存在，拒绝导出")
        mask_intervals = self._subtitle_mask_intervals(project, context)
        output = mux_candidate(
            self.settings.ffmpeg,
            self._source(project),
            mixed,
            subtitle,
            self._work(project) / "candidate.mp4",
            mask_intervals=mask_intervals,
        )
        output_info = probe_media(self.settings.ffprobe, output)
        source_duration = float(project.get("duration") or context["metadata"].get("probe", {}).get("duration") or 0)
        if float(output_info.get("duration") or 0) + 0.05 < source_duration:
            raise BlockedError("EXPORT_TRUNCATED", "候选成片短于源视频，拒绝报告为完成", "检查混音时长与 FFmpeg 编码参数")
        self._set_project(project["id"], output_path=str(output), output_sha256=sha256_file(output))
        context["metadata"]["output"] = str(output)

    def _subtitle_mask_intervals(self, project: dict[str, Any], context: dict[str, Any]) -> list[dict[str, float]]:
        duration = float(project.get("duration") or context["metadata"].get("probe", {}).get("duration") or 0)
        separation = context["metadata"].get("separation") or {}
        intervals = [dict(item) for item in (separation.get("song_intervals") or [])]
        rows = self.db.fetchall(
            "SELECT start_sec, end_sec, metadata_json FROM segments WHERE project_id = ? AND kind = 'dialogue'",
            (project["id"],),
        )
        for row in rows:
            alignment = load(row.get("metadata_json"), {}).get("timing_alignment") or {}
            starts = [float(row["start_sec"]), float(alignment.get("voice_start_sec", row["start_sec"])), float(alignment.get("asr_start_sec", row["start_sec"]))]
            ends = [float(row["end_sec"]), float(alignment.get("voice_end_sec", row["end_sec"])), float(alignment.get("asr_end_sec", row["end_sec"]))]
            start = max(0.0, min(starts) - SUBTITLE_MASK_PREROLL_SEC)
            end = min(duration or float("inf"), max(ends) + SUBTITLE_MASK_TAIL_SEC)
            if end > start:
                intervals.append({"start": start, "end": end})
        return self._merge_song_intervals(intervals)

    def _mix_export_dependencies(self, project: dict[str, Any], context: dict[str, Any]) -> None:
        self._mix(context)
        write_srt(self.db.fetchall("SELECT * FROM segments WHERE project_id = ? ORDER BY segment_index", (project["id"],)), self._work(project) / "target.srt")
        context["metadata"]["subtitle_path"] = str(self._work(project) / "target.srt")
        self._export(context)

    def _stage_artifact_valid(self, stage: str, context: dict[str, Any]) -> bool:
        metadata = context["metadata"]
        project = context["project"]
        if stage == "probe":
            return float(project.get("duration") or 0) > 0 and bool(metadata.get("probe"))
        if stage == "separate":
            value = metadata.get("separation") or {}
            return all(Path(str(value.get(key) or "")).is_file() for key in ("speech", "background"))
        if stage == "transcribe":
            return bool(metadata.get("transcript")) and bool(self.db.fetchone("SELECT 1 AS ok FROM segments WHERE project_id = ? LIMIT 1", (project["id"],)))
        if stage == "diarize":
            return bool(metadata.get("diarization")) and Path(str(metadata["diarization"])).is_file()
        if stage == "characters":
            dialogue = self.db.fetchone("SELECT COUNT(DISTINCT speaker_key) AS count FROM segments WHERE project_id = ? AND kind = 'dialogue' AND speaker_key IS NOT NULL", (project["id"],))
            profiles = self.db.fetchone("SELECT COUNT(DISTINCT speaker_key) AS count FROM segments WHERE project_id = ? AND kind = 'dialogue' AND speaker_key IS NOT NULL AND voice_profile IS NOT NULL", (project["id"],))
            return int((dialogue or {}).get("count") or 0) > 0 and int((dialogue or {}).get("count") or 0) == int((profiles or {}).get("count") or 0)
        if stage == "rewrite":
            return not self.db.fetchone("SELECT 1 AS pending FROM segments WHERE project_id = ? AND kind = 'dialogue' AND (target_text IS NULL OR target_text = '')", (project["id"],))
        if stage == "synthesize":
            pending = self.db.fetchone("SELECT 1 AS pending FROM segments WHERE project_id = ? AND kind = 'dialogue' AND (status != 'synthesized' OR audio_path IS NULL OR audio_sha256 IS NULL)", (project["id"],))
            if pending:
                return False
            return all(
                Path(str(row.get("audio_path") or "")).is_file()
                and row.get("audio_sha256")
                and sha256_file(Path(str(row["audio_path"]))) == row["audio_sha256"]
                for row in self.db.fetchall("SELECT audio_path, audio_sha256 FROM segments WHERE project_id = ? AND kind = 'dialogue'", (project["id"],))
            )
        if stage == "mix":
            return Path(str(metadata.get("mixed") or "")).is_file()
        if stage == "subtitle":
            return Path(str(metadata.get("subtitle_path") or "")).is_file()
        if stage == "export":
            return bool(project.get("output_path")) and Path(str(project.get("output_path"))).is_file()
        return False

    def invalidate(self, project_id: str, from_stage: str) -> None:
        index = self._stage_index(from_stage)
        project = self.db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            return
        metadata = load(project.get("metadata_json"), {})
        for stage in STAGES[index:]:
            metadata.pop({"probe": "probe", "separate": "separation", "transcribe": "transcript", "diarize": "diarization", "characters": "characters", "mix": "mixed", "subtitle": "subtitle_path", "export": "output"}.get(stage, stage), None)
        if index <= self._stage_index("transcribe"):
            self.db.execute("DELETE FROM segments WHERE project_id = ?", (project_id,))
        elif index <= self._stage_index("synthesize"):
            self.db.execute("UPDATE segments SET audio_path = NULL, audio_sha256 = NULL, status = CASE WHEN kind = 'dialogue' THEN 'rewritten' ELSE status END, duration_delta = NULL WHERE project_id = ?", (project_id,))
        if index <= self._stage_index("rewrite"):
            self.db.execute("UPDATE segments SET target_text = NULL, target_revision = target_revision + 1, status = CASE WHEN kind = 'dialogue' THEN 'pending' ELSE status END, updated_at = ? WHERE project_id = ?", (now_iso(), project_id))
        self._set_project(project_id, metadata_json=dump(metadata), output_path=None, output_sha256=None, current_stage=from_stage, progress=0, status="created", status_message="相关缓存已失效")

    @staticmethod
    def _stage_index(stage: str) -> int:
        try:
            return STAGES.index(stage)
        except ValueError as exc:
            raise BlockedError("STAGE_INVALID", f"未知处理阶段：{stage}") from exc

    @staticmethod
    def _stage_message(stage: str) -> str:
        return {"probe": "正在探测源视频", "separate": "正在分离对白、音乐和音效", "transcribe": "正在获取句级转写", "diarize": "正在进行真实说话人分段", "characters": "正在匹配系列角色并建立音色", "rewrite": "正在按等级改写对白", "synthesize": "正在逐句生成并校验配音", "mix": "正在按原时间轴混音", "subtitle": "正在生成目标语言字幕", "export": "正在导出候选 MP4"}.get(stage, stage)

    def _check_cancel(self) -> None:
        if self.cancelled():
            raise PipelineCancelled

    @staticmethod
    def _source(project: dict[str, Any]) -> Path:
        return Path(str(project["source_path"])).resolve()

    @staticmethod
    def _work(project: dict[str, Any]) -> Path:
        path = Path(str(project["work_dir"])).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _persist_context(self, project_id: str, metadata: dict[str, Any], checkpoint: dict[str, Any]) -> None:
        self._set_project(project_id, metadata_json=dump(metadata), checkpoint_json=dump(checkpoint), updated_at=now_iso())

    @staticmethod
    def _embedding(value: Any) -> list[float] | None:
        if not isinstance(value, (list, tuple)) or not value:
            return None
        try:
            result = [float(item) for item in value]
        except (TypeError, ValueError):
            return None
        return result if all(math.isfinite(item) for item in result) else None

    @classmethod
    def _embedding_file(cls, path: Any) -> list[float] | None:
        if not path:
            return None
        try:
            return cls._embedding(json.loads(Path(str(path)).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _same_embedding_model(left: Any, right: Any) -> bool:
        # Equal vector lengths do not mean two encoders share a vector space.
        return isinstance(left, str) and bool(left.strip()) and left == right

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right):
            return -1.0
        denominator = math.sqrt(sum(item * item for item in left)) * math.sqrt(sum(item * item for item in right))
        return sum(a * b for a, b in zip(left, right)) / denominator if denominator else -1.0

    def _set_project(self, project_id: str, **values: Any) -> None:
        if values:
            self.db.update("projects", values, "id = ?", (project_id,))

    def _set_job(self, job_id: str, **values: Any) -> None:
        if values:
            values.setdefault("updated_at", now_iso())
            self.db.update("jobs", values, "id = ?", (job_id,))

    def _fail_job(self, job: dict[str, Any], code: str, message: str, *, blocked: bool) -> None:
        status = "blocked" if blocked else "failed"
        self._set_job(job["id"], status=status, error_code=code, error_message=message, message=message, completed_at=now_iso())
        self._set_project(job["project_id"], status=status, status_message=message)

    def _record_anomaly(self, project_id: str, segment_id: str | None, kind: str, severity: str, blocking: bool, message: str, action: str, details: Any) -> None:
        existing = self.db.fetchone("SELECT id FROM anomalies WHERE project_id = ? AND COALESCE(segment_id, '') = COALESCE(?, '') AND kind = ? AND resolved = 0", (project_id, segment_id, kind))
        values = {"segment_id": segment_id, "severity": severity, "blocking": int(blocking), "message": message, "action": action, "details_json": dump(details), "updated_at": now_iso()}
        if existing:
            self.db.update("anomalies", values, "id = ?", (existing["id"],))
        else:
            values.update({"id": new_id("anomaly"), "project_id": project_id, "kind": kind, "resolved": 0, "created_at": now_iso()})
            self.db.insert("anomalies", values)

    def _resolve_stage_anomalies(self, project_id: str, stage: str) -> None:
        """Resolve only blocking errors whose owning stage just succeeded."""
        recoverable = RECOVERABLE_STAGE_ANOMALIES.get(stage, set())
        rows = self.db.fetchall(
            "SELECT id, kind, details_json FROM anomalies WHERE project_id = ? AND resolved = 0 AND blocking = 1",
            (project_id,),
        )
        for row in rows:
            kind = str(row.get("kind") or "")
            if kind == "PIPELINE_UNEXPECTED":
                # New records carry the stage. The original failed job did
                # not, but its failure was at transcribe and is safe to
                # retire only when transcribe has now completed.
                details = load(row.get("details_json"), {})
                recorded_stage = details.get("stage") if isinstance(details, dict) else None
                if (recorded_stage and recorded_stage != stage) or (not recorded_stage and stage != "transcribe"):
                    continue
            elif kind not in recoverable:
                continue
            self.db.update("anomalies", {"resolved": 1, "updated_at": now_iso()}, "id = ?", (row["id"],))
