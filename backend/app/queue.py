from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from .config import Settings
from .db import Database, dump, new_id, now_iso
from .pipeline import Pipeline


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
            active = self.db.fetchone(
                "SELECT * FROM jobs WHERE project_id = ? AND status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            )
            if active and kind == "process":
                return active
            job_id = new_id("job")
            now = now_iso()
            payload = dict(checkpoint or {})
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
        old = self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not old:
            return None
        project = self.db.fetchone("SELECT * FROM projects WHERE id = ?", (old["project_id"],))
        if not project:
            return None
        stage = from_stage or old.get("stage") or "auto"
        if stage == "done":
            stage = "auto"
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
