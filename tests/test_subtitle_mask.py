from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from backend.app.adapters import media
from backend.app.config import settings as default_settings
from backend.app.db import Database, now_iso
from backend.app.pipeline import Pipeline


def test_pipeline_masks_song_and_source_dialogue_caption_windows(tmp_path):
    settings = replace(default_settings, root_dir=tmp_path, work_dir=tmp_path / "work", db_path=tmp_path / "db.sqlite3")
    db = Database(settings.db_path)
    db.init()
    now = now_iso()
    db.insert("series", {"id": "series", "name": "测试", "source_language": "en", "target_language": "zh-CN", "level": "L1", "speed": 0.85, "status": "ready", "glossary_json": "{}", "created_at": now, "updated_at": now})
    project_id = "project"
    db.insert("projects", {"id": project_id, "series_id": "series", "title": "测试", "source_path": "source.mp4", "source_sha256": "hash", "duration": 10.0, "status": "created", "current_stage": "export", "progress": 0, "status_message": "", "work_dir": str(tmp_path / "work" / project_id), "metadata_json": "{}", "checkpoint_json": "{}", "created_at": now, "updated_at": now})
    db.insert("segments", {"id": "segment", "project_id": project_id, "segment_index": 0, "start_sec": 4.0, "end_sec": 5.0, "speaker_key": "speaker-1", "speaker_name": "角色 1", "kind": "dialogue", "source_text": "Hi", "target_text": "你好", "target_language": "zh-CN", "speed": 0.85, "duration_delta": None, "voice_profile": "profile", "audio_path": None, "audio_sha256": None, "status": "synthesized", "source_revision": 1, "target_revision": 1, "error_message": None, "metadata_json": json.dumps({"timing_alignment": {"asr_start_sec": 3.7, "asr_end_sec": 5.2, "voice_start_sec": 4.1, "voice_end_sec": 4.8}}), "created_at": now, "updated_at": now})
    pipeline = Pipeline(settings, db)
    intervals = pipeline._subtitle_mask_intervals(
        db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,)),
        {"metadata": {"separation": {"song_intervals": [{"start": 0.0, "end": 2.0}]}}},
    )

    assert intervals[0] == {"start": 0.0, "end": 2.0}
    assert intervals[1]["start"] == pytest.approx(3.4)
    assert intervals[1]["end"] == pytest.approx(6.0)


def test_mux_candidate_masks_before_burning_target_subtitles(tmp_path, monkeypatch):
    captured: list[str] = []

    def fake_run(command, **_kwargs):
        captured.extend(command)
        Path(command[-1]).write_bytes(b"x" * 2048)

    monkeypatch.setattr(media, "_run", fake_run)
    output = tmp_path / "candidate.mp4"
    media.mux_candidate(
        "ffmpeg",
        tmp_path / "source.mp4",
        tmp_path / "audio.wav",
        tmp_path / "target.srt",
        output,
        mask_intervals=[{"start": 0.0, "end": 2.0}],
    )
    video_filter = captured[captured.index("-vf") + 1]

    assert video_filter.index("drawbox=") < video_filter.index("subtitles=")
    assert "x=iw*0.22" in video_filter
    assert "between(t,0.000,2.000)" in video_filter
    assert "force_style='MarginV=25,BorderStyle=1,Outline=2,Shadow=0'" in video_filter
    assert output.is_file()
