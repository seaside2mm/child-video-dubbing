from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS series (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_language TEXT NOT NULL DEFAULT 'auto',
    target_language TEXT NOT NULL,
    level TEXT NOT NULL,
    speed REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    glossary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    duration REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'created',
    current_stage TEXT NOT NULL DEFAULT 'probe',
    progress REAL NOT NULL DEFAULT 0,
    status_message TEXT NOT NULL DEFAULT '',
    work_dir TEXT NOT NULL,
    output_path TEXT,
    output_sha256 TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS characters (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    speaker_key TEXT NOT NULL,
    name TEXT NOT NULL,
    main_sample_path TEXT,
    backup_sample_path TEXT,
    voice_profile TEXT,
    status TEXT NOT NULL DEFAULT 'candidate',
    source_project_id TEXT,
    embedding_path TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(series_id, speaker_key)
);

CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    speaker_key TEXT,
    speaker_name TEXT,
    kind TEXT NOT NULL DEFAULT 'dialogue',
    source_text TEXT NOT NULL,
    target_text TEXT,
    target_language TEXT,
    speed REAL,
    duration_delta REAL,
    voice_profile TEXT,
    audio_path TEXT,
    audio_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    source_revision INTEGER NOT NULL DEFAULT 1,
    target_revision INTEGER NOT NULL DEFAULT 1,
    error_message TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, segment_index)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'probe',
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    from_stage TEXT,
    force INTEGER NOT NULL DEFAULT 0,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS anomalies (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    segment_id TEXT,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    blocking INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT '',
    resolved INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_series ON projects(series_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_segments_project ON segments(project_id, segment_index);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_anomalies_project ON anomalies(project_id, resolved, severity);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load(value: str | None, default: Any = None) -> Any:
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default if default is not None else {}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, tuple(params))

    def executemany(self, sql: str, params: Iterable[Iterable[Any]]) -> None:
        with self.connect() as conn:
            conn.executemany(sql, params)

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    def insert(self, table: str, values: dict[str, Any]) -> None:
        keys = list(values)
        columns = ", ".join(keys)
        placeholders = ", ".join("?" for _ in keys)
        self.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", [values[k] for k in keys])

    def update(self, table: str, values: dict[str, Any], where: str, params: Iterable[Any]) -> None:
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.execute(f"UPDATE {table} SET {assignments} WHERE {where}", [*values.values(), *params])
