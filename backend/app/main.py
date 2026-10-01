from __future__ import annotations

import mimetypes
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
from .pipeline import Pipeline, STAGES
from .queue import JobQueue


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

    app = FastAPI(title="童声配音台", version="0.1.0", lifespan=lifespan)
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
        return value

    def character_row(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "series_id": row["series_id"], "speaker_key": row["speaker_key"], "name": row["name"], "main_sample_url": f"/api/series/{row['series_id']}/characters/{row['id']}/sample/main" if row.get("main_sample_path") else None, "backup_sample_url": f"/api/series/{row['series_id']}/characters/{row['id']}/sample/backup" if row.get("backup_sample_path") else None, "voice_profile": row.get("voice_profile"), "status": row.get("status"), "source_project_id": row.get("source_project_id"), "embedding_verified": bool(load(row.get("metadata_json"), {}).get("embedding_verified"))}

    def project_row(row: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
        value = _json_row(row) or {}
        series = app_db.fetchone("SELECT * FROM series WHERE id = ?", (row["series_id"],))
        if series:
            value.update({"series_name": series["name"], "target_language": series["target_language"], "level": series["level"], "speed": series["speed"], "source_language": series["source_language"]})
        value.update({"source_filename": Path(row["source_path"]).name, "stage": row.get("current_stage")})
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
            values["updated_at"] = now_iso()
            app_db.update("characters", values, "id = ?", (character_id,))
            for project in app_db.fetchall("SELECT * FROM projects WHERE series_id = ?", (series_id,)):
                app_db.execute("UPDATE segments SET audio_path = NULL, audio_sha256 = NULL, status = CASE WHEN kind = 'dialogue' THEN 'rewritten' ELSE status END, duration_delta = NULL, updated_at = ? WHERE project_id = ? AND speaker_key = ?", (now_iso(), project["id"], row["speaker_key"]))
                app_db.update("projects", {"output_path": None, "output_sha256": None, "status": "created", "status_message": "角色映射已修改，相关配音缓存已失效", "updated_at": now_iso()}, "id = ?", (project["id"],))
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
        return job_row(runtime.queue.enqueue(project_id, kind=payload.kind, from_stage=payload.from_stage, force=payload.force)) or {}

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
        row = runtime.queue.retry(job_id, from_stage=from_stage)
        if not row:
            _error(404, "JOB_NOT_FOUND", "任务不存在或项目已删除")
        return {"job_id": row["id"], "status": row.get("status"), "stage": row.get("stage"), "job": job_row(row)}

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
        invalidate_product_cache(project)
        return segment_row(app_db.fetchone("SELECT * FROM segments WHERE id = ?", (segment_id,)) or row)

    @app.post("/api/projects/{project_id}/segments/{segment_id}/regenerate", status_code=202)
    def regenerate_segment(project_id: str, segment_id: str) -> dict[str, Any]:
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        segment = app_db.fetchone("SELECT * FROM segments WHERE id = ? AND project_id = ?", (segment_id, project_id))
        if not project or not segment:
            _error(404, "SEGMENT_NOT_FOUND", "句级片段不存在")
        if segment.get("kind") != "dialogue" or not (segment.get("target_text") or "").strip():
            _error(409, "SEGMENT_NOT_SYNTHESIZABLE", "该片段没有可配音的目标对白")
        app_db.update("segments", {"audio_path": None, "audio_sha256": None, "status": "rewritten", "updated_at": now_iso()}, "id = ?", (segment_id,))
        invalidate_product_cache(project)
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
        if not app_db.fetchone("SELECT id FROM anomalies WHERE id = ? AND project_id = ?", (anomaly_id, project_id)):
            _error(404, "ANOMALY_NOT_FOUND", "异常不存在")
        app_db.update("anomalies", {"resolved": 1, "updated_at": now_iso()}, "id = ?", (anomaly_id,))
        return anomaly_row(app_db.fetchone("SELECT * FROM anomalies WHERE id = ?", (anomaly_id,)) or {})

    @app.post("/api/projects/{project_id}/separation", status_code=201)
    def import_separation(project_id: str, payload: SeparationImport) -> dict[str, Any]:
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
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
        metadata["separation"] = {"speech": copied["speech"], "music": copied["music"], "effects": copied["effects"], "background": str(background), "manifest": None, "song_intervals": payload.song_intervals, "imported": True}
        Pipeline(app_settings, app_db).invalidate(project_id, "transcribe")
        app_db.update("projects", {"metadata_json": dump(metadata), "checkpoint_json": dump({"completed_stages": ["probe", "separate"]}), "current_stage": "transcribe", "status": "created", "status_message": "已导入真实分离轨；等待句级转写", "updated_at": now_iso()}, "id = ?", (project_id,))
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
        return {"source_url": f"/api/projects/{project_id}/media/source" if source.is_file() else None, "output_url": f"/api/projects/{project_id}/media/output" if project.get("output_path") and output.is_file() else None, "duration": project.get("duration"), "subtitle_url": f"/api/projects/{project_id}/media/subtitle" if subtitle.is_file() else None, "ready": bool(project.get("output_path") and output.is_file()), "warnings": warnings}

    @app.post("/api/projects/{project_id}/export")
    def export_project(project_id: str, _payload: ExportRequest | None = None) -> dict[str, Any]:
        project = app_db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            _error(404, "PROJECT_NOT_FOUND", "项目不存在")
        blocking = app_db.fetchone("SELECT COUNT(*) AS count FROM anomalies WHERE project_id = ? AND blocking = 1 AND resolved = 0", (project_id,))
        output = Path(str(project["output_path"] or ""))
        source = Path(project["source_path"])
        if int((blocking or {}).get("count") or 0) or not project.get("output_path") or not output.is_file():
            _error(409, "EXPORT_BLOCKED", "存在未解决阻断异常或真实成片尚未生成", "完成真实分离、转写、角色、改写、配音与混音后再导出", {"blocking_count": int((blocking or {}).get("count") or 0)})
        if not source.is_file() or sha256_file(source) != project["source_sha256"]:
            _error(409, "SOURCE_CHANGED", "源文件哈希已变化，拒绝导出旧缓存", "重新导入源视频并处理")
        return {"path": str(output), "output_path": str(output), "sha256": project.get("output_sha256") or sha256_file(output), "output_url": f"/api/projects/{project_id}/media/output", "download_url": f"/api/projects/{project_id}/media/output", "status": project.get("status")}

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
        elif kind == "output":
            if not project.get("output_path"):
                _error(409, "OUTPUT_NOT_READY", "候选成片尚未生成")
            path = safe_project_path(project, Path(project["output_path"]))
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
