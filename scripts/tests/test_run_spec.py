#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Contract tests for frozen one-task RunSpec planning."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import inspect
import uuid
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from unittest import mock

import waystone.runs.spec as spec_module
from waystone.runs.artifacts import ArtifactStore
from waystone.runs.spec import (
    AcceptanceReadinessError,
    ReviewDecision,
    ReviewRequirement,
    RunInputChangedDuringPlanningError,
    RunInputDriftError,
    SnapshotError,
    UninitializedRunSpecError,
    assert_task_input_current,
    detect_task_input_drift,
    load_run_spec,
    plan_one_task_run,
    read_base_snapshot,
)
from waystone.runs.store import EntityKind, FilesystemInfo, RunStore


class RunSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.base = Path(self._temporary_directory.name)

    def project(self, *, acceptance: bool = True) -> Path:
        root = self.base / "repo"
        root.mkdir()
        init_repo(root)
        accept = (
            "    accept:\n"
            "      - output records the requested owner property\n"
            "      - failure remains a typed refusal\n"
            if acceptance else ""
        )
        (root / ".waystone.yml").write_text(
            "version: 1\nproject: fixture\n", encoding="utf-8")
        (root / "tasks.yaml").write_text(
            "version: 1\n"
            "project: fixture\n"
            "tasks:\n"
            "  - id: feat/dependency\n"
            "    title: prerequisite\n"
            "    status: done\n"
            "  - id: feat/example\n"
            "    title: preserve owner intent\n"
            "    status: pending\n"
            "    deps: [feat/dependency]\n"
            "    scope: [waystone/runs, scripts/tests/test_run_spec.py]\n"
            f"{accept}",
            encoding="utf-8",
        )
        (root / ".gitignore").write_text("ignored.bin\n", encoding="utf-8")
        (root / "tracked.txt").write_bytes(b"tracked-base\n")
        (root / "mixed.txt").write_bytes(b"mixed-base\n")
        (root / "delete.txt").write_bytes(b"delete-base\n")
        git(root, "add", "-A")
        self.assertEqual(git(root, "commit", "-qm", "project fixture").returncode, 0)
        return root

    @contextmanager
    def supported_filesystem(self):
        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)):
            yield

    def plan(self, root: Path, **kwargs):
        with self.supported_filesystem():
            return plan_one_task_run("feat/example", start=root, **kwargs)

    def load(self, root: Path, run_id: str):
        with self.supported_filesystem():
            return load_run_spec(run_id, start=root)

    def snapshot(self, root: Path, run_id: str):
        with self.supported_filesystem():
            return read_base_snapshot(run_id, start=root)

    @staticmethod
    def raw_status(root: Path) -> bytes:
        environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z",
             "--untracked-files=all"],
            capture_output=True,
            env=environment,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
        return result.stdout

    @staticmethod
    def index_bytes(root: Path) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "index"],
            capture_output=True,
            check=True,
        )
        path = Path(os.fsdecode(result.stdout.rstrip(b"\n")))
        if not path.is_absolute():
            path = root / path
        return path.read_bytes()

    def open_store(self, root: Path) -> RunStore:
        with self.supported_filesystem():
            store = RunStore.open(root)
        self.addCleanup(store.close)
        return store

    def test_plan_freezes_owner_input_and_persists_one_run_one_job(self):
        root = self.project()
        decision = ReviewDecision(
            requirement=ReviewRequirement.REQUIRED,
            reason="trust-surface-store",
            rule_id="builtin:trust-surface-store",
            policy_digest="sha256:" + "a" * 64,
        )

        spec = self.plan(root, review_decision=decision)

        run_uuid = uuid.UUID(spec.run_id)
        self.assertEqual(run_uuid.version, 7)
        self.assertEqual(str(run_uuid), spec.run_id)
        self.assertEqual(spec.readiness, "frozen-ready")
        self.assertEqual(spec.job_input.task_id, "feat/example")
        self.assertEqual(spec.job_input.title, "preserve owner intent")
        self.assertEqual(
            spec.job_input.acceptance_criteria,
            ("output records the requested owner property",
             "failure remains a typed refusal"),
        )
        self.assertEqual(spec.job_input.dependencies, ("feat/dependency",))
        self.assertEqual(
            spec.job_input.scope,
            ("waystone/runs", "scripts/tests/test_run_spec.py"),
        )
        self.assertRegex(spec.job_input.input_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(spec.run_spec_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(spec.retry.max_attempts_per_job, 1)
        self.assertEqual(spec.retry.max_total_attempts, 1)
        self.assertGreater(spec.retry.time_budget.limit, 0)
        self.assertGreater(spec.retry.cost_budget.limit, 0)
        self.assertEqual(spec.retry.budget_exhaustion_policy, "stop")
        self.assertEqual(spec.review_decision, decision)

        store = self.open_store(root)
        self.assertEqual(store.get_run(spec.run_id).state, "frozen-ready")
        job = store.get_entity(EntityKind.JOB, spec.job_id)
        self.assertEqual(job.run_id, spec.run_id)
        self.assertEqual(job.state, "planned")
        self.assertEqual(
            store._connection.execute("SELECT count(*) FROM runs").fetchone()[0],  # noqa: SLF001
            1,
        )
        self.assertEqual(
            store._connection.execute("SELECT count(*) FROM jobs").fetchone()[0],  # noqa: SLF001
            1,
        )
        spec_reference = store.get_artifact_reference(f"run-spec:{spec.run_id}")
        snapshot_reference = store.get_artifact_reference(f"base-snapshot:{spec.run_id}")
        self.assertEqual(spec_reference.digest, spec.run_spec_digest)
        self.assertEqual(snapshot_reference.digest, spec.base_snapshot.digest)
        artifact_store = ArtifactStore(root)
        self.assertEqual(
            artifact_store.read_reference(spec_reference), spec.canonical_bytes())
        self.assertEqual(self.load(root, spec.run_id), spec)

    def test_missing_acceptance_refuses_before_run_or_project_state_creation(self):
        root = self.project(acceptance=False)

        with self.assertRaises(AcceptanceReadinessError) as raised:
            self.plan(root)

        self.assertEqual(raised.exception.code, "criterion-empty")
        self.assertEqual(raised.exception.task_id, "feat/example")
        self.assertFalse((root / ".waystone").exists())

    def test_incomplete_explicit_review_exemption_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "review reason"):
            ReviewDecision(
                requirement=ReviewRequirement.NONE,
                reason="explicit-review-exemption",
                rule_id="project:exemption",
                policy_digest="sha256:" + "a" * 64,
            )

    def test_snapshot_includes_dirty_staged_and_untracked_without_mutating_index(self):
        root = self.project()
        (root / "tracked.txt").write_bytes(b"tracked-worktree\x00\xff")
        (root / "staged.txt").write_bytes(b"staged-content\n")
        (root / "mixed.txt").write_bytes(b"mixed-staged\n")
        git(root, "add", "staged.txt", "mixed.txt")
        (root / "mixed.txt").write_bytes(b"mixed-final\n")
        (root / "untracked.bin").write_bytes(b"untracked\x00\xfe")
        (root / "ignored.bin").write_bytes(b"ignored")
        (root / "delete.txt").unlink()
        os.chmod(root / "staged.txt", 0o755)

        status_before = self.raw_status(root)
        index_before = self.index_bytes(root)
        head_before = git(root, "rev-parse", "HEAD").stdout.strip()

        spec = self.plan(root)

        index_after = self.index_bytes(root)
        status_after = self.raw_status(root)
        head_after = git(root, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(index_after, index_before)
        self.assertEqual(status_after, status_before)
        self.assertEqual(head_after, head_before)

        snapshot = self.snapshot(root, spec.run_id)
        entries = {entry.path: entry for entry in snapshot.entries}
        self.assertEqual(snapshot.head, head_before)
        self.assertEqual(entries[b"tracked.txt"].content, b"tracked-worktree\x00\xff")
        self.assertEqual(entries[b"staged.txt"].content, b"staged-content\n")
        self.assertEqual(entries[b"staged.txt"].mode, "100755")
        self.assertEqual(entries[b"mixed.txt"].content, b"mixed-final\n")
        self.assertEqual(entries[b"untracked.bin"].content, b"untracked\x00\xfe")
        self.assertEqual(entries[b"delete.txt"].state, "deleted")
        self.assertNotIn(b"ignored.bin", entries)

    def test_registry_drift_is_typed_and_cannot_rewrite_frozen_input(self):
        root = self.project()
        spec = self.plan(root)
        store = self.open_store(root)
        reference = store.get_artifact_reference(f"run-spec:{spec.run_id}")
        original_artifact = ArtifactStore(root).read_reference(reference)

        tasks_path = root / "tasks.yaml"
        tasks_path.write_text(
            tasks_path.read_text(encoding="utf-8")
            .replace("preserve owner intent", "changed owner intent")
            .replace("failure remains a typed refusal", "acceptance changed"),
            encoding="utf-8",
        )

        with self.supported_filesystem():
            drift = detect_task_input_drift(spec.run_id, start=root)
        self.assertIsNotNone(drift)
        self.assertEqual(drift.run_id, spec.run_id)
        self.assertEqual(drift.task_id, "feat/example")
        self.assertEqual(drift.frozen_digest, spec.job_input.input_digest)
        self.assertEqual(drift.changed_fields, ("acceptance_criteria", "title"))

        with self.supported_filesystem():
            with self.assertRaises(RunInputDriftError) as raised:
                assert_task_input_current(spec.run_id, start=root)
        self.assertEqual(raised.exception.code, "run_input_drift")
        self.assertEqual(raised.exception.run_id, spec.run_id)
        self.assertEqual(self.load(root, spec.run_id), spec)
        self.assertEqual(ArtifactStore(root).read_reference(reference), original_artifact)
        self.assertEqual(store.get_run(spec.run_id).state, "frozen-ready")
        with self.assertRaises(FrozenInstanceError):
            spec.job_input.title = "worker override"  # type: ignore[misc]

        parameters = inspect.signature(plan_one_task_run).parameters
        self.assertNotIn("title", parameters)
        self.assertNotIn("acceptance", parameters)
        self.assertNotIn("dependencies", parameters)
        self.assertNotIn("scope", parameters)

    def test_uninitialized_root_refuses_before_store_and_creates_nothing(self):
        root = self.base / "uninitialized"
        root.mkdir()

        with mock.patch(
                "waystone.runs.spec.RunStore.open",
                side_effect=AssertionError("store gate must not be reached")):
            with self.assertRaises(UninitializedRunSpecError) as raised:
                plan_one_task_run("feat/example", start=root)

        self.assertEqual(raised.exception.code, "uninitialized_project")
        self.assertFalse((root / ".waystone").exists())

    def test_snapshot_refuses_concurrent_tree_change_before_creating_state(self):
        root = self.project()
        original = spec_module._snapshot_entries  # noqa: SLF001 - deterministic race fixture
        calls = 0

        def mutate_after_first_capture(project_root: Path):
            nonlocal calls
            result = original(project_root)
            calls += 1
            if calls == 1:
                (project_root / "tracked.txt").write_bytes(b"concurrent change")
            return result

        with mock.patch.object(
                spec_module, "_snapshot_entries", side_effect=mutate_after_first_capture):
            with self.assertRaises(SnapshotError) as raised:
                self.plan(root)

        self.assertEqual(raised.exception.code, "snapshot_unavailable")
        self.assertFalse((root / ".waystone").exists())

    def test_malformed_task_registry_is_a_typed_refusal(self):
        root = self.project()
        (root / "tasks.yaml").write_text("tasks: [unterminated\n", encoding="utf-8")

        with self.assertRaises(spec_module.InvalidTaskInputError) as raised:
            self.plan(root)

        self.assertEqual(raised.exception.code, "invalid_task_input")
        self.assertFalse((root / ".waystone").exists())

    def test_snapshot_does_not_trust_assume_unchanged_index_hint(self):
        root = self.project()
        self.assertEqual(
            git(root, "update-index", "--assume-unchanged", "tracked.txt").returncode, 0)
        (root / "tracked.txt").write_bytes(b"hidden tracked dirt")
        self.assertEqual(git(root, "diff", "--name-only", "HEAD").stdout, "")

        spec = self.plan(root)
        snapshot = self.snapshot(root, spec.run_id)
        entries = {entry.path: entry for entry in snapshot.entries}

        self.assertEqual(entries[b"tracked.txt"].content, b"hidden tracked dirt")

    def test_task_change_during_planning_refuses_before_run_creation(self):
        root = self.project()
        original = spec_module._capture_snapshot  # noqa: SLF001 - deterministic race fixture

        def mutate_after_snapshot(project_root: Path):
            snapshot = original(project_root)
            tasks_path = project_root / "tasks.yaml"
            tasks_path.write_text(
                tasks_path.read_text(encoding="utf-8").replace(
                    "preserve owner intent", "concurrent owner change"),
                encoding="utf-8",
            )
            return snapshot

        with mock.patch.object(
                spec_module, "_capture_snapshot", side_effect=mutate_after_snapshot):
            with self.assertRaises(RunInputChangedDuringPlanningError) as raised:
                self.plan(root)

        self.assertEqual(raised.exception.code, "run_input_changed_during_planning")
        self.assertFalse((root / ".waystone").exists())

    def test_absent_skip_worktree_path_refuses_even_with_assume_unchanged(self):
        root = self.project()
        self.assertEqual(
            git(root, "update-index", "--assume-unchanged", "tracked.txt").returncode, 0)
        self.assertEqual(
            git(root, "update-index", "--skip-worktree", "tracked.txt").returncode, 0)
        (root / "tracked.txt").unlink()
        flags = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-v", "tracked.txt"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertTrue(flags.startswith("s "), flags)

        with self.assertRaises(SnapshotError) as raised:
            self.plan(root)

        self.assertEqual(raised.exception.code, "snapshot_unavailable")
        self.assertFalse((root / ".waystone").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
