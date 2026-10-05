from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.config import settings as default_settings
from backend.app.db import Database, now_iso
from backend.app.main import create_app
from backend.app.pipeline import Pipeline
import backend.app.pipeline as pipeline_module


def test_detect_songs_keeps_and_merges_manual_override(tmp_path, monkeypatch):
    settings = replace(default_settings, root_dir=tmp_path, work_dir=tmp_path / "work", db_path=tmp_path / "db.sqlite3")
    db = Database(settings.db_path)
    db.init()
    now = now_iso()
    series_id, project_id = "series-song-test", "project-song-test"
    work_dir = settings.work_dir / project_id
    work_dir.mkdir(parents=True)
    db.insert("series", {"id": series_id, "name": "测试", "source_language": "zh", "target_language": "zh-CN", "level": "L1", "speed": 0.85, "status": "ready", "glossary_json": "{}", "created_at": now, "updated_at": now})
    db.insert("projects", {"id": project_id, "series_id": series_id, "title": "测试", "source_path": str(tmp_path / "source.mp4"), "source_sha256": "source", "duration": 10.0, "status": "created", "current_stage": "transcribe", "progress": 0, "status_message": "", "work_dir": str(work_dir), "metadata_json": "{}", "checkpoint_json": "{}", "created_at": now, "updated_at": now})
    runtime = settings.root_dir / "work/model-runtime/venv/Scripts/python.exe"
    script = settings.root_dir / "scripts/model_song_detection.py"
    runtime.parent.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    runtime.touch()
    script.touch()
    (work_dir / "song-detection-input.wav").touch()
    detected = {"status": "no_song", "model": "YAMNet", "song_intervals": [{"start": 2.0, "end": 5.0}]}

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_text(json.dumps(detected), encoding="utf-8")

    monkeypatch.setattr(pipeline_module, "_run", fake_run)
    pipeline = Pipeline(settings, db)
    context = {"project": db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,)), "metadata": {"separation": {"speech": "speech.wav", "background": "background.wav", "song_intervals_override": [{"start": 0.0, "end": 3.0}]}}}

    pipeline._detect_songs(context)
    assert context["metadata"]["separation"]["song_intervals"] == [{"start": 0.0, "end": 5.0}]
    assert context["metadata"]["separation"]["song_intervals_detected"] == detected["song_intervals"]
    assert context["metadata"]["separation"]["song_detection"]["status"] == "no_song"
    assert context["metadata"]["separation"]["song_detection"]["manual_override_count"] == 1

    pipeline._detect_songs(context)
    assert context["metadata"]["separation"]["song_intervals"] == [{"start": 0.0, "end": 5.0}]


def test_transcribe_marks_as_song_dialogue_starting_inside_manual_interval(tmp_path, monkeypatch):
    settings = replace(default_settings, root_dir=tmp_path, work_dir=tmp_path / "work", db_path=tmp_path / "db.sqlite3")
    db = Database(settings.db_path)
    db.init()
    now = now_iso()
    series_id, project_id = "series-song-test", "project-song-test"
    work_dir = settings.work_dir / project_id
    work_dir.mkdir(parents=True)
    speech = work_dir / "speech.wav"
    speech.touch()
    db.insert("series", {"id": series_id, "name": "测试", "source_language": "zh", "target_language": "zh-CN", "level": "L1", "speed": 0.85, "status": "ready", "glossary_json": "{}", "created_at": now, "updated_at": now})
    db.insert("projects", {"id": project_id, "series_id": series_id, "title": "测试", "source_path": str(tmp_path / "source.mp4"), "source_sha256": "source", "duration": 10.0, "status": "created", "current_stage": "transcribe", "progress": 0, "status_message": "", "work_dir": str(work_dir), "metadata_json": "{}", "checkpoint_json": "{}", "created_at": now, "updated_at": now})
    pipeline = Pipeline(settings, db)
    pipeline._detect_songs = lambda _context: None

    class FakeTranscriber:
        @staticmethod
        def transcribe(_speech, _language):
            return {"segments": [{"start": 0.0, "end": 4.0, "text": "片头歌词"}, {"start": 4.0, "end": 6.0, "text": "普通对白"}]}

    pipeline.transcriber = FakeTranscriber()
    context = {"project": db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,)), "metadata": {"separation": {"speech": str(speech), "song_intervals": [], "song_intervals_override": [{"start": 0.0, "end": 1.0}]}}}
    pipeline._transcribe(context)

    rows = db.fetchall("SELECT kind, source_text, metadata_json FROM segments WHERE project_id = ? ORDER BY segment_index", (project_id,))
    assert [row["kind"] for row in rows] == ["song", "dialogue"]
    assert json.loads(rows[0]["metadata_json"])["song_interval_override"] is True
    assert db.fetchone("SELECT 1 FROM anomalies WHERE project_id = ? AND kind = 'SONG_UNREWRITTEN'", (project_id,)) is None


def test_song_interval_api_validates_and_invalidates_from_transcribe(tmp_path):
    ffmpeg = default_settings.ffmpeg
    source = tmp_path / "fixture.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1.2", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "1.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)],
        check=True,
        capture_output=True,
    )
    settings = replace(default_settings, root_dir=tmp_path, work_dir=tmp_path / "work", db_path=tmp_path / "db.sqlite3", bandit_command="")
    speech, background = tmp_path / "speech.wav", tmp_path / "background.wav"
    speech.write_bytes(b"speech")
    background.write_bytes(b"background")
    with TestClient(create_app(settings)) as client:
        series = client.post("/api/series", json={"name": "测试", "target_language": "zh-CN"}).json()
        project = client.post(f"/api/series/{series['id']}/projects", json={"source_path": str(source)}).json()
        imported = client.post(f"/api/projects/{project['id']}/separation", json={"speech_path": str(speech), "background_path": str(background), "song_intervals": []})
        assert imported.status_code == 201

        updated = client.put(f"/api/projects/{project['id']}/song-intervals", json={"intervals": [{"start": 0.0, "end": 0.8}]})
        assert updated.status_code == 200
        saved = client.get(f"/api/projects/{project['id']}").json()
        assert saved["metadata"]["separation"]["song_intervals_override"] == [{"start": 0.0, "end": 0.8}]
        assert saved["checkpoint"]["completed_stages"] == ["probe", "separate"]
        assert saved["current_stage"] == "transcribe"

        invalid = client.put(f"/api/projects/{project['id']}/song-intervals", json={"intervals": [{"start": 0.0, "end": 1.3}]})
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "SONG_INTERVAL_INVALID"
