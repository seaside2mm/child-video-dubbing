from dataclasses import replace
import subprocess
import sys

import pytest

from backend.app.config import settings as default_settings
from backend.app.db import Database
from backend.app.queue import JobQueue


def make_queue(tmp_path):
    settings = replace(default_settings, db_path=tmp_path / "test.sqlite3", work_dir=tmp_path)
    return JobQueue(settings, Database(settings.db_path))


def test_second_worker_refused_before_database_recovery(tmp_path):
    first, second = make_queue(tmp_path), make_queue(tmp_path)
    first.start()
    try:
        with pytest.raises(RuntimeError, match="worker"):
            second.start()
        assert second._thread is None
    finally:
        first.stop()
    second.start()
    second.stop()


def test_process_lock_blocks_then_releases(tmp_path):
    queue = make_queue(tmp_path)
    code = '''
import sys
from pathlib import Path
from dataclasses import replace
from backend.app.config import settings
from backend.app.queue import JobQueue
from backend.app.db import Database
s = replace(settings, db_path=Path(sys.argv[1]))
q = JobQueue(s, Database(s.db_path))
try:
    q._acquire_lease()
except RuntimeError:
    sys.exit(23)
q._release_lease()
'''
    queue._acquire_lease()
    try:
        blocked = subprocess.run([sys.executable, "-c", code, str(queue.settings.db_path)], capture_output=True)
        assert blocked.returncode == 23, blocked.stderr.decode(errors="replace")
    finally:
        queue._release_lease()
    released = subprocess.run([sys.executable, "-c", code, str(queue.settings.db_path)], capture_output=True)
    assert released.returncode == 0, released.stderr.decode(errors="replace")
