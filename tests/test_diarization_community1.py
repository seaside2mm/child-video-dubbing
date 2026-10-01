from __future__ import annotations

import json
import sys
import wave
import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.app.adapters import diarization
from backend.app.adapters.base import BlockedError
from backend.app.adapters.diarization import (
    COMMUNITY1_EMBEDDING_MODEL_ID,
    COMMUNITY1_MODEL_ID,
    ECAPA_EMBEDDING_MODEL_ID,
    DiarizationAdapter,
)
from backend.app.config import Settings, settings as default_settings
from scripts import model_community_diarization as community1


def test_missing_hub_dependency_is_not_reported_as_missing_token(monkeypatch):
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    result = community1.status()
    assert result["code"] == "COMMUNITY1_DEPENDENCIES_MISSING"


def test_pcm_reader_preserves_channel_order_rate_and_amplitude(tmp_path):
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack("<hhhh", 16384, -16384, 0, 8192))
    samples, rate = community1.load_pcm_wave(path)
    assert rate == 44100
    assert samples.tolist() == [[0.5, 0.0], [-0.5, 0.25]]


def _output(embedding_rows, exclusive_speakers=("SPEAKER_01",)):
    regular = SimpleNamespace(labels=lambda: ["SPEAKER_00", "SPEAKER_01"])
    turns = [
        (SimpleNamespace(start=float(i), end=float(i + 1)), None, speaker)
        for i, speaker in enumerate(exclusive_speakers)
    ]
    exclusive = SimpleNamespace(itertracks=lambda yield_label: iter(turns))
    return SimpleNamespace(
        speaker_diarization=regular,
        exclusive_speaker_diarization=exclusive,
        speaker_embeddings=embedding_rows,
    )


def test_serializer_maps_embedding_rows_by_regular_label_and_uses_exclusive_turns():
    payload = community1.serialize_output(_output([[3.0, 4.0], [0.0, 2.0]]))

    assert payload["model"] == COMMUNITY1_MODEL_ID
    assert payload["embedding_model"] == COMMUNITY1_EMBEDDING_MODEL_ID
    assert payload["speaker_embeddings"] == {"SPEAKER_00": [0.6, 0.8], "SPEAKER_01": [0.0, 1.0]}
    assert payload["segments"] == [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_01"}]


@pytest.mark.parametrize(
    "rows,match",
    [([[1.0, 0.0]], "rows do not align"), ([[0.0, 0.0], [1.0, 0.0]], "zero vector")],
)
def test_serializer_rejects_unusable_embedding_mapping(rows, match):
    with pytest.raises(ValueError, match=match):
        community1.serialize_output(_output(rows))


def test_config_defaults_to_community1_and_allows_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DUBBING_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("PYANNOTE_MODEL", raising=False)
    assert Settings.load(tmp_path).pyannote_model == COMMUNITY1_MODEL_ID

    monkeypatch.setenv("PYANNOTE_MODEL", "org/custom-diarizer")
    assert Settings.load(tmp_path).pyannote_model == "org/custom-diarizer"


def _community_settings(tmp_path):
    return replace(default_settings, root_dir=tmp_path, diarization_command="", pyannote_model=COMMUNITY1_MODEL_ID)


def _install_fake_community_runtime(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "model_community_diarization.py").write_text("", encoding="utf-8")
    runtime = tmp_path / "work" / "model-runtime" / "pyannote-community-1" / "venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"fake runtime")
    return runtime


def test_community_missing_runtime_is_blocked_without_ecapa_fallback(tmp_path, monkeypatch):
    adapter = DiarizationAdapter(_community_settings(tmp_path))
    monkeypatch.setattr(adapter, "_local_command", lambda: pytest.fail("must not fall back to ECAPA"))
    status = adapter.status()

    assert status.status == "blocked"
    with pytest.raises(BlockedError, match="Community-1") as error:
        adapter.diarize(tmp_path / "speech.wav", tmp_path / "diarization.json")
    assert error.value.code == "COMMUNITY1_RUNTIME_REQUIRED"


def test_community_adapter_normalizes_exclusive_turn_and_embedding_model(tmp_path, monkeypatch):
    runtime = _install_fake_community_runtime(tmp_path)
    adapter = DiarizationAdapter(_community_settings(tmp_path))
    monkeypatch.setattr(adapter, "_local_command", lambda: pytest.fail("must not fall back to ECAPA"))
    status_commands = []

    def fake_subprocess_run(command, **kwargs):
        status_commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "ready", "model": COMMUNITY1_MODEL_ID, "embedding_model": COMMUNITY1_EMBEDDING_MODEL_ID}),
            stderr="",
        )

    monkeypatch.setattr(diarization.subprocess, "run", fake_subprocess_run)
    output_path = tmp_path / "diarization.json"

    def fake_run(command, timeout):
        assert command[0] == str(runtime)
        output_path.write_text(json.dumps({
            "status": "ok",
            "backend": "pyannote-community-1",
            "model": COMMUNITY1_MODEL_ID,
            "embedding_model": COMMUNITY1_EMBEDDING_MODEL_ID,
            "segments": [{"start": 2.0, "end": 3.0, "speaker": "SPEAKER_01"}],
            "speaker_embeddings": {"SPEAKER_01": [0.0, 1.0]},
        }), encoding="utf-8")

    monkeypatch.setattr(diarization, "_run", fake_run)
    turns = adapter.diarize(tmp_path / "speech.wav", output_path)

    assert status_commands[0][0] == str(runtime)
    assert turns == [{
        "start": 2.0,
        "end": 3.0,
        "speaker": "SPEAKER_01",
        "embedding": [0.0, 1.0],
        "embedding_model": COMMUNITY1_EMBEDDING_MODEL_ID,
        "cluster_id": None,
    }]


def test_community_rejects_ecapa_or_missing_speaker_vectors(tmp_path, monkeypatch):
    _install_fake_community_runtime(tmp_path)
    adapter = DiarizationAdapter(_community_settings(tmp_path))
    monkeypatch.setattr(diarization.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"status": "ready", "model": COMMUNITY1_MODEL_ID, "embedding_model": COMMUNITY1_EMBEDDING_MODEL_ID}),
        stderr="",
    ))
    output_path = tmp_path / "diarization.json"

    for output in (
        {"status": "ok", "backend": "speechbrain-ecapa-voxceleb", "model": ECAPA_EMBEDDING_MODEL_ID,
         "segments": [{"start": 0, "end": 1, "speaker": "cluster-0"}],
         "cluster_embeddings": {"cluster-0": [1.0, 0.0]}},
        {"status": "ok", "backend": "pyannote-community-1", "model": COMMUNITY1_MODEL_ID,
         "embedding_model": COMMUNITY1_EMBEDDING_MODEL_ID,
         "segments": [{"start": 0, "end": 1, "speaker": "SPEAKER_00"}],
         "cluster_embeddings": {"SPEAKER_00": [1.0, 0.0]}},
    ):
        monkeypatch.setattr(diarization, "_run", lambda command, timeout, payload=output: output_path.write_text(json.dumps(payload), encoding="utf-8"))
        with pytest.raises(BlockedError):
            adapter.diarize(tmp_path / "speech.wav", output_path)


def test_assignment_namespaces_community_keys_and_carries_model_identifiers():
    community_turn = {
        "start": 0,
        "end": 1,
        "speaker": "SPEAKER_00",
        "embedding": [0.0, 1.0],
        "embedding_model": COMMUNITY1_EMBEDDING_MODEL_ID,
    }
    ecapa_turn = {
        "start": 0,
        "end": 1,
        "speaker": "cluster-0",
        "embedding": [1.0, 0.0],
        "embedding_model": ECAPA_EMBEDDING_MODEL_ID,
    }

    community = DiarizationAdapter.assign([{"start": 0, "end": 1}], [community_turn], "project")
    ecapa = DiarizationAdapter.assign([{"start": 0, "end": 1}], [ecapa_turn], "project")

    assert community[0]["speaker_key"] == "project:community1:SPEAKER_00"
    assert community[0]["speaker_embedding_model"] == COMMUNITY1_EMBEDDING_MODEL_ID
    assert ecapa[0]["speaker_key"] == "project:cluster-0"
    assert ecapa[0]["speaker_embedding_model"] == ECAPA_EMBEDDING_MODEL_ID
