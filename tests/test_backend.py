from __future__ import annotations

import subprocess
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.app.adapters.media import write_srt
from backend.app.config import settings as default_settings
from backend.app.main import create_app


def test_series_and_missing_source_are_explicit(tmp_path):
    app_settings = replace(default_settings, root_dir=tmp_path, work_dir=tmp_path / "work", db_path=tmp_path / "db.sqlite3")
    with TestClient(create_app(app_settings)) as client:
        response = client.post("/api/series", json={"name": "夹具系列", "target_language": "zh-CN", "level": "L1", "speed": 0.82})
        assert response.status_code == 201
        series = response.json()
        assert series["name"] == "夹具系列"
        assert client.get("/api/series").json()[0]["id"] == series["id"]

        missing = client.post(f"/api/series/{series['id']}/projects", json={"source_path": str(tmp_path / "missing.mp4")})
        assert missing.status_code == 409
        assert missing.json()["error"]["code"] == "SOURCE_NOT_FOUND"
        assert client.get("/api/projects").json() == []


def test_real_media_import_preserves_source_and_queue_does_not_fake_success(tmp_path):
    ffmpeg = default_settings.ffmpeg
    source = tmp_path / "fixture.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1.2", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "1.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)],
        check=True,
        capture_output=True,
    )
    original_bytes = source.read_bytes()
    app_settings = replace(default_settings, root_dir=tmp_path, work_dir=tmp_path / "work", db_path=tmp_path / "db.sqlite3", bandit_command="")
    with TestClient(create_app(app_settings)) as client:
        series = client.post("/api/series", json={"name": "媒体夹具", "target_language": "zh-CN"}).json()
        imported = client.post(f"/api/series/{series['id']}/projects", json={"source_path": str(source), "title": "第 01 集"})
        assert imported.status_code == 201
        project = imported.json()
        assert project["source_sha256"]
        assert source.read_bytes() == original_bytes
        assert project["source_exists"] is True

        queued = client.post(f"/api/projects/{project['id']}/jobs", json={"kind": "process", "from_stage": "auto"})
        assert queued.status_code == 202
        job_id = queued.json()["id"]
        for _ in range(30):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"awaiting_confirmation", "blocked", "failed", "completed", "completed_with_warnings"}:
                break
            time.sleep(0.1)
        assert job["status"] == "awaiting_confirmation"
        assert job["stage"] == "probe"
        assert client.get(f"/api/projects/{project['id']}").json()["checkpoint"]["pending_confirmation_stage"] == "probe"
        assert client.post(f"/api/projects/{project['id']}/export", json={}).status_code == 409
        assert source.read_bytes() == original_bytes


def test_srt_skips_song_segments(tmp_path):
    output = tmp_path / "target.srt"
    write_srt(
        [
            {"kind": "song", "start_sec": 0, "end_sec": 2, "target_text": "不应出现"},
            {"kind": "dialogue", "start_sec": 2, "end_sec": 3, "target_text": "可以出现"},
        ],
        output,
    )
    text = output.read_text(encoding="utf-8-sig")
    assert "不应出现" not in text
    assert "可以出现" in text
