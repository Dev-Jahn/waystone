#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Contract tests for M1-B carrier/user actions transport."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from waystone.adapters import git as git_adapter
from waystone.jobs.domain import ExecutorKind
from waystone.runs import store as store_module
from waystone.runs.artifacts import ArtifactStore
from waystone.runs.effects import (
    ArtifactWriteEffect,
    EffectEngine,
    EffectResult,
    EffectResultState,
    RunnerCompletionMarker,
    RunnerExecutionEffect,
    publish_runner_completion,
)
from waystone.runs.lease import (
    LeaseManager,
    LeasePrincipalMismatch,
    LeasePrincipalUnknown,
)
from waystone.runs.store import EntityKind, FilesystemInfo, RunStore, TransitionReason
from waystone.runs.transport import (
    ActionNotCurrent,
    ActionPlanRefusal,
    ActionResultSchema,
    ActionTransport,
    ArtifactDigestMismatch,
    EngineExecutorUnavailable,
    EngineTestEvidenceRefusal,
    FencingEpochMismatch,
    GitFactsMismatch,
    InputDigestMismatch,
    ResultField,
    ResultSchemaMismatch,
    ResultValueKind,
    RunNotActionable,
    TransportFailureCode,
    TransportExitCode,
    UnclassifiedTransportFailure,
    decode_envelope,
    encode_envelope,
    failure_envelope,
)


class RunTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.com")
        self.git("config", "user.name", "Fixture")
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "base.txt")
        self.git("commit", "-qm", "base")
        (self.root / ".waystone.yml").write_text(
            "version: 1\nproject: transport-fixture\n", encoding="utf-8")
        with mock.patch.object(
                store_module, "_probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)):
            self.store = RunStore.open(self.root)
        self.addCleanup(self.store.close)
        self.leases = LeaseManager(self.store)
        self.artifacts = ArtifactStore(self.root)
        self.runner_launches = []

        def runner_executor(intent):
            self.runner_launches.append(intent)
            stdout = self.artifacts.write(
                f"stdout:{intent.action_id}\n".encode("utf-8"))
            stderr = self.artifacts.write(
                f"stderr:{intent.action_id}\n".encode("utf-8"))
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
                    stdout_artifact_digest=stdout.digest,
                    stderr_artifact_digest=stderr.digest,
                ),
            )

        self.runner_executor = runner_executor
        self.effects = EffectEngine(
            self.store, self.leases,
            runner_executor=runner_executor,
            runner_identity_verifier=lambda marker: (
                marker.process_identity == f"fixture-process:{marker.action_id}"),
        )
        self.transport = ActionTransport(self.store, self.effects)
        self.run = self.store.create_run()
        self.store.create_job(self.run.entity_id, "job")
        self.number = 0

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args], check=True,
            capture_output=True, text=True)
        return result.stdout.strip()

    @staticmethod
    def digest(payload: bytes) -> str:
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def attempt(self, prefix: str) -> tuple[str, str]:
        self.number += 1
        suffix = f"{prefix}-{self.number}"
        attempt_id = f"attempt-{suffix}"
        action_id = f"action-{suffix}"
        self.store.create_attempt(self.run.entity_id, "job", attempt_id)
        return attempt_id, action_id

    def outward(
            self, prefix: str = "outward", *, git_facts: bool = False,
            artifact_names: tuple[str, ...] = (),
            test_action_ids: tuple[str, ...] = ()) -> dict[str, object]:
        attempt_id, action_id = self.attempt(prefix)
        schema = ActionResultSchema(
            (ResultField("summary", ResultValueKind.STRING),),
            artifact_names=artifact_names,
            requires_git_facts=git_facts,
        )
        return self.transport._plan_outward_action(  # noqa: SLF001
            self.run.entity_id, "job", attempt_id, action_id,
            action_kind="worker-result", executor_kind=ExecutorKind.CARRIER,
            input_payload={"goal": prefix}, result_schema=schema,
            git_repository=self.root if git_facts else None,
            test_action_ids=test_action_ids,
        )

    def completed_runner(self, prefix: str = "tests"):
        attempt_id, action_id = self.attempt(prefix)
        plan = self.effects.plan_effect(
            self.run.entity_id, "job", attempt_id, action_id,
            RunnerExecutionEffect(self.digest(f"invocation:{prefix}".encode("utf-8"))))
        result = self.effects.reconcile_actions((action_id,))[0]
        self.assertEqual(result.state, EffectResultState.COMPLETED)
        return plan

    def durable_snapshot(self):
        with self.store._connection_lock:  # noqa: SLF001
            tables = tuple(
                row["name"] for row in self.store._connection.execute(  # noqa: SLF001
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall())
            rows = {
                table: tuple(
                    tuple(row) for row in self.store._connection.execute(  # noqa: SLF001
                        f'SELECT * FROM "{table}" ORDER BY rowid').fetchall())
                for table in tables
            }
        directory = self.root / ".waystone" / "artifacts"
        files = tuple(
            (path.relative_to(directory).as_posix(), path.read_bytes())
            for path in sorted(directory.glob("**/*")) if path.is_file()
        ) if directory.exists() else ()
        return rows, files

    def valid_submit(self, action: dict[str, object], *, artifact: bytes = b"evidence"):
        payload = {
            "artifacts": [],
            "entity_version": action["entity_version"],
            "fencing_epoch": action["fencing_epoch"],
            "input_digest": action["input_digest"],
            "result": {"summary": "done"},
        }
        names = action["result_schema"]["artifact_names"]
        if names:
            payload["artifacts"] = [{
                "content_base64": base64.b64encode(artifact).decode("ascii"),
                "digest": self.digest(artifact),
                "name": names[0],
            }]
        if action["result_schema"]["requires_git_facts"]:
            payload["git_facts"] = self.transport._derive_git_facts(  # noqa: SLF001
                self.transport._load_outward_plan(  # noqa: SLF001
                    self.store.get_entity(EntityKind.ACTION, action["action_id"])
                ).git_contract)
        return payload

    def test_actions_next_exhausts_engine_action_and_returns_only_new_outward_action(self):
        attempt_id, action_id = self.attempt("engine")
        plan = self.effects.plan_effect(
            self.run.entity_id, "job", attempt_id, action_id,
            ArtifactWriteEffect(b"engine-owned"))
        original_driver = self.effects._execute_driver  # noqa: SLF001

        def execute_then_publish(effect_plan, principal, intent):
            original_driver(effect_plan, principal, intent)
            self.outward("published-after-engine")

        with mock.patch.object(
                self.effects, "_execute_driver", side_effect=execute_then_publish):
            result = self.transport.actions_next(self.run.entity_id)

        self.assertEqual(result["action"]["executor_kind"], "carrier")
        self.assertEqual(set(result["action"]["ownership"]), {"expires_at", "kind"})
        self.assertNotIn("owner_token", result["action"])
        self.assertNotEqual(result["action"]["action_id"], plan.action_id)
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, plan.action_id).state, "completed")

    def test_positive_in_flight_runner_returns_busy_without_blocking(self):
        attempt_id, action_id = self.attempt("runner")
        plan = self.effects.plan_effect(
            self.run.entity_id, "job", attempt_id, action_id,
            RunnerExecutionEffect(self.digest(b"runner invocation")))
        claimed = self.effects.claim_effect(plan, ttl_seconds=30)
        intent, reference = self.effects._make_intent(plan, claimed.principal)  # noqa: SLF001
        self.effects._transition(  # noqa: SLF001
            claimed.principal, self.leases.guard_effect_start,
            next_state="effect", reason=TransitionReason.PROCESS_STARTED,
            evidence_digest=reference.digest, references=(reference,))
        observed = EffectResult(action_id, EffectResultState.IN_FLIGHT, reason="positive heartbeat")
        started = time.monotonic()
        with mock.patch.object(self.effects, "inspect_effect", return_value=observed):
            result = self.transport.actions_next(self.run.entity_id)
        elapsed = time.monotonic() - started
        self.assertEqual(result["engine"], "busy")
        self.assertIsNone(result["action"])
        self.assertGreater(result["poll_after_s"], 0)
        self.assertEqual(result["run_state"], self.run.state)
        self.assertLess(elapsed, 0.2)

    def test_planned_runner_never_calls_synchronous_executor(self):
        attempt_id, action_id = self.attempt("runner-undispatchable")
        calls = []

        def sleeping_executor(intent):
            calls.append(intent)
            time.sleep(0.5)

        effects = EffectEngine(
            self.store, self.leases,
            runner_executor=sleeping_executor,
            runner_identity_verifier=lambda _marker: True,
        )
        transport = ActionTransport(self.store, effects)
        effects.plan_effect(
            self.run.entity_id, "job", attempt_id, action_id,
            RunnerExecutionEffect(self.digest(b"runner unavailable")))
        started = time.monotonic()
        with self.assertRaises(EngineExecutorUnavailable):
            transport.actions_next(self.run.entity_id)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(calls, [])

    def test_ready_outward_action_wins_over_in_flight_engine_action(self):
        attempt_id, action_id = self.attempt("runner-priority")
        plan = self.effects.plan_effect(
            self.run.entity_id, "job", attempt_id, action_id,
            RunnerExecutionEffect(self.digest(b"runner priority")))
        claimed = self.effects.claim_effect(plan, ttl_seconds=30)
        _intent, reference = self.effects._make_intent(  # noqa: SLF001
            plan, claimed.principal)
        self.effects._transition(  # noqa: SLF001
            claimed.principal, self.leases.guard_effect_start,
            next_state="effect", reason=TransitionReason.PROCESS_STARTED,
            evidence_digest=reference.digest, references=(reference,))
        outward = self.outward("priority-outward")
        with mock.patch.object(
                self.effects, "inspect_effect",
                side_effect=AssertionError("engine branch must not run")):
            result = self.transport.actions_next(self.run.entity_id)
        self.assertEqual(result["action"]["action_id"], outward["action_id"])

    def test_actions_next_recovers_atomic_planned_outward_action(self):
        attempt_id, action_id = self.attempt("recover-outward")
        with mock.patch.object(
                self.transport, "_claim_outward_action",  # noqa: SLF001
                side_effect=LeasePrincipalMismatch(action_id, "claim")):
            with self.assertRaises(LeasePrincipalMismatch):
                self.transport._plan_outward_action(  # noqa: SLF001
                    self.run.entity_id, "job", attempt_id, action_id,
                    action_kind="worker-result",
                    executor_kind=ExecutorKind.CARRIER,
                    input_payload={"goal": "recover"},
                    result_schema=ActionResultSchema((
                        ResultField("summary", ResultValueKind.STRING),)),
                )
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, action_id).state, "planned")
        self.assertEqual(
            self.store.get_artifact_reference(
                f"transport-action-plan:{action_id}").kind.value,
            "evidence")
        result = self.transport.actions_next(self.run.entity_id)
        self.assertEqual(result["action"]["action_id"], action_id)
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, action_id).state, "claimed")

    def test_unknown_effect_is_idle_only_when_run_is_actually_blocked(self):
        attempt_id, action_id = self.attempt("runner-unknown")
        plan = self.effects.plan_effect(
            self.run.entity_id, "job", attempt_id, action_id,
            RunnerExecutionEffect(self.digest(b"runner unknown")))
        claimed = self.effects.claim_effect(plan, ttl_seconds=30)
        _intent, reference = self.effects._make_intent(plan, claimed.principal)  # noqa: SLF001
        self.effects._transition(  # noqa: SLF001
            claimed.principal, self.leases.guard_effect_start,
            next_state="effect", reason=TransitionReason.PROCESS_STARTED,
            evidence_digest=reference.digest, references=(reference,))
        with self.assertRaises(RunNotActionable):
            self.transport.actions_next(self.run.entity_id)
        current_run = self.store.get_entity(EntityKind.RUN, self.run.entity_id)
        self.store.record_transition(
            EntityKind.RUN, self.run.entity_id, expected_version=current_run.version,
            next_state="blocked", reason=TransitionReason.PLANNED)
        self.assertEqual(self.transport.actions_next(self.run.entity_id), {
            "action": None, "engine": "idle", "run_state": "blocked",
            "reason": "effect_unknown",
        })

    def test_conflict_effect_is_idle_only_when_run_is_actually_blocked(self):
        attempt_id, action_id = self.attempt("effect-conflict")
        self.effects.plan_effect(
            self.run.entity_id, "job", attempt_id, action_id,
            ArtifactWriteEffect(b"conflict"))
        conflict = EffectResult(
            action_id, EffectResultState.CONFLICT, reason="fixture conflict")
        with mock.patch.object(
                self.effects, "reconcile_actions", return_value=(conflict,)):
            with self.assertRaises(RunNotActionable):
                self.transport.actions_next(self.run.entity_id)
            current_run = self.store.get_entity(EntityKind.RUN, self.run.entity_id)
            self.store.record_transition(
                EntityKind.RUN, self.run.entity_id,
                expected_version=current_run.version, next_state="blocked",
                reason=TransitionReason.PLANNED)
            self.assertEqual(self.transport.actions_next(self.run.entity_id), {
                "action": None, "engine": "idle", "run_state": "blocked",
                "reason": "effect_conflict",
            })

    def test_idle_reasons_are_closed_for_terminal_and_wait_states(self):
        for state, expected_state, reason in (
                ("completed", "completed", "run_completed"),
                ("waiting_user", "waiting_user", "run_waiting_user"),
                ("blocked", "blocked", "run_blocked")):
            with self.subTest(state=state):
                run = self.store.create_run(initial_state=state)
                self.assertEqual(self.transport.actions_next(run.entity_id), {
                    "action": None, "engine": "idle", "run_state": expected_state,
                    "reason": reason,
                })
        run = self.store.create_run(initial_state="created")
        with self.assertRaises(RunNotActionable):
            self.transport.actions_next(run.entity_id)

    def test_plain_action_is_not_silently_inferred_as_carrier(self):
        attempt_id, action_id = self.attempt("plain")
        self.store.create_action(self.run.entity_id, "job", attempt_id, action_id)
        with self.assertRaises(ActionPlanRefusal):
            self.transport.actions_next(self.run.entity_id)

    def _assert_refusal_unchanged(self, action, payload, error_type):
        durable_before = self.durable_snapshot()
        before = self.store.get_entity(EntityKind.ACTION, action["action_id"])
        with self.assertRaises(error_type):
            self.transport.submit(action["action_id"], payload)
        after = self.store.get_entity(EntityKind.ACTION, action["action_id"])
        self.assertEqual(after, before)
        self.assertEqual(self.durable_snapshot(), durable_before)

    def test_submit_refuses_noncurrent_claim_without_state_change(self):
        action = self.outward("current")
        payload = self.valid_submit(action)
        payload["entity_version"] += 1
        self._assert_refusal_unchanged(action, payload, ActionNotCurrent)

    def test_submit_refuses_input_digest_mismatch_without_state_change(self):
        action = self.outward("input")
        payload = self.valid_submit(action)
        payload["input_digest"] = self.digest(b"forged-input")
        self._assert_refusal_unchanged(action, payload, InputDigestMismatch)

    def test_submit_refuses_stale_fencing_epoch_without_state_change(self):
        action = self.outward("fence")
        payload = self.valid_submit(action)
        payload["fencing_epoch"] += 1
        self._assert_refusal_unchanged(action, payload, FencingEpochMismatch)

    def test_submit_refuses_action_result_schema_mismatch_without_state_change(self):
        action = self.outward("schema")
        payload = self.valid_submit(action)
        payload["result"] = {"summary": 42}
        self._assert_refusal_unchanged(action, payload, ResultSchemaMismatch)

    def test_carrier_test_results_and_non_json_objects_are_not_accepted_as_authority(self):
        attempt_id, action_id = self.attempt("test-results")
        with self.assertRaises(ActionPlanRefusal):
            self.transport._plan_outward_action(  # noqa: SLF001
                self.run.entity_id, "job", attempt_id, action_id,
                action_kind="worker-result", executor_kind=ExecutorKind.CARRIER,
                input_payload={"goal": "tests"},
                result_schema=ActionResultSchema((
                    ResultField("test_results", ResultValueKind.OBJECT),)),
            )
        json_attempt, json_action = self.attempt("json-object")
        action = self.transport._plan_outward_action(  # noqa: SLF001
            self.run.entity_id, "job", json_attempt, json_action,
            action_kind="worker-result", executor_kind=ExecutorKind.CARRIER,
            input_payload={"goal": "json"},
            result_schema=ActionResultSchema((
                ResultField("details", ResultValueKind.OBJECT),)),
        )
        payload = {
            "artifacts": [], "entity_version": action["entity_version"],
            "fencing_epoch": action["fencing_epoch"],
            "input_digest": action["input_digest"],
            "result": {"details": {"path": Path("not-json")}},
        }
        self._assert_refusal_unchanged(action, payload, ResultSchemaMismatch)

    def test_submit_refuses_artifact_digest_mismatch_without_state_change(self):
        action = self.outward("artifact", artifact_names=("report",))
        payload = self.valid_submit(action)
        payload["artifacts"][0]["digest"] = self.digest(b"different")
        self._assert_refusal_unchanged(action, payload, ArtifactDigestMismatch)

    def test_submit_rederives_git_facts_and_refuses_carrier_forgery(self):
        action = self.outward("git-forgery", git_facts=True)
        path = self.root / "changed.txt"
        path.write_text("result\n", encoding="utf-8")
        self.git("add", "changed.txt")
        self.git("commit", "-qm", "result")
        payload = self.valid_submit(action)
        payload["git_facts"] = {
            "changed_files": ["invented.txt"], "result_sha": "0" * 40}
        original = git_adapter.git_read_bytes
        with mock.patch(
                "waystone.runs.transport.git_adapter.git_read_bytes",
                wraps=original) as read_git:
            self._assert_refusal_unchanged(action, payload, GitFactsMismatch)
        commands = [call.args[1] for call in read_git.call_args_list]
        self.assertIn("rev-parse", commands)
        self.assertIn("diff", commands)

    def test_submit_persists_exact_engine_derived_git_facts(self):
        action = self.outward("git-valid", git_facts=True)
        path = self.root / "derived.txt"
        path.write_text("derived\n", encoding="utf-8")
        self.git("add", "derived.txt")
        self.git("commit", "-qm", "derived")
        payload = self.valid_submit(action)
        result = self.transport.submit(action["action_id"], payload)
        evidence = json.loads(self.artifacts.read(result["result_digest"]))
        self.assertEqual(evidence["git_facts"], {
            "changed_files": ["derived.txt"],
            "result_sha": self.git("rev-parse", "HEAD"),
        })

    def test_submit_preserves_engine_derived_non_utf8_git_paths(self):
        action = self.outward("git-non-utf8", git_facts=True)
        raw_name = b"non-utf8-\xff.txt"

        def git_bytes(*args: str, payload: bytes | None = None) -> bytes:
            return subprocess.run(
                ["git", "-C", str(self.root), *args], input=payload,
                check=True, capture_output=True).stdout

        blob = git_bytes("hash-object", "-w", "--stdin", payload=b"raw path\n").strip()
        base_tree = git_bytes("ls-tree", "-z", "HEAD")
        raw_entry = b"100644 blob " + blob + b"\t" + raw_name + b"\0"
        tree = git_bytes("mktree", "-z", payload=base_tree + raw_entry).strip()
        parent = git_bytes("rev-parse", "HEAD").strip()
        commit = git_bytes(
            "commit-tree", os.fsdecode(tree), "-p", os.fsdecode(parent),
            payload=b"non-utf8 path\n").strip()
        git_bytes("update-ref", "HEAD", os.fsdecode(commit), os.fsdecode(parent))
        payload = self.valid_submit(action)
        result = self.transport.submit(action["action_id"], payload)
        evidence = json.loads(self.artifacts.read(result["result_digest"]))
        self.assertEqual(
            [os.fsencode(path) for path in evidence["git_facts"]["changed_files"]],
            [raw_name],
        )

    def test_guard_refusal_writes_no_git_observation_artifacts_or_durable_rows(self):
        action = self.outward(
            "guard-race", git_facts=True, artifact_names=("report",))
        payload = self.valid_submit(action, artifact=b"guarded evidence")
        before = self.durable_snapshot()
        mismatch = LeasePrincipalMismatch(action["action_id"], "submit")
        with mock.patch.object(
                self.leases, "guard_submit", side_effect=mismatch), mock.patch(
                "waystone.runs.transport.git_adapter.git_read_bytes") as git_read, mock.patch.object(
                self.transport._artifacts, "write",  # noqa: SLF001
                wraps=self.transport._artifacts.write) as artifact_write:  # noqa: SLF001
            with self.assertRaises(ActionNotCurrent):
                self.transport.submit(action["action_id"], payload)
        self.assertEqual(git_read.call_count, 0)
        self.assertEqual(artifact_write.call_count, 0)
        self.assertEqual(self.durable_snapshot(), before)

    def test_final_guard_race_preserves_authoritative_state(self):
        action = self.outward(
            "final-guard-race", git_facts=True, artifact_names=("report",))
        payload = self.valid_submit(action, artifact=b"guarded evidence")
        before = self.durable_snapshot()
        real_guard = self.leases.guard_submit
        guard_calls = 0

        def race_at_final_guard(principal, callback):
            nonlocal guard_calls
            guard_calls += 1
            if guard_calls == 2:
                raise LeasePrincipalMismatch(action["action_id"], "submit")
            return real_guard(principal, callback)

        with mock.patch.object(
                self.leases, "guard_submit", side_effect=race_at_final_guard), mock.patch.object(
                self.transport._artifacts, "write",  # noqa: SLF001
                wraps=self.transport._artifacts.write) as artifact_write:  # noqa: SLF001
            with self.assertRaises(ActionNotCurrent):
                self.transport.submit(action["action_id"], payload)
        after = self.durable_snapshot()
        self.assertEqual(guard_calls, 2)
        self.assertEqual(artifact_write.call_count, 2)
        self.assertEqual(after[0], before[0])
        self.assertNotEqual(after[1], before[1])

    def test_submit_preserves_plan_refusal_and_maps_unknown_principal(self):
        plan_action = self.outward("typed-plan")
        with mock.patch.object(
                self.transport, "_load_outward_plan",  # noqa: SLF001
                side_effect=ActionPlanRefusal("corrupt plan")):
            with self.assertRaises(ActionPlanRefusal):
                self.transport.submit(
                    plan_action["action_id"], self.valid_submit(plan_action))

        lease_action = self.outward("typed-lease")
        lease_error = LeasePrincipalUnknown(
            lease_action["action_id"], "transport-read", "fixture")
        with mock.patch.object(
                self.transport, "_current_principal", side_effect=lease_error):  # noqa: SLF001
            with self.assertRaises(ActionNotCurrent):
                self.transport.submit(lease_action["action_id"], {
                    "artifacts": [],
                    "entity_version": lease_action["entity_version"],
                    "fencing_epoch": lease_action["fencing_epoch"],
                    "input_digest": lease_action["input_digest"],
                    "result": {"summary": "done"},
                })

    def test_submit_persists_only_engine_observed_runner_test_results(self):
        runner = self.completed_runner("runner-evidence")
        action = self.outward(
            "runner-result", test_action_ids=(runner.action_id,))
        result = self.transport.submit(
            action["action_id"], self.valid_submit(action))
        evidence = json.loads(self.artifacts.read(result["result_digest"]))
        self.assertEqual(evidence["test_results"], [{
            "invocation_digest": runner.spec["invocation_digest"],
            "returncode": 0,
            "runner_action_id": runner.action_id,
            "signal": None,
            "stderr_artifact_digest": self.digest(
                f"stderr:{runner.action_id}\n".encode("utf-8")),
            "stdout_artifact_digest": self.digest(
                f"stdout:{runner.action_id}\n".encode("utf-8")),
        }])

    def test_submit_refuses_tampered_runner_authority_without_state_change(self):
        runner = self.completed_runner("runner-tamper")
        action = self.outward(
            "runner-tampered-result", test_action_ids=(runner.action_id,))
        marker_path = Path(runner.spec["completion_marker"])
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["returncode"] = 1
        marker_path.write_text(
            json.dumps(marker, sort_keys=True, separators=(",", ":")),
            encoding="utf-8")
        before = self.durable_snapshot()
        self._assert_refusal_unchanged(
            action, self.valid_submit(action), EngineTestEvidenceRefusal)
        self.assertEqual(self.durable_snapshot(), before)

    def test_valid_submit_commits_engine_decided_completion(self):
        action = self.outward("valid", artifact_names=("report",))
        result = self.transport.submit(action["action_id"], self.valid_submit(action))
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(
            self.store.get_entity(EntityKind.ACTION, action["action_id"]).state, "completed")

    def test_failure_envelopes_classify_transient_contract_and_unknown(self):
        transient_errors = [ConnectionError("offline")]
        try:
            raise TimeoutError("timeout")
        except TimeoutError as cause:
            try:
                raise RuntimeError("wrapped") from cause
            except RuntimeError as wrapped:
                transient_errors.append(wrapped)

        class HttpFailure(RuntimeError):
            def __init__(self, status_code):
                self.status_code = status_code

        transient_errors.append(HttpFailure(503))
        for error in transient_errors:
            with self.subTest(error=type(error).__name__):
                exit_code, transient = failure_envelope(error)
                self.assertEqual(exit_code, TransportExitCode.TEMPORARY_FAILURE)
                self.assertEqual(
                    transient["code"],
                    TransportFailureCode.TRANSIENT_TRANSPORT_FAILURE.value)
                self.assertIs(transient["recoverable"], True)
                self.assertEqual(transient["next_actions"], [])
        exit_code, transient = failure_envelope(ConnectionError(
            "token=secret-value /private/runtime/path"))
        self.assertEqual(exit_code, TransportExitCode.TEMPORARY_FAILURE)
        self.assertEqual(transient["detail"], "ConnectionError")
        self.assertNotIn("secret-value", transient["detail"])
        self.assertNotIn("/private/runtime/path", transient["detail"])
        exit_code, terminal = failure_envelope(ResultSchemaMismatch("bad result"))
        self.assertEqual(exit_code, TransportExitCode.REFUSED)
        self.assertEqual(
            terminal["code"], TransportFailureCode.RESULT_SCHEMA_MISMATCH.value)
        self.assertIs(terminal["recoverable"], False)
        self.assertEqual(terminal["detail"], "bad result")
        exit_code, stale = failure_envelope(
            LeasePrincipalMismatch("action-stale", "submit"))
        self.assertEqual(exit_code, TransportExitCode.REFUSED)
        self.assertEqual(stale["code"], TransportFailureCode.ACTION_NOT_CURRENT.value)
        self.assertIs(stale["recoverable"], False)
        for error in (
                RuntimeError(
                    "token=secret-value /private/runtime/path\n"
                    "Traceback (most recent call last): ..."),
                HttpFailure(404),
                UnclassifiedTransportFailure(
                    "token=secret-value /private/runtime/path\n"
                    "Traceback (most recent call last): ..."),
        ):
            with self.subTest(error=str(error)):
                exit_code, unknown = failure_envelope(error)
                self.assertEqual(exit_code, TransportExitCode.UNCLASSIFIED)
                self.assertEqual(unknown["code"], "unclassified")
                self.assertIs(unknown["recoverable"], False)
                expected_detail = (
                    type(error).__name__
                    if not isinstance(error, UnclassifiedTransportFailure)
                    else "UnclassifiedTransportFailure"
                )
                self.assertEqual(unknown["detail"], expected_detail)
                self.assertNotIn("secret-value", unknown["detail"])
                self.assertNotIn("/private/runtime/path", unknown["detail"])
                self.assertNotIn("Traceback", unknown["detail"])
                self.assertEqual(decode_envelope(encode_envelope(unknown)), unknown)

    def test_envelope_codec_accepts_registered_shapes_and_rejects_unknown_code(self):
        action = self.outward("codec")
        outward = {"action": action}
        busy = {
            "action": None, "engine": "busy", "poll_after_s": 1,
            "run_state": "created",
        }
        submit = self.transport.submit(action["action_id"], self.valid_submit(action))
        for envelope in (outward, busy, submit):
            self.assertEqual(decode_envelope(encode_envelope(envelope)), envelope)
        unknown = {
            "ok": False, "code": "future-code", "detail": "future failure",
            "recoverable": False,
            "next_actions": [],
        }
        raw = json.dumps(
            unknown, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with self.assertRaises(ValueError):
            decode_envelope(raw)
        with self.assertRaises(ValueError):
            encode_envelope(unknown)

    def test_decoder_accepts_legacy_failure_envelope_without_detail(self):
        legacy = {
            "ok": False,
            "code": TransportFailureCode.ACTION_PLAN_INVALID.value,
            "recoverable": False,
            "next_actions": [],
        }
        raw = json.dumps(
            legacy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(decode_envelope(raw), legacy)

    def test_transport_exit_code_values_remain_stable(self):
        self.assertEqual(int(TransportExitCode.OK), 0)
        self.assertEqual(int(TransportExitCode.UNCLASSIFIED), 1)
        self.assertEqual(int(TransportExitCode.REFUSED), 2)
        self.assertEqual(int(TransportExitCode.TEMPORARY_FAILURE), 75)


if __name__ == "__main__":
    unittest.main()
