from __future__ import annotations

import json
from dataclasses import replace

import pytest

from backend.app.adapters import diarization
from backend.app.adapters.diarization import DiarizationAdapter
from backend.app.config import settings as default_settings


def test_transcript_windows_keep_sentence_boundaries_and_skip_too_short_audio():
    pytest.importorskip("numpy", reason="transcription model runtime dependency is optional")
    from scripts.model_diarization import _transcript_windows

    segments = [
        {"start": 0.0, "end": 1.7, "text": "第一句"},
        {"start": 1.8, "end": 2.4, "text": "嗯"},
        {"start": 2.5, "end": 3.5, "text": "第二句"},
    ]

    windows = _transcript_windows(segments, [(0, 17), (18, 24), (25, 35)], 10, 40)

    assert windows == [
        {"start": 0, "end": 17, "transcript_index": 0},
        {"start": 25, "end": 35, "transcript_index": 2},
    ]


def test_long_asr_segment_is_split_without_crossing_its_transcript_index():
    pytest.importorskip("numpy", reason="transcription model runtime dependency is optional")
    from scripts.model_diarization import _transcript_windows

    windows = _transcript_windows(
        [{"start": 0, "end": 10, "text": "较长的一句"}],
        [(0, 100)],
        10,
        100,
        max_window_seconds=4,
    )

    assert windows == [
        {"start": 0, "end": 40, "transcript_index": 0},
        {"start": 40, "end": 80, "transcript_index": 0},
        {"start": 80, "end": 100, "transcript_index": 0},
    ]


def test_adapter_passes_sibling_transcript_and_preserves_cluster_trace(monkeypatch, tmp_path):
    settings = replace(default_settings, root_dir=tmp_path, diarization_command="", pyannote_model="")
    adapter = DiarizationAdapter(settings)
    audio_path = tmp_path / "speech.wav"
    transcript_path = tmp_path / "transcript.json"
    output_path = tmp_path / "diarization.json"
    audio_path.write_bytes(b"audio")
    transcript_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(adapter, "_local_command", lambda: ["python", "model_diarization.py"])
    commands = []

    def fake_run(command, timeout):
        commands.append(command)
        output_path.write_text(
            json.dumps({
                "segments": [{"start": 0.8, "end": 1.6, "speaker": "cluster-2", "cluster_id": 2, "transcript_index": 5}],
                "cluster_embeddings": {"cluster-2": [0.2, 0.8]},
            }),
            encoding="utf-8",
        )

    monkeypatch.setattr(diarization, "_run", fake_run)

    turns = adapter.diarize(audio_path, output_path)

    assert commands[0][commands[0].index("--transcript") + 1] == str(transcript_path)
    assert turns[0]["transcript_index"] == 5
    assert turns[0]["cluster_id"] == 2
    assert turns[0]["embedding"] == [0.2, 0.8]
