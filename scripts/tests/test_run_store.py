#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Contract tests for the M1-B transactional runtime store kernel."""
from __future__ import annotations

import hashlib
import inspect
import os
import re
import sqlite3
import stat
import sys
import tempfile
import threading
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
_WAYSTONE_PRELOADED = "waystone" in sys.modules
sys.path.insert(0, str(ROOT))
try:
    from waystone.runs import artifacts as artifacts_module  # noqa: E402
    from waystone.runs import store as store_module  # noqa: E402
    from waystone.runs.artifacts import (  # noqa: E402
        ArtifactIntegrityError,
        ArtifactNotFoundError,
        ArtifactReference,
        ArtifactReferenceKind,
        ArtifactStore,
        DanglingArtifactReferenceError,
    )
    from waystone.runs.store import (  # noqa: E402
        AppendOnlyConflict,
        CorruptRuntimeRecordError,
        EntityKind,
        EntityVersionConflict,
        FilesystemInfo,
        RunStore,
        StatePathSymlinkError,
        TransitionReason,
        UninitializedProjectError,
        UnsafeStatePermissionsError,
        UnsupportedSchemaVersionError,
        UnsupportedStateFilesystemError,
    )
finally:
    sys.path.pop(0)
    if not _WAYSTONE_PRELOADED:
        sys.modules.pop("waystone", None)
del _WAYSTONE_PRELOADED


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class _StoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.base = Path(self._temporary_directory.name)

    def project(self, name: str = "project") -> Path:
        root = self.base / name
        root.mkdir()
        (root / ".waystone.yml").write_text("version: 1\nproject: fixture\n", encoding="utf-8")
        return root

    @contextmanager
    def supported_filesystem(self):
        with mock.patch.object(
                store_module, "_probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)):
            yield

    def open_store(self, root: Path) -> RunStore:
        with self.supported_filesystem():
            opened = RunStore.open(root)
        self.addCleanup(opened.close)
        return opened

    def database(self, root: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(root / ".waystone" / "state.db", isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


class RunStoreTests(_StoreFixture):
    def test_schema_v2_has_required_authority_audit_telemetry_reference_and_cache_tables(self):
        root = self.project()
        store = self.open_store(root)

        rows = store._connection.execute(  # noqa: SLF001 - schema contract fixture
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        tables = {row[0] for row in rows}
        self.assertTrue({
            "schema_version", "runs", "jobs", "attempts", "actions", "transitions",
            "leases", "action_runtime", "artifacts", "cache",
        }.issubset(tables))
        for table in ("runs", "jobs", "attempts", "actions"):
            columns = {
                row[1] for row in store._connection.execute(f"PRAGMA table_info({table})")
            }
            self.assertIn("state", columns)
            self.assertIn("version", columns)
            self.assertIn("record_digest", columns)
        self.assertEqual(
            store._connection.execute("SELECT version FROM schema_version").fetchone()[0], 2)
        self.assertEqual(
            store._connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertGreater(
            store._connection.execute("PRAGMA busy_timeout").fetchone()[0], 0)
        self.assertEqual((root / ".waystone" / ".gitignore").read_bytes(), b"*\n")
        identity_columns = {
            "runs": "run_id", "jobs": "job_id", "attempts": "attempt_id",
            "actions": "action_id", "artifacts": "reference_id",
        }
        for table, identity in identity_columns.items():
            columns = {
                row[1]: row for row in store._connection.execute(f"PRAGMA table_info({table})")
            }
            self.assertEqual(columns[identity][3], 1, f"{table}.{identity} must be NOT NULL")

    def test_concurrent_first_open_bootstraps_one_idempotent_wal_schema(self):
        root = self.project()
        barrier = threading.Barrier(2)

        def open_together() -> RunStore:
            barrier.wait()
            return RunStore.open(root)

        with self.supported_filesystem(), ThreadPoolExecutor(max_workers=2) as executor:
            stores = list(executor.map(lambda _: open_together(), range(2)))
        for store in stores:
            self.addCleanup(store.close)
            self.assertEqual(store.schema_version, 2)
            self.assertEqual(
                store._connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual((root / ".waystone" / ".gitignore").read_bytes(), b"*\n")

    def test_missing_wal_sidecars_remain_sqlite_owned_until_connect(self):
        root = self.project()
        original_connect = store_module._connect  # noqa: SLF001 - open boundary probe
        observed: dict[str, bool] = {}

        def inspect_connect(database_path: Path) -> sqlite3.Connection:
            observed["database"] = database_path.exists()
            observed["wal"] = Path(f"{database_path}-wal").exists()
            observed["shm"] = Path(f"{database_path}-shm").exists()
            return original_connect(database_path)

        with self.supported_filesystem(), mock.patch.object(
                store_module, "_connect", side_effect=inspect_connect):
            store = RunStore.open(root)
        self.addCleanup(store.close)

        self.assertEqual(observed, {"database": True, "wal": False, "shm": False})

    def test_same_store_concurrent_cas_is_serialized_to_one_typed_conflict(self):
        root = self.project()
        store = self.open_store(root)
        run = store.create_run()
        barrier = threading.Barrier(2)

        def compete(state: str):
            barrier.wait()
            try:
                return store.record_transition(
                    EntityKind.RUN,
                    run.entity_id,
                    expected_version=0,
                    next_state=state,
                    reason=TransitionReason.EFFECT_OBSERVED,
                )
            except EntityVersionConflict as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(compete, ("first", "second")))
        self.assertEqual(sum(isinstance(result, EntityVersionConflict) for result in results), 1)
        self.assertEqual(store.get_run(run.entity_id).version, 1)

    def test_schema_bootstrap_is_transactional_idempotent_and_refuses_newer_version(self):
        root = self.project()

        def fail_after_ddl(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TABLE migration_must_rollback(value TEXT)")
            raise RuntimeError("injected migration failure")

        with self.supported_filesystem(), mock.patch.dict(
                store_module._MIGRATIONS, {2: fail_after_ddl}, clear=True):  # noqa: SLF001
            with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                RunStore.open(root)

        connection = self.database(root)
        self.assertIsNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'migration_must_rollback'").fetchone())

        store = self.open_store(root)
        store.close()
        reopened = self.open_store(root)
        self.assertEqual(reopened.schema_version, 2)
        reopened.close()

        connection.execute("UPDATE schema_version SET version = 1")
        with self.supported_filesystem(), mock.patch.object(
                store_module, "_enable_wal",
                side_effect=AssertionError("v1 schema must be refused before journal mutation")):
            with self.assertRaises(UnsupportedSchemaVersionError) as raised:
                RunStore.open(root)
        self.assertEqual(raised.exception.code, "schema_version_unsupported")
        self.assertEqual((raised.exception.found, raised.exception.supported), (1, 2))

        fake = mock.Mock()
        fake.in_transaction = True
        fake.commit.side_effect = RuntimeError("commit fault")
        with self.assertRaisesRegex(RuntimeError, "commit fault"):
            with store_module._immediate_transaction(fake):  # noqa: SLF001
                pass
        fake.rollback.assert_called_once()

    def test_jw_gpt_014_transition_current_state_and_artifact_reference_are_one_transaction(self):
        """JW-GPT-014: observation and recording cannot split across transaction/API paths."""
        root = self.project()
        store = self.open_store(root)
        run = store.create_run()
        reference = ArtifactReference(
            reference_id="evidence-1",
            kind=ArtifactReferenceKind.EVIDENCE,
            digest=_sha256(b"evidence"),
            size=len(b"evidence"),
        )

        public_methods = {
            name for name, value in inspect.getmembers(RunStore, inspect.isfunction)
            if not name.startswith("_")
        }
        self.assertEqual(public_methods, {
            "close", "create_action", "create_attempt", "create_job", "create_run",
            "get_artifact_reference", "get_entity", "get_run", "provide_context",
            "record_context_request", "record_transition",
        })

        def crash(stage: str) -> None:
            if stage == "after_transition_insert":
                raise RuntimeError("injected crash")

        with mock.patch.object(store, "_transaction_fault_point", side_effect=crash):  # noqa: SLF001
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                store.record_transition(
                    EntityKind.RUN,
                    run.entity_id,
                    expected_version=0,
                    next_state="executing",
                    reason=TransitionReason.EFFECT_OBSERVED,
                    evidence_digest=reference.digest,
                    artifact_references=(reference,),
                )

        unchanged = store.get_run(run.entity_id)
        self.assertEqual((unchanged.state, unchanged.version), ("created", 0))
        self.assertEqual(store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM transitions WHERE run_id = ?", (run.entity_id,)
        ).fetchone()[0], 1)
        self.assertEqual(store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM artifacts WHERE run_id = ?", (run.entity_id,)
        ).fetchone()[0], 0)

        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)  # noqa: SLF001
        try:
            changed = store.record_transition(
                EntityKind.RUN,
                run.entity_id,
                expected_version=0,
                next_state="executing",
                reason=TransitionReason.EFFECT_OBSERVED,
                evidence_digest=reference.digest,
                artifact_references=(reference,),
            )
        finally:
            store._connection.set_trace_callback(None)  # noqa: SLF001
        self.assertEqual((changed.state, changed.version), ("executing", 1))
        self.assertEqual(store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM transitions WHERE run_id = ?", (run.entity_id,)
        ).fetchone()[0], 2)
        self.assertEqual(store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM artifacts WHERE run_id = ?", (run.entity_id,)
        ).fetchone()[0], 1)
        normalized = [statement.strip().upper() for statement in statements]
        begin = next(index for index, statement in enumerate(normalized)
                     if statement == "BEGIN IMMEDIATE")
        transition = next(index for index, statement in enumerate(normalized)
                          if statement.startswith("INSERT INTO TRANSITIONS"))
        current = next(index for index, statement in enumerate(normalized)
                       if statement.startswith("UPDATE RUNS SET"))
        artifact = next(index for index, statement in enumerate(normalized)
                        if statement.startswith("INSERT INTO ARTIFACTS"))
        commit = next(index for index, statement in enumerate(normalized)
                      if statement == "COMMIT")
        self.assertLess(begin, transition)
        self.assertLess(transition, current)
        self.assertLess(current, artifact)
        self.assertLess(artifact, commit)

    def test_stale_concurrent_cas_is_typed_conflict_and_loser_has_zero_partial_writes(self):
        root = self.project()
        first = self.open_store(root)
        run = first.create_run()
        second = self.open_store(root)
        barrier = threading.Barrier(2)

        def compete(store: RunStore, suffix: str):
            barrier.wait()
            reference = ArtifactReference(
                reference_id=f"evidence-{suffix}",
                kind=ArtifactReferenceKind.EVIDENCE,
                digest=_sha256(suffix.encode()),
                size=len(suffix),
            )
            try:
                return store.record_transition(
                    EntityKind.RUN,
                    run.entity_id,
                    expected_version=0,
                    next_state=f"state-{suffix}",
                    reason=TransitionReason.EFFECT_OBSERVED,
                    evidence_digest=reference.digest,
                    artifact_references=(reference,),
                )
            except EntityVersionConflict as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda pair: compete(*pair), ((first, "a"), (second, "b"))))

        self.assertEqual(sum(isinstance(value, EntityVersionConflict) for value in results), 1)
        self.assertEqual(sum(not isinstance(value, EntityVersionConflict) for value in results), 1)
        self.assertEqual(first.get_run(run.entity_id).version, 1)
        self.assertEqual(first._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM transitions WHERE run_id = ?", (run.entity_id,)
        ).fetchone()[0], 2)
        self.assertEqual(first._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM artifacts WHERE run_id = ?", (run.entity_id,)
        ).fetchone()[0], 1)

    def test_transitions_reject_update_and_delete_in_code_and_persistent_triggers(self):
        root = self.project()
        store = self.open_store(root)
        run = store.create_run()

        with self.assertRaises(sqlite3.DatabaseError):
            store._connection.execute(  # noqa: SLF001 - code-layer authorizer assertion
                "UPDATE transitions SET reason = reason WHERE run_id = ?", (run.entity_id,))

        external = self.database(root)
        with self.assertRaises(sqlite3.IntegrityError):
            external.execute(
                "UPDATE transitions SET reason = reason WHERE run_id = ?", (run.entity_id,))
        external.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            external.execute("DELETE FROM transitions WHERE run_id = ?", (run.entity_id,))

    def test_pc18_attempt_evidence_and_decision_identities_are_append_only(self):
        root = self.project()
        store = self.open_store(root)
        run = store.create_run()
        store.create_job(run.entity_id, "job-1")
        attempt = store.create_attempt(run.entity_id, "job-1", "attempt-1")

        with self.assertRaises(AppendOnlyConflict):
            store.create_attempt(run.entity_id, "job-1", "attempt-1")
        self.assertEqual(store.get_entity(EntityKind.ATTEMPT, "attempt-1"), attempt)

        evidence = ArtifactReference(
            reference_id="evidence-1",
            kind=ArtifactReferenceKind.EVIDENCE,
            digest=_sha256(b"evidence-v1"),
            size=len(b"evidence-v1"),
        )
        decision = ArtifactReference(
            reference_id="decision-1",
            kind=ArtifactReferenceKind.DECISION,
            digest=_sha256(b"accept"),
            size=len(b"accept"),
        )
        store.record_transition(
            EntityKind.ATTEMPT,
            attempt.entity_id,
            expected_version=0,
            next_state="deciding",
            reason=TransitionReason.EFFECT_OBSERVED,
            evidence_digest=evidence.digest,
            artifact_references=(evidence, decision),
        )

        replacement = ArtifactReference(
            reference_id="evidence-1",
            kind=ArtifactReferenceKind.EVIDENCE,
            digest=_sha256(b"replacement"),
            size=len(b"replacement"),
        )
        with self.assertRaises(AppendOnlyConflict):
            store.record_transition(
                EntityKind.ATTEMPT,
                attempt.entity_id,
                expected_version=1,
                next_state="accepted",
                reason=TransitionReason.COMPLETED,
                evidence_digest=replacement.digest,
                artifact_references=(replacement,),
            )
        decision_replacement = ArtifactReference(
            reference_id="decision-1",
            kind=ArtifactReferenceKind.DECISION,
            digest=_sha256(b"replacement-decision"),
            size=len(b"replacement-decision"),
        )
        with self.assertRaises(AppendOnlyConflict):
            store.record_transition(
                EntityKind.ATTEMPT,
                attempt.entity_id,
                expected_version=1,
                next_state="accepted",
                reason=TransitionReason.COMPLETED,
                evidence_digest=decision_replacement.digest,
                artifact_references=(decision_replacement,),
            )
        self.assertEqual(store.get_entity(EntityKind.ATTEMPT, "attempt-1").version, 1)
        self.assertEqual(store.get_artifact_reference("evidence-1"), evidence)
        self.assertEqual(store.get_artifact_reference("decision-1"), decision)

        with self.assertRaises(sqlite3.DatabaseError):
            store._connection.execute("DELETE FROM artifacts WHERE reference_id = 'evidence-1'")

        external = self.database(root)
        with self.assertRaises(sqlite3.IntegrityError):
            external.execute("UPDATE attempts SET attempt_id = 'replacement-attempt' "
                             "WHERE attempt_id = 'attempt-1'")
        external.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            external.execute("DELETE FROM attempts WHERE attempt_id = 'attempt-1'")
        external.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            external.execute("UPDATE artifacts SET digest = digest WHERE reference_id = 'evidence-1'")
        external.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            external.execute("DELETE FROM artifacts WHERE reference_id = 'decision-1'")

    def test_pc19_corrupt_run_is_typed_unknown_without_blocking_healthy_run(self):
        root = self.project()
        store = self.open_store(root)
        corrupt = store.create_run()
        healthy = store.create_run()
        store.create_job(corrupt.entity_id, "corrupt-job")
        store.create_job(healthy.entity_id, "healthy-job")

        external = self.database(root)
        external.execute("UPDATE jobs SET state = 'forged-valid-looking-state' "
                         "WHERE job_id = 'corrupt-job'")

        with self.assertRaises(CorruptRuntimeRecordError) as raised:
            store.get_run(corrupt.entity_id)
        self.assertEqual(raised.exception.code, "corrupt_runtime_record")
        self.assertEqual(raised.exception.run_id, corrupt.entity_id)
        self.assertEqual(raised.exception.state, "unknown")
        observed = store.get_run(healthy.entity_id)
        self.assertEqual((observed.entity_id, observed.state), (healthy.entity_id, "created"))

    def test_pc19_missing_or_reparented_children_and_historical_damage_are_run_local(self):
        root = self.project()
        store = self.open_store(root)
        missing = store.create_run()
        reparented = store.create_run()
        audit_reparented = store.create_run()
        run_audit_reparented = store.create_run()
        historical = store.create_run()
        healthy = store.create_run()
        store.create_job(missing.entity_id, "missing-job")
        store.create_job(reparented.entity_id, "reparented-job")
        store.create_job(audit_reparented.entity_id, "audit-reparented-job")
        store.create_job(healthy.entity_id, "healthy-job")
        store.record_transition(
            EntityKind.RUN,
            historical.entity_id,
            expected_version=0,
            next_state="executing",
            reason=TransitionReason.EFFECT_OBSERVED,
        )

        external = self.database(root)
        external.execute("PRAGMA foreign_keys=OFF")
        external.execute("DROP TRIGGER jobs_no_delete")
        external.execute("DROP TRIGGER jobs_identity_no_update")
        external.execute("DROP TRIGGER transitions_no_update")
        external.execute("DELETE FROM jobs WHERE job_id = 'missing-job'")
        external.execute(
            "UPDATE jobs SET run_id = ? WHERE job_id = 'reparented-job'",
            (healthy.entity_id,),
        )
        external.execute(
            "UPDATE transitions SET run_id = ? "
            "WHERE entity_kind = 'job' AND entity_id = 'audit-reparented-job' "
            "AND entity_version = 0",
            (healthy.entity_id,),
        )
        external.execute(
            "UPDATE transitions SET run_id = ? "
            "WHERE entity_kind = 'run' AND entity_id = ? AND entity_version = 0",
            (healthy.entity_id, run_audit_reparented.entity_id),
        )
        external.execute(
            "UPDATE transitions SET prev_state = 'forged' "
            "WHERE run_id = ? AND entity_kind = 'run' AND entity_version = 1",
            (historical.entity_id,),
        )

        for damaged in (
                missing, reparented, audit_reparented, run_audit_reparented, historical):
            with self.subTest(run_id=damaged.entity_id):
                with self.assertRaises(CorruptRuntimeRecordError) as raised:
                    store.get_run(damaged.entity_id)
                self.assertEqual(raised.exception.run_id, damaged.entity_id)
                self.assertEqual(raised.exception.state, "unknown")
        self.assertEqual(store.get_run(healthy.entity_id).state, "created")

    def test_damaged_artifact_link_is_corrupt_not_missing_reference(self):
        root = self.project()
        store = self.open_store(root)
        run = store.create_run()
        reference = ArtifactReference(
            reference_id="linked-evidence",
            kind=ArtifactReferenceKind.EVIDENCE,
            digest=_sha256(b"linked"),
            size=len(b"linked"),
        )
        store.record_transition(
            EntityKind.RUN,
            run.entity_id,
            expected_version=0,
            next_state="observed",
            reason=TransitionReason.EFFECT_OBSERVED,
            evidence_digest=reference.digest,
            artifact_references=(reference,),
        )
        external = self.database(root)
        external.execute("PRAGMA foreign_keys=OFF")
        external.execute("DROP TRIGGER transitions_no_delete")
        external.execute(
            "DELETE FROM transitions WHERE run_id = ? AND entity_version = 1", (run.entity_id,))

        with self.assertRaises(CorruptRuntimeRecordError):
            store.get_artifact_reference(reference.reference_id)

    def test_run_id_is_canonical_lowercase_rfc9562_uuid7(self):
        unix_ms = 0x0123456789AB
        with mock.patch.object(store_module.secrets, "randbits", return_value=(1 << 74) - 1):
            run_id = store_module.generate_run_id(unix_ms=unix_ms)

        self.assertRegex(
            run_id,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        self.assertEqual(run_id, run_id.lower())
        parsed = uuid.UUID(run_id)
        self.assertEqual(parsed.version, 7)
        self.assertEqual(parsed.int >> 80, unix_ms)

    def test_run_id_unique_collision_retries_without_touching_existing_run(self):
        root = self.project()
        store = self.open_store(root)
        existing = store.create_run(initial_state="existing")
        replacement = store_module.generate_run_id(unix_ms=1)
        self.assertNotEqual(replacement, existing.entity_id)

        with mock.patch.object(
                store_module, "generate_run_id",
                side_effect=(existing.entity_id, replacement)) as generator:
            created = store.create_run(initial_state="new")

        self.assertEqual(generator.call_count, 2)
        self.assertEqual(created.entity_id, replacement)
        self.assertEqual(
            (store.get_run(existing.entity_id).state, store.get_run(existing.entity_id).version),
            ("existing", 0),
        )
        self.assertEqual(store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM transitions WHERE run_id = ?", (existing.entity_id,)
        ).fetchone()[0], 1)

    def test_e09_runtime_identity_ignores_ambient_host_cwd_and_filesystem_metadata(self):
        root = self.project()
        store = self.open_store(root)

        with mock.patch.dict(os.environ, {
            "HOSTNAME": "ambient-host", "PWD": "/ambient/cwd", "WAYSTONE_HOME": str(self.base / "home"),
        }), mock.patch.object(store_module.os, "getpid", side_effect=AssertionError("PID authority")), \
                mock.patch.object(store_module.Path, "cwd", side_effect=AssertionError("cwd authority")), \
                mock.patch.object(store_module.os, "listdir", side_effect=AssertionError("directory order")), \
                mock.patch.object(store_module.os, "scandir", side_effect=AssertionError("directory order")), \
                mock.patch.object(store_module.time, "time_ns", return_value=1_700_000_000_000_000_000), \
                mock.patch.object(store_module.secrets, "randbits", return_value=1):
            created = store.create_run()
        self.assertRegex(created.entity_id, r"^[0-9a-f-]+$")

        forbidden = re.compile(r"hostname|cwd|mtime|ctime|inode|enumerat|directory_order")
        for table in ("runs", "jobs", "attempts", "actions", "transitions", "artifacts"):
            columns = [
                row[1] for row in store._connection.execute(f"PRAGMA table_info({table})")
            ]
            self.assertFalse([column for column in columns if forbidden.search(column)])

    def test_uninitialized_root_is_typed_refusal_with_no_waystone_write(self):
        root = self.base / "uninitialized"
        root.mkdir()

        with self.assertRaises(UninitializedProjectError) as raised:
            RunStore.open(root)

        self.assertEqual(raised.exception.code, "uninitialized_project")
        self.assertFalse((root / ".waystone").exists())

    def test_unsupported_filesystem_refuses_before_database_open(self):
        root = self.project()
        with mock.patch.object(
                store_module, "_probe_state_filesystem",
                return_value=FilesystemInfo(filesystem="nfs", mount_point=Path("/network"))), \
                mock.patch.object(store_module, "_connect") as connect:
            with self.assertRaises(UnsupportedStateFilesystemError) as raised:
                RunStore.open(root)

        self.assertEqual(raised.exception.code, "unsupported_state_filesystem")
        self.assertEqual(raised.exception.filesystem, "nfs")
        connect.assert_not_called()
        self.assertFalse((root / ".waystone").exists())

    def test_filesystem_probe_does_not_use_unrelated_same_device_mount(self):
        root = self.project()
        unrelated = self.base / "unrelated-mount"
        unrelated.mkdir()
        mount = FilesystemInfo(filesystem="ext4", mount_point=unrelated)

        with mock.patch.object(store_module.platform, "system", return_value="Linux"), \
                mock.patch.object(store_module, "_linux_mounts", return_value=[mount]), \
                mock.patch.object(store_module, "_connect") as connect:
            with self.assertRaises(UnsupportedStateFilesystemError) as raised:
                RunStore.open(root)

        self.assertEqual(raised.exception.filesystem, "unknown")
        self.assertEqual(raised.exception.reason, "filesystem_not_proven_supported")
        connect.assert_not_called()
        self.assertFalse((root / ".waystone").exists())

    def test_darwin_firmlink_uses_writable_data_volume_not_sealed_root(self):
        mounted = mock.Mock(
            returncode=0,
            stdout=(
                "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
                "/dev/disk3s5 on /System/Volumes/Data "
                "(apfs, local, journaled, root data)\n"
            ),
        )
        with mock.patch.object(store_module.subprocess, "run", return_value=mounted), \
                mock.patch.object(
                    store_module.Path, "read_text", return_value="/private\tprivate\n"):
            mounts = store_module._darwin_mounts()  # noqa: SLF001

        private = [entry for entry in mounts if entry.mount_point == Path("/private")]
        self.assertEqual(len(private), 1)
        self.assertEqual(private[0].filesystem, "apfs")
        self.assertNotIn("read-only", private[0].options)

    def test_wal_mismatch_is_typed_refusal_without_path_or_journal_fallback(self):
        root = self.project()
        alternate = self.base / "ambient-home"

        with mock.patch.dict(os.environ, {"WAYSTONE_HOME": str(alternate)}), \
                self.supported_filesystem(), \
                mock.patch.object(store_module, "_enable_wal", return_value="delete") as enable:
            with self.assertRaises(UnsupportedStateFilesystemError) as raised:
                RunStore.open(root)

        self.assertEqual(raised.exception.code, "unsupported_state_filesystem")
        self.assertEqual(raised.exception.reason, "journal_mode_mismatch")
        enable.assert_called_once()
        self.assertFalse((alternate / "state.db").exists())

    def test_wal_io_error_and_virtual_or_read_only_filesystem_are_typed_refusals(self):
        wal_root = self.project("wal-error")
        with self.supported_filesystem(), mock.patch.object(
                store_module, "_enable_wal", side_effect=sqlite3.OperationalError("wal I/O")):
            with self.assertRaises(UnsupportedStateFilesystemError) as wal_error:
                RunStore.open(wal_root)
        self.assertEqual(wal_error.exception.reason, "journal_mode_unavailable")

        for name, filesystem in (
                ("virtual", FilesystemInfo("overlay", Path("/"))),
                ("unproven-writable", FilesystemInfo("ext4", Path("/"))),
                ("read-only", FilesystemInfo(
                    "ext4", Path("/"), frozenset({"ro"}), writable=True))):
            root = self.project(name)
            with self.subTest(name=name), mock.patch.object(
                    store_module, "_probe_state_filesystem", return_value=filesystem):
                with self.assertRaises(UnsupportedStateFilesystemError):
                    RunStore.open(root)
            self.assertFalse((root / ".waystone").exists())

    def test_run_store_constructor_cannot_bypass_root_filesystem_or_wal_gate(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        with self.assertRaises(TypeError):
            RunStore(
                Path("/uninitialized"), Path(":memory:"), connection,
                FilesystemInfo("nfs", Path("/network")), 1)

    def test_new_runtime_objects_have_exact_modes_and_nofollow(self):
        root = self.project()
        real_open = os.open
        observed_flags: dict[str, list[int]] = {}

        def inspect_open(path, flags, mode=0o777, *, dir_fd=None):
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            name = Path(path).name
            if name in {"state.db", "state.db-wal", "state.db-shm"}:
                observed_flags.setdefault(name, []).append(flags)
            return descriptor

        previous_umask = os.umask(0)
        try:
            with self.supported_filesystem(), mock.patch.object(
                    store_module.os, "open", side_effect=inspect_open):
                store = RunStore.open(root)
        finally:
            os.umask(previous_umask)
        self.addCleanup(store.close)

        state = root.resolve() / ".waystone"
        paths = {
            "state.db": state / "state.db",
            "state.db-wal": state / "state.db-wal",
            "state.db-shm": state / "state.db-shm",
        }
        self.assertEqual(stat.S_IMODE(state.lstat().st_mode), 0o700)
        for name, path in paths.items():
            with self.subTest(path=name):
                self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o600)
                self.assertTrue(observed_flags.get(name))
                self.assertTrue(all(
                    flags & os.O_NOFOLLOW for flags in observed_flags[name]))

    def test_existing_sidecar_unsafe_mode_or_foreign_owner_refuses_before_connect(self):
        for suffix in ("-wal", "-shm"):
            with self.subTest(suffix=suffix, fault="non-owner-write"):
                root = self.project(f"unsafe-{suffix[1:]}")
                store = self.open_store(root)
                sidecar = Path(f"{store.database_path}{suffix}")
                sidecar.chmod(0o666)
                with self.supported_filesystem(), mock.patch.object(
                        store_module, "_connect",
                        side_effect=AssertionError("unsafe sidecar reached sqlite connect")):
                    with self.assertRaises(UnsafeStatePermissionsError) as raised:
                        RunStore.open(root)
                self.assertEqual(raised.exception.code, "unsafe_state_permissions")
                self.assertEqual(raised.exception.path, sidecar)
                self.assertEqual(stat.S_IMODE(sidecar.lstat().st_mode), 0o666)
                sidecar.chmod(0o600)

        root = self.project("foreign-owner")
        store = self.open_store(root)
        sidecar = Path(f"{store.database_path}-wal")
        effective_uid = os.geteuid()

        def selective_euid(path: Path) -> int:
            return effective_uid + 1 if Path(path) == sidecar else effective_uid

        with self.supported_filesystem(), mock.patch.object(
                store_module, "_effective_uid", side_effect=selective_euid), \
                mock.patch.object(
                    store_module, "_connect",
                    side_effect=AssertionError("foreign-owner sidecar reached sqlite connect")):
            with self.assertRaises(UnsafeStatePermissionsError) as raised:
                RunStore.open(root)
        self.assertEqual(raised.exception.code, "unsafe_state_permissions")
        self.assertEqual(raised.exception.path, sidecar)
        self.assertEqual(stat.S_IMODE(sidecar.lstat().st_mode), 0o600)

    def test_unsafe_existing_state_directory_refuses_without_repair_or_write(self):
        root = self.project("unsafe-state-directory")
        state = root / ".waystone"
        state.mkdir(mode=0o700)
        state.chmod(0o777)

        with self.supported_filesystem(), mock.patch.object(
                store_module, "_connect",
                side_effect=AssertionError("unsafe state directory reached sqlite connect")):
            with self.assertRaises(UnsafeStatePermissionsError) as raised:
                RunStore.open(root)

        self.assertEqual(raised.exception.code, "unsafe_state_permissions")
        self.assertEqual(raised.exception.path, root.resolve() / ".waystone")
        self.assertEqual(stat.S_IMODE(state.lstat().st_mode), 0o777)
        self.assertEqual(list(state.iterdir()), [])

    def test_state_database_symlink_is_refused_at_nofollow_open(self):
        root = self.project("state-symlink")
        state = root / ".waystone"
        state.mkdir(mode=0o700)
        target = self.base / "outside-state-target"
        target.write_bytes(b"outside remains unchanged")
        target.chmod(0o600)
        database = state / "state.db"
        database.symlink_to(target)
        real_open = os.open
        observed_flags: list[int] = []

        def inspect_open(path, flags, mode=0o777, *, dir_fd=None):
            if Path(path).name == "state.db":
                observed_flags.append(flags)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with self.supported_filesystem(), mock.patch.object(
                store_module.os, "open", side_effect=inspect_open), \
                mock.patch.object(
                    store_module, "_connect",
                    side_effect=AssertionError("symlinked state DB reached sqlite connect")):
            with self.assertRaises(StatePathSymlinkError) as raised:
                RunStore.open(root)

        self.assertEqual(raised.exception.code, "state_path_symlink")
        self.assertTrue(observed_flags)
        self.assertTrue(all(flags & os.O_NOFOLLOW for flags in observed_flags))
        self.assertTrue(database.is_symlink())
        self.assertEqual(target.read_bytes(), b"outside remains unchanged")
        self.assertFalse((state / ".gitignore").exists())


class ArtifactStoreTests(_StoreFixture):
    def _tamper_artifact_bytes(self, path: Path, content: bytes) -> None:
        """Overwrite a published artifact's bytes as a deliberate external-tamper simulation.

        ADR-0013 publishes the final content-addressed artifact as immutable (0400), so a
        plain write is refused. A real out-of-band tamperer would relax the mode, corrupt
        the bytes, and restore 0400; this reproduces exactly that dance, test-only.
        """
        os.chmod(path, 0o600)
        path.write_bytes(content)
        os.chmod(path, 0o400)

    @contextmanager
    def _artifact_descriptor_read_fails(self, error: OSError):
        """Fail the artifact byte-read at the descriptor seam the store actually reads through.

        Post-ADR-0013 reads go through a no-follow ``os.open`` descriptor wrapped by
        ``os.fdopen``, so the pre-hardening ``Path.read_bytes`` injection point no longer
        executes. Failing the stream read on that descriptor re-exercises the store's
        'bytes are unreadable' integrity branch.
        """
        real_fdopen = artifacts_module.os.fdopen

        class _FailingReader:
            def __init__(self, descriptor: int):
                self._descriptor = descriptor

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                os.close(self._descriptor)
                return False

            def read(self, *args, **kwargs):
                raise error

        def fake_fdopen(descriptor, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "")
            if "r" in mode:
                return _FailingReader(descriptor)
            return real_fdopen(descriptor, *args, **kwargs)

        with mock.patch.object(artifacts_module.os, "fdopen", side_effect=fake_fdopen):
            yield

    def test_artifact_staging_creation_and_atomic_publish_have_exact_modes(self):
        root = self.project()
        artifacts = ArtifactStore(root)
        real_open = os.open
        real_replace = os.replace
        staging_observations: list[tuple[int, int, int]] = []
        publish_source_modes: list[int] = []

        def inspect_open(path, flags, mode=0o777, *, dir_fd=None):
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if Path(path).name.startswith(".artifact-") and flags & os.O_CREAT:
                staging_observations.append((
                    flags,
                    mode,
                    stat.S_IMODE(os.fstat(descriptor).st_mode),
                ))
            return descriptor

        def inspect_replace(source, destination):
            if Path(destination).parent == artifacts.directory:
                publish_source_modes.append(stat.S_IMODE(Path(source).lstat().st_mode))
            return real_replace(source, destination)

        previous_umask = os.umask(0)
        try:
            with mock.patch.object(
                    artifacts_module.os, "open", side_effect=inspect_open), \
                    mock.patch.object(
                        artifacts_module.os, "replace", side_effect=inspect_replace):
                stored = artifacts.write(b"permission contract")
        finally:
            os.umask(previous_umask)

        state = root.resolve() / ".waystone"
        self.assertEqual(stat.S_IMODE(state.lstat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(artifacts.directory.lstat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(stored.path.lstat().st_mode), 0o400)
        self.assertEqual(publish_source_modes, [0o400])
        self.assertEqual(len(staging_observations), 1)
        flags, requested_mode, created_mode = staging_observations[0]
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertEqual(requested_mode, 0o600)
        self.assertEqual(created_mode, 0o600)

    def test_artifact_root_and_leaf_symlinks_are_typed_without_following(self):
        root = self.project("artifact-root-symlink")
        state = root / ".waystone"
        state.mkdir(mode=0o700)
        outside_directory = self.base / "outside-artifacts"
        outside_directory.mkdir()
        (state / "artifacts").symlink_to(outside_directory, target_is_directory=True)

        with self.assertRaises(ArtifactIntegrityError) as root_error:
            ArtifactStore(root).write(b"must not escape")
        self.assertEqual(root_error.exception.code, "artifact_root_symlink")
        self.assertEqual(list(outside_directory.iterdir()), [])

        root = self.project("artifact-leaf-symlink")
        artifacts = ArtifactStore(root)
        artifacts.write(b"directory seed")
        content = b"outside artifact bytes"
        digest = _sha256(content)
        outside_file = self.base / "outside-artifact-file"
        outside_file.write_bytes(content)
        leaf = artifacts.path_for(digest)
        leaf.symlink_to(outside_file)

        with self.assertRaises(ArtifactIntegrityError) as leaf_error:
            artifacts.read(digest)
        self.assertEqual(leaf_error.exception.code, "artifact_path_symlink")
        self.assertTrue(leaf.is_symlink())
        self.assertEqual(outside_file.read_bytes(), content)

    def test_concurrent_first_artifact_write_is_idempotent_and_self_ignored(self):
        root = self.project()
        first = ArtifactStore(root)
        second = ArtifactStore(root)
        barrier = threading.Barrier(2)

        def write_together(store: ArtifactStore):
            barrier.wait()
            return store.write(b"same immutable bytes")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write_together, (first, second)))
        self.assertEqual(results[0].digest, results[1].digest)
        self.assertEqual(first.read(results[0].digest), b"same immutable bytes")
        self.assertEqual((root / ".waystone" / ".gitignore").read_bytes(), b"*\n")

    def test_concurrent_artifact_publish_survives_verified_inode_churn(self):
        for round_number in range(40):
            root = self.project(f"concurrent-publish-{round_number}")
            stores = [ArtifactStore(root) for _ in range(8)]
            barrier = threading.Barrier(len(stores))

            def write_together(store: ArtifactStore):
                barrier.wait()
                return store.write(b"same concurrent bytes")

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(write_together, stores))
            self.assertEqual(len({result.digest for result in results}), 1)
            self.assertEqual(stat.S_IMODE(results[0].path.lstat().st_mode), 0o400)

    def test_artifact_write_uses_same_directory_temp_atomic_rename_and_post_write_rehash(self):
        root = self.project()
        artifacts = ArtifactStore(root)
        payload = b"complete artifact bytes"
        digest = _sha256(payload)
        expected = artifacts.path_for(digest)
        real_replace = os.replace
        calls: list[tuple[Path, Path]] = []

        def inspect_replace(source, destination) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            if destination_path != expected:
                real_replace(source_path, destination_path)
                return
            self.assertEqual(source_path.parent, destination_path.parent)
            self.assertFalse(destination_path.exists())
            self.assertEqual(source_path.read_bytes(), payload)
            calls.append((source_path, destination_path))
            real_replace(source_path, destination_path)

        with mock.patch.object(artifacts_module.os, "replace", side_effect=inspect_replace):
            stored = artifacts.write(payload)

        self.assertEqual(len(calls), 1)
        self.assertEqual((stored.digest, stored.size, stored.path), (digest, len(payload), expected))
        self.assertEqual(artifacts.read(digest), payload)

        def corrupt_after_publish(source, destination) -> None:
            real_replace(source, destination)
            Path(destination).write_bytes(b"corrupt-after-rename")

        other = b"other artifact"
        with mock.patch.object(
                artifacts_module.os, "replace", side_effect=corrupt_after_publish):
            with self.assertRaises(ArtifactIntegrityError):
                artifacts.write(other)

    def test_artifact_read_reports_corruption_and_unreadable_bytes_as_integrity_errors(self):
        root = self.project()
        artifacts = ArtifactStore(root)
        stored = artifacts.write(b"trusted")
        # Deliberate external tamper of the now-immutable (0400) artifact bytes.
        self._tamper_artifact_bytes(stored.path, b"tampered")

        with self.assertRaises(ArtifactIntegrityError) as mismatch:
            artifacts.read(stored.digest)
        self.assertEqual(mismatch.exception.code, "artifact_integrity_error")

        self._tamper_artifact_bytes(stored.path, b"trusted")
        # The store reads bytes through a no-follow descriptor, not Path.read_bytes, so
        # inject the unreadable/vanished fault at that real descriptor-read seam.
        with self._artifact_descriptor_read_fails(PermissionError("denied")):
            with self.assertRaises(ArtifactIntegrityError) as unreadable:
                artifacts.read(stored.digest)
        self.assertEqual(unreadable.exception.code, "artifact_integrity_error")

        with self._artifact_descriptor_read_fails(FileNotFoundError("vanished")):
            with self.assertRaises(ArtifactIntegrityError) as disappeared:
                artifacts.read(stored.digest)
        self.assertEqual(disappeared.exception.code, "artifact_integrity_error")

    def test_corrupt_existing_artifact_is_not_silently_repaired(self):
        root = self.project()
        artifacts = ArtifactStore(root)
        stored = artifacts.write(b"original")
        # Deliberate external tamper of the now-immutable (0400) artifact bytes.
        self._tamper_artifact_bytes(stored.path, b"corrupt")

        with self.assertRaises(ArtifactIntegrityError):
            artifacts.write(b"original")

        self.assertEqual(stored.path.read_bytes(), b"corrupt")

    def test_missing_raw_digest_and_dangling_reference_are_typed_differently(self):
        root = self.project()
        artifacts = ArtifactStore(root)
        stored = artifacts.write(b"evidence")
        store = self.open_store(root)
        run = store.create_run()
        reference = ArtifactReference(
            reference_id="evidence-1",
            kind=ArtifactReferenceKind.EVIDENCE,
            digest=stored.digest,
            size=stored.size,
        )
        store.record_transition(
            EntityKind.RUN,
            run.entity_id,
            expected_version=0,
            next_state="evidence-recorded",
            reason=TransitionReason.EFFECT_OBSERVED,
            evidence_digest=stored.digest,
            artifact_references=(reference,),
        )
        recorded_reference = store.get_artifact_reference("evidence-1")
        stored.path.unlink()

        with self.assertRaises(ArtifactNotFoundError) as missing:
            artifacts.read(stored.digest)
        self.assertEqual(missing.exception.code, "artifact_not_found")

        with self.assertRaises(DanglingArtifactReferenceError) as dangling:
            artifacts.read_reference(recorded_reference)
        self.assertEqual(dangling.exception.code, "dangling_artifact_reference")
        self.assertEqual(dangling.exception.reference_id, "evidence-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
