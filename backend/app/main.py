from __future__ import annotations

import mimetypes
import math
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .adapters.base import AdapterError
from .adapters.diarization import DiarizationAdapter
from .adapters.faster_whisper import FasterWhisperAdapter
from .adapters.media import probe_media, sha256_file
from .adapters.omnivoice import OmniVoiceAdapter
from .adapters.separation import SeparationAdapter
from .adapters.text_rewriter import LEVEL_RULES, TextRewriterAdapter
from .config import Settings, settings as default_settings
from .db import Database, dump, load, new_id, now_iso
from .pipeline import Pipeline, RECOVERABLE_STAGE_ANOMALIES, STAGES
from .queue import JobQueue, QueueConflict


ANOMALY_STAGE_BY_KIND = {
    kind: stage for stage, kinds in RECOVERABLE_STAGE_ANOMALIES.items() for kind in kinds
}
ANOMALY_STAGE_BY_KIND.update({
    "DIARIZATION_FRAGMENTED": "diarize",
    "SONG_DETECTION_UNCERTAIN": "separate",
    "CROSS_EPISODE_VOICE_UNVERIFIED": "characters",
    "SONG_UNREWRITTEN": "transcribe",
})


def anomaly_stage(row: dict[str, Any]) -> str | None:
    details = load(row.get("details_json"), {})
    recorded = details.get("stage") if isinstance(details, dict) else None
    if recorded in STAGES:
        return recorded
    kind = str(row.get("kind") or "")
    if kind == "STAGE_ARTIFACT_INVALID":
        return None
    if kind == "PIPELINE_UNEXPECTED":
        return "transcribe"
    return ANOMALY_STAGE_BY_KIND.get(kind)


class ApiFailure(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, action: str = "", details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.action = action
        self.details = details or {}


class SeriesCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_language: str = "auto"
    target_language: str = Field(default="zh-CN", min_length=2, max_length=30)
    level: str = "L1"
    speed: float = 0.82

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("系列名称不能为空")
        return value

    @field_validator("level")
    @classmethod
    def valid_level(cls, value: str) -> str:
        if value not in {"L1", "L2", "L3", "L4", "L5"}:
            raise ValueError("level 只能为 L1-L5")
        return value

    @field_validator("speed")
    @classmethod
    def valid_speed(cls, value: float) -> float:
        if not 0.55 <= value <= 1.15:
            raise ValueError("speed 必须在 0.55-1.15")
        return value


class ProjectPathCreate(BaseModel):
    source_path: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=200)


class JobCreate(BaseModel):
    kind: str = "process"
    from_stage: str = "auto"
    force: bool = False
    project_id: str | None = None


class StageConfirm(BaseModel):
    revision: int = Field(ge=1)
    accepted_warning_ids: list[str] = Field(default_factory=list)
    artifact_sha256: str | None = None


class SegmentPatch(BaseModel):
    target_text: str | None = Field(default=None, max_length=2000)
    speed: float | None = None
    speaker_key: str | None = Field(default=None, max_length=200)

    @field_validator("speed")
    @classmethod
    def valid_segment_speed(cls, value: float | None) -> float | None:
        if value is not None and not 0.55 <= value <= 1.15:
            raise ValueError("speed 必须在 0.55-1.15")
        return value


class CharacterPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    main_sample_path: str | None = None
    backup_sample_path: str | None = None
    voice_profile: str | None = Field(default=None, max_length=200)


class ExportRequest(BaseModel):
    burn_subtitles: bool = True


class SeparationImport(BaseModel):
    speech_path: str = Field(min_length=1)
    music_path: str | None = None
    effects_path: str | None = None
    background_path: str | None = None
    song_intervals: list[dict[str, float]] = Field(default_factory=list)


class SongIntervalsPatch(BaseModel):
    intervals: list[dict[str, float]]


class Runtime:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.queue = JobQueue(settings, db)


def _json_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    for key in ("metadata_json", "checkpoint_json", "glossary_json", "details_json"):
        if key in value:
            value[key.removesuffix("_json")] = load(value.pop(key), {})
    for key in ("force", "blocking", "resolved"):
        if key in value:
            value[key] = bool(value[key])
    return value


def _error(status_code: int, code: str, message: str, action: str = "", details: Any = None) -> None:
    raise ApiFailure(status_code, code, message, action, details)


def _service_payload(status: Any) -> dict[str, Any]:
    return status.as_dict() if hasattr(status, "as_dict") else dict(status)


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    app_settings = settings or default_settings
    app_db = db or Database(app_settings.db_path)
    runtime = Runtime(app_settings, app_db)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime.db.init()
        runtime.queue.start()
        yield
        runtime.queue.stop()

    app = FastAPI(title="童声配音台", version="1.0.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8787", "http://localhost:8787"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(ApiFailure)
    async def api_failure_handler(_request: Request, exc: ApiFailure):
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message, "action": exc.action, "details": exc.details}})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"error": {"code": "INVALID_REQUEST", "message": "请求参数无效", "action": "检查请求字段", "details": jsonable_encoder(exc.errors())}})

    @app.exception_handler(AdapterError)
    async def adapter_error_handler(_request: Request, exc: AdapterError):
        return JSONResponse(status_code=409, content={"error": {"code": exc.code, "message": exc.message, "action": exc.action, "details": exc.details}})

    def series_row(row: dict[str, Any]) -> dict[str, Any]:
        value = _json_row(row) or {}
        value["project_count"] = int((app_db.fetchone("SELECT COUNT(*) AS count FROM projects WHERE series_id = ?", (row["id"],)) or {}).get("count") or 0)
        value["character_count"] = int((app_db.fetchone("SELECT COUNT(*) AS count FROM characters WHERE series_id = ?", (row["id"],)) or {}).get("count") or 0)
        return value

    def job_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
        value = _json_row(row)
        if value is None:
            return None
        value["checkpoint"] = value.get("checkpoint", {})
        value["error"] = None if not (value.get("error_code") or value.get("error_message")) else {"code": value.get("error_code"), "message": value.get("error_message")}
        value["job_id"] = value.get("id")
        return value

    def segment_row(row: dict[str, Any]) -> dict[str, Any]:
        value = _json_row(row) or {}
        value.update({"index": row["segment_index"], "start": row["start_sec"], "end": row["end_sec"], "metadata": load(row.get("metadata_json"), {})})
        value["audio_url"] = f"/api/projects/{row['project_id']}/media/segment/{row['id']}" if row.get("audio_path") and Path(str(row["audio_path"])).is_file() else None
        return value

    def anomaly_row(row: dict[str, Any]) -> dict[str, Any]:
        value = _json_row(row) or {}
        value["blocking"] = bool(row.get("blocking"))
        value["resolved"] = bool(row.get("resolved"))
        value["stage"] = anomaly_stage(row)
        return value

    def character_row(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "series_id": row["series_id"], "speaker_key": row["speaker_key"], "name": row["name"], "main_sample_url": f"/api/series/{row['series_id']}/characters/{row['id']}/sample/main" if row.get("main_sample_path") else None, "backup_sample_url": f"/api/series/{row['series_id']}/characters/{row['id']}/sample/backup" if row.get("backup_sample_path") else None, "voice_profile": row.get("voice_profile"), "status": row.get("status"), "source_project_id": row.get("source_project_id"), "embedding_verified": bool(load(row.get("metadata_json"), {}).get("embedding_verified"))}

    def project_row(row: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
        value = _json_row(row) or {}
        series = app_db.fetchone("SELECT * FROM series WHERE id = ?", (row["series_id"],))
        if series:
            value.update({"series_name": series["name"], "target_language": series["target_language"], "level": series["level"], "speed": series["speed"], "source_language": series["source_language"]})
        value.update({"source_filename": Path(row["source_path"]).name, "stage": row.get("current_stage")})
        checkpoint = value.get("checkpoint") if isinstance(value.get("checkpoint"), dict) else {}
        value.update({
            "pending_confirmation_stage": checkpoint.get("pending_confirmation_stage"),
            "pending_confirmation_revision": checkpoint.get("pending_confirmation_revision"),
            "confirmed_stages": checkpoint.get("confirmed_stages", []),
            "confirmation_history": checkpoint.get("confirmation_history", []),
        })
        value["exception_count"] = int((app_db.fetchone("SELECT COUNT(*) AS count FROM anomalies WHERE project_id = ? AND resolved = 0", (row["id"],)) or {}).get("count") or 0)
        value["segment_count"] = int((app_db.fetchone("SELECT COUNT(*) AS count FROM segments WHERE project_id = ?", (row["id"],)) or {}).get("count") or 0)
        current = app_db.fetchone("SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC LIMIT 1", (row["id"],))
        if current:
            current["project_title"] = row.get("title")
        value["current_job"] = job_row(current)
        value["job"] = value["current_job"]
        source = Path(row["source_path"])
        value["source_exists"] = source.is_file()
        value["source_url"] = f"/api/projects/{row['id']}/media/source" if source.is_file() else None
        output = Path(str(row.get("output_path") or ""))
        value["output_exists"] = bool(row.get("output_path") and output.is_file())
        value["output_url"] = f"/api/projects/{row['id']}/media/output" if value["output_exists"] else None
        subtitle = Path(row["work_dir"]) / "target.srt"
        value["subtitle_url"] = f"/api/projects/{row['id']}/media/subtitle" if subtitle.is_file() else None
        if full:
            value["segments"] = [segment_row(item) for item in app_db.fetchall("SELECT * FROM segments WHERE project_id = ? ORDER BY segment_index", (row["id"],))]
            value["anomalies"] = [anomaly_row(item) for item in app_db.fetchall("SELECT * FROM anomalies WHERE project_id = ? ORDER BY created_at", (row["id"],))]
        return value

    def ensure_project_idle(project: dict[str, Any]) -> None:
        if project.get("status") in {"queued", "running", "processing"} or app_db.fetchone("SELECT 1 AS active FROM jobs WHERE project_id = ? AND status IN ('queued', 'running') LIMIT 1", (project["id"],)):
            _error(409, "PROJECT_BUSY", "项目正在处理，不能并发修改阶段结果", "等当前阶段完成后再编辑")

    def reopen_stage_for_review(project_id: str, stage: str, reason: str) -> bool:
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
        checkpoint = load(project.get("checkpoint_json"), {})
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        revisions = checkpoint.setdefault("stage_revisions", {})
        history = checkpoint.setdefault("confirmation_history", [])
        stage_index = STAGES.index(stage)
        now = now_iso()
        for item in history:
            if item.get("valid", True) and item.get("stage") in STAGES and STAGES.index(item["stage"]) >= stage_index:
                item.update({"valid": False, "invalidated_at": now, "invalidation_reason": reason})
        checkpoint["confirmed_stages"] = [item for item in checkpoint.get("confirmed_stages", []) if item in STAGES and STAGES.index(item) < stage_index]
        for item in STAGES[stage_index:]:
            revisions[item] = int(revisions.get(item, 1)) + 1
        previous_pending = checkpoint.get("pending_confirmation_stage")
        completed = set(checkpoint.get("completed_stages", []))
        confirmed = set(checkpoint.get("confirmed_stages", []))
        earlier_unconfirmed = next((item for item in STAGES[:stage_index + 1] if item in completed and item not in confirmed), None)
        missing_before = next((item for item in STAGES[:stage_index] if item not in completed), None)
        pending = previous_pending if previous_pending in STAGES and STAGES.index(previous_pending) < stage_index else earlier_unconfirmed
        metadata = load(project.get("metadata_json"), {})
        artifact_valid = Pipeline(app_settings, app_db)._stage_artifact_valid(stage, {"project": project, "metadata": metadata})
        if pending is None and missing_before is None and artifact_valid:
            pending = stage
        if pending:
            checkpoint["pending_confirmation_stage"] = pending
            checkpoint["pending_confirmation_revision"] = int(revisions.get(pending, 1))
        else:
            checkpoint.pop("pending_confirmation_stage", None)
            checkpoint.pop("pending_confirmation_revision", None)
        current_stage = pending or missing_before or stage
        progress = (STAGES.index(current_stage) + 1) / len(STAGES) if pending else 0
        status = "awaiting_confirmation" if pending else "created"
        message = f"{current_stage} 已更新，等待人工确认" if pending else f"{stage} 输入已更新，请继续处理本阶段"
        with app_db.connect() as conn:
            latest = conn.execute("SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
            if not latest:
                job_id = new_id("job")
                conn.execute(
                    "INSERT INTO jobs (id, project_id, kind, status, stage, progress, message, from_stage, force, checkpoint_json, created_at, updated_at) VALUES (?, ?, 'process', ?, ?, ?, ?, 'auto', 0, ?, ?, ?)",
                    (job_id, project_id, status, current_stage, progress, message, dump(checkpoint), now, now),
                )
            elif latest["status"] in {"queued", "running"}:
                _error(409, "PROJECT_BUSY", "项目任务已开始处理，不能修改阶段结果", "等当前阶段完成后再编辑")
            else:
                job_status = status if status == "awaiting_confirmation" else "superseded"
                conn.execute("UPDATE jobs SET checkpoint_json = ?, stage = ?, progress = ?, status = ?, message = ?, error_code = NULL, error_message = NULL, completed_at = NULL, updated_at = ? WHERE id = ?", (dump(checkpoint), current_stage, progress, job_status, message, now, latest["id"]))
            conn.execute("UPDATE projects SET checkpoint_json = ?, current_stage = ?, progress = ?, status = ?, status_message = ?, updated_at = ? WHERE id = ?", (dump(checkpoint), current_stage, progress, status, message, now, project_id))
        return bool(pending)

    health_cache: dict[str, Any] = {"at": 0.0, "value": None}
    health_condition = threading.Condition()
    health_refreshing = False

    def _health(force: bool = False) -> dict[str, Any]:
        nonlocal health_refreshing
        with health_condition:
            while health_refreshing:
                health_condition.wait()
                if health_cache["value"] is not None:
                    return health_cache["value"]
            age = time.monotonic() - health_cache["at"]
            if health_cache["value"] is not None and not force and age < 60:
                return health_cache["value"]
            health_refreshing = True
            try:
                faster = FasterWhisperAdapter(app_settings).status()
                omni = OmniVoiceAdapter(app_settings).status()
                separation = SeparationAdapter(app_settings).status()
                diarization = DiarizationAdapter(app_settings).status()
                rewrite = TextRewriterAdapter(app_settings).status()
                ffmpeg_ok = bool(app_settings.ffmpeg and (Path(app_settings.ffmpeg).is_file() or shutil.which(app_settings.ffmpeg)))
                ffprobe_ok = bool(app_settings.ffprobe and (Path(app_settings.ffprobe).is_file() or shutil.which(app_settings.ffprobe)))
                services = {"faster_whisper": _service_payload(faster), "omnivoice": _service_payload(omni), "text_rewriter": _service_payload(rewrite), "bandit": _service_payload(separation), "diarization": _service_payload(diarization), "ffmpeg": {"status": "ok" if ffmpeg_ok else "missing", "detail": "FFmpeg 可用" if ffmpeg_ok else "找不到 FFmpeg"}, "ffprobe": {"status": "ok" if ffprobe_ok else "missing", "detail": "ffprobe 可用" if ffprobe_ok else "找不到 ffprobe"}}
                overall = "healthy" if all(item["status"] in {"ok", "configured"} for item in services.values()) else "degraded"
                if not ffmpeg_ok or not ffprobe_ok:
                    overall = "blocked"
                value = {"overall": overall, "app": {"name": "儿童视频分级配音工具", "application_id": "child-video-dubbing", "version": app.version, "database": "ok", "ffmpeg": "ok" if ffmpeg_ok else "missing", "ffprobe": "ok" if ffprobe_ok else "missing"}, "services": services, "capabilities": {"real_transcription": faster.status == "ok", "real_voice_generation": omni.status == "ok", "real_separation": separation.status == "configured", "real_diarization": diarization.status == "configured", "real_text_rewrite": rewrite.status == "configured", "demo_mode": False}, "checked_at": now_iso(), "message": "本地后端已连接，但部分真实能力尚未可用" if overall != "healthy" else "本地后端与依赖已连接"}
                health_cache.update({"at": time.monotonic(), "value": value})
                return value
            finally:
                health_refreshing = False
                health_condition.notify_all()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return _health()

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        levels = {key: {"rule": rule, "default_speed": speed} for key, rule, speed in [("L1", LEVEL_RULES["L1"], 0.82), ("L2", LEVEL_RULES["L2"], 0.86), ("L3", LEVEL_RULES["L3"], 0.90), ("L4", LEVEL_RULES["L4"], 0.94), ("L5", LEVEL_RULES["L5"], 1.0)]}
        return {"config": app_settings.safe(), "levels": levels, "speed": {"min": 0.55, "max": 1.15, "default": 0.82}, "secrets": {"text_api_key_configured": bool(app_settings.text_api_key), "exposed": False}}

    @app.post("/api/settings/recheck")
    def recheck_settings() -> dict[str, Any]:
        return _health(True)

    @app.get("/api/series")
    def list_series() -> list[dict[str, Any]]:
        return [series_row(row) for row in app_db.fetchall("SELECT * FROM series ORDER BY updated_at DESC")]

    @app.post("/api/series", status_code=201)
    def create_series(payload: SeriesCreate) -> dict[str, Any]:
        series_id, now = new_id("series"), now_iso()
        app_db.insert("series", {"id": series_id, "name": payload.name, "source_language": payload.source_language, "target_language": payload.target_language, "level": payload.level, "speed": payload.speed, "status": "ready", "glossary_json": "{}", "created_at": now, "updated_at": now})
        return series_row(app_db.fetchone("SELECT * FROM series WHERE id = ?", (series_id,)) or {})

    @app.get("/api/series/{series_id}")
    def get_series(series_id: str) -> dict[str, Any]:
        row = app_db.fetchone("SELECT * FROM series WHERE id = ?", (series_id,))
        if not row:
            _error(404, "SERIES_NOT_FOUND", "系列不存在")
        value = series_row(row)
        value["projects"] = [project_row(item) for item in app_db.fetchall("SELECT * FROM projects WHERE series_id = ? ORDER BY updated_at DESC", (series_id,))]
        value["characters"] = [character_row(item) for item in app_db.fetchall("SELECT * FROM characters WHERE series_id = ? ORDER BY created_at", (series_id,))]
        return value

    @app.get("/api/series/{series_id}/characters")
    def list_characters(series_id: str) -> list[dict[str, Any]]:
        if not app_db.fetchone("SELECT id FROM series WHERE id = ?", (series_id,)):
            _error(404, "SERIES_NOT_FOUND", "系列不存在")
        return [character_row(row) for row in app_db.fetchall("SELECT * FROM characters WHERE series_id = ? ORDER BY created_at", (series_id,))]

    @app.patch("/api/series/{series_id}/characters/{character_id}")
    def patch_character(series_id: str, character_id: str, payload: CharacterPatch) -> dict[str, Any]:
        row = app_db.fetchone("SELECT * FROM characters WHERE id = ? AND series_id = ?", (character_id, series_id))
        if not row:
            _error(404, "CHARACTER_NOT_FOUND", "角色不存在")
        values = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None}
        if values:
            projects = app_db.fetchall("SELECT * FROM projects WHERE series_id = ?", (series_id,))
            for project in projects:
                ensure_project_idle(project)
            values["updated_at"] = now_iso()
            app_db.update("characters", values, "id = ?", (character_id,))
            updated_character = app_db.fetchone("SELECT * FROM characters WHERE id = ?", (character_id,)) or row
            for project in projects:
                app_db.execute("UPDATE segments SET speaker_name = ?, voice_profile = ?, audio_path = NULL, audio_sha256 = NULL, status = CASE WHEN kind = 'dialogue' THEN 'rewritten' ELSE status END, duration_delta = NULL, updated_at = ? WHERE project_id = ? AND speaker_key = ?", (updated_character.get("name"), updated_character.get("voice_profile"), now_iso(), project["id"], row["speaker_key"]))
                Pipeline(app_settings, app_db).invalidate(project["id"], "synthesize")
                reopen_stage_for_review(project["id"], "characters", "角色音色映射已修改")
        return character_row(app_db.fetchone("SELECT * FROM characters WHERE id = ?", (character_id,)) or row)

    async def _read_project_request(request: Request) -> tuple[str, str | None, str | None, bytes | None]:
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            form = await request.form()
            upload = form.get("file")
            content = await upload.read() if upload is not None and hasattr(upload, "read") else None
            filename = getattr(upload, "filename", None) if upload is not None else None
            return str(form.get("series_id") or ""), str(form.get("title") or "") or None, filename, content
        try:
            body = await request.json()
        except Exception:
            body = {}
        return str(body.get("series_id") or ""), body.get("title"), body.get("source_path"), None

    async def _create_project(series_id: str, title: str | None, source_path: str | None, upload_name: str | None, upload_bytes: bytes | None) -> dict[str, Any]:
        if not app_db.fetchone("SELECT id FROM series WHERE id = ?", (series_id,)):
            _error(404, "SERIES_NOT_FOUND", "系列不存在")
        project_id = new_id("project")
        work_dir = (app_settings.work_dir / "projects" / project_id).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        if upload_bytes is not None:
            source = work_dir / "source" / Path(upload_name or "source.mp4").name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(upload_bytes)
        elif source_path:
            source = Path(source_path).expanduser().resolve()
            if not source.is_file():
                _error(409, "SOURCE_NOT_FOUND", "源视频不存在或当前会话无法访问", "恢复源路径或选择可访问的文件")
        else:
            _error(422, "SOURCE_REQUIRED", "必须提供 source_path 或视频文件")
        info = probe_media(app_settings.ffprobe, source)
        if info.get("video_streams", 0) < 1 or not info.get("duration"):
            _error(409, "SOURCE_INVALID", "文件没有可用视频流或时长")
        now = now_iso()
        title_value = (title or source.stem).strip() or source.stem
        app_db.insert("projects", {"id": project_id, "series_id": series_id, "title": title_value, "source_path": str(source), "source_sha256": sha256_file(source), "duration": float(info["duration"]), "status": "created", "current_stage": "probe", "progress": 0, "status_message": "已导入并完成媒体探测", "work_dir": str(work_dir), "metadata_json": dump({"probe": info}), "checkpoint_json": "{}", "created_at": now, "updated_at": now})
        return project_row(app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,)) or {})

    @app.post("/api/projects", status_code=201)
    async def import_project(request: Request) -> dict[str, Any]:
        series_id, title, name_or_source, content = await _read_project_request(request)
        return await _create_project(series_id, title, None if content is not None else name_or_source, name_or_source if content is not None else None, content)

    @app.post("/api/series/{series_id}/projects", status_code=201)
    async def import_project_by_series(series_id: str, payload: ProjectPathCreate) -> dict[str, Any]:
        return await _create_project(series_id, payload.title, payload.source_path, None, None)

    @app.get("/api/projects")
    def list_projects(series_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if series_id:
            clauses.append("series_id = ?")
            params.append(series_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return [project_row(row) for row in app_db.fetchall(f"SELECT * FROM projects{where} ORDER BY updated_at DESC", params)]

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        row = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not row:
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
        return project_row(row, full=True)

    def enqueue_for_project(project_id: str, payload: JobCreate) -> dict[str, Any]:
        if not app_db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,)):
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
        if payload.kind not in {"process", "segment"}:
            _error(422, "JOB_KIND_INVALID", "不支持的任务类型")
        if payload.from_stage != "auto" and payload.from_stage not in STAGES:
            _error(422, "STAGE_INVALID", "from_stage 必须是 auto 或有效处理阶段")
        try:
            return job_row(runtime.queue.enqueue(project_id, kind=payload.kind, from_stage=payload.from_stage, force=payload.force)) or {}
        except QueueConflict as exc:
            _error(409, exc.code, exc.message, "先处理当前阶段审核卡，或明确选择重新处理当前阶段")

    @app.post("/api/projects/{project_id}/jobs", status_code=202)
    def enqueue_project(project_id: str, payload: JobCreate) -> dict[str, Any]:
        return enqueue_for_project(project_id, payload)

    @app.post("/api/jobs", status_code=202)
    def enqueue_compat(payload: JobCreate) -> dict[str, Any]:
        if not payload.project_id:
            _error(422, "PROJECT_REQUIRED", "请求缺少 project_id")
        return enqueue_for_project(payload.project_id, payload)

    @app.get("/api/jobs")
    def list_jobs(status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs" + (" WHERE status = ?" if status else "") + " ORDER BY created_at DESC"
        result = []
        for row in app_db.fetchall(query, (status,) if status else ()):
            value = job_row(row) or {}
            project = app_db.fetchone("SELECT title FROM projects WHERE id = ?", (row["project_id"],))
            value["project_title"] = project["title"] if project else None
            result.append(value)
        return result

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        row = app_db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not row:
            _error(404, "JOB_NOT_FOUND", "任务不存在")
        return job_row(row) or {}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        row = runtime.queue.cancel(job_id)
        if not row:
            _error(404, "JOB_NOT_FOUND", "任务不存在")
        return job_row(row) or {}

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def retry_job(job_id: str, request: Request) -> dict[str, Any]:
        from_stage = "auto"
        if "application/json" in request.headers.get("content-type", ""):
            try:
                from_stage = (await request.json()).get("from_stage") or "auto"
            except Exception:
                from_stage = "auto"
        if from_stage != "auto" and from_stage not in STAGES:
            _error(422, "STAGE_INVALID", "重试阶段无效")
        try:
            row = runtime.queue.retry(job_id, from_stage=from_stage)
        except QueueConflict as exc:
            _error(409, exc.code, exc.message, "刷新项目后选择当前待确认阶段")
        if not row:
            _error(404, "JOB_NOT_FOUND", "任务不存在或项目已删除")
        return {"job_id": row["id"], "status": row.get("status"), "stage": row.get("stage"), "job": job_row(row)}

    @app.post("/api/projects/{project_id}/stages/{stage}/confirm")
    def confirm_stage(project_id: str, stage: str, payload: StageConfirm) -> dict[str, Any]:
        if stage not in STAGES:
            _error(422, "STAGE_INVALID", "确认阶段无效")
        with runtime.queue._lock:
            project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
            if not project:
                _error(404, "PROJECT_NOT_FOUND", "项目不存在")
            checkpoint = load(project.get("checkpoint_json"), {})
            if not isinstance(checkpoint, dict):
                checkpoint = {}
            pending = checkpoint.get("pending_confirmation_stage")
            revision = int(checkpoint.get("stage_revisions", {}).get(stage, 1))
            if pending != stage:
                already = next((record for record in reversed(checkpoint.get("confirmation_history", [])) if record.get("stage") == stage and int(record.get("revision", 0)) == payload.revision and record.get("valid", True)), None)
                if already:
                    return {"status": project.get("status"), "stage": stage, "idempotent": True, "next_stage": checkpoint.get("pending_confirmation_stage")}
                _error(409, "STAGE_NOT_PENDING", "该阶段当前不在待确认状态", "刷新项目并确认界面显示的当前阶段")
            if payload.revision != revision or int(checkpoint.get("pending_confirmation_revision", revision)) != revision:
                _error(409, "STAGE_REVISION_CONFLICT", "阶段结果已变化，当前页面内容已过期", "刷新阶段结果后重新检查")
            confirmed_before = set(checkpoint.get("confirmed_stages", []))
            if any(item not in confirmed_before for item in STAGES[:STAGES.index(stage)]):
                _error(409, "PRIOR_STAGE_CONFIRMATION_REQUIRED", "前序阶段尚未逐项确认，不能越级确认", "返回第一个待确认阶段")
            metadata = load(project.get("metadata_json"), {})
            if not Pipeline(app_settings, app_db)._stage_artifact_valid(stage, {"project": project, "metadata": metadata}):
                _error(409, "STAGE_ARTIFACT_INVALID", "本阶段产物缺失或校验未通过，不能确认", "重新处理本阶段并检查异常")
            if stage == "export":
                source = Path(project["source_path"])
                output = Path(str(project.get("output_path") or ""))
                if not source.is_file() or sha256_file(source) != project.get("source_sha256"):
                    _error(409, "SOURCE_CHANGED", "源视频与处理时的文件哈希不一致", "重新导入源视频后再处理")
                actual_sha = sha256_file(output) if output.is_file() else ""
                if not actual_sha or actual_sha != project.get("output_sha256") or payload.artifact_sha256 != actual_sha:
                    _error(409, "OUTPUT_REVISION_CONFLICT", "候选成片已变化或页面预览版本过期", "重新加载候选成片并再次检查")
            with app_db.connect() as conn:
                current = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                if not current:
                    _error(404, "PROJECT_NOT_FOUND", "项目不存在")
                current_project = dict(current)
                current_checkpoint = load(current_project.get("checkpoint_json"), {})
                if current_checkpoint.get("pending_confirmation_stage") != stage or int(current_checkpoint.get("stage_revisions", {}).get(stage, 1)) != payload.revision:
                    _error(409, "STAGE_CONFIRMATION_CONFLICT", "待确认阶段已被另一项操作修改", "刷新项目状态后重试")
                if any(item not in set(current_checkpoint.get("confirmed_stages", [])) for item in STAGES[:STAGES.index(stage)]):
                    _error(409, "PRIOR_STAGE_CONFIRMATION_REQUIRED", "前序阶段尚未逐项确认，不能越级确认", "返回第一个待确认阶段")
                anomaly_rows = [dict(row) for row in conn.execute("SELECT id, kind, details_json, blocking, severity FROM anomalies WHERE project_id = ? AND resolved = 0", (project_id,)).fetchall()]
                stage_index = STAGES.index(stage)
                open_blockers = [row for row in anomaly_rows if (row["blocking"] or row["severity"] == "blocking") and (anomaly_stage(row) is None or STAGES.index(anomaly_stage(row)) <= stage_index)]
                if open_blockers:
                    _error(409, "BLOCKING_ANOMALIES_OPEN", "仍有未解决的阻断异常，不能确认本阶段", "先重新处理所属阶段并复核异常")
                accepted_before = {
                    str(item)
                    for record in current_checkpoint.get("confirmation_history", [])
                    if record.get("valid", True) and record.get("stage") in STAGES and STAGES.index(record["stage"]) < stage_index
                    for item in record.get("accepted_warning_ids", [])
                }
                warnings = {
                    str(row["id"]) for row in anomaly_rows
                    if not row["blocking"] and row["severity"] != "blocking"
                    and (anomaly_stage(row) is None or anomaly_stage(row) == stage)
                    and str(row["id"]) not in accepted_before
                }
                accepted = {str(item) for item in payload.accepted_warning_ids}
                if warnings != accepted:
                    _error(409, "WARNING_ACKNOWLEDGEMENT_REQUIRED", "当前警告清单已变化或尚未全部确认", "查看本阶段警告后再确认")
                history = current_checkpoint.setdefault("confirmation_history", [])
                record = {"stage": stage, "revision": payload.revision, "valid": True, "confirmed_at": now_iso(), "accepted_warning_ids": sorted(accepted)}
                if stage == "export":
                    record["artifact_sha256"] = payload.artifact_sha256
                history.append(record)
                confirmed = set(current_checkpoint.get("confirmed_stages", []))
                confirmed.add(stage)
                current_checkpoint["confirmed_stages"] = [item for item in STAGES if item in confirmed]
                current_checkpoint.pop("pending_confirmation_stage", None)
                current_checkpoint.pop("pending_confirmation_revision", None)
                now = now_iso()
                encoded = dump(current_checkpoint)
                job = conn.execute("SELECT * FROM jobs WHERE project_id = ? AND status = 'awaiting_confirmation' ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
                if not job or job["stage"] != stage:
                    _error(409, "STAGE_JOB_NOT_WAITING", "待确认任务状态不一致，未执行推进", "刷新状态并检查后端日志")
                if stage == "export":
                    warning_count = sum(1 for row in anomaly_rows if not row["blocking"] and row["severity"] != "blocking")
                    status = "completed_with_warnings" if warning_count else "completed"
                    message = "用户已确认候选成片"
                    conn.execute("UPDATE projects SET checkpoint_json = ?, status = ?, current_stage = 'done', progress = 1, status_message = ?, updated_at = ? WHERE id = ?", (encoded, status, message, now, project_id))
                    conn.execute("UPDATE jobs SET checkpoint_json = ?, status = ?, stage = 'done', progress = 1, message = ?, completed_at = ?, updated_at = ? WHERE id = ?", (encoded, status, message, now, now, job["id"]))
                    next_stage = None
                else:
                    next_stage = STAGES[STAGES.index(stage) + 1]
                    progress = (STAGES.index(stage) + 1) / len(STAGES)
                    message = f"已确认 {stage}，等待继续 {next_stage}"
                    conn.execute("UPDATE projects SET checkpoint_json = ?, status = 'queued', current_stage = ?, progress = ?, status_message = ?, updated_at = ? WHERE id = ?", (encoded, next_stage, progress, message, now, project_id))
                    conn.execute("UPDATE jobs SET checkpoint_json = ?, kind = 'process', status = 'queued', stage = ?, from_stage = 'auto', progress = ?, message = ?, completed_at = NULL, updated_at = ? WHERE id = ?", (encoded, next_stage, progress, message, now, job["id"]))
        if next_stage:
            runtime.queue.notify()
        return {"status": status if stage == "export" else "queued", "stage": stage, "idempotent": False, "next_stage": next_stage}

    @app.get("/api/projects/{project_id}/segments")
    def list_segments(project_id: str) -> list[dict[str, Any]]:
        if not app_db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,)):
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
        return [segment_row(row) for row in app_db.fetchall("SELECT * FROM segments WHERE project_id = ? ORDER BY segment_index", (project_id,))]

    def invalidate_product_cache(project: dict[str, Any]) -> None:
        Pipeline(app_settings, app_db).invalidate(project["id"], "synthesize")

    @app.patch("/api/projects/{project_id}/segments/{segment_id}")
    def patch_segment(project_id: str, segment_id: str, payload: SegmentPatch) -> dict[str, Any]:
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = app_db.fetchone("SELECT * FROM segments WHERE id = ? AND project_id = ?", (segment_id, project_id))
        if not project or not row:
            _error(404, "SEGMENT_NOT_FOUND", "句级片段不存在")
        ensure_project_idle(project)
        provided = payload.model_dump(exclude_unset=True)
        if "target_text" in provided and not str(provided["target_text"] or "").strip():
            _error(422, "TARGET_TEXT_EMPTY", "目标台词不能为空")
        values: dict[str, Any] = {}
        if "target_text" in provided:
            values["target_text"] = str(provided["target_text"]).strip()
        if "speed" in provided:
            values["speed"] = provided["speed"]
        if "speaker_key" in provided:
            values["speaker_key"] = provided["speaker_key"]
            character = app_db.fetchone("SELECT name, voice_profile FROM characters WHERE series_id = ? AND speaker_key = ?", (project["series_id"], provided["speaker_key"]))
            values["speaker_name"] = character["name"] if character else None
            values["voice_profile"] = character["voice_profile"] if character else None
        if not values:
            return segment_row(row)
        values.update({"target_revision": int(row["target_revision"] or 1) + 1, "audio_path": None, "audio_sha256": None, "duration_delta": None, "status": "rewritten" if row.get("kind") == "dialogue" else row.get("status"), "error_message": None, "updated_at": now_iso()})
        app_db.update("segments", values, "id = ?", (segment_id,))
        affected = min((stage for field, stage in (("speaker_key", "characters"), ("target_text", "rewrite"), ("speed", "synthesize")) if field in provided), key=STAGES.index)
        Pipeline(app_settings, app_db).invalidate(project_id, "synthesize")
        if affected != "synthesize":
            reopen_stage_for_review(project_id, affected, "人工修改了本阶段结果")
        return segment_row(app_db.fetchone("SELECT * FROM segments WHERE id = ?", (segment_id,)) or row)

    @app.post("/api/projects/{project_id}/segments/{segment_id}/regenerate", status_code=202)
    def regenerate_segment(project_id: str, segment_id: str) -> dict[str, Any]:
        with runtime.queue._lock:
            project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
            segment = app_db.fetchone("SELECT * FROM segments WHERE id = ? AND project_id = ?", (segment_id, project_id))
            if not project or not segment:
                _error(404, "SEGMENT_NOT_FOUND", "句级片段不存在")
            ensure_project_idle(project)
            checkpoint = load(project.get("checkpoint_json"), {})
            pending = checkpoint.get("pending_confirmation_stage") if isinstance(checkpoint, dict) else None
            if project.get("status") not in {"completed", "completed_with_warnings", "awaiting_confirmation"} or (pending and pending != "synthesize"):
                _error(409, "SYNTHESIS_REVIEW_REQUIRED", "句级重新配音只允许在配音审核关或已完成项目中执行", "先完成当前阶段确认，或重新处理配音阶段")
            confirmed = set(checkpoint.get("confirmed_stages", [])) if isinstance(checkpoint, dict) else set()
            if any(stage not in confirmed for stage in STAGES[:STAGES.index("synthesize")]):
                _error(409, "PRIOR_STAGE_CONFIRMATION_REQUIRED", "前序阶段尚未逐项确认，不能单句重新配音", "先完成前序阶段复核")
            if segment.get("kind") != "dialogue" or not (segment.get("target_text") or "").strip():
                _error(409, "SEGMENT_NOT_SYNTHESIZABLE", "该片段没有可配音的目标对白")
            app_db.update("segments", {"audio_path": None, "audio_sha256": None, "status": "rewritten", "updated_at": now_iso()}, "id = ?", (segment_id,))
            Pipeline(app_settings, app_db).invalidate(project_id, "mix")
            reopen_stage_for_review(project_id, "synthesize", "句级音频将重新生成")
            row = runtime.queue.enqueue(project_id, kind="segment", checkpoint={"segment_id": segment_id})
            return {"job_id": row["id"], "status": row.get("status"), "stage": row.get("stage", "synthesize"), "message": "已加入句级真实配音队列；不会回用旧缓存"}

    @app.get("/api/projects/{project_id}/anomalies")
    def list_anomalies(project_id: str, include_resolved: bool = False) -> list[dict[str, Any]]:
        if not app_db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,)):
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
        where = "project_id = ?" if include_resolved else "project_id = ? AND resolved = 0"
        return [anomaly_row(row) for row in app_db.fetchall(f"SELECT * FROM anomalies WHERE {where} ORDER BY blocking DESC, created_at DESC", (project_id,))]

    @app.post("/api/projects/{project_id}/anomalies/{anomaly_id}/resolve")
    def resolve_anomaly(project_id: str, anomaly_id: str) -> dict[str, Any]:
        anomaly = app_db.fetchone("SELECT * FROM anomalies WHERE id = ? AND project_id = ?", (anomaly_id, project_id))
        if not anomaly:
            _error(404, "ANOMALY_NOT_FOUND", "异常不存在")
        if anomaly.get("blocking") or anomaly.get("severity") == "blocking":
            _error(409, "BLOCKING_ANOMALY_REQUIRES_STAGE_RETRY", "阻断异常必须由所属阶段重新处理并复核，不能手动清除标志解锁", "修复原因后重试异常所属阶段")
        app_db.update("anomalies", {"resolved": 1, "updated_at": now_iso()}, "id = ?", (anomaly_id,))
        return anomaly_row(app_db.fetchone("SELECT * FROM anomalies WHERE id = ?", (anomaly_id,)) or {})

    @app.post("/api/projects/{project_id}/separation", status_code=201)
    def import_separation(project_id: str, payload: SeparationImport) -> dict[str, Any]:
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
        ensure_project_idle(project)
        work_dir = Path(project["work_dir"]).resolve() / "separation-import"
        work_dir.mkdir(parents=True, exist_ok=True)
        copied: dict[str, str | None] = {}
        for key, raw in {"speech": payload.speech_path, "music": payload.music_path, "effects": payload.effects_path, "background": payload.background_path}.items():
            if not raw:
                copied[key] = None
                continue
            source = Path(raw).expanduser().resolve()
            if not source.is_file():
                _error(409, "SEPARATION_INPUT_NOT_FOUND", f"分离轨不存在：{key}", "确认本地绝对路径")
            destination = work_dir / f"{key}{source.suffix.lower()}"
            shutil.copy2(source, destination)
            copied[key] = str(destination)
        if not copied["background"] and not (copied["music"] and copied["effects"]):
            _error(409, "SEPARATION_BACKGROUND_MISSING", "必须提供 background 或 music+effects，不能用原混音冒充", "重新导入已分离轨")
        background = Path(copied["background"]) if copied["background"] else SeparationAdapter(app_settings)._mix_background(Path(copied["music"]), Path(copied["effects"]), work_dir / "background.wav")
        metadata = load(project.get("metadata_json"), {})
        metadata["separation"] = {"speech": copied["speech"], "music": copied["music"], "effects": copied["effects"], "background": str(background), "manifest": None, "song_intervals": payload.song_intervals, "song_intervals_override": payload.song_intervals, "imported": True}
        Pipeline(app_settings, app_db).invalidate(project_id, "transcribe")
        app_db.update("projects", {"metadata_json": dump(metadata), "updated_at": now_iso()}, "id = ?", (project_id,))
        reopen_stage_for_review(project_id, "separate", "对白/背景分离轨已导入或替换")
        return project_row(app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,)) or {})

    @app.put("/api/projects/{project_id}/song-intervals")
    def set_song_intervals(project_id: str, payload: SongIntervalsPatch) -> dict[str, Any]:
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
        ensure_project_idle(project)
        metadata = load(project.get("metadata_json"), {})
        separation = metadata.get("separation") or {}
        if not separation.get("speech") or not separation.get("background"):
            _error(409, "SEPARATION_REQUIRED", "请先完成真实对白与背景分离，再设置歌曲区间")
        intervals: list[dict[str, float]] = []
        duration = float(project.get("duration") or 0)
        for item in payload.intervals:
            start, end = float(item.get("start", math.nan)), float(item.get("end", math.nan))
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or (duration > 0 and end > duration):
                _error(422, "SONG_INTERVAL_INVALID", "歌曲区间必须是视频时长内有效的 start/end 时间")
            intervals.append({"start": start, "end": end})
        Pipeline(app_settings, app_db).invalidate(project_id, "transcribe")
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,)) or project
        metadata = load(project.get("metadata_json"), {})
        separation = metadata.get("separation") or {}
        separation["song_intervals_override"] = intervals
        metadata["separation"] = separation
        app_db.update("projects", {"metadata_json": dump(metadata), "updated_at": now_iso()}, "id = ?", (project_id,))
        return project_row(app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,)) or {})

    @app.get("/api/projects/{project_id}/preview")
    def preview(project_id: str) -> dict[str, Any]:
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
        source = Path(project["source_path"])
        output = Path(str(project["output_path"] or ""))
        subtitle = Path(project["work_dir"]) / "target.srt"
        warnings = [item["message"] for item in list_anomalies(project_id) if not item.get("blocking")]
        metadata = load(project.get("metadata_json"), {})
        separation = metadata.get("separation") or {}
        return {
            "source_url": f"/api/projects/{project_id}/media/source" if source.is_file() else None,
            "output_url": f"/api/projects/{project_id}/media/candidate" if project.get("output_path") and output.is_file() else None,
            "speech_url": f"/api/projects/{project_id}/media/stem/speech" if Path(str(separation.get("speech") or "")).is_file() else None,
            "background_url": f"/api/projects/{project_id}/media/stem/background" if Path(str(separation.get("background") or "")).is_file() else None,
            "music_url": f"/api/projects/{project_id}/media/stem/music" if Path(str(separation.get("music") or "")).is_file() else None,
            "effects_url": f"/api/projects/{project_id}/media/stem/effects" if Path(str(separation.get("effects") or "")).is_file() else None,
            "mix_url": f"/api/projects/{project_id}/media/mix" if Path(str(metadata.get("mixed") or "")).is_file() else None,
            "duration": project.get("duration"),
            "subtitle_url": f"/api/projects/{project_id}/media/subtitle" if subtitle.is_file() else None,
            "output_sha256": project.get("output_sha256"),
            "ready": bool(project.get("output_path") and output.is_file()),
            "warnings": warnings,
        }

    def confirmed_output_sha(project: dict[str, Any]) -> str | None:
        checkpoint = load(project.get("checkpoint_json"), {})
        if "export" not in checkpoint.get("confirmed_stages", []):
            return None
        confirmation = next((item for item in reversed(checkpoint.get("confirmation_history", [])) if item.get("stage") == "export" and item.get("valid", True)), None)
        output = Path(str(project.get("output_path") or ""))
        if not confirmation or not confirmation.get("artifact_sha256") or not output.is_file():
            return None
        actual_sha = sha256_file(output)
        if actual_sha != project.get("output_sha256") or actual_sha != confirmation["artifact_sha256"]:
            return None
        return actual_sha

    @app.post("/api/projects/{project_id}/export")
    def export_project(project_id: str, _payload: ExportRequest | None = None) -> dict[str, Any]:
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
        blocking = app_db.fetchone("SELECT COUNT(*) AS count FROM anomalies WHERE project_id = ? AND (blocking = 1 OR severity = 'blocking') AND resolved = 0", (project_id,))
        output = Path(str(project["output_path"] or ""))
        source = Path(project["source_path"])
        if int((blocking or {}).get("count") or 0) or not project.get("output_path") or not output.is_file():
            _error(409, "EXPORT_BLOCKED", "存在未解决阻断异常或真实成片尚未生成", "完成真实分离、转写、角色、改写、配音与混音后再导出", {"blocking_count": int((blocking or {}).get("count") or 0)})
        if not source.is_file() or sha256_file(source) != project["source_sha256"]:
            _error(409, "SOURCE_CHANGED", "源文件哈希已变化，拒绝导出旧缓存", "重新导入源视频并处理")
        checkpoint = load(project.get("checkpoint_json"), {})
        if "export" not in checkpoint.get("confirmed_stages", []):
            _error(409, "FINAL_CONFIRMATION_REQUIRED", "候选成片尚未通过最终人工验收", "先在项目页预览并确认当前输出版本")
        actual_sha = confirmed_output_sha(project)
        if not actual_sha:
            _error(409, "OUTPUT_REVISION_CONFLICT", "候选成片与最终确认时的文件哈希不一致", "重新加载候选成片并再次检查")
        return {"path": str(output), "output_path": str(output), "sha256": actual_sha, "output_url": f"/api/projects/{project_id}/media/output", "download_url": f"/api/projects/{project_id}/media/output", "status": project.get("status")}

    def safe_project_path(project: dict[str, Any], path: Path, *, source: bool = False) -> Path:
        path = path.resolve()
        work = Path(project["work_dir"]).resolve()
        if source and path == Path(project["source_path"]).resolve():
            return path
        try:
            path.relative_to(work)
        except ValueError:
            _error(404, "MEDIA_NOT_REGISTERED", "媒体不在当前项目已登记目录内")
        return path

    @app.get("/api/projects/{project_id}/media/{kind:path}")
    def media(project_id: str, kind: str):
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
        if kind == "source":
            path = safe_project_path(project, Path(project["source_path"]), source=True)
        elif kind in {"candidate", "output"}:
            if not project.get("output_path"):
                _error(409, "OUTPUT_NOT_READY", "候选成片尚未生成")
            if kind == "output":
                checkpoint = load(project.get("checkpoint_json"), {})
                if "export" not in checkpoint.get("confirmed_stages", []):
                    _error(409, "FINAL_CONFIRMATION_REQUIRED", "候选成片尚未通过最终人工验收")
                if not confirmed_output_sha(project):
                    _error(409, "OUTPUT_REVISION_CONFLICT", "候选成片与最终确认时的文件哈希不一致")
            path = safe_project_path(project, Path(project["output_path"]))
        elif kind.startswith("stem/"):
            stem_name = kind.split("/", 1)[1]
            if stem_name not in {"speech", "background", "music", "effects"}:
                _error(404, "MEDIA_KIND_UNKNOWN", "不支持的音轨类型")
            separation = load(project.get("metadata_json"), {}).get("separation") or {}
            stem_path = separation.get(stem_name)
            if not stem_path:
                _error(404, "STEM_NOT_AVAILABLE", f"本项目没有可试听的{stem_name}轨")
            path = safe_project_path(project, Path(str(stem_path)))
        elif kind == "mix":
            mixed = load(project.get("metadata_json"), {}).get("mixed")
            if not mixed:
                _error(404, "MIX_NOT_AVAILABLE", "混音候选尚未生成")
            path = safe_project_path(project, Path(str(mixed)))
        elif kind == "subtitle":
            path = safe_project_path(project, Path(project["work_dir"]) / "target.srt")
        elif kind.startswith("segment/"):
            row = app_db.fetchone("SELECT audio_path FROM segments WHERE id = ? AND project_id = ?", (kind.split("/", 1)[1], project_id))
            if not row or not row.get("audio_path"):
                _error(404, "SEGMENT_AUDIO_NOT_READY", "句级音频尚未生成")
            path = safe_project_path(project, Path(row["audio_path"]))
        else:
            _error(404, "MEDIA_KIND_UNKNOWN", "不支持的媒体类型")
        if not path.is_file():
            _error(404, "MEDIA_NOT_FOUND", "媒体文件不存在")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream", filename=path.name)

    @app.get("/api/series/{series_id}/characters/{character_id}/sample/{which}")
    def character_sample(series_id: str, character_id: str, which: str):
        row = app_db.fetchone("SELECT * FROM characters WHERE id = ? AND series_id = ?", (character_id, series_id))
        if not row or which not in {"main", "backup"}:
            _error(404, "SAMPLE_NOT_FOUND", "参考音频不存在")
        path = Path(str(row.get("main_sample_path") if which == "main" else row.get("backup_sample_path") or ""))
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (row.get("source_project_id"),))
        if not project:
            _error(404, "SAMPLE_NOT_FOUND", "参考音频不存在")
        path = safe_project_path(project, path)
        if not path.is_file():
            _error(404, "SAMPLE_NOT_FOUND", "参考音频不存在")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "audio/wav", filename=path.name)

    frontend_dist = app_settings.root_dir / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="frontend-assets")

        @app.get("/", include_in_schema=False)
        def frontend_index():
            return FileResponse(frontend_dist / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        def frontend_missing():
            return {"name": "童声配音台", "message": "后端已启动；请构建 frontend/dist 或运行 frontend 的 Vite 开发服务器", "docs": "/docs"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8787, reload=False)
