from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from backend.app.adapters.base import BlockedError
from backend.app.adapters.diarization import DiarizationAdapter
from backend.app.config import settings as default_settings
from backend.app.db import Database, now_iso
from backend.app.pipeline import Pipeline
import backend.app.pipeline as pipeline_module


class FakeTTS:
    def __init__(self, durations: list[float]):
        self.durations = durations
        self.calls: list[str] = []
        self.duration_by_path: dict[str, float] = {}

    def generate(self, text: str, output: Path, **_kwargs):
        self.calls.append(text)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"wav")
        self.duration_by_path[str(output)] = self.durations[len(self.calls) - 1]
        return output


class FakeRewriter:
    def __init__(self):
        self.calls: list[dict] = []

    def rewrite(self, segments, **kwargs):
        self.calls.append(kwargs)
        return [{"id": segments[0]["id"], "target_text": f"候选{len(self.calls)}"}]


def make_fixture(tmp_path, *, end: float = 1.0):
    settings = replace(default_settings, root_dir=tmp_path, work_dir=tmp_path / "work", db_path=tmp_path / "db.sqlite3")
    db = Database(settings.db_path)
    db.init()
    now = now_iso()
    series_id, project_id, character_id, segment_id = "series-test", "project-test", "character-test", "segment-test"
    work_dir = settings.work_dir / project_id
    work_dir.mkdir(parents=True)
    db.insert("series", {"id": series_id, "name": "测试", "source_language": "en", "target_language": "zh-CN", "level": "L1", "speed": 1.0, "status": "ready", "glossary_json": "{}", "created_at": now, "updated_at": now})
    db.insert("projects", {"id": project_id, "series_id": series_id, "title": "测试项目", "source_path": str(tmp_path / "source.mp4"), "source_sha256": "source", "duration": 2.0, "status": "created", "current_stage": "synthesize", "progress": 0, "status_message": "", "work_dir": str(work_dir), "metadata_json": "{}", "checkpoint_json": "{}", "created_at": now, "updated_at": now})
    db.insert("characters", {"id": character_id, "series_id": series_id, "speaker_key": "speaker-1", "name": "角色 1", "voice_profile": "profile-1", "status": "verified", "source_project_id": project_id, "metadata_json": "{}", "created_at": now, "updated_at": now})
    db.insert("segments", {"id": segment_id, "project_id": project_id, "segment_index": 0, "start_sec": 0.0, "end_sec": end, "speaker_key": "speaker-1", "speaker_name": "角色 1", "kind": "dialogue", "source_text": "Hello", "target_text": "你好", "target_language": "zh-CN", "speed": 1.0, "duration_delta": None, "voice_profile": "profile-1", "audio_path": None, "audio_sha256": None, "status": "rewritten", "source_revision": 1, "target_revision": 1, "error_message": None, "metadata_json": "{}", "created_at": now, "updated_at": now})
    pipeline = Pipeline(settings, db)
    return pipeline, db, db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,)), db.fetchone("SELECT * FROM segments WHERE id = ?", (segment_id,))


def test_over_duration_rewrites_at_most_twice_and_remeasures(tmp_path, monkeypatch):
    pipeline, db, project, segment = make_fixture(tmp_path)
    tts = FakeTTS([1.2, 0.7])
    rewriter = FakeRewriter()
    pipeline.tts = tts
    pipeline.rewriter = rewriter
    monkeypatch.setattr(pipeline_module, "probe_media", lambda _ffprobe, path: {"duration": tts.duration_by_path[str(path)]})
    monkeypatch.setattr(pipeline_module, "sha256_file", lambda _path: "audio-hash")

    pipeline._synthesize_segments(project, [segment])

    saved = db.fetchone("SELECT * FROM segments WHERE id = ?", (segment["id"],))
    assert len(tts.calls) == 2
    assert len(rewriter.calls) == 1
    assert rewriter.calls[0]["measured_duration"] == 1.2
    assert rewriter.calls[0]["current_target_text"] == "你好"
    assert rewriter.calls[0]["level"] == "L1"
    assert saved["target_text"] == "候选1"
    assert saved["target_revision"] == 2
    assert saved["status"] == "synthesized"
    assert saved["end_sec"] == pytest.approx(0.7)


def test_diarize_moves_asr_window_to_voice_and_caps_tail_at_next_line(tmp_path):
    pipeline, db, project, first = make_fixture(tmp_path)
    db.update("projects", {"duration": 6.0}, "id = ?", (project["id"],))
    now = now_iso()
    second_id = "segment-second"
    db.insert("segments", {"id": second_id, "project_id": project["id"], "segment_index": 1, "start_sec": 1.5, "end_sec": 2.0, "speaker_key": None, "speaker_name": None, "kind": "dialogue", "source_text": "There", "target_text": None, "target_language": "zh-CN", "speed": 1.0, "duration_delta": None, "voice_profile": None, "audio_path": None, "audio_sha256": None, "status": "pending", "source_revision": 1, "target_revision": 1, "error_message": None, "metadata_json": "{}", "created_at": now, "updated_at": now})

    class Diarizer:
        def diarize(self, _audio_path, _output_path):
            return [
                {"start": 0.5, "end": 0.9, "speaker": "speaker-a"},
                {"start": 1.6, "end": 1.9, "speaker": "speaker-b"},
            ]

        assign = staticmethod(DiarizationAdapter.assign)

    pipeline.diarizer = Diarizer()
    context = {"project": db.fetchone("SELECT * FROM projects WHERE id = ?", (project["id"],)), "metadata": {"separation": {"speech": str(tmp_path / "speech.wav")}}}

    pipeline._diarize(context)

    saved_first = db.fetchone("SELECT * FROM segments WHERE id = ?", (first["id"],))
    saved_second = db.fetchone("SELECT * FROM segments WHERE id = ?", (second_id,))
    timing = json.loads(saved_first["metadata_json"])["timing_alignment"]
    assert saved_first["start_sec"] == pytest.approx(0.42)
    assert saved_first["end_sec"] == pytest.approx(1.52)
    assert timing["asr_start_sec"] == 0.0
    assert timing["voice_start_sec"] == 0.5
    assert timing["available_end_sec"] == pytest.approx(saved_first["end_sec"])
    assert saved_second["start_sec"] == pytest.approx(1.52)


def test_over_duration_blocks_after_two_rewrites_without_truncation(tmp_path, monkeypatch):
    pipeline, db, project, segment = make_fixture(tmp_path)
    tts = FakeTTS([1.2, 1.1, 1.1])
    rewriter = FakeRewriter()
    pipeline.tts = tts
    pipeline.rewriter = rewriter
    monkeypatch.setattr(pipeline_module, "probe_media", lambda _ffprobe, path: {"duration": tts.duration_by_path[str(path)]})

    with pytest.raises(BlockedError) as caught:
        pipeline._synthesize_segments(project, [segment])

    saved = db.fetchone("SELECT * FROM segments WHERE id = ?", (segment["id"],))
    assert caught.value.code == "TTS_OVER_DURATION"
    assert len(tts.calls) == 3
    assert len(rewriter.calls) == 2
    assert saved["status"] == "blocked"
    assert saved["duration_delta"] == pytest.approx(0.1)
    assert saved["audio_path"] is None


def test_short_asr_window_blocks_before_tts_or_rewriter(tmp_path):
    pipeline, db, project, segment = make_fixture(tmp_path, end=0.12)
    tts = FakeTTS([])
    rewriter = FakeRewriter()
    pipeline.tts = tts
    pipeline.rewriter = rewriter

    with pytest.raises(BlockedError) as caught:
        pipeline._synthesize_segments(project, [segment])

    assert caught.value.code == "SEGMENT_WINDOW_TOO_SHORT"
    assert tts.calls == []
    assert rewriter.calls == []
    anomaly = db.fetchone("SELECT * FROM anomalies WHERE project_id = ?", (project["id"],))
    assert anomaly["kind"] == "SEGMENT_WINDOW_TOO_SHORT"
    assert anomaly["blocking"] == 1


def test_fragmented_diarization_blocks_before_character_creation(tmp_path):
    pipeline, db, project, _segment = make_fixture(tmp_path)
    now = now_iso()
    for index in range(1, 33):
        db.insert("segments", {"id": f"segment-{index}", "project_id": project["id"], "segment_index": index, "start_sec": float(index), "end_sec": float(index) + 0.5, "speaker_key": None, "speaker_name": None, "kind": "dialogue", "source_text": "Hello", "target_text": None, "target_language": "zh-CN", "speed": 1.0, "duration_delta": None, "voice_profile": None, "audio_path": None, "audio_sha256": None, "status": "pending", "source_revision": 1, "target_revision": 1, "error_message": None, "metadata_json": "{}", "created_at": now, "updated_at": now})

    class FragmentedDiarizer:
        def diarize(self, _audio_path, _output_path):
            return [{"start": 0.0, "end": 1.0, "speaker": "cluster"}]

        @staticmethod
        def assign(rows, _diarization, _project_key):
            for index, row in enumerate(rows):
                row["speaker_key"] = f"cluster-{index}"
            return rows

    pipeline.diarizer = FragmentedDiarizer()
    context = {"project": project, "metadata": {"separation": {"speech": str(tmp_path / "speech.wav")}}}

    with pytest.raises(BlockedError) as caught:
        pipeline._diarize(context)

    assert caught.value.code == "DIARIZATION_FRAGMENTED"
    anomaly = db.fetchone("SELECT * FROM anomalies WHERE project_id = ? AND kind = ?", (project["id"], "DIARIZATION_FRAGMENTED"))
    assert anomaly["blocking"] == 1
