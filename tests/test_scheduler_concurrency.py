"""Tests for scheduler file safety — fcntl locking and atomic writes.

Covers:
- Concurrent writer stress test (multiprocessing)
- Atomic write crash safety (partial write never visible)
- Lock contention (shared vs exclusive)
- _locked_read / _atomic_write correctness
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from aya.scheduler import (
    _atomic_write,
    _file_lock,
    _locked_read,
)


@pytest.fixture(autouse=True)
def _isolate_scheduler(tmp_path, monkeypatch):
    """Point scheduler at a temp directory so tests don't touch real data."""
    scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
    alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
    scheduler_file.parent.mkdir(parents=True)
    scheduler_file.write_text(json.dumps({"items": []}))
    alerts_file.write_text(json.dumps({"alerts": []}))

    monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
    monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)


# ── Atomic write ─────────────────────────────────────────────────────────────


class TestAtomicWrite:
    def test_basic_write(self, tmp_path):
        path = tmp_path / "test.json"
        _atomic_write(path, {"key": "value"})
        data = json.loads(path.read_text())
        assert data == {"key": "value"}

    def test_overwrites_existing(self, tmp_path):
        path = tmp_path / "test.json"
        path.write_text('{"old": true}')
        _atomic_write(path, {"new": True})
        data = json.loads(path.read_text())
        assert data == {"new": True}
        assert "old" not in data

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "deep" / "test.json"
        _atomic_write(path, {"nested": True})
        assert path.exists()
        assert json.loads(path.read_text()) == {"nested": True}

    def test_no_tmp_files_remain(self, tmp_path):
        path = tmp_path / "test.json"
        _atomic_write(path, {"clean": True})
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Leftover tmp files: {tmp_files}"

    def test_file_is_valid_json_or_absent_never_partial(self, tmp_path):
        """After atomic write, file is always complete valid JSON."""
        path = tmp_path / "test.json"
        for i in range(50):
            _atomic_write(path, {"iteration": i, "data": "x" * 1000})
            content = path.read_text()
            data = json.loads(content)  # Must not raise
            assert data["iteration"] == i


# ── Locked read ──────────────────────────────────────────────────────────────


class TestLockedRead:
    def test_read_missing_file(self, tmp_path):
        result = _locked_read(tmp_path / "nonexistent.json")
        assert result is None

    def test_read_corrupt_file(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("not json {{{")
        # Need the lock file location to be set up
        (tmp_path / "assistant" / "memory").mkdir(parents=True, exist_ok=True)
        result = _locked_read(path)
        assert result is None

    def test_read_valid_file(self, tmp_path):
        path = tmp_path / "assistant" / "memory" / "test.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"items": [{"id": "test"}]}')
        result = _locked_read(path)
        assert result == {"items": [{"id": "test"}]}


# ── File lock ────────────────────────────────────────────────────────────────


class TestFileLock:
    def test_exclusive_lock_blocks_exclusive(self, tmp_path, monkeypatch):
        """Two exclusive locks on the same file — second must wait."""
        import fcntl

        lock_path = tmp_path / "assistant" / "memory" / ".scheduler.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Acquire the lock manually
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)

        # Try non-blocking from another fd — should fail
        fd2 = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Cleanup
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        os.close(fd2)

    def test_shared_lock_allows_shared(self, tmp_path, monkeypatch):
        """Multiple shared locks can coexist."""
        import fcntl

        lock_path = tmp_path / "assistant" / "memory" / ".scheduler.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        fd1 = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
        fd2 = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd1, fcntl.LOCK_SH)
        fcntl.flock(fd2, fcntl.LOCK_SH | fcntl.LOCK_NB)  # Should not raise

        fcntl.flock(fd1, fcntl.LOCK_UN)
        fcntl.flock(fd2, fcntl.LOCK_UN)
        os.close(fd1)
        os.close(fd2)

    def test_context_manager_releases_lock(self):
        """Lock is released after context manager exits."""
        import fcntl

        with _file_lock():
            pass  # Lock held here

        # Lock should be released — verify by acquiring non-blocking
        from aya.scheduler import _lock_file

        lock_path = _lock_file()
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Should not raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ── Concurrent writers ───────────────────────────────────────────────────────


def _worker_add_items(scheduler_file_str: str, alerts_file_str: str, worker_id: int, count: int):
    """Worker process: add `count` items using the atomic read-modify-write pattern."""
    import aya.scheduler
    from aya.scheduler import _atomic_write, _file_lock, _load_items_unlocked, _scheduler_file

    scheduler_file = Path(scheduler_file_str)
    alerts_file = Path(alerts_file_str)
    aya.scheduler.SCHEDULER_FILE = scheduler_file
    aya.scheduler.ALERTS_FILE = alerts_file

    for i in range(count):
        with _file_lock():
            items = _load_items_unlocked()
            items.append(
                {"id": f"worker-{worker_id}-item-{i}", "type": "reminder", "status": "pending"}
            )
            _atomic_write(_scheduler_file(), {"items": items})


class TestConcurrentWriters:
    def test_no_items_lost_under_contention(self, tmp_path, monkeypatch):
        """10 workers each adding 10 items — all 100 must be present at end."""
        scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
        alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"

        num_workers = 10
        items_per_worker = 10

        processes = []
        for w in range(num_workers):
            p = multiprocessing.Process(
                target=_worker_add_items,
                args=(str(scheduler_file), str(alerts_file), w, items_per_worker),
            )
            processes.append(p)

        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=30)
        for p in processes:
            if p.is_alive():
                p.terminate()
                pytest.fail(f"Worker process {p.pid} did not finish within timeout")
            assert p.exitcode == 0, f"Worker process exited with code {p.exitcode}"

        # Verify all items present
        data = json.loads(scheduler_file.read_text())
        items = data["items"]
        assert len(items) == num_workers * items_per_worker, (
            f"Expected {num_workers * items_per_worker} items, got {len(items)}"
        )

        # Verify no duplicates
        ids = [i["id"] for i in items]
        assert len(set(ids)) == len(ids), "Duplicate IDs found"

    def test_file_always_valid_json(self, tmp_path, monkeypatch):
        """During concurrent writes, file is never partial/corrupt."""
        scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
        alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"

        num_workers = 5
        items_per_worker = 20
        stop_event = multiprocessing.Event()

        def _reader(path_str: str, error_list, stop):
            """Continuously read and parse the file, report any corruption."""
            path = Path(path_str)
            while not stop.is_set():
                try:
                    content = path.read_text()
                    json.loads(content)
                except json.JSONDecodeError as e:
                    error_list.append(f"Corrupt JSON: {e}")
                except FileNotFoundError:
                    pass  # File may not exist between rename
                time.sleep(0.001)

        manager = multiprocessing.Manager()
        errors = manager.list()

        # Start reader
        reader = multiprocessing.Process(
            target=_reader, args=(str(scheduler_file), errors, stop_event)
        )
        reader.start()

        # Start writers
        writers = []
        for w in range(num_workers):
            p = multiprocessing.Process(
                target=_worker_add_items,
                args=(str(scheduler_file), str(alerts_file), w, items_per_worker),
            )
            writers.append(p)

        for p in writers:
            p.start()
        for p in writers:
            p.join(timeout=30)
        for p in writers:
            if p.is_alive():
                p.terminate()
                pytest.fail(f"Writer process {p.pid} did not finish within timeout")
            assert p.exitcode == 0, f"Writer process exited with code {p.exitcode}"

        stop_event.set()
        reader.join(timeout=5)
        if reader.is_alive():
            reader.terminate()

        assert list(errors) == [], f"File corruption detected during writes: {errors}"
