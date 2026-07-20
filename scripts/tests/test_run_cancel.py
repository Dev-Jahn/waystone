#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Contract tests for M1-B cancellation, quiescence, and cleanup."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
_WAYSTONE_PRELOADED = "waystone" in sys.modules
sys.path.insert(0, str(ROOT))
try:
    from waystone.runs import store as store_module  # noqa: E402
    from waystone.runs.artifacts import ArtifactStore  # noqa: E402
    from waystone.runs.cancel import (  # noqa: E402
        CancelPendingReason,
        CancellationEngine,
        CancellationIdentityRefusal,
        CancellationScopeRefusal,
        CleanupDisposition,
        CleanupExecutionError,
        CleanupRefused,
        SignalCapabilityUnavailable,
        SignalDeliveryError,
    )
    from waystone.runs.effects import (  # noqa: E402
        EffectEngine,
        EffectResult,
        EffectResultState,
        RunnerExecutionEffect,
    )
    from waystone.runs.lease import LeaseManager  # noqa: E402
    from waystone.runs.store import (  # noqa: E402
        EntityKind,
        FilesystemInfo,
        RunStore,
    )
    from waystone.runs.supervisor import (  # noqa: E402
        HeartbeatFreshness,
        LivenessObservation,
        LivenessState,
        ProcessIdentity,
        RunnerInvocation,
        Supervisor,
        capture_process_identity,
        observe_process_identity,
    )
finally:
    sys.path.pop(0)
    if not _WAYSTONE_PRELOADED:
        sys.modules.pop("waystone", None)
del _WAYSTONE_PRELOADED


class RunCancelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.sandbox = Path(self._temporary_directory.name)
        self.root = self.sandbox / "project"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.com")
        self.git("config", "user.name", "Fixture")
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "base.txt")
        self.git("commit", "-qm", "base")
        self.base_oid = self.git("rev-parse", "HEAD")
        (self.root / ".waystone.yml").write_text(
            "version: 1\nproject: cancel-fixture\n", encoding="utf-8")
        with mock.patch.object(
                store_module, "_probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)):
            self.store = RunStore.open(self.root)
        self.addCleanup(self.store.close)
        self.leases = LeaseManager(self.store)
        self.effects = EffectEngine(self.store, self.leases)
        self.supervisor = Supervisor(
            self.store, self.leases, invocations={},
            heartbeat_interval=1, lease_ttl=5)
        self.cancellation = CancellationEngine(
            self.store, self.effects, self.leases, self.supervisor)
        self._number = 0

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    @staticmethod
    def sha256(payload: bytes) -> str:
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def wait_for(self, predicate, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for cancellation fixture state")

    def graph(self, label: str):
        self._number += 1
        stem = f"{label}-{self._number}"
        run = self.store.create_run(initial_state="running")
        job_id = f"job-{stem}"
        attempt_id = f"attempt-{stem}"
        self.store.create_job(run.entity_id, job_id)
        self.store.create_attempt(run.entity_id, job_id, attempt_id)
        return run, job_id, attempt_id, f"action-{stem}"

    def plan_runner(self, label: str, *, effects: EffectEngine | None = None):
        engine = self.effects if effects is None else effects
        run, job_id, attempt_id, action_id = self.graph(label)
        digest = self.sha256(f"runner-{label}-{self._number}".encode())
        plan = engine.plan_effect(
            run.entity_id, job_id, attempt_id, action_id,
            RunnerExecutionEffect(digest))
        claimed = engine.claim_effect(plan, ttl_seconds=30)
        action = self.store.get_entity(EntityKind.ACTION, action_id)
        return run, action, claimed.principal, digest

    def launch_actual(self, label: str, program: str):
        run, job_id, attempt_id, action_id = self.graph(label)
        digest = self.sha256(f"actual-{label}-{self._number}".encode())
        invocation = RunnerInvocation(
            (sys.executable, "-c", program), self.root)
        supervisor = Supervisor(
            self.store, self.leases, invocations={digest: invocation},
            heartbeat_interval=0.05, lease_ttl=0.5)
        effects = EffectEngine(
            self.store, self.leases,
            runner_executor=supervisor.runner_executor,
            runner_identity_verifier=supervisor.runner_identity_verifier)
        plan = effects.plan_effect(
            run.entity_id, job_id, attempt_id, action_id,
            RunnerExecutionEffect(digest))
        claimed = effects.claim_effect(plan, ttl_seconds=5)
        effects.execute_effect(claimed)
        runtime_path = supervisor._runtime_path(action_id)  # noqa: SLF001
        self.wait_for(runtime_path.is_file)
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        identity = ProcessIdentity.from_payload(runtime["process_identity"])

        def settle_if_needed() -> None:
            if observe_process_identity(identity).state is LivenessState.ALIVE:
                os.kill(identity.pid, signal.SIGKILL)

        self.addCleanup(settle_if_needed)
        return run, action_id, supervisor, effects, identity

    def terminal_actual(self, label: str):
        run, action_id, supervisor, effects, _identity = self.launch_actual(
            label, "pass")
        self.wait_for(
            lambda: supervisor.probe_action(action_id).state is LivenessState.EXITED)
        cancellation = CancellationEngine(
            self.store, effects, self.leases, supervisor)
        cancellation.request_cancel(
            run.entity_id, action_id, reason="user-requested")
        terminal = cancellation.resume_cancel(run.entity_id, action_id)
        self.assertEqual(terminal.state, "canceled")
        return run, action_id, supervisor, effects

    def resources(self, label: str):
        worktree = self.sandbox / f"worktree-{label}"
        worktree.mkdir()
        sentinel = worktree / "sentinel.bin"
        sentinel.write_bytes(f"worktree-{label}".encode())
        ref = f"refs/heads/cancel-{label}"
        self.git("update-ref", ref, self.base_oid)
        artifact = ArtifactStore(self.root).write(
            f"artifact-{label}".encode()).path
        return worktree, sentinel, ref, artifact

    def resource_snapshot(self, resources):
        worktree, sentinel, ref, artifact = resources
        return {
            "worktree_exists": worktree.is_dir(),
            "worktree_bytes": sentinel.read_bytes() if sentinel.exists() else None,
            "ref_oid": self.git("rev-parse", ref) if self.ref_exists(ref) else None,
            "artifact_bytes": artifact.read_bytes() if artifact.exists() else None,
        }

    def ref_exists(self, ref: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self.root), "show-ref", "--verify", "--quiet", ref],
            check=False,
        )
        return result.returncode == 0

    def cleanup_executor(self, resources, calls, expected_action_id: str):
        worktree, _sentinel, ref, artifact = resources

        def cleanup(plan) -> None:
            self.assertFalse(self.store._connection.in_transaction)  # noqa: SLF001
            self.assertEqual(plan.action_id, expected_action_id)
            self.assertEqual(plan.principal.action_id, expected_action_id)
            self.assertEqual(
                plan.cleanup_principal.action_id, plan.cleanup_action_id)
            self.assertEqual(plan.executor_id, "fixture.cleanup.v1")
            calls.append(plan.cleanup_id)
            if worktree.exists():
                shutil.rmtree(worktree)
            if self.ref_exists(ref):
                self.git("update-ref", "-d", ref)
            if artifact.exists():
                artifact.unlink()

        return cleanup

    @staticmethod
    def unknown(reason: str = "process-observation-unavailable:fixture"):
        return LivenessObservation(
            LivenessState.UNKNOWN, reason,
            heartbeat=HeartbeatFreshness.UNKNOWN)

    @staticmethod
    def alive():
        return LivenessObservation(
            LivenessState.ALIVE, "process-identity-matched",
            heartbeat=HeartbeatFreshness.FRESH)

    @staticmethod
    def exited():
        return LivenessObservation(
            LivenessState.EXITED, "supervisor-wait-status",
            exact_identity_absent=True,
            heartbeat=HeartbeatFreshness.STALE)

    def run_transitions(self, run_id: str) -> list[str]:
        rows = self.store._connection.execute(  # noqa: SLF001
            "SELECT next_state FROM transitions WHERE entity_kind = ? "
            "AND entity_id = ? ORDER BY entity_version",
            (EntityKind.RUN.value, run_id),
        ).fetchall()
        return [row["next_state"] for row in rows]

    def publish_identity_fixture(
            self, run, action, principal, digest: str, mode: str = "valid"):
        executable = str(Path(sys.executable).resolve())
        launch_token = f"launch-{action.entity_id}"
        worker_identity = capture_process_identity(
            os.getpid(), action_id=action.entity_id,
            owner_token=principal.owner_token,
            fencing_epoch=principal.fencing_epoch,
            resolved_executable=executable,
            invocation_digest=digest)
        supervisor_identity = capture_process_identity(
            os.getpid(), action_id=action.entity_id,
            owner_token=principal.owner_token,
            fencing_epoch=principal.fencing_epoch,
            resolved_executable=executable)
        launch = {
            "schema": "waystone-supervisor-launch-1",
            "project_root": str(self.root.resolve()),
            "run_id": run.entity_id,
            "job_id": action.parent_job_id,
            "action_id": action.entity_id,
            "owner_token": principal.owner_token,
            "fencing_epoch": principal.fencing_epoch,
            "entity_version": principal.entity_version,
            "invocation_digest": digest,
            "launch_token": launch_token,
            "completion_marker_path": str(
                self.supervisor._marker_path(action.entity_id)),  # noqa: SLF001
            "argv": [executable],
            "cwd": str(self.root.resolve()),
            "heartbeat_interval": 1.0,
            "lease_ttl": 5.0,
        }
        runtime = {
            "schema": "waystone-supervisor-runtime-1",
            "run_id": run.entity_id,
            "job_id": action.parent_job_id,
            "action_id": action.entity_id,
            "owner_token": principal.owner_token,
            "fencing_epoch": principal.fencing_epoch,
            "entity_version": principal.entity_version,
            "invocation_digest": digest,
            "launch_token": launch_token,
            "started_at": "2026-07-21T00:00:00Z",
            "supervisor_identity": supervisor_identity.to_payload(),
            "process_identity": worker_identity.to_payload(),
        }
        if mode == "runtime-run":
            runtime["run_id"] = "different-run"
        elif mode == "runtime-action":
            runtime["action_id"] = "different-action"
        elif mode == "runtime-owner":
            runtime["owner_token"] = "different-owner"
        elif mode == "runtime-fence":
            runtime["fencing_epoch"] = principal.fencing_epoch + 1
        elif mode == "runtime-version":
            runtime["entity_version"] = principal.entity_version + 1
        elif mode == "launch-owner":
            launch["owner_token"] = "different-owner"
        elif mode.startswith("embedded-"):
            embedded = dict(runtime["process_identity"])
            if mode == "embedded-action":
                embedded["action_id"] = "different-action"
            elif mode == "embedded-owner":
                embedded["supervisor_owner_token"] = "different-owner"
            elif mode == "embedded-fence":
                embedded["fencing_epoch"] = principal.fencing_epoch + 1
            elif mode == "embedded-invocation":
                embedded["invocation_digest"] = self.sha256(b"different")
            elif mode == "embedded-executable":
                embedded["resolved_executable"] = "/different/executable"
            runtime["process_identity"] = embedded
        launch_path = self.supervisor._launch_path(action.entity_id)  # noqa: SLF001
        runtime_path = self.supervisor._runtime_path(action.entity_id)  # noqa: SLF001
        launch_path.parent.mkdir(parents=True, exist_ok=True)
        launch_path.write_text(json.dumps(launch), encoding="utf-8")
        runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
        return worker_identity

    def test_fixture_4_case_1_unknown_effect_records_intent_and_preserves_resources(self):
        """§6 fixture 4 / case 1: unknown-effect records intent; bytes stay exact."""
        run, action, _principal, _digest = self.plan_runner("unknown")
        action_before = action
        lease_before = self.store._connection.execute(  # noqa: SLF001
            "SELECT owner_token, fencing_epoch, entity_version FROM leases "
            "WHERE lease_id = ?",
            (action.entity_id,),
        ).fetchone()
        resources = self.resources("fixture-4")
        before = self.resource_snapshot(resources)
        signal_calls = []
        cleanup_calls = []
        engine = CancellationEngine(
            self.store, self.effects, self.leases, self.supervisor,
            signal_sender=lambda identity: signal_calls.append(identity),
            cleanup_executor=lambda plan: cleanup_calls.append(plan),
            cleanup_executor_id="fixture.cleanup.v1")

        result = engine.request_cancel(
            run.entity_id, action.entity_id, reason="user-requested")

        self.assertEqual(result.state, "cancel-pending(reason=unknown-effect)")
        self.assertEqual(result.pending_reason, CancelPendingReason.UNKNOWN_EFFECT)
        self.assertFalse(result.signal_sent)
        self.assertEqual(signal_calls, [])
        self.assertEqual(cleanup_calls, [])
        self.assertEqual(
            self.run_transitions(run.entity_id)[-2:],
            ["cancel-requested", "cancel-pending(reason=unknown-effect)"])
        self.assertNotIn("stopping", self.run_transitions(run.entity_id))
        self.assertNotIn("canceled", self.run_transitions(run.entity_id))
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, action.entity_id),
            action_before)
        lease_after = self.store._connection.execute(  # noqa: SLF001
            "SELECT owner_token, fencing_epoch, entity_version FROM leases "
            "WHERE lease_id = ?",
            (action.entity_id,),
        ).fetchone()
        self.assertEqual(tuple(lease_after), tuple(lease_before))
        self.assertEqual(self.resource_snapshot(resources), before)
        intent = self.store.get_artifact_reference(
            f"cancellation-intent:{run.entity_id}")
        intent_payload = json.loads(ArtifactStore(self.root).read_reference(intent))
        self.assertEqual(intent_payload["action_id"], action.entity_id)
        self.assertEqual(intent_payload["reason"], "user-requested")

    def test_fixture_4_case_2_unverified_identity_never_signals(self):
        """§6 fixture 4 / case 2: stale/incoherent identity signals zero times."""
        modes = (
            "runtime-run", "runtime-action", "runtime-owner", "runtime-fence",
            "runtime-version", "launch-owner", "embedded-action",
            "embedded-owner", "embedded-fence", "embedded-invocation",
            "embedded-executable",
        )
        for mode in modes:
            with self.subTest(mode=mode):
                run, action, principal, digest = self.plan_runner(mode)
                self.publish_identity_fixture(
                    run, action, principal, digest, mode)
                resources = self.resources(mode)
                before = self.resource_snapshot(resources)
                sent = []
                engine = CancellationEngine(
                    self.store, self.effects, self.leases, self.supervisor,
                    signal_sender=lambda identity: sent.append(identity))

                self.assertEqual(
                    self.supervisor.probe_action(action.entity_id).state,
                    LivenessState.ALIVE)
                result = engine.request_cancel(
                    run.entity_id, action.entity_id, reason="user-requested")

                self.assertEqual(sent, [])
                self.assertEqual(
                    result.state, "cancel-pending(reason=identity-unknown)")
                self.assertEqual(
                    self.run_transitions(run.entity_id)[-2:],
                    ["cancel-requested",
                     "cancel-pending(reason=identity-unknown)"])
                self.assertNotIn("stopping", self.run_transitions(run.entity_id))
                self.assertEqual(self.resource_snapshot(resources), before)

        run, action, principal, _digest = self.plan_runner("stale")
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        stale = capture_process_identity(
            child.pid, action_id=action.entity_id,
            owner_token=principal.owner_token,
            fencing_epoch=principal.fencing_epoch,
            resolved_executable=str(Path(sys.executable).resolve()))
        child.terminate()
        child.wait(timeout=5)
        sent = []
        engine = CancellationEngine(
            self.store, self.effects, self.leases, self.supervisor,
            signal_sender=lambda identity: sent.append(identity))
        with mock.patch.object(
                engine, "_recorded_identity", return_value=stale), mock.patch.object(
                self.supervisor, "probe_action", return_value=self.alive()):
            result = engine.request_cancel(
                run.entity_id, action.entity_id, reason="user-requested")
        self.assertEqual(sent, [])
        self.assertEqual(
            result.state, "cancel-pending(reason=identity-unknown)")

    def test_missing_signal_capability_fails_loud_after_intent(self):
        run, action, principal, digest = self.plan_runner("missing-signal")
        self.publish_identity_fixture(run, action, principal, digest)

        with self.assertRaises(SignalCapabilityUnavailable):
            self.cancellation.request_cancel(
                run.entity_id, action.entity_id, reason="user-requested")

        self.assertEqual(
            self.store.get_run(run.entity_id).state, "cancel-requested")
        self.assertNotIn(
            "cancel-pending(reason=identity-unknown)",
            self.run_transitions(run.entity_id))

    def test_fixture_4_case_3_exited_unreconciled_refuses_terminal_and_cleanup(self):
        """§6 fixture 4 / case 3: EXITED alone cannot terminalize or clean."""
        run, action, _principal, _digest = self.plan_runner("unreconciled")
        resources = self.resources("unreconciled")
        before = self.resource_snapshot(resources)
        observation = self.exited()
        effect = EffectResult(
            action.entity_id, EffectResultState.EXITED_UNRECONCILED)
        cleanup_calls = []
        engine = CancellationEngine(
            self.store, self.effects, self.leases, self.supervisor,
            cleanup_executor=lambda plan: cleanup_calls.append(plan),
            cleanup_executor_id="fixture.cleanup.v1")
        self.assertTrue(observation.destructive_resolution_allowed)

        with mock.patch.object(
                self.effects, "inspect_effect", return_value=effect), mock.patch.object(
                self.effects, "reconcile_actions",
                return_value=(effect,)) as reconcile, mock.patch.object(
                self.supervisor, "probe_action", return_value=observation):
            requested = engine.request_cancel(
                run.entity_id, action.entity_id, reason="user-requested")
            resumed = engine.resume_cancel(run.entity_id, action.entity_id)
            with self.assertRaises(CleanupRefused):
                engine.cleanup(run.entity_id, action.entity_id)

        self.assertEqual(requested.state, "cancel-requested")
        self.assertEqual(
            resumed.state, "cancel-pending(reason=unknown-effect)")
        self.assertNotIn("canceled", self.run_transitions(run.entity_id))
        reconcile.assert_called_once()
        self.assertEqual(cleanup_calls, [])
        self.assertEqual(self.resource_snapshot(resources), before)

        gate_run, gate_action, gate_supervisor, gate_effects = (
            self.terminal_actual("cleanup-and-gate"))
        gate_resources = self.resources("cleanup-and-gate")
        gate_before = self.resource_snapshot(gate_resources)
        gate_calls = []
        gate_engine = CancellationEngine(
            self.store, gate_effects, self.leases, gate_supervisor,
            cleanup_executor=lambda plan: gate_calls.append(plan),
            cleanup_executor_id="fixture.cleanup.v1")
        unreconciled = EffectResult(
            gate_action, EffectResultState.EXITED_UNRECONCILED)
        with mock.patch.object(
                gate_effects, "inspect_effect",
                return_value=unreconciled), mock.patch.object(
                gate_supervisor, "probe_action",
                return_value=observation):
            with self.assertRaises(CleanupRefused) as cleanup_refusal:
                gate_engine.cleanup(gate_run.entity_id, gate_action)
        self.assertIn("reconciliation", cleanup_refusal.exception.detail)
        self.assertEqual(gate_calls, [])
        self.assertEqual(
            self.resource_snapshot(gate_resources), gate_before)

    def test_fixture_4_case_4_verified_reconcile_cleanup_is_restart_idempotent(self):
        """§6 fixture 4 / case 4: verified stop, reconcile, then cleanup twice."""
        run, action_id, supervisor, effects, identity = self.launch_actual(
            "normal", "import time; time.sleep(30)")
        resources = self.resources("normal")
        sibling_resources = self.resources("normal-unrelated")
        before = self.resource_snapshot(resources)
        sibling_before = self.resource_snapshot(sibling_resources)
        signals = []
        cleanup_calls = []

        def send_signal(target: ProcessIdentity) -> None:
            state = self.store._connection.execute(  # noqa: SLF001
                "SELECT state FROM runs WHERE run_id = ?",
                (run.entity_id,),
            ).fetchone()["state"]
            self.assertEqual(state, "stopping")
            signals.append(target)
            os.kill(target.pid, signal.SIGTERM)

        cancellation = CancellationEngine(
            self.store, effects, self.leases, supervisor,
            signal_sender=send_signal,
            cleanup_executor=self.cleanup_executor(
                resources, cleanup_calls, action_id),
            cleanup_executor_id="fixture.cleanup.v1")
        self.wait_for(
            lambda: supervisor.probe_action(action_id).state is LivenessState.ALIVE)
        requested = cancellation.request_cancel(
            run.entity_id, action_id, reason="user-requested")
        self.assertEqual(requested.state, "stopping")
        self.assertTrue(requested.signal_sent)
        self.assertEqual(signals, [identity])
        self.assertEqual(self.resource_snapshot(resources), before)
        self.assertEqual(self.resource_snapshot(sibling_resources), sibling_before)
        self.assertEqual(cleanup_calls, [])
        self.wait_for(
            lambda: supervisor.probe_action(action_id).state is LivenessState.EXITED)
        self.assertEqual(
            effects.inspect_effect(action_id).state,
            EffectResultState.EXITED_UNRECONCILED)
        self.assertEqual(self.resource_snapshot(resources), before)

        with mock.patch.object(
                effects, "reconcile_actions",
                wraps=effects.reconcile_actions) as reconcile:
            terminal = cancellation.resume_cancel(run.entity_id, action_id)
        completed_action = self.store.get_entity(EntityKind.ACTION, action_id)
        self.assertEqual(terminal.state, "canceled")
        self.assertEqual(completed_action.state, "completed")
        reconcile.assert_called_once()
        self.assertEqual(
            self.run_transitions(run.entity_id)[-3:],
            ["cancel-requested", "stopping", "canceled"])
        self.assertEqual(self.resource_snapshot(resources), before)
        self.assertEqual(cleanup_calls, [])

        first = cancellation.cleanup(run.entity_id, action_id)
        cleanup_action = self.store.get_entity(
            EntityKind.ACTION, first.cleanup_action_id)
        self.assertEqual(cleanup_action.state, "cleanup-completed")
        cleanup_transitions = self.store._connection.execute(  # noqa: SLF001
            "SELECT next_state FROM transitions WHERE entity_kind = ? "
            "AND entity_id = ? ORDER BY entity_version",
            (EntityKind.ACTION.value, cleanup_action.entity_id),
        ).fetchall()
        self.assertEqual(
            [row["next_state"] for row in cleanup_transitions],
            ["planned", "cleanup-ready", "cleanup-executing",
             "cleanup-completed"])
        self.assertEqual(
            self.resource_snapshot(resources),
            {"worktree_exists": False, "worktree_bytes": None,
             "ref_oid": None, "artifact_bytes": None})
        self.assertEqual(self.resource_snapshot(sibling_resources), sibling_before)

        def poison_cleanup(_plan) -> None:
            raise AssertionError("durable completed cleanup must not execute again")

        restarted = CancellationEngine(
            self.store, effects, self.leases, supervisor,
            cleanup_executor=poison_cleanup,
            cleanup_executor_id="fixture.cleanup.v1")
        with mock.patch.object(
                effects, "inspect_effect",
                side_effect=AssertionError("second cleanup must not inspect effect")), \
                mock.patch.object(
                    supervisor, "probe_action",
                    side_effect=AssertionError(
                        "second cleanup must not probe liveness")):
            second = restarted.cleanup(run.entity_id, action_id)
        self.assertEqual(first.disposition, CleanupDisposition.CLEANED)
        self.assertEqual(second.disposition, CleanupDisposition.NOOP)
        self.assertEqual(second.cleanup_action_id, first.cleanup_action_id)
        self.assertEqual(len(cleanup_calls), 1)

    def test_fixture_4_case_5_expiry_and_no_heartbeat_never_authorize_cleanup(self):
        """§6 fixture 4 / case 5: expiry plus heartbeat absence is no authority."""
        run, action_id, supervisor, effects = self.terminal_actual("expired")
        resources = self.resources("expired")
        before = self.resource_snapshot(resources)
        cleanup_calls = []
        engine = CancellationEngine(
            self.store, effects, self.leases, supervisor,
            cleanup_executor=self.cleanup_executor(
                resources, cleanup_calls, action_id),
            cleanup_executor_id="fixture.cleanup.v1")
        self.store._connection.execute(  # noqa: SLF001 - durable expiry fixture
            "UPDATE leases SET expires_at = '1970-01-01T00:00:00Z' "
            "WHERE lease_id = ?",
            (action_id,),
        )
        self.store._connection.execute(  # noqa: SLF001
            "UPDATE action_runtime SET heartbeat_at = NULL WHERE action_id = ?",
            (action_id,),
        )
        supervisor._heartbeat_path(action_id).unlink(missing_ok=True)  # noqa: SLF001
        lease = self.store._connection.execute(  # noqa: SLF001
            "SELECT expires_at FROM leases WHERE lease_id = ?",
            (action_id,),
        ).fetchone()
        heartbeat = self.store._connection.execute(  # noqa: SLF001
            "SELECT heartbeat_at FROM action_runtime WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        self.assertEqual(lease["expires_at"], "1970-01-01T00:00:00Z")
        self.assertIsNone(heartbeat["heartbeat_at"])
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, action_id).state,
            "completed")
        action_count = self.store._connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM actions WHERE run_id = ?",
            (run.entity_id,),
        ).fetchone()[0]

        with mock.patch.object(
                effects, "inspect_effect",
                return_value=EffectResult(
                    action_id, EffectResultState.NOOP)), mock.patch.object(
                supervisor, "probe_action",
                return_value=self.unknown(
                    "heartbeat-stale-process-observation-unavailable")):
            with self.assertRaises(CleanupRefused) as raised:
                engine.cleanup(run.entity_id, action_id)

        self.assertIn("positive process exit", raised.exception.detail)
        self.assertEqual(
            self.store._connection.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM actions WHERE run_id = ?",
                (run.entity_id,),
            ).fetchone()[0],
            action_count)
        self.assertEqual(cleanup_calls, [])
        self.assertEqual(self.resource_snapshot(resources), before)

    def test_signal_failure_in_stopping_is_retryable_after_restart(self):
        run, action, principal, digest = self.plan_runner("signal-retry")
        identity = self.publish_identity_fixture(
            run, action, principal, digest)

        def fail_before_signal(_identity) -> None:
            raise OSError("fixture pre-syscall failure")

        first = CancellationEngine(
            self.store, self.effects, self.leases, self.supervisor,
            signal_sender=fail_before_signal)
        with self.assertRaises(SignalDeliveryError):
            first.request_cancel(
                run.entity_id, action.entity_id, reason="user-requested")
        self.assertEqual(self.store.get_run(run.entity_id).state, "stopping")

        sent = []
        restarted = CancellationEngine(
            self.store, self.effects, self.leases, self.supervisor,
            signal_sender=lambda target: sent.append(target))
        resumed = restarted.resume_cancel(run.entity_id, action.entity_id)
        self.assertEqual(resumed.state, "stopping")
        self.assertTrue(resumed.signal_sent)
        self.assertEqual(sent, [identity])
        self.assertEqual(
            self.run_transitions(run.entity_id).count("stopping"), 1)

    def test_partial_cleanup_failure_is_typed_and_resumes_same_action(self):
        run, action_id, supervisor, effects = self.terminal_actual(
            "cleanup-resume")
        resources = self.resources("cleanup-resume")
        worktree, _sentinel, ref, artifact = resources
        calls = []

        def partial_then_fail(plan) -> None:
            self.assertFalse(self.store._connection.in_transaction)  # noqa: SLF001
            calls.append(plan.cleanup_action_id)
            shutil.rmtree(worktree)
            raise OSError("fixture crash after first idempotent deletion")

        first = CancellationEngine(
            self.store, effects, self.leases, supervisor,
            cleanup_executor=partial_then_fail,
            cleanup_executor_id="fixture.cleanup.v1")
        with self.assertRaises(CleanupExecutionError):
            first.cleanup(run.entity_id, action_id)
        cleanup_action_id = calls[0]
        self.assertEqual(
            self.store.get_entity(
                EntityKind.ACTION, cleanup_action_id).state,
            "cleanup-executing")
        self.assertFalse(worktree.exists())
        self.assertTrue(self.ref_exists(ref))
        self.assertTrue(artifact.exists())

        def resume_idempotently(plan) -> None:
            self.assertFalse(self.store._connection.in_transaction)  # noqa: SLF001
            calls.append(plan.cleanup_action_id)
            if worktree.exists():
                shutil.rmtree(worktree)
            if self.ref_exists(ref):
                self.git("update-ref", "-d", ref)
            if artifact.exists():
                artifact.unlink()

        restarted = CancellationEngine(
            self.store, effects, self.leases, supervisor,
            cleanup_executor=resume_idempotently,
            cleanup_executor_id="fixture.cleanup.v1")
        result = restarted.cleanup(run.entity_id, action_id)
        self.assertEqual(result.disposition, CleanupDisposition.CLEANED)
        self.assertEqual(calls, [cleanup_action_id, cleanup_action_id])
        self.assertEqual(
            self.store.get_entity(
                EntityKind.ACTION, cleanup_action_id).state,
            "cleanup-completed")
        self.assertEqual(
            self.resource_snapshot(resources),
            {"worktree_exists": False, "worktree_bytes": None,
             "ref_oid": None, "artifact_bytes": None})

    def test_live_sibling_added_after_terminal_blocks_source_cleanup(self):
        run, action_id, supervisor, effects = self.terminal_actual("sibling")
        source = self.store.get_entity(EntityKind.ACTION, action_id)
        sibling_attempt = "attempt-live-sibling"
        sibling_action = "action-live-sibling"
        self.store.create_attempt(
            run.entity_id, source.parent_job_id, sibling_attempt)
        self.store.create_action(
            run.entity_id, source.parent_job_id, sibling_attempt,
            sibling_action, initial_state="running")
        resources = self.resources("sibling-scope")
        before = self.resource_snapshot(resources)
        cleanup_calls = []
        engine = CancellationEngine(
            self.store, effects, self.leases, supervisor,
            cleanup_executor=self.cleanup_executor(
                resources, cleanup_calls, action_id),
            cleanup_executor_id="fixture.cleanup.v1")

        with self.assertRaises(CancellationScopeRefusal):
            engine.cleanup(run.entity_id, action_id)
        with self.assertRaises(CancellationIdentityRefusal):
            engine.cleanup(run.entity_id, sibling_action)

        self.assertEqual(cleanup_calls, [])
        self.assertEqual(self.resource_snapshot(resources), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
