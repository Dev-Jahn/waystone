#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Contract tests for the M1-B external effect commit protocol."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
_WAYSTONE_PRELOADED = "waystone" in sys.modules
sys.path.insert(0, str(ROOT))
try:
    from waystone.runs import effects as effects_module  # noqa: E402
    from waystone.runs import store as store_module  # noqa: E402
    from waystone.runs.effects import (  # noqa: E402
        ArtifactWriteEffect,
        ClaimedEffect,
        EffectAlreadyExecuted,
        EffectEngine,
        EffectKind,
        EffectObservation,
        EffectResultState,
        EffectRetryRefused,
        GitRefEffect,
        InvalidEffectPlan,
        ObservationDisposition,
        PatchIntegrationEffect,
        RunnerCompletionMarker,
        RunnerExecutionEffect,
        UnsupportedEffectKind,
        WorktreeEffect,
        publish_runner_completion,
    )
    from waystone.runs.lease import (  # noqa: E402
        LeaseManager,
        LeasePrincipalMismatch,
    )
    from waystone.runs.store import (  # noqa: E402
        EntityKind,
        FilesystemInfo,
        GuardedEffectTransitionRequired,
        RecordNotFoundError,
        RunStore,
        TransitionReason,
    )
finally:
    sys.path.pop(0)
    if not _WAYSTONE_PRELOADED:
        sys.modules.pop("waystone", None)
del _WAYSTONE_PRELOADED


class InjectedCrash(BaseException):
    """Model process death without being caught by ordinary rollback handling."""


@dataclass(frozen=True)
class EffectCase:
    kind: EffectKind
    action_id: str
    effect: object
    claimed: ClaimedEffect
    target: object


class RunEffectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.sandbox = Path(self._temporary_directory.name)
        self.root = self.sandbox / "project"
        self.root.mkdir()
        self.git(self.root, "init", "-q")
        self.git(self.root, "config", "user.email", "fixture@example.com")
        self.git(self.root, "config", "user.name", "Fixture")
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        self.git(self.root, "add", "base.txt")
        self.git(self.root, "commit", "-qm", "base")
        self.base_oid = self.git(self.root, "rev-parse", "HEAD")
        self.base_tree = self.git(self.root, "rev-parse", "HEAD^{tree}")
        (self.root / ".waystone.yml").write_text(
            "version: 1\nproject: effect-fixture\n", encoding="utf-8")
        with mock.patch.object(
                store_module, "_probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)):
            self.store = RunStore.open(self.root)
        self.addCleanup(self.store.close)
        self.leases = LeaseManager(self.store)
        self.runner_calls: dict[str, int] = {}
        self.engine = EffectEngine(
            self.store, self.leases,
            runner_executor=self.runner_executor,
            runner_identity_verifier=self.runner_identity_verifier,
        )
        self.effect_calls: dict[str, int] = {}
        original_driver = self.engine._execute_driver  # noqa: SLF001

        def counted_driver(plan, principal, intent):
            self.effect_calls[plan.action_id] = self.effect_calls.get(plan.action_id, 0) + 1
            return original_driver(plan, principal, intent)

        driver_patch = mock.patch.object(
            self.engine, "_execute_driver", side_effect=counted_driver)
        driver_patch.start()
        self.addCleanup(driver_patch.stop)
        self.run = self.store.create_run()
        self.store.create_job(self.run.entity_id, "job")
        self._number = 0

    @staticmethod
    def git(root: Path, *args: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args], input=input_text,
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    @staticmethod
    def sha256(payload: bytes) -> str:
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def runner_executor(self, intent) -> None:
        self.runner_calls[intent.action_id] = self.runner_calls.get(intent.action_id, 0) + 1
        empty = self.engine._artifacts.write(b"").digest  # noqa: SLF001
        publish_runner_completion(
            intent.completion_marker_path,
            RunnerCompletionMarker(
                run_id=intent.run_id,
                job_id=intent.job_id,
                action_id=intent.action_id,
                fencing_epoch=intent.fencing_epoch,
                launch_token=intent.launch_token,
                process_identity=f"fixture-process:{intent.action_id}",
                started_at="2026-07-21T00:00:00Z",
                finished_at="2026-07-21T00:00:01Z",
                returncode=0,
                signal=None,
                stdout_artifact_digest=empty,
                stderr_artifact_digest=empty,
            ),
        )

    @staticmethod
    def runner_identity_verifier(marker: RunnerCompletionMarker) -> bool:
        return marker.process_identity == f"fixture-process:{marker.action_id}"

    def reopen_effect_engine(self) -> tuple[RunStore, EffectEngine]:
        with mock.patch.object(
                store_module, "_probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)):
            reopened_store = RunStore.open(self.root)
        self.addCleanup(reopened_store.close)
        reopened_engine = EffectEngine(
            reopened_store, LeaseManager(reopened_store),
            runner_executor=self.runner_executor,
            runner_identity_verifier=self.runner_identity_verifier,
        )
        original_driver = reopened_engine._execute_driver  # noqa: SLF001

        def counted_driver(plan, principal, intent):
            self.effect_calls[plan.action_id] = self.effect_calls.get(plan.action_id, 0) + 1
            return original_driver(plan, principal, intent)

        driver_patch = mock.patch.object(
            reopened_engine, "_execute_driver", side_effect=counted_driver)
        driver_patch.start()
        self.addCleanup(driver_patch.stop)
        return reopened_store, reopened_engine

    def integration_commit(self, suffix: str) -> tuple[str, str]:
        blob = self.git(
            self.root, "hash-object", "-w", "--stdin",
            input_text=f"integration-{suffix}\n")
        tree = self.git(
            self.root, "mktree",
            input_text=f"100644 blob {blob}\tintegration-{suffix}.txt\n")
        commit = self.git(
            self.root, "commit-tree", tree, "-p", self.base_oid,
            input_text=f"integration {suffix}\n")
        return commit, tree

    def make_case(self, kind: EffectKind, prefix: str = "case") -> EffectCase:
        self._number += 1
        suffix = f"{prefix}-{self._number}"
        attempt_id = f"attempt-{suffix}"
        action_id = f"action-{suffix}"
        self.store.create_attempt(self.run.entity_id, "job", attempt_id)
        target: object
        if kind is EffectKind.GIT_REF:
            ref = f"refs/heads/effect-{suffix}"
            effect = GitRefEffect(self.root, ref, None, self.base_oid)
            target = ref
        elif kind is EffectKind.WORKTREE:
            branch = f"worktree-{suffix}"
            path = self.sandbox / f"worktree-{suffix}"
            effect = WorktreeEffect(
                self.root, path, f"refs/heads/{branch}", self.base_oid)
            target = path
        elif kind is EffectKind.ARTIFACT_WRITE:
            content = f"artifact-{suffix}".encode("utf-8")
            effect = ArtifactWriteEffect(content)
            target = self.engine._artifacts.path_for(self.sha256(content))  # noqa: SLF001
        elif kind is EffectKind.RUNNER_EXECUTION:
            effect = RunnerExecutionEffect(
                self.sha256(f"invocation-{suffix}".encode("utf-8")))
            target = action_id
        else:
            commit, tree = self.integration_commit(suffix)
            ref = f"refs/heads/integration-{suffix}"
            self.git(self.root, "update-ref", ref, self.base_oid)
            effect = PatchIntegrationEffect(
                self.root, ref, self.base_oid, self.base_tree, commit, tree)
            target = (ref, commit, tree)
        plan = self.engine.plan_effect(
            self.run.entity_id, "job", attempt_id, action_id, effect)
        claimed = self.engine.claim_effect(plan, ttl_seconds=30)
        return EffectCase(kind, action_id, effect, claimed, target)

    def assert_external_effect(self, case: EffectCase) -> None:
        if case.kind is EffectKind.GIT_REF:
            self.assertEqual(self.git(self.root, "rev-parse", case.target), self.base_oid)
        elif case.kind is EffectKind.WORKTREE:
            path = Path(case.target)
            self.assertEqual(self.git(path, "rev-parse", "HEAD"), self.base_oid)
            self.assertEqual(
                self.git(path, "symbolic-ref", "HEAD"),
                case.claimed.plan.spec["dedicated_ref"])
        elif case.kind is EffectKind.ARTIFACT_WRITE:
            self.assertEqual(Path(case.target).read_bytes(), case.effect.content)
        elif case.kind is EffectKind.RUNNER_EXECUTION:
            self.assertEqual(self.runner_calls.get(case.action_id), 1)
            marker = Path(case.claimed.plan.spec["completion_marker"])
            self.assertTrue(marker.is_file())
        else:
            ref, commit, tree = case.target
            self.assertEqual(self.git(self.root, "rev-parse", ref), commit)
            self.assertEqual(self.git(self.root, "rev-parse", f"{commit}^{{tree}}"), tree)
            self.assertEqual(
                self.git(self.root, "rev-parse", f"{commit}^"), self.base_oid)

    def transition_states(self, action_id: str) -> list[str]:
        return [
            row[0] for row in self.store._connection.execute(  # noqa: SLF001
                "SELECT next_state FROM transitions WHERE entity_kind = ? AND entity_id = ? "
                "ORDER BY entity_version",
                (EntityKind.ACTION.value, action_id),
            ).fetchall()
        ]

    def test_plan_is_atomic_immutable_and_unknown_kinds_refuse_before_mutation(self):
        before_actions = self.store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM actions").fetchone()[0]
        before_artifacts = set(
            self.engine._artifacts.directory.glob("sha256-*")  # noqa: SLF001
            if self.engine._artifacts.directory.exists() else ())  # noqa: SLF001
        for kind in ("push", "github-marker"):
            with self.subTest(kind=kind), self.assertRaises(UnsupportedEffectKind) as raised:
                self.engine.plan_effect(
                    self.run.entity_id, "job", "missing-attempt",
                    f"unsupported-{kind}", kind)
            self.assertEqual(raised.exception.code, "unsupported_effect_kind")
        self.assertEqual(self.store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM actions").fetchone()[0], before_actions)
        after_artifacts = set(
            self.engine._artifacts.directory.glob("sha256-*")  # noqa: SLF001
            if self.engine._artifacts.directory.exists() else ())  # noqa: SLF001
        self.assertEqual(after_artifacts, before_artifacts)

        self.store.create_attempt(self.run.entity_id, "job", "attempt-plan")
        action_id = "action-plan"
        content = b"immutable plan payload"
        plan = self.engine.plan_effect(
            self.run.entity_id, "job", "attempt-plan", action_id,
            ArtifactWriteEffect(content))
        reference = self.store.get_artifact_reference(f"effect-plan:{action_id}")
        envelope = json.loads(
            self.engine._artifacts.read_reference(reference).decode("utf-8"))  # noqa: SLF001
        self.assertEqual(envelope["input_digest"], plan.input_digest)
        self.assertEqual(envelope["idempotency_key"], plan.idempotency_key)
        self.assertEqual(envelope["idempotency_basis"]["action_id"], action_id)
        self.assertEqual(envelope["idempotency_basis"]["kind"], "artifact-write")
        self.assertEqual(
            envelope["idempotency_basis"]["input_digest"], plan.input_digest)
        self.assertIn("target", envelope["idempotency_basis"])
        self.assertIn("expected", envelope["idempotency_basis"])
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, action_id).state, "planned")

        self.store.create_attempt(self.run.entity_id, "job", "attempt-plan-fault")
        with mock.patch.object(
                self.store, "_transaction_fault_point",  # noqa: SLF001
                side_effect=RuntimeError("injected planning fault")):
            with self.assertRaisesRegex(RuntimeError, "injected planning fault"):
                self.engine.plan_effect(
                    self.run.entity_id, "job", "attempt-plan-fault",
                    "action-plan-fault", ArtifactWriteEffect(b"fault"))
        with self.assertRaises(RecordNotFoundError):
            self.store.get_entity(EntityKind.ACTION, "action-plan-fault")

    def test_five_stage_lifecycle_commits_observed_digest_under_exact_lease(self):
        case = self.make_case(EffectKind.ARTIFACT_WRITE, "lifecycle")
        result = self.engine.execute_effect(case.claimed)
        self.assertEqual(result.state, EffectResultState.COMPLETED)
        self.assert_external_effect(case)
        self.assertEqual(
            self.transition_states(case.action_id),
            ["created", "planned", "claimed", "effect", "observed", "completed"])
        action = self.store.get_entity(EntityKind.ACTION, case.action_id)
        lease = self.store._connection.execute(  # noqa: SLF001
            "SELECT owner_token, fencing_epoch, entity_version FROM leases WHERE lease_id = ?",
            (case.action_id,),
        ).fetchone()
        self.assertEqual(lease[0], result.principal.owner_token)
        self.assertEqual(lease[1], result.principal.fencing_epoch)
        self.assertEqual(lease[2], action.version)
        observed = self.store._connection.execute(  # noqa: SLF001
            "SELECT evidence_digest FROM transitions WHERE entity_id = ? "
            "AND next_state = 'observed'",
            (case.action_id,),
        ).fetchone()[0]
        completed = self.store._connection.execute(  # noqa: SLF001
            "SELECT evidence_digest FROM transitions WHERE entity_id = ? "
            "AND next_state = 'completed'",
            (case.action_id,),
        ).fetchone()[0]
        self.assertEqual(observed, result.observed_digest)
        self.assertEqual(completed, observed)
        receipt = self.store._connection.execute(  # noqa: SLF001
            "SELECT digest FROM artifacts WHERE reference_id LIKE 'effect-observation:%' "
            "AND entity_id = ?", (case.action_id,),
        ).fetchone()[0]
        self.assertNotEqual(receipt, observed)

    def test_effect_action_cannot_bypass_guarded_five_stage_lifecycle(self):
        self.store.create_attempt(
            self.run.entity_id, "job", "attempt-generic-transition-bypass")
        content = b"must-not-be-published-by-store-transition"
        plan = self.engine.plan_effect(
            self.run.entity_id, "job", "attempt-generic-transition-bypass",
            "action-generic-transition-bypass", ArtifactWriteEffect(content))
        with self.assertRaises(GuardedEffectTransitionRequired) as raised:
            self.store.record_transition(
                EntityKind.ACTION,
                plan.action_id,
                expected_version=1,
                next_state="completed",
                reason=TransitionReason.COMPLETED,
                evidence_digest=plan.input_digest,
            )
        self.assertEqual(raised.exception.code, "guarded_effect_transition_required")
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, plan.action_id).state,
            "planned")
        self.assertEqual(
            self.transition_states(plan.action_id), ["created", "planned"])
        target = self.engine._artifacts.path_for(self.sha256(content))  # noqa: SLF001
        self.assertFalse(target.exists())

    def test_registered_effect_kinds_use_real_authority_observation(self):
        for kind in EffectKind:
            with self.subTest(kind=kind.value):
                case = self.make_case(kind, "authority")
                result = self.engine.execute_effect(case.claimed)
                self.assertEqual(result.state, EffectResultState.COMPLETED)
                self.assertEqual(self.effect_calls.get(case.action_id), 1)
                self.assert_external_effect(case)

    def test_all_effect_kinds_crash_before_effect_reconcile_one_first_execution(self):
        for kind in EffectKind:
            with self.subTest(kind=kind.value):
                case = self.make_case(kind, "pre-crash")

                def crash(stage, plan):
                    if stage == "before-effect-intent":
                        raise InjectedCrash()

                with mock.patch.object(
                        self.engine, "_effect_fault_point", side_effect=crash):  # noqa: SLF001
                    with self.assertRaises(InjectedCrash):
                        self.engine.execute_effect(case.claimed)
                self.assertEqual(
                    self.store.get_entity(EntityKind.ACTION, case.action_id).state,
                    "claimed")
                self.assertEqual(self.effect_calls.get(case.action_id, 0), 0)
                reopened_store, reopened_engine = self.reopen_effect_engine()
                self.assertEqual(
                    reopened_store.get_entity(EntityKind.ACTION, case.action_id).state,
                    "claimed")
                first = reopened_engine.reconcile_actions(
                    [case.action_id], quiescence_probe=lambda plan: True)[0]
                second = reopened_engine.reconcile_actions([case.action_id])[0]
                self.assertEqual(first.state, EffectResultState.COMPLETED)
                self.assertEqual(second.state, EffectResultState.NOOP)
                self.assertEqual(self.effect_calls.get(case.action_id), 1)
                self.assert_external_effect(case)

    def test_all_effect_kinds_crash_after_effect_reconcile_without_reexecution(self):
        for kind in EffectKind:
            with self.subTest(kind=kind.value):
                case = self.make_case(kind, "post-crash")

                def crash(stage, plan):
                    if stage == "after-external-effect":
                        raise InjectedCrash()

                with mock.patch.object(
                        self.engine, "_effect_fault_point", side_effect=crash):  # noqa: SLF001
                    with self.assertRaises(InjectedCrash):
                        self.engine.execute_effect(case.claimed)
                self.assertEqual(
                    self.store.get_entity(EntityKind.ACTION, case.action_id).state,
                    "effect")
                self.assertEqual(self.effect_calls.get(case.action_id), 1)
                self.assert_external_effect(case)
                reopened_store, reopened_engine = self.reopen_effect_engine()
                self.assertEqual(
                    reopened_store.get_entity(EntityKind.ACTION, case.action_id).state,
                    "effect")
                first = reopened_engine.reconcile_actions([case.action_id])[0]
                second = reopened_engine.reconcile_actions([case.action_id])[0]
                self.assertEqual(first.state, EffectResultState.COMPLETED)
                self.assertEqual(second.state, EffectResultState.NOOP)
                self.assertEqual(self.effect_calls.get(case.action_id), 1)
                with self.assertRaises(EffectAlreadyExecuted) as duplicate:
                    self.engine.execute_effect(case.claimed)
                self.assertEqual(duplicate.exception.code, "effect_already_executed")
                self.assertEqual(self.effect_calls.get(case.action_id), 1)

    def test_fixture_3_exited_unreconciled_runner_completes_exactly_once(self):
        """계획 §6 exit fixture 3:
        effect 후 completion 전 kill은 resume 1회로 수렴한다.
        """
        case = self.make_case(EffectKind.RUNNER_EXECUTION, "fixture-3")

        def crash(stage, plan):
            if stage == "after-external-effect":
                raise InjectedCrash()

        with mock.patch.object(
                self.engine, "_effect_fault_point", side_effect=crash):  # noqa: SLF001
            with self.assertRaises(InjectedCrash):
                self.engine.execute_effect(case.claimed)
        status = self.engine.inspect_effect(case.action_id)
        self.assertEqual(status.state, EffectResultState.EXITED_UNRECONCILED)
        self.assertEqual(self.runner_calls.get(case.action_id), 1)
        before = self.store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM transitions WHERE entity_id = ? AND next_state = 'completed'",
            (case.action_id,),
        ).fetchone()[0]
        self.assertEqual(before, 0)
        reopened_store, reopened_engine = self.reopen_effect_engine()
        first = reopened_engine.reconcile_actions([case.action_id])[0]
        first_version = reopened_store.get_entity(EntityKind.ACTION, case.action_id).version
        first_count = reopened_store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM transitions WHERE entity_id = ? AND next_state = 'completed'",
            (case.action_id,),
        ).fetchone()[0]
        second = reopened_engine.reconcile_actions([case.action_id])[0]
        self.assertEqual(first.state, EffectResultState.COMPLETED)
        self.assertEqual(second.state, EffectResultState.NOOP)
        self.assertEqual(first_count, 1)
        self.assertEqual(
            reopened_store.get_entity(EntityKind.ACTION, case.action_id).version,
            first_version)
        self.assertEqual(self.runner_calls.get(case.action_id), 1)

    def test_all_kinds_unavailable_observation_wait_unknown_without_destruction(self):
        unknown = EffectObservation(
            ObservationDisposition.UNKNOWN, {}, reason="observation-channel-unavailable")
        for kind in EffectKind:
            with self.subTest(kind=kind.value):
                case = self.make_case(kind, "unknown")

                def crash(stage, plan):
                    if stage == "after-effect-intent":
                        raise InjectedCrash()

                with mock.patch.object(
                        self.engine, "_effect_fault_point", side_effect=crash):  # noqa: SLF001
                    with self.assertRaises(InjectedCrash):
                        self.engine.execute_effect(case.claimed)
                self.assertEqual(self.effect_calls.get(case.action_id, 0), 0)
                with mock.patch.object(
                        self.engine, "_observe", return_value=unknown):  # noqa: SLF001
                    first = self.engine.reconcile_actions(
                        [case.action_id], quiescence_probe=lambda plan: True)[0]
                    second = self.engine.reconcile_actions(
                        [case.action_id], quiescence_probe=lambda plan: True)[0]
                self.assertEqual(first.state, EffectResultState.UNKNOWN_EFFECT)
                self.assertEqual(second.state, EffectResultState.UNKNOWN_EFFECT)
                self.assertEqual(self.effect_calls.get(case.action_id, 0), 0)
                self.assertEqual(
                    self.store.get_entity(EntityKind.ACTION, case.action_id).state,
                    "effect")

    def test_real_git_and_filesystem_read_failures_are_unknown_not_absent(self):
        for kind in (
                EffectKind.GIT_REF, EffectKind.WORKTREE,
                EffectKind.PATCH_INTEGRATION):
            with self.subTest(kind=kind.value):
                case = self.make_case(kind, "unreadable-git")
                with mock.patch.object(
                        effects_module.git_adapter, "git_rc",
                        return_value=(127, "", "observation unavailable")):
                    result = self.engine.execute_effect(case.claimed)
                self.assertEqual(result.state, EffectResultState.UNKNOWN_EFFECT)
                self.assertEqual(self.effect_calls.get(case.action_id, 0), 0)
                self.assertEqual(
                    self.store.get_entity(EntityKind.ACTION, case.action_id).state,
                    "claimed")

        artifact = self.make_case(EffectKind.ARTIFACT_WRITE, "unreadable-artifact")
        target = Path(artifact.target)
        original_lstat = Path.lstat

        def unreadable(path):
            if path == target:
                raise PermissionError("artifact observation unavailable")
            return original_lstat(path)

        with mock.patch.object(Path, "lstat", new=unreadable):
            result = self.engine.execute_effect(artifact.claimed)
        self.assertEqual(result.state, EffectResultState.UNKNOWN_EFFECT)
        self.assertEqual(self.effect_calls.get(artifact.action_id, 0), 0)
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, artifact.action_id).state,
            "claimed")

    def test_malformed_worktree_authority_is_unknown_not_absent(self):
        case = self.make_case(EffectKind.WORKTREE, "malformed-worktree-list")
        real_git_rc = effects_module.git_adapter.git_rc
        malformed_outputs = (
            "",
            f"HEAD {self.base_oid}\0branch "
            f"{case.claimed.plan.spec['dedicated_ref']}\0\0",
            f"worktree {self.root}\0HEAD {self.base_oid}\0branch "
            f"{case.claimed.plan.spec['dedicated_ref']}\0",
        )
        for output in malformed_outputs:
            with self.subTest(output=repr(output)):
                def malformed_list(repository, *args):
                    if args == ("worktree", "list", "--porcelain", "-z"):
                        return 0, output, ""
                    return real_git_rc(repository, *args)

                with mock.patch.object(
                        effects_module.git_adapter, "git_rc",
                        side_effect=malformed_list):
                    result = self.engine.execute_effect(case.claimed)
                self.assertEqual(result.state, EffectResultState.UNKNOWN_EFFECT)
        self.assertEqual(self.effect_calls.get(case.action_id, 0), 0)
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, case.action_id).state,
            "claimed")

    def test_git_adapter_exceptions_and_malformed_facts_are_unknown(self):
        exploded = self.make_case(EffectKind.GIT_REF, "git-adapter-exception")
        with mock.patch.object(
                effects_module.git_adapter, "git_rc",
                side_effect=RuntimeError("adapter decode failure")):
            exploded_result = self.engine.execute_effect(exploded.claimed)
        self.assertEqual(exploded_result.state, EffectResultState.UNKNOWN_EFFECT)
        self.assertEqual(self.effect_calls.get(exploded.action_id, 0), 0)

        empty_git_dir = self.make_case(EffectKind.GIT_REF, "empty-git-dir-fact")
        real_git_rc = effects_module.git_adapter.git_rc

        def empty_repository_fact(repository, *args):
            if args == ("rev-parse", "--git-dir"):
                return 0, "", ""
            return real_git_rc(repository, *args)

        with mock.patch.object(
                effects_module.git_adapter, "git_rc",
                side_effect=empty_repository_fact):
            empty_result = self.engine.execute_effect(empty_git_dir.claimed)
        self.assertEqual(empty_result.state, EffectResultState.UNKNOWN_EFFECT)
        self.assertEqual(self.effect_calls.get(empty_git_dir.action_id, 0), 0)

        worktree = self.make_case(EffectKind.WORKTREE, "malformed-worktree-head")
        self.engine.execute_effect(worktree.claimed)

        def malformed_head(repository, *args):
            if (Path(repository).resolve() == Path(worktree.target).resolve()
                    and args == ("rev-parse", "--verify", "HEAD")):
                return 0, "", ""
            return real_git_rc(repository, *args)

        with mock.patch.object(
                effects_module.git_adapter, "git_rc", side_effect=malformed_head):
            worktree_observation = self.engine._observe(  # noqa: SLF001
                worktree.claimed.plan)
        self.assertEqual(
            worktree_observation.disposition, ObservationDisposition.UNKNOWN)

        patch = self.make_case(EffectKind.PATCH_INTEGRATION, "malformed-parent-tree")

        def malformed_tree(repository, *args):
            if args == (
                    "rev-parse", "--verify",
                    f"{patch.claimed.plan.spec['expected_parent_oid']}^{{tree}}"):
                return 0, "", ""
            return real_git_rc(repository, *args)

        with mock.patch.object(
                effects_module.git_adapter, "git_rc", side_effect=malformed_tree):
            patch_result = self.engine.execute_effect(patch.claimed)
        self.assertEqual(patch_result.state, EffectResultState.UNKNOWN_EFFECT)
        self.assertEqual(self.effect_calls.get(patch.action_id, 0), 0)

    def test_full_ref_boundary_and_broken_ref_authority_fail_before_effect(self):
        self.store.create_attempt(
            self.run.entity_id, "job", "attempt-one-level-ref")
        with self.assertRaises(InvalidEffectPlan):
            self.engine.plan_effect(
                self.run.entity_id, "job", "attempt-one-level-ref",
                "action-one-level-git-ref",
                GitRefEffect(self.root, "ORIG_HEAD", None, self.base_oid),
            )
        commit, tree = self.integration_commit("one-level-patch-ref")
        with self.assertRaises(InvalidEffectPlan):
            self.engine.plan_effect(
                self.run.entity_id, "job", "attempt-one-level-ref",
                "action-one-level-patch-ref",
                PatchIntegrationEffect(
                    self.root, "AUTO_MERGE", self.base_oid, self.base_tree,
                    commit, tree),
            )

        self.store.create_attempt(
            self.run.entity_id, "job", "attempt-broken-ref-authority")
        ref = "refs/heads/broken-authority"
        broken_path = self.root / ".git" / ref
        broken_path.parent.mkdir(parents=True, exist_ok=True)
        broken_path.write_text("not-an-oid\n", encoding="utf-8")
        plan = self.engine.plan_effect(
            self.run.entity_id, "job", "attempt-broken-ref-authority",
            "action-broken-ref-authority",
            GitRefEffect(self.root, ref, None, self.base_oid),
        )
        claimed = self.engine.claim_effect(plan, ttl_seconds=30)
        result = self.engine.execute_effect(claimed)
        self.assertEqual(result.state, EffectResultState.UNKNOWN_EFFECT)
        self.assertEqual(self.effect_calls.get(plan.action_id, 0), 0)
        self.assertEqual(broken_path.read_text(encoding="utf-8"), "not-an-oid\n")

    def test_worktree_ref_registered_elsewhere_or_partial_after_intent_conflicts(self):
        self.store.create_attempt(
            self.run.entity_id, "job", "attempt-worktree-ref-elsewhere")
        branch = "worktree-ref-elsewhere"
        dedicated_ref = f"refs/heads/{branch}"
        other_path = self.sandbox / "other-worktree-registration"
        self.git(self.root, "branch", branch, self.base_oid)
        self.git(self.root, "worktree", "add", "--quiet", str(other_path), branch)
        target_path = self.sandbox / "different-worktree-target"
        plan = self.engine.plan_effect(
            self.run.entity_id, "job", "attempt-worktree-ref-elsewhere",
            "action-worktree-ref-elsewhere",
            WorktreeEffect(
                self.root, target_path, dedicated_ref, self.base_oid),
        )
        claimed = self.engine.claim_effect(plan, ttl_seconds=30)
        result = self.engine.execute_effect(claimed)
        self.assertEqual(result.state, EffectResultState.CONFLICT)
        self.assertEqual(self.effect_calls.get(plan.action_id, 0), 0)
        self.assertFalse(target_path.exists())

        partial = self.make_case(EffectKind.WORKTREE, "partial-worktree-ref")

        def crash(stage, durable_plan):
            if stage == "after-effect-intent":
                raise InjectedCrash()

        with mock.patch.object(
                self.engine, "_effect_fault_point", side_effect=crash):  # noqa: SLF001
            with self.assertRaises(InjectedCrash):
                self.engine.execute_effect(partial.claimed)
        partial_ref = str(partial.claimed.plan.spec["dedicated_ref"])
        self.git(
            self.root, "update-ref", partial_ref,
            str(partial.claimed.plan.spec["expected_head_oid"]))
        partial_result = self.engine.reconcile_actions([partial.action_id])[0]
        self.assertEqual(partial_result.state, EffectResultState.CONFLICT)
        self.assertEqual(self.effect_calls.get(partial.action_id, 0), 0)

    def test_runner_write_ahead_intent_missing_or_mismatched_marker_never_relaunches(self):
        case = self.make_case(EffectKind.RUNNER_EXECUTION, "runner-wai")

        def crash(stage, plan):
            if stage == "after-effect-intent":
                raise InjectedCrash()

        with mock.patch.object(
                self.engine, "_effect_fault_point", side_effect=crash):  # noqa: SLF001
            with self.assertRaises(InjectedCrash):
                self.engine.execute_effect(case.claimed)
        missing = self.engine.reconcile_actions(
            [case.action_id], quiescence_probe=lambda plan: True)[0]
        self.assertEqual(missing.state, EffectResultState.UNKNOWN_EFFECT)
        self.assertEqual(self.runner_calls.get(case.action_id, 0), 0)

        marker_path = Path(case.claimed.plan.spec["completion_marker"])
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("{}", encoding="utf-8")
        mismatch = self.engine.reconcile_actions(
            [case.action_id], quiescence_probe=lambda plan: True)[0]
        self.assertEqual(mismatch.state, EffectResultState.UNKNOWN_EFFECT)
        self.assertEqual(self.runner_calls.get(case.action_id, 0), 0)

    def test_runner_marker_identity_and_output_artifacts_require_independent_proof(self):
        def publish_marker(intent, *, process_identity, output_digest):
            publish_runner_completion(
                intent.completion_marker_path,
                RunnerCompletionMarker(
                    run_id=intent.run_id,
                    job_id=intent.job_id,
                    action_id=intent.action_id,
                    fencing_epoch=intent.fencing_epoch,
                    launch_token=intent.launch_token,
                    process_identity=process_identity,
                    started_at="2026-07-21T00:00:00Z",
                    finished_at="2026-07-21T00:00:01Z",
                    returncode=0,
                    signal=None,
                    stdout_artifact_digest=output_digest,
                    stderr_artifact_digest=output_digest,
                ),
            )

        forged = self.make_case(EffectKind.RUNNER_EXECUTION, "runner-forged-identity")
        published = self.engine._artifacts.write(b"published-output").digest  # noqa: SLF001

        def forged_executor(intent):
            publish_marker(
                intent, process_identity="forged-process", output_digest=published)

        with mock.patch.object(self.engine, "_runner_executor", forged_executor):  # noqa: SLF001
            forged_result = self.engine.execute_effect(forged.claimed)
        self.assertEqual(forged_result.state, EffectResultState.UNKNOWN_EFFECT)
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, forged.action_id).state,
            "effect")

        missing = self.make_case(EffectKind.RUNNER_EXECUTION, "runner-missing-output")
        unpublished = self.sha256(b"unpublished-runner-output")

        def missing_executor(intent):
            publish_marker(
                intent,
                process_identity=f"fixture-process:{intent.action_id}",
                output_digest=unpublished,
            )

        with mock.patch.object(self.engine, "_runner_executor", missing_executor):  # noqa: SLF001
            missing_result = self.engine.execute_effect(missing.claimed)
        self.assertEqual(missing_result.state, EffectResultState.UNKNOWN_EFFECT)
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, missing.action_id).state,
            "effect")

    def test_observed_recovery_requires_bound_rehashable_observation_receipt(self):
        case = self.make_case(EffectKind.ARTIFACT_WRITE, "missing-observation-receipt")

        def crash(stage, plan):
            if stage == "after-observed":
                raise InjectedCrash()

        with mock.patch.object(
                self.engine, "_effect_fault_point", side_effect=crash):  # noqa: SLF001
            with self.assertRaises(InjectedCrash):
                self.engine.execute_effect(case.claimed)
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, case.action_id).state,
            "observed")
        receipt = self.store._connection.execute(  # noqa: SLF001
            "SELECT digest FROM artifacts WHERE entity_id = ? "
            "AND reference_id LIKE 'effect-observation:%'",
            (case.action_id,),
        ).fetchone()[0]
        self.engine._artifacts.path_for(receipt).unlink()  # noqa: SLF001

        result = self.engine.reconcile_actions([case.action_id])[0]
        self.assertEqual(result.state, EffectResultState.UNKNOWN_EFFECT)
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, case.action_id).state,
            "observed")
        completed = self.store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM transitions WHERE entity_id = ? "
            "AND next_state = 'completed'",
            (case.action_id,),
        ).fetchone()[0]
        self.assertEqual(completed, 0)

    def test_stale_principal_effect_start_submit_and_completion_are_guarded(self):
        stages = (
            ("before-effect-intent", "claimed", 0, 0),
            ("after-effect-intent", "effect", 0, 1),
            ("after-external-effect", "effect", 1, 2),
            ("after-observed", "observed", 1, 3),
        )
        for stage, expected_state, expected_calls, expected_artifacts in stages:
            with self.subTest(stage=stage):
                case = self.make_case(EffectKind.ARTIFACT_WRITE, f"stale-{stage}")
                before_artifacts = set(
                    self.engine._artifacts.directory.glob("sha256-*")  # noqa: SLF001
                )

                def stale(point, plan):
                    if point == stage:
                        self.store._connection.execute(  # noqa: SLF001
                            "UPDATE leases SET owner_token = ? WHERE lease_id = ?",
                            (f"replacement-owner-{stage}", plan.action_id),
                        )

                with mock.patch.object(
                        self.engine, "_effect_fault_point", side_effect=stale):  # noqa: SLF001
                    with self.assertRaises(LeasePrincipalMismatch) as raised:
                        self.engine.execute_effect(case.claimed)
                self.assertEqual(raised.exception.code, "lease_principal_mismatch")
                self.assertEqual(
                    self.store.get_entity(EntityKind.ACTION, case.action_id).state,
                    expected_state)
                self.assertEqual(
                    self.effect_calls.get(case.action_id, 0), expected_calls)
                after_artifacts = set(
                    self.engine._artifacts.directory.glob("sha256-*")  # noqa: SLF001
                )
                self.assertEqual(
                    len(after_artifacts - before_artifacts), expected_artifacts)

    def test_retry_requires_completed_old_action_new_attempt_and_new_action(self):
        old = self.make_case(EffectKind.ARTIFACT_WRITE, "retry-old")
        self.engine.execute_effect(old.claimed)
        self.store.create_attempt(self.run.entity_id, "job", "attempt-retry-new")
        with self.assertRaises(EffectRetryRefused):
            self.engine.plan_retry_effect(
                old.action_id, run_id=self.run.entity_id, job_id="job",
                attempt_id="attempt-retry-new", action_id=old.action_id,
                effect=ArtifactWriteEffect(b"retry"))
        with self.assertRaises(EffectRetryRefused):
            self.engine.plan_retry_effect(
                old.action_id, run_id=self.run.entity_id, job_id="job",
                attempt_id=old.claimed.plan.attempt_id, action_id="retry-same-attempt",
                effect=ArtifactWriteEffect(b"retry"))

        pending = self.make_case(EffectKind.ARTIFACT_WRITE, "retry-pending")
        self.store.create_attempt(self.run.entity_id, "job", "attempt-retry-pending")
        with self.assertRaises(EffectRetryRefused):
            self.engine.plan_retry_effect(
                pending.action_id, run_id=self.run.entity_id, job_id="job",
                attempt_id="attempt-retry-pending", action_id="retry-nonterminal",
                effect=ArtifactWriteEffect(b"retry"))

        retry = self.engine.plan_retry_effect(
            old.action_id, run_id=self.run.entity_id, job_id="job",
            attempt_id="attempt-retry-new", action_id="retry-valid",
            effect=ArtifactWriteEffect(b"retry"))
        self.assertEqual(retry.retry_of, old.action_id)
        self.assertNotEqual(retry.action_id, old.action_id)
        self.assertNotEqual(retry.attempt_id, old.claimed.plan.attempt_id)
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, retry.action_id).state,
            "planned")

    def test_runner_retry_lineage_cannot_be_bypassed_with_plain_plan(self):
        uncertain = self.make_case(EffectKind.RUNNER_EXECUTION, "runner-uncertain-retry")

        def crash(stage, plan):
            if stage == "after-effect-intent":
                raise InjectedCrash()

        with mock.patch.object(
                self.engine, "_effect_fault_point", side_effect=crash):  # noqa: SLF001
            with self.assertRaises(InjectedCrash):
                self.engine.execute_effect(uncertain.claimed)
        self.store.create_attempt(
            self.run.entity_id, "job", "attempt-runner-bypass")
        with self.assertRaises(EffectRetryRefused):
            self.engine.plan_effect(
                self.run.entity_id, "job", "attempt-runner-bypass",
                "action-runner-bypass", uncertain.effect)
        with self.assertRaises(RecordNotFoundError):
            self.store.get_entity(EntityKind.ACTION, "action-runner-bypass")
        self.assertEqual(self.runner_calls.get(uncertain.action_id, 0), 0)

        completed = self.make_case(EffectKind.RUNNER_EXECUTION, "runner-completed-retry")
        self.engine.execute_effect(completed.claimed)
        self.store.create_attempt(
            self.run.entity_id, "job", "attempt-runner-completed-retry")
        with self.assertRaises(EffectRetryRefused):
            self.engine.plan_effect(
                self.run.entity_id, "job", "attempt-runner-completed-retry",
                "action-runner-no-lineage", completed.effect)
        retry = self.engine.plan_retry_effect(
            completed.action_id,
            run_id=self.run.entity_id,
            job_id="job",
            attempt_id="attempt-runner-completed-retry",
            action_id="action-runner-explicit-retry",
            effect=completed.effect,
        )
        self.assertEqual(retry.retry_of, completed.action_id)

    def test_concurrent_runner_planning_atomically_reserves_one_invocation(self):
        for attempt_id in ("attempt-runner-race-a", "attempt-runner-race-b"):
            self.store.create_attempt(self.run.entity_id, "job", attempt_id)
        with mock.patch.object(
                store_module, "_probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)):
            other_store = RunStore.open(self.root)
        self.addCleanup(other_store.close)
        other_engine = EffectEngine(
            other_store, LeaseManager(other_store),
            runner_executor=self.runner_executor,
            runner_identity_verifier=self.runner_identity_verifier,
        )
        invocation = RunnerExecutionEffect(self.sha256(b"one-concurrent-invocation"))
        barrier = threading.Barrier(2)

        def synchronize_lineage_check(**kwargs):
            del kwargs
            barrier.wait()

        def plan(engine, attempt_id, action_id):
            try:
                return engine.plan_effect(
                    self.run.entity_id, "job", attempt_id, action_id, invocation)
            except EffectRetryRefused as error:
                return error

        with mock.patch.object(
                self.engine, "_validate_runner_retry_lineage",
                side_effect=synchronize_lineage_check), mock.patch.object(
                    other_engine, "_validate_runner_retry_lineage",
                    side_effect=synchronize_lineage_check), ThreadPoolExecutor(
                        max_workers=2) as executor:
            futures = (
                executor.submit(
                    plan, self.engine,
                    "attempt-runner-race-a", "action-runner-race-a"),
                executor.submit(
                    plan, other_engine,
                    "attempt-runner-race-b", "action-runner-race-b"),
            )
            results = tuple(future.result() for future in futures)
        self.assertEqual(
            sum(isinstance(result, EffectRetryRefused) for result in results), 1)
        planned = self.store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM actions WHERE action_id IN (?, ?)",
            ("action-runner-race-a", "action-runner-race-b"),
        ).fetchone()[0]
        self.assertEqual(planned, 1)

    def test_conflicting_git_and_artifact_state_is_preserved_without_blind_retry(self):
        self._number += 1
        attempt = f"attempt-conflict-{self._number}"
        self.store.create_attempt(self.run.entity_id, "job", attempt)
        other_commit, _ = self.integration_commit("git-conflict")
        ref = "refs/heads/conflicting-ref"
        self.git(self.root, "update-ref", ref, other_commit)
        git_plan = self.engine.plan_effect(
            self.run.entity_id, "job", attempt, "action-git-conflict",
            GitRefEffect(self.root, ref, self.base_oid, self.base_oid))
        git_claimed = self.engine.claim_effect(git_plan, ttl_seconds=30)
        git_result = self.engine.execute_effect(git_claimed)
        self.assertEqual(git_result.state, EffectResultState.CONFLICT)
        self.assertEqual(self.git(self.root, "rev-parse", ref), other_commit)
        self.assertEqual(self.effect_calls.get("action-git-conflict", 0), 0)

        self.store.create_attempt(
            self.run.entity_id, "job", "attempt-artifact-conflict")
        content = b"desired artifact"
        artifact_plan = self.engine.plan_effect(
            self.run.entity_id, "job", "attempt-artifact-conflict",
            "action-artifact-conflict", ArtifactWriteEffect(content))
        target = self.engine._artifacts.path_for(self.sha256(content))  # noqa: SLF001
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"different bytes")
        artifact_claimed = self.engine.claim_effect(artifact_plan, ttl_seconds=30)
        artifact_result = self.engine.execute_effect(artifact_claimed)
        self.assertEqual(artifact_result.state, EffectResultState.CONFLICT)
        self.assertEqual(target.read_bytes(), b"different bytes")
        self.assertEqual(self.effect_calls.get("action-artifact-conflict", 0), 0)

    def test_git_ref_expected_old_oid_cas_updates_only_the_expected_state(self):
        self.store.create_attempt(self.run.entity_id, "job", "attempt-git-cas")
        desired, _ = self.integration_commit("git-cas")
        ref = "refs/heads/git-cas"
        self.git(self.root, "update-ref", ref, self.base_oid)
        plan = self.engine.plan_effect(
            self.run.entity_id, "job", "attempt-git-cas", "action-git-cas",
            GitRefEffect(self.root, ref, self.base_oid, desired))
        claimed = self.engine.claim_effect(plan, ttl_seconds=30)
        result = self.engine.execute_effect(claimed)
        self.assertEqual(result.state, EffectResultState.COMPLETED)
        self.assertEqual(self.git(self.root, "rev-parse", ref), desired)
        self.assertEqual(self.effect_calls.get("action-git-cas"), 1)

    def test_patch_adoption_rederives_expected_parent_tree_precondition(self):
        self.store.create_attempt(
            self.run.entity_id, "job", "attempt-patch-parent-tree")
        desired, desired_tree = self.integration_commit("patch-parent-tree")
        ref = "refs/heads/patch-parent-tree"
        self.git(self.root, "update-ref", ref, desired)
        plan = self.engine.plan_effect(
            self.run.entity_id, "job", "attempt-patch-parent-tree",
            "action-patch-parent-tree",
            PatchIntegrationEffect(
                self.root, ref, self.base_oid, "0" * 40,
                desired, desired_tree),
        )
        claimed = self.engine.claim_effect(plan, ttl_seconds=30)
        result = self.engine.execute_effect(claimed)
        self.assertEqual(result.state, EffectResultState.CONFLICT)
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, plan.action_id).state,
            "claimed")
        self.assertEqual(self.effect_calls.get(plan.action_id, 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
