#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Stress detached run completion against aggressive RunStore.get_run polling.

This is an opt-in diagnostic harness, not a deterministic contract test. It reuses
the production CLI e2e fixture and records SQLite extended errors plus WAL sidecar
and process state at the point of failure.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import time
import unittest
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from types import MethodType
from unittest import mock

import test_run_cli as cli_fixture
from waystone.runs import store as store_module
from waystone.runs.store import RunStore, StateDatabaseError


class _Recorder:
    def __init__(self) -> None:
        self.polls = 0
        self.direct_reads = 0
        self.detached_runs = 0
        self.failures: list[dict[str, object]] = []
        self.trace: deque[str] = deque(maxlen=32)
        self.root: Path | None = None
        self.run_id: str | None = None

    def set_run(self, root: Path, run_id: str) -> None:
        self.root = root
        self.run_id = run_id
        self.detached_runs += 1

    def record_trace(self, statement: str) -> None:
        self.trace.append(statement)

    @staticmethod
    def _path_state(path: Path) -> dict[str, object]:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return {"exists": False}
        except OSError as error:
            return {"error": f"{type(error).__name__}: {error}"}
        return {
            "exists": True,
            "inode": info.st_ino,
            "mode": oct(info.st_mode),
            "mtime_ns": info.st_mtime_ns,
            "size": info.st_size,
        }

    def _process_state(self) -> list[str]:
        if self.root is None:
            return []
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,state=,command="],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            return [f"unavailable: {type(error).__name__}: {error}"]
        root = str(self.root)
        return [
            line.strip() for line in result.stdout.splitlines()
            if root in line
        ]

    def _open_files(self, database: Path) -> list[str]:
        executable = shutil.which("lsof")
        if executable is None:
            return ["unavailable: lsof not installed"]
        try:
            result = subprocess.run(
                [executable, str(database), f"{database}-wal", f"{database}-shm"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as error:
            return [f"unavailable: {type(error).__name__}: {error}"]
        return result.stdout.splitlines()

    @staticmethod
    def _sqlite_cause(error: BaseException) -> BaseException | None:
        candidate: BaseException | None = error
        while candidate is not None:
            if isinstance(candidate, sqlite3.DatabaseError):
                return candidate
            candidate = candidate.__cause__
        return None

    def capture(self, error: BaseException, source: str) -> None:
        database = None if self.root is None else self.root / ".waystone" / "state.db"
        sqlite_error = self._sqlite_cause(error)
        payload: dict[str, object] = {
            "source": source,
            "elapsed_ns": time.monotonic_ns(),
            "run_id": self.run_id,
            "error_type": type(error).__name__,
            "error": str(error),
            "sqlite_errorcode": getattr(sqlite_error, "sqlite_errorcode", None),
            "sqlite_errorname": getattr(sqlite_error, "sqlite_errorname", None),
            "sqlite_message": None if sqlite_error is None else str(sqlite_error),
            "recent_sql": list(self.trace),
            "processes": self._process_state(),
        }
        if database is not None:
            payload["files"] = {
                path.name: self._path_state(path)
                for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
            }
            payload["lsof"] = self._open_files(database)
        self.failures.append(payload)


def _resume_until_closeout(case, output, run_id: str, recorder: _Recorder) -> None:
    recorder.set_run(case.root, run_id)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        output.seek(0)
        output.truncate(0)
        recorder.polls += 1
        result = cli_fixture.run_group.main(["resume", run_id])
        try:
            with RunStore.open(case.root) as store:
                store.get_run(run_id)
            recorder.direct_reads += 1
        except StateDatabaseError:
            # The patched read transaction captured the original sqlite exception
            # before RunStore wrapped it and closed the connection.
            pass
        if result == 0 and "run_closeout_ready" in output.getvalue():
            return
    case.fail(f"run {run_id} did not reach closeout-ready: {output.getvalue()}")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.rounds < 1:
        parser.error("--rounds must be positive")
    return arguments


def main() -> int:
    arguments = _arguments()
    recorder = _Recorder()
    original_connect = store_module._connect  # noqa: SLF001 - diagnostic seam
    original_read_transaction = store_module._read_transaction  # noqa: SLF001

    def traced_connect(database_path: Path) -> sqlite3.Connection:
        connection = original_connect(database_path)
        connection.set_trace_callback(recorder.record_trace)
        return connection

    @contextmanager
    def traced_read_transaction(connection: sqlite3.Connection):
        try:
            with original_read_transaction(connection):
                yield
        except sqlite3.DatabaseError as error:
            recorder.capture(error, "read-transaction-before-store-close")
            raise

    started = time.monotonic()
    test_failures: list[dict[str, object]] = []
    with mock.patch.object(store_module, "_connect", side_effect=traced_connect), \
            mock.patch.object(
                store_module, "_read_transaction", side_effect=traced_read_transaction):
        for round_number in range(1, arguments.rounds + 1):
            result = unittest.TestResult()
            case = cli_fixture.RunCliTests(
                methodName="test_e2e6_public_evaluate_then_promote_executes_frozen_full_chain")
            case._resume_until_closeout = MethodType(  # noqa: SLF001 - stress override
                lambda bound, output, run_id: _resume_until_closeout(
                    bound, output, run_id, recorder),
                case,
            )
            case.run(result)
            if result.failures or result.errors:
                test_failures.append({
                    "round": round_number,
                    "failures": [text for _case, text in result.failures],
                    "errors": [text for _case, text in result.errors],
                })

    report = {
        "conditions": {
            "poll_sleep_s": 0,
            "scenario": "detached explore, evaluate, and promote fixture runs",
            "sqlite_version": sqlite3.sqlite_version,
        },
        "direct_reads": recorder.direct_reads,
        "elapsed_s": round(time.monotonic() - started, 3),
        "failures": recorder.failures,
        "polls": recorder.polls,
        "rounds": arguments.rounds,
        "detached_runs": recorder.detached_runs,
        "test_failures": test_failures,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "detached_runs": recorder.detached_runs,
        "direct_reads": recorder.direct_reads,
        "elapsed_s": report["elapsed_s"],
        "sqlite_failures": len(recorder.failures),
        "polls": recorder.polls,
        "rounds": arguments.rounds,
        "test_failures": len(test_failures),
    }, sort_keys=True))
    return 1 if test_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
