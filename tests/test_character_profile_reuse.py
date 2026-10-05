from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from backend.app.config import settings as default_settings
from backend.app.db import Database, now_iso
from backend.app.pipeline import Pipeline


def test_cross_episode_match_ignores_unreferenced_character_profiles(tmp_path, monkeypatch):
    settings = replace(default_settings, root_dir=tmp_path, work_dir=tmp_path / "work", db_path=tmp_path / "db.sqlite3")
    db = Database(settings.db_path)
    db.init()
    now = now_iso()
    series_id = "series-character-test"

    db.insert("series", {"id": series_id, "name": "测试", "source_language": "en", "target_language": "zh-CN", "level": "L1", "speed": 0.85, "status": "ready", "glossary_json": "{}", "created_at": now, "updated_at": now})
    for project_id in ("project-current", "project-previous"):
        work_dir = settings.work_dir / project_id
        work_dir.mkdir(parents=True)
        db.insert("projects", {"id": project_id, "series_id": series_id, "title": project_id, "source_path": str(tmp_path / f"{project_id}.mp4"), "source_sha256": project_id, "duration": 10.0, "status": "created", "current_stage": "characters", "progress": 0, "status_message": "", "work_dir": str(work_dir), "metadata_json": "{}", "checkpoint_json": "{}", "created_at": now, "updated_at": now})

    embedding_path = tmp_path / "speaker.json"
    embedding_path.write_text(json.dumps([1.0, 0.0]), encoding="utf-8")
    secondary_embedding_path = tmp_path / "speaker-secondary.json"
    secondary_embedding_path.write_text(json.dumps([0.8, 0.6]), encoding="utf-8")
    candidates = (
        ("character-0-stale", "stale-speaker", "角色 25", embedding_path),
        ("character-a-secondary", "secondary-speaker", "角色 2", secondary_embedding_path),
        ("character-z-active", "active-speaker", "角色 1", embedding_path),
    )
    for character_id, speaker_key, name, candidate_embedding_path in candidates:
        db.insert("characters", {"id": character_id, "series_id": series_id, "speaker_key": speaker_key, "name": name, "main_sample_path": str(tmp_path / f"{character_id}.wav"), "voice_profile": f"profile-{character_id}", "status": "verified", "source_project_id": "project-previous", "embedding_path": str(candidate_embedding_path), "metadata_json": json.dumps({"embedding_model": "community1"}), "created_at": now, "updated_at": now})

    db.insert("segments", {"id": "old-active-segment", "project_id": "project-previous", "segment_index": 0, "start_sec": 0.0, "end_sec": 1.0, "speaker_key": "active-speaker", "speaker_name": "角色", "kind": "dialogue", "source_text": "Hello", "status": "recognized", "metadata_json": "{}", "created_at": now, "updated_at": now})
    db.insert("segments", {"id": "old-secondary-segment", "project_id": "project-previous", "segment_index": 1, "start_sec": 2.0, "end_sec": 3.0, "speaker_key": "secondary-speaker", "speaker_name": "角色", "kind": "dialogue", "source_text": "Goodbye", "status": "recognized", "metadata_json": "{}", "created_at": now, "updated_at": now})
    db.insert("segments", {"id": "current-segment", "project_id": "project-current", "segment_index": 0, "start_sec": 0.0, "end_sec": 1.0, "speaker_key": "new-speaker", "speaker_name": None, "kind": "dialogue", "source_text": "Hello", "status": "recognized", "metadata_json": json.dumps({"speaker_embedding": [1.0, 0.0], "speaker_embedding_model": "community1"}), "created_at": now, "updated_at": now})
    db.insert("segments", {"id": "current-second-character-segment", "project_id": "project-current", "segment_index": 1, "start_sec": 2.0, "end_sec": 3.0, "speaker_key": "second-new-speaker", "speaker_name": None, "kind": "dialogue", "source_text": "Hi again", "status": "recognized", "metadata_json": json.dumps({"speaker_embedding": [0.95, 0.25], "speaker_embedding_model": "community1"}), "created_at": now, "updated_at": now})
    db.insert("segments", {"id": "current-new-character-segment", "project_id": "project-current", "segment_index": 2, "start_sec": 4.0, "end_sec": 5.0, "speaker_key": "unmatched-speaker", "speaker_name": None, "kind": "dialogue", "source_text": "Goodbye", "status": "recognized", "metadata_json": json.dumps({"speaker_embedding": [0.0, 0.0, 1.0], "speaker_embedding_model": "community1"}), "created_at": now, "updated_at": now})

    from backend.app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "extract_audio", lambda _ffmpeg, _source, output, **_kwargs: Path(output))
    pipeline = Pipeline(settings, db)
    pipeline.tts.create_voice_profile = lambda *_args, **_kwargs: "profile-new"
    project = db.fetchone("SELECT * FROM projects WHERE id = 'project-current'")
    pipeline._characters({"project": project, "metadata": {"separation": {"speech": str(tmp_path / "speech.wav")}}})

    updated = db.fetchone("SELECT * FROM segments WHERE id = 'current-segment'")
    assert updated["speaker_key"] == "active-speaker"
    assert updated["voice_profile"] == "profile-character-z-active"
    assert db.fetchone("SELECT id FROM characters WHERE speaker_key = 'stale-speaker'") is not None
    second = db.fetchone("SELECT * FROM segments WHERE id = 'current-second-character-segment'")
    assert second["speaker_key"] == "secondary-speaker"
    assert second["voice_profile"] == "profile-character-a-secondary"
    assert db.fetchone("SELECT name FROM characters WHERE speaker_key = 'unmatched-speaker'")["name"] == "角色 3"
