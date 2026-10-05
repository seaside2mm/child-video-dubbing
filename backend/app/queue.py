from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from .config import Settings
from .db import Database, dump, load, new_id, now_iso
from .pipeline import Pipeline


class QueueConflict(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class JobQueue:
    """Small persistent single-worker queue; SQLite is the source of truth."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._lease = None

    def _acquire_lease(self) -> None:
        # OS locks survive neither crashes nor process exits; no stale PID cleanup.
        path = self.settings.db_path.resolve().with_suffix(".worker.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("该项目数据库已有处理 worker；拒绝重复启动或恢复运行中的任务") from exc
        self._lease = handle

    def _release_lease(self) -> None:
        handle, self._lease = self._lease, None
        if handle is not None:
            handle.close()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._acquire_lease()
            try:
                self.db.init()
                self.db.execute(
                    "UPDATE jobs SET status = 'queued', message = '后端重启后恢复排队', updated_at = ? WHERE status = 'running'",
                    (now_iso(),),
                )
                self.db.execute(
                    "UPDATE projects SET status = 'queued', status_message = '后端重启后恢复排队', updated_at = ? WHERE id IN (SELECT project_id FROM jobs WHERE status = 'queued')",
                    (now_iso(),),
                )
                self._stop.clear()
                self._thread = threading.Thread(target=self._run_with_lease, name="dubbing-worker", daemon=True)
                self._thread.start()
            except BaseException:
                self._release_lease()
                raise

    def _run_with_lease(self) -> None:
        try:
            self._run()
        finally:
            self._release_lease()

    def stop(self) -> None:
        self._stop.set()
        for event in self._cancel.values():
            event.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        if not thread or not thread.is_alive():
            self._thread = None

    def notify(self) -> None:
        self._wake.set()

    def enqueue(self, project_id: str, *, kind: str = "process", from_stage: str | None = None, force: bool = False, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            project = self.db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
            if not project:
                raise QueueConflict("PROJECT_NOT_FOUND", "项目不存在")
            project_checkpoint = load(project.get("checkpoint_json"), {})
            pending = project_checkpoint.get("pending_confirmation_stage")
            payload = dict(checkpoint or {})
            if pending:
                segment_id = payload.get("segment_id")
                if kind != "segment" or pending != "synthesize" or not segment_id:
                    raise QueueConflict("STAGE_CONFIRMATION_REQUIRED", "当前阶段等待人工确认；不能通过普通任务入口继续")
                segment = self.db.fetchone("SELECT id FROM segments WHERE id = ? AND project_id = ?", (segment_id, project_id))
                waiting = self.db.fetchone("SELECT * FROM jobs WHERE project_id = ? AND status = 'awaiting_confirmation' ORDER BY created_at DESC LIMIT 1", (project_id,))
                if not segment or not waiting or waiting.get("stage") != pending:
                    raise QueueConflict("STAGE_CONFIRMATION_REQUIRED", "句级重配音仅允许替换当前待确认的配音阶段")
                job_id = new_id("job")
                now = now_iso()
                with self.db.connect() as conn:
                    current = conn.execute("SELECT checkpoint_json FROM projects WHERE id = ?", (project_id,)).fetchone()
                    latest = load(current["checkpoint_json"], {}) if current else {}
                    if latest.get("pending_confirmation_stage") != pending:
                        raise QueueConflict("STAGE_CONFIRMATION_CONFLICT", "待确认阶段已变化，请刷新后重试")
                    current_waiting = conn.execute("SELECT * FROM jobs WHERE id = ? AND status = 'awaiting_confirmation'", (waiting["id"],)).fetchone()
                    if not current_waiting or current_waiting["stage"] != pending:
                        raise QueueConflict("STAGE_CONFIRMATION_CONFLICT", "待确认任务已变化，请刷新后重试")
                    latest.pop("pending_confirmation_stage", None)
                    latest.pop("pending_confirmation_revision", None)
                    conn.execute("UPDATE projects SET checkpoint_json = ?, status = 'queued', current_stage = ?, status_message = ?, updated_at = ? WHERE id = ?", (dump(latest), pending, "正在重新生成句级配音", now, project_id))
                    conn.execute("UPDATE jobs SET status = 'superseded', message = ?, completed_at = ?, updated_at = ? WHERE id = ?", ("已由句级重配音任务替代", now, now, waiting["id"]))
                    conn.execute(
                        "INSERT INTO jobs (id, project_id, kind, status, stage, progress, message, from_stage, force, checkpoint_json, created_at, updated_at) VALUES (?, ?, 'segment', 'queued', ?, 0, ?, ?, 0, ?, ?, ?)",
                        (job_id, project_id, pending, "已加入本地句级配音队列", pending, dump(payload), now, now),
                    )
                self.notify()
                return self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,)) or {"id": job_id, "project_id": project_id, "status": "queued", "stage": pending}
            active = self.db.fetchone(
                "SELECT * FROM jobs WHERE project_id = ? AND status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            )
            if active and kind == "process":
                return active
            if active and kind == "segment":
                raise QueueConflict("PROJECT_BUSY", "该项目已有处理中任务，不能重复提交句级配音")
            job_id = new_id("job")
            now = now_iso()
            self.db.insert(
                "jobs",
                {
                    "id": job_id,
                    "project_id": project_id,
                    "kind": kind,
                    "status": "queued",
                    "stage": from_stage if from_stage and from_stage != "auto" else "probe",
                    "progress": 0,
                    "message": "已加入本地单 worker 队列",
                    "from_stage": from_stage,
                    "force": int(force),
                    "checkpoint_json": dump(payload),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            self.db.update("projects", {"status": "queued", "status_message": "已加入本地任务队列", "updated_at": now}, "id = ?", (project_id,))
        self.notify()
        return self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,)) or {"id": job_id, "project_id": project_id, "status": "queued", "stage": "probe"}

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        job = self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job:
            return None
        if job["status"] == "queued":
            self.db.update("jobs", {"status": "cancelled", "message": "任务已取消", "completed_at": now_iso(), "updated_at": now_iso()}, "id = ?", (job_id,))
            if not self.db.fetchone("SELECT 1 FROM jobs WHERE project_id = ? AND id != ? AND status IN ('queued', 'running')", (job["project_id"], job_id)):
                self.db.update("projects", {"status": "paused", "status_message": "任务已取消；已保留已有缓存", "updated_at": now_iso()}, "id = ?", (job["project_id"],))
        elif job["status"] == "running":
            self._cancel.setdefault(job_id, threading.Event()).set()
            self.db.update("jobs", {"message": "正在请求取消", "updated_at": now_iso()}, "id = ?", (job_id,))
        return self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def retry(self, job_id: str, *, from_stage: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            return self._retry_locked(job_id, from_stage=from_stage)

    def _retry_locked(self, job_id: str, *, from_stage: str | None = None) -> dict[str, Any] | None:
        old = self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not old:
            return None
        project = self.db.fetchone("SELECT * FROM projects WHERE id = ?", (old["project_id"],))
        if not project:
            return None
        stage = from_stage or old.get("stage") or "auto"
        if stage == "done":
            stage = "auto"
        checkpoint = load(project.get("checkpoint_json"), {})
        pending = checkpoint.get("pending_confirmation_stage")
        if pending:
            if stage == "auto":
                stage = pending
            if old.get("status") != "awaiting_confirmation" or stage != pending:
                raise QueueConflict("STAGE_CONFIRMATION_REQUIRED", "只能重新处理当前待确认阶段，不能跳过或回退到其他阶段")
            job_id_next = new_id("job")
            now = now_iso()
            checkpoint.pop("pending_confirmation_stage", None)
            checkpoint.pop("pending_confirmation_revision", None)
            with self.db.connect() as conn:
                current = conn.execute("SELECT checkpoint_json FROM projects WHERE id = ?", (project["id"],)).fetchone()
                latest = load(current["checkpoint_json"], {}) if current else {}
                if latest.get("pending_confirmation_stage") != pending:
                    raise QueueConflict("STAGE_CONFIRMATION_CONFLICT", "待确认阶段已变化，请刷新后重试")
                conn.execute("UPDATE projects SET checkpoint_json = ?, status = 'queued', current_stage = ?, status_message = ?, updated_at = ? WHERE id = ?", (dump(checkpoint), stage, "正在重新处理当前阶段", now, project["id"]))
                conn.execute("UPDATE jobs SET status = 'superseded', message = ?, completed_at = ?, updated_at = ? WHERE id = ?", ("已由重新处理当前阶段的新任务替代", now, now, old["id"]))
                conn.execute(
                    "INSERT INTO jobs (id, project_id, kind, status, stage, progress, message, from_stage, force, checkpoint_json, created_at, updated_at) VALUES (?, ?, 'process', 'queued', ?, 0, ?, ?, 0, '{}', ?, ?)",
                    (job_id_next, project["id"], stage, "已加入本地单 worker 队列", stage, now, now),
                )
            self.notify()
            return self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id_next,))
        if old.get("status") == "superseded":
            raise QueueConflict("JOB_SUPERSEDED", "该任务已被新任务替代，不能再次重试")
        if stage != "auto":
            self.db.update("projects", {"status": "created", "status_message": "已准备从指定阶段重试", "updated_at": now_iso()}, "id = ?", (project["id"],))
        return self.enqueue(project["id"], from_stage=stage, force=False)

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self._claim_next()
            if not job:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            cancel_event = self._cancel.setdefault(job["id"], threading.Event())
            pipeline = Pipeline(self.settings, self.db, cancelled=cancel_event.is_set)
            try:
                pipeline.run(job["id"])
            finally:
                self._cancel.pop(job["id"], None)

    def _claim_next(self) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1").fetchone()
            if not row:
                return None
            now = now_iso()
            updated = conn.execute(
                "UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?, message = 'worker 已开始执行' WHERE id = ? AND status = 'queued'",
                (now, now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())
