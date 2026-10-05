from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings as default_settings
from backend.app.db import Database, dump, load, now_iso
from backend.app.main import create_app
from backend.app.pipeline import Pipeline
from backend.app.queue import JobQueue, QueueConflict


def make_database(tmp_path):
    settings = replace(default_settings, root_dir=tmp_path, work_dir=tmp_path / "work", db_path=tmp_path / "db.sqlite3")
    db = Database(settings.db_path)
    db.init()
    now = now_iso()
    db.insert("series", {"id": "series-1", "name": "测试系列", "target_language": "zh-CN", "level": "L1", "speed": 0.82, "created_at": now, "updated_at": now})
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source video")
    project = {
        "id": "project-1", "series_id": "series-1", "title": "测试项目", "source_path": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "duration": 10.0,
        "status": "created", "current_stage": "probe", "progress": 0, "status_message": "",
        "work_dir": str(tmp_path / "work" / "project-1"), "metadata_json": dump({"probe": {"duration": 10, "video_streams": 1, "audio_streams": 1}}),
        "checkpoint_json": "{}", "created_at": now, "updated_at": now,
    }
    db.insert("projects", project)
    return settings, db, project, source


def add_waiting_job(db, project_id="project-1", stage="probe", *, checkpoint=None):
    now = now_iso()
    checkpoint = checkpoint or {
        "completed_stages": [stage], "confirmed_stages": [], "stage_revisions": {stage: 1},
        "pending_confirmation_stage": stage, "pending_confirmation_revision": 1,
    }
    db.update("projects", {"status": "awaiting_confirmation", "current_stage": stage, "checkpoint_json": dump(checkpoint), "updated_at": now}, "id = ?", (project_id,))
    db.insert("jobs", {
        "id": "job-1", "project_id": project_id, "kind": "process", "status": "awaiting_confirmation", "stage": stage,
        "progress": 0.1, "message": "等待人工确认", "from_stage": "auto", "force": 0,
        "checkpoint_json": dump(checkpoint), "created_at": now, "updated_at": now,
    })
    return checkpoint


def test_pipeline_stops_after_each_stage_and_resumes_at_next(tmp_path):
    settings, db, project, _source = make_database(tmp_path)
    job = JobQueue(settings, db).enqueue(project["id"])
    pipeline = Pipeline(settings, db)
    calls = []
    pipeline._run_stage = lambda stage, _context: calls.append(stage)
    pipeline._stage_artifact_valid = lambda _stage, _context: True

    pipeline.run(job["id"])
    saved = db.fetchone("SELECT * FROM projects WHERE id = ?", (project["id"],))
    checkpoint = load(saved["checkpoint_json"], {})
    assert calls == ["probe"]
    assert saved["status"] == "awaiting_confirmation"
    assert checkpoint["pending_confirmation_stage"] == "probe"

    confirmed = {**checkpoint, "pending_confirmation_stage": None, "pending_confirmation_revision": None, "confirmed_stages": ["probe"]}
    db.update("projects", {"checkpoint_json": dump(confirmed), "status": "queued"}, "id = ?", (project["id"],))
    db.update("jobs", {"checkpoint_json": dump(confirmed), "status": "queued"}, "id = ?", (job["id"],))
    pipeline.run(job["id"])
    saved = db.fetchone("SELECT * FROM projects WHERE id = ?", (project["id"],))
    checkpoint = load(saved["checkpoint_json"], {})
    assert calls == ["probe", "separate"]
    assert checkpoint["pending_confirmation_stage"] == "separate"
    assert db.fetchone("SELECT status FROM jobs WHERE id = ?", (job["id"],))["status"] == "awaiting_confirmation"


def test_enqueue_cannot_bypass_pending_stage(tmp_path):
    settings, db, project, _source = make_database(tmp_path)
    add_waiting_job(db)
    queue = JobQueue(settings, db)
    with pytest.raises(QueueConflict, match="不能通过普通任务入口继续"):
        queue.enqueue(project["id"], from_stage="export", force=True)


def test_segment_regeneration_can_replace_only_the_pending_synthesis_job(tmp_path):
    settings, db, project, _source = make_database(tmp_path)
    add_waiting_job(db, stage="synthesize")
    now = now_iso()
    db.insert("segments", {
        "id": "segment-1", "project_id": project["id"], "segment_index": 0, "start_sec": 0, "end_sec": 1,
        "source_text": "hello", "target_text": "你好", "created_at": now, "updated_at": now,
    })

    job = JobQueue(settings, db).enqueue(project["id"], kind="segment", checkpoint={"segment_id": "segment-1"})

    assert job["kind"] == "segment"
    assert job["stage"] == "synthesize"
    assert job["status"] == "queued"
    assert db.fetchone("SELECT status FROM jobs WHERE id = 'job-1'")["status"] == "superseded"
    checkpoint = load(db.fetchone("SELECT checkpoint_json FROM projects WHERE id = ?", (project["id"],))["checkpoint_json"], {})
    assert checkpoint.get("pending_confirmation_stage") is None


def test_duplicate_segment_regeneration_is_rejected_while_job_is_active(tmp_path):
    settings, db, project, _source = make_database(tmp_path)
    now = now_iso()
    db.insert("segments", {
        "id": "segment-1", "project_id": project["id"], "segment_index": 0, "start_sec": 0, "end_sec": 1,
        "source_text": "hello", "target_text": "你好", "created_at": now, "updated_at": now,
    })
    queue = JobQueue(settings, db)
    first = queue.enqueue(project["id"], kind="segment", checkpoint={"segment_id": "segment-1"})

    with pytest.raises(QueueConflict, match="不能重复提交句级配音"):
        queue.enqueue(project["id"], kind="segment", checkpoint={"segment_id": "segment-1"})

    assert db.fetchone("SELECT COUNT(*) AS count FROM jobs WHERE project_id = ? AND status IN ('queued', 'running')", (project["id"],))["count"] == 1


def test_successful_stage_retry_resolves_severity_only_blocker(tmp_path):
    settings, db, project, _source = make_database(tmp_path)
    now = now_iso()
    db.insert("anomalies", {
        "id": "anomaly-legacy", "project_id": project["id"], "kind": "TTS_OVER_DURATION", "severity": "blocking", "blocking": 0,
        "message": "历史阻断记录", "details_json": dump({"stage": "synthesize"}), "resolved": 0, "created_at": now, "updated_at": now,
    })

    Pipeline(settings, db)._resolve_stage_anomalies(project["id"], "synthesize")

    assert db.fetchone("SELECT resolved FROM anomalies WHERE id = 'anomaly-legacy'")["resolved"] == 1


def test_confirm_advances_once_and_records_audit(tmp_path):
    settings, db, project, _source = make_database(tmp_path)
    add_waiting_job(db)
    app = create_app(settings, db)
    app.state.runtime.queue._run = lambda: None
    with TestClient(app) as client:
        result = client.post(f"/api/projects/{project['id']}/stages/probe/confirm", json={"revision": 1, "accepted_warning_ids": []})
        assert result.status_code == 200
        assert result.json()["next_stage"] == "separate"
        assert result.json()["idempotent"] is False

        duplicate = client.post(f"/api/projects/{project['id']}/stages/probe/confirm", json={"revision": 1, "accepted_warning_ids": []})
        assert duplicate.status_code == 200
        assert duplicate.json()["idempotent"] is True

    saved = db.fetchone("SELECT * FROM projects WHERE id = ?", (project["id"],))
    checkpoint = load(saved["checkpoint_json"], {})
    assert checkpoint["confirmed_stages"] == ["probe"]
    assert checkpoint.get("pending_confirmation_stage") is None
    assert len(checkpoint["confirmation_history"]) == 1
    assert db.fetchone("SELECT stage, status FROM jobs WHERE id = 'job-1'") == {"stage": "separate", "status": "queued"}


def test_confirm_rejects_open_blockers_and_stale_revision(tmp_path):
    settings, db, project, _source = make_database(tmp_path)
    add_waiting_job(db)
    now = now_iso()
    db.insert("anomalies", {"id": "anomaly-1", "project_id": project["id"], "kind": "TEST_BLOCKER", "severity": "blocking", "blocking": 0, "message": "请复核", "resolved": 0, "created_at": now, "updated_at": now})
    app = create_app(settings, db)
    app.state.runtime.queue._run = lambda: None
    with TestClient(app) as client:
        blocker = client.post(f"/api/projects/{project['id']}/stages/probe/confirm", json={"revision": 1, "accepted_warning_ids": []})
        assert blocker.status_code == 409
        assert blocker.json()["error"]["code"] == "BLOCKING_ANOMALIES_OPEN"
        db.update("anomalies", {"resolved": 1}, "id = ?", ("anomaly-1",))
        stale = client.post(f"/api/projects/{project['id']}/stages/probe/confirm", json={"revision": 7, "accepted_warning_ids": []})
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "STAGE_REVISION_CONFLICT"


def test_later_stage_blocker_does_not_lock_earlier_confirmation(tmp_path):
    settings, db, project, _source = make_database(tmp_path)
    add_waiting_job(db, stage="probe")
    now = now_iso()
    db.insert("anomalies", {"id": "anomaly-synth", "project_id": project["id"], "kind": "TTS_OVER_DURATION", "severity": "blocking", "blocking": 1, "message": "配音过长", "details_json": dump({"stage": "synthesize"}), "resolved": 0, "created_at": now, "updated_at": now})
    app = create_app(settings, db)
    app.state.runtime.queue._run = lambda: None
    with TestClient(app) as client:
        result = client.post(f"/api/projects/{project['id']}/stages/probe/confirm", json={"revision": 1, "accepted_warning_ids": []})
        assert result.status_code == 200
        assert result.json()["next_stage"] == "separate"


def test_confirmation_only_requires_warnings_for_current_stage(tmp_path):
    settings, db, project, _source = make_database(tmp_path)
    add_waiting_job(db, stage="probe")
    now = now_iso()
    db.insert("anomalies", {"id": "warning-synth", "project_id": project["id"], "kind": "CROSS_EPISODE_VOICE_UNVERIFIED", "severity": "warning", "blocking": 0, "message": "配音映射待验证", "details_json": dump({"stage": "characters"}), "resolved": 0, "created_at": now, "updated_at": now})
    app = create_app(settings, db)
    app.state.runtime.queue._run = lambda: None
    with TestClient(app) as client:
        result = client.post(f"/api/projects/{project['id']}/stages/probe/confirm", json={"revision": 1, "accepted_warning_ids": []})
        assert result.status_code == 200


def test_retry_without_stage_uses_current_pending_stage(tmp_path):
    settings, db, project, _source = make_database(tmp_path)
    add_waiting_job(db, stage="probe")
    app = create_app(settings, db)
    app.state.runtime.queue._run = lambda: None

    with TestClient(app) as client:
        result = client.post("/api/jobs/job-1/retry")

    assert result.status_code == 202
    assert result.json()["stage"] == "probe"
    assert db.fetchone("SELECT status FROM jobs WHERE id = 'job-1'")["status"] == "superseded"


def test_retry_rejects_a_job_already_superseded_by_segment_regeneration(tmp_path):
    settings, db, project, _source = make_database(tmp_path)
    add_waiting_job(db, stage="synthesize")
    db.update("jobs", {"status": "superseded"}, "id = ?", ("job-1",))
    checkpoint = load(db.fetchone("SELECT checkpoint_json FROM projects WHERE id = ?", (project["id"],))["checkpoint_json"], {})
    checkpoint.pop("pending_confirmation_stage", None)
    db.update("projects", {"checkpoint_json": dump(checkpoint)}, "id = ?", (project["id"],))

    with pytest.raises(QueueConflict, match="已被新任务替代"):
        JobQueue(settings, db).retry("job-1")


def test_final_confirmation_binds_exact_output_hash_and_unlocks_export(tmp_path):
    settings, db, project, source = make_database(tmp_path)
    work = tmp_path / "work" / project["id"]
    work.mkdir(parents=True)
    output = work / "candidate.mp4"
    output.write_bytes(b"candidate video")
    output_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    completed_stages = ["probe", "separate", "transcribe", "diarize", "characters", "rewrite", "synthesize", "mix", "subtitle", "export"]
    confirmed = completed_stages[:-1]
    checkpoint = {
        "completed_stages": completed_stages, "confirmed_stages": confirmed,
        "stage_revisions": {"export": 1}, "pending_confirmation_stage": "export", "pending_confirmation_revision": 1,
        "confirmation_history": [{"stage": "characters", "revision": 1, "valid": True, "accepted_warning_ids": ["warning-character"]}],
    }
    db.update("projects", {"checkpoint_json": dump(checkpoint), "status": "awaiting_confirmation", "current_stage": "export", "output_path": str(output), "output_sha256": output_sha, "metadata_json": dump({"probe": {"duration": 10}, "mixed": str(work / "mix.wav"), "subtitle_path": str(work / "target.srt")})}, "id = ?", (project["id"],))
    (work / "mix.wav").write_bytes(b"mix")
    (work / "target.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
    now = now_iso()
    db.insert("jobs", {"id": "job-1", "project_id": project["id"], "kind": "process", "status": "awaiting_confirmation", "stage": "export", "progress": 1, "message": "待验收", "from_stage": "auto", "force": 0, "checkpoint_json": dump(checkpoint), "created_at": now, "updated_at": now})
    db.insert("anomalies", {"id": "warning-character", "project_id": project["id"], "kind": "TEST_WARNING", "severity": "warning", "blocking": 0, "message": "角色待复核", "details_json": dump({"stage": "characters"}), "resolved": 0, "created_at": now, "updated_at": now})
    app = create_app(settings, db)
    app.state.runtime.queue._run = lambda: None
    with TestClient(app) as client:
        preview = client.get(f"/api/projects/{project['id']}/preview")
        assert preview.json()["output_url"].endswith("/media/candidate")
        candidate = client.get(preview.json()["output_url"])
        assert candidate.status_code == 200
        locked_output = client.get(f"/api/projects/{project['id']}/media/output")
        assert locked_output.status_code == 409
        assert locked_output.json()["error"]["code"] == "FINAL_CONFIRMATION_REQUIRED"
        wrong = client.post(f"/api/projects/{project['id']}/stages/export/confirm", json={"revision": 1, "artifact_sha256": "wrong", "accepted_warning_ids": []})
        assert wrong.status_code == 409
        assert wrong.json()["error"]["code"] == "OUTPUT_REVISION_CONFLICT"
        result = client.post(f"/api/projects/{project['id']}/stages/export/confirm", json={"revision": 1, "artifact_sha256": output_sha, "accepted_warning_ids": []})
        assert result.status_code == 200
        assert result.json()["status"] == "completed_with_warnings"
        final_media = client.get(f"/api/projects/{project['id']}/media/output")
        assert final_media.status_code == 200
        exported = client.post(f"/api/projects/{project['id']}/export", json={})
        assert exported.status_code == 200
        assert exported.json()["sha256"] == output_sha
        output.write_bytes(b"tampered candidate")
        changed_media = client.get(f"/api/projects/{project['id']}/media/output")
        changed_export = client.post(f"/api/projects/{project['id']}/export", json={})
        assert changed_media.status_code == changed_export.status_code == 409
        assert changed_export.json()["error"]["code"] == "OUTPUT_REVISION_CONFLICT"
    final = load(db.fetchone("SELECT checkpoint_json FROM projects WHERE id = ?", (project["id"],))["checkpoint_json"], {})
    assert final["confirmation_history"][-1]["artifact_sha256"] == output_sha
