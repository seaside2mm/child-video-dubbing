from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Lock
import time

from backend.app.adapters.base import ServiceStatus
from backend.app.adapters.diarization import DiarizationAdapter
from backend.app.adapters.faster_whisper import FasterWhisperAdapter
from backend.app.adapters.omnivoice import OmniVoiceAdapter
from backend.app.adapters.separation import SeparationAdapter
from backend.app.adapters.text_rewriter import TextRewriterAdapter
from backend.app.config import settings as default_settings
from backend.app.main import create_app


def endpoint(app, path: str):
    return next(route.endpoint for route in app.routes if getattr(route, "path", None) == path)


def test_health_probes_are_single_flight_and_cached(tmp_path, monkeypatch):
    app_settings = replace(default_settings, root_dir=tmp_path, work_dir=tmp_path / "work", db_path=tmp_path / "db.sqlite3")
    app = create_app(app_settings)
    calls = 0
    calls_lock = Lock()

    def fake_status(*_args, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.03)
        return ServiceStatus("configured", "fixture")

    for adapter in (FasterWhisperAdapter, OmniVoiceAdapter, SeparationAdapter, DiarizationAdapter, TextRewriterAdapter):
        monkeypatch.setattr(adapter, "status", fake_status)

    health = endpoint(app, "/api/health")
    recheck = endpoint(app, "/api/settings/recheck")
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _index: health(), range(5)))

    assert calls == 5
    assert {result["checked_at"] for result in results} == {results[0]["checked_at"]}
    health()
    assert calls == 5

    recheck()
    assert calls == 10
