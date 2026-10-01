from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from backend.app.adapters.base import RemoteServiceError
from backend.app.adapters import faster_whisper
from backend.app.config import settings as default_settings


def make_adapter() -> faster_whisper.FasterWhisperAdapter:
    settings = replace(
        default_settings,
        faster_whisper_url="http://speaches.test",
        faster_whisper_model="Systran/faster-whisper-large-v3",
    )
    return faster_whisper.FasterWhisperAdapter(settings)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(409, text="Model already loaded"),
        httpx.Response(409, json={"detail": "Model already loaded"}),
    ],
)
def test_ensure_model_loaded_accepts_explicit_already_loaded_conflict(monkeypatch, response):
    requests = []

    def fake_post(url, **kwargs):
        requests.append(url)
        return response

    monkeypatch.setattr(faster_whisper.httpx, "post", fake_post)

    make_adapter().ensure_model_loaded()

    assert requests == ["http://speaches.test/api/ps/Systran%2Ffaster-whisper-large-v3"]


def test_ensure_model_loaded_does_not_hide_other_conflicts(monkeypatch):
    monkeypatch.setattr(
        faster_whisper.httpx,
        "post",
        lambda url, **kwargs: httpx.Response(409, text="Model load conflict"),
    )

    with pytest.raises(RemoteServiceError) as error:
        make_adapter().ensure_model_loaded()

    assert error.value.code == "FASTER_WHISPER_MODEL_LOAD_FAILED"
    assert error.value.details["detail"] == "Model load conflict"


def test_transcribe_encodes_multipart_fields_as_mapping(tmp_path, monkeypatch):
    audio_path = tmp_path / "spoken.wav"
    audio_path.write_bytes(b"wav-data")
    requests = []

    def fake_post(url, **kwargs):
        if "/api/ps/" in url:
            return httpx.Response(200)
        data = kwargs["data"]
        assert isinstance(data, dict)
        request = httpx.Request(url=url, method="POST", files=kwargs["files"], data=data)
        requests.append(request.read())
        return httpx.Response(
            200,
            json={"language": "zh", "text": "你好", "segments": [{"start": 0, "end": 1, "text": "你好"}]},
        )

    monkeypatch.setattr(faster_whisper.httpx, "post", fake_post)

    result = make_adapter().transcribe(audio_path, "zh-CN")

    assert len(requests) == 1
    assert b'name="timestamp_granularities[]"' in requests[0]
    assert b"segment" in requests[0]
    assert b'name="language"' in requests[0]
    assert b"zh" in requests[0]
    assert result["segments"] == [{"index": 0, "start": 0.0, "end": 1.0, "text": "你好", "words": []}]
