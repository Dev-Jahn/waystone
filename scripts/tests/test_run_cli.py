#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Contract tests for the M1-B run-engine CLI bridge."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from waystone.cli import main as cli_main
from waystone.cli import run_group
from waystone.jobs.domain import ExecutionCategory, Role, RoleBinding
from waystone.runs import store as store_module
from waystone.runs.engine import PreflightInputs, RunAssembly, RunEngine
from waystone.runs.preflight import (
    CapabilitySet,
    CheckCapabilityProbe,
    CheckDefinition,
    CheckPhase,
    DependencyConstraint,
    EnvironmentPreparationReceipt,
    EnvironmentPreparationStep,
    MaterializedToolchain,
    NetworkCacheRequirements,
    ObservationStatus,
    ProbeTarget,
    RoleCapability,
    RunnerCapabilities,
    RunnerContext,
    RuntimeObservation,
    SandboxContract,
    ToolchainObservation,
    ToolchainRequirement,
    VerificationPlanDefinition,
    WorkingDirectoryRule,
    record_runner_proof,
)
from waystone.runs.store import EntityKind, FilesystemInfo, RunStore
from waystone.runs.supervisor import RunnerInvocation
from waystone.runs.verify import (
    ActorIdentity,
    CriterionResult,
    DecisionInput,
    DecisionOutcome,
    EngineCheckOutput,
    FixtureVerifierResult,
    VerifierAdapter,
    VerifierOutput,
)


def sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class RunCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.check_sandbox = SandboxContract(
            "isolated-worktree-write", "process-exec", "network-denied")
        self.verifier_sandbox = SandboxContract(
            "read-only", "process-exec", "network-denied")
        self.verifier = ActorIdentity("verifier-fixture", Role.VERIFIER)
        self.coordinator = ActorIdentity("coordinator-fixture", Role.COORDINATOR)

    @contextlib.contextmanager
    def supported_filesystem(self):
        with mock.patch.object(
                store_module, "_probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)):
            yield

    def project(self, *, delay: float = 0.0, worker_backend: str = "codex:gpt-test"):
        root = self.base / f"project-{len(tuple(self.base.iterdir()))}"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text(
            "version: 1\nproject: cli-fixture\n", encoding="utf-8")
        (root / "tasks.yaml").write_text(
            "version: 1\n"
            "project: cli-fixture\n"
            "tasks:\n"
            "  - id: feat/example\n"
            "    title: complete one engine run\n"
            "    status: pending\n"
            "    scope: [result.txt]\n"
            "    accept:\n"
            "      - result commit contains the fixture output\n",
            encoding="utf-8",
        )
        (root / ".gitignore").write_text(
            ".waystone/\n.toolchains/\n", encoding="utf-8")
        (root / "fixture_runner.py").write_text(
            "from pathlib import Path\n"
            "import subprocess, time\n"
            f"time.sleep({delay!r})\n"
            "Path('result.txt').write_text('fixture result\\n', encoding='utf-8')\n"
            "subprocess.run(['git', 'add', 'result.txt'], check=True)\n"
            "subprocess.run(['git', 'commit', '-qm', 'fixture result'], check=True)\n",
            encoding="utf-8",
        )
        git(root, "add", "-A")
        self.assertEqual(git(root, "commit", "-qm", "cli fixture").returncode, 0)

        state = root / ".waystone"
        state.mkdir()
        (state / "profile.yml").write_text(
            "schema: waystone-profile-1\n"
            "bindings:\n"
            f"  implementer: {{execution: external-runner, backend: '{worker_backend}'}}\n"
            "  verifier: {backend: 'codex:gpt-verify', entry: adversarial-review}\n",
            encoding="utf-8",
        )
        toolchain = root / ".toolchains" / "fixture.bin"
        toolchain.parent.mkdir()
        toolchain.write_bytes(b"fixture-toolchain-v1")
        worker = self.base / f"worker-{root.name}"
        self.assertEqual(
            git(root, "worktree", "add", "-q", "-b", "worker-result", str(worker)).returncode,
            0,
        )
        return root, worker, toolchain

    @staticmethod
    def observations() -> tuple[RuntimeObservation, ...]:
        definitions = (
            ("cache-boundary", "engine:cache-boundary", False),
            ("platform-kernel", "engine:platform-kernel", False),
            ("process-security", "engine:process-security", True),
            ("runner-binary", "runner-adapter:binary", False),
            ("runner-config-content", "runner-adapter:config", True),
            ("runner-version", "runner-adapter:version", False),
            ("sandbox-contract", "engine:sandbox-contract", False),
        )
        return tuple(RuntimeObservation(
            axis,
            source,
            ObservationStatus.NOT_OBSERVED if absent else ObservationStatus.OBSERVED,
            None if absent else sha256(axis.encode("utf-8")),
        ) for axis, source, absent in definitions)

    def definition(self) -> VerificationPlanDefinition:
        source = "lock:fixture@local/fixture.bin"
        return VerificationPlanDefinition(
            required_checks=(CheckDefinition(
                check_id="fixture-check",
                phase=CheckPhase.VERIFICATION,
                command=(sys.executable, "fixture_runner.py"),
                working_directory=WorkingDirectoryRule.JOB_ROOT,
                expected_exit_codes=(0,),
                expected_evidence_kinds=("stderr", "stdout"),
                environment=(),
                fixture_digests=(sha256(b"fixture-contract"),),
                required_toolchain_ids=("fixture-toolchain",),
                sandbox=self.check_sandbox,
                worker_execution_required=True,
            ),),
            required_toolchains=(ToolchainRequirement(
                toolchain_id="fixture-toolchain",
                executable="fixture-runner",
                runtime="python>=3.10",
                source_id=source,
                content_digest=sha256(b"fixture-toolchain-v1"),
                size=len(b"fixture-toolchain-v1"),
                dependencies=(DependencyConstraint("fixture", "==1"),),
            ),),
            environment_preparation=(EnvironmentPreparationStep(
                sequence=0,
                step_id="materialize-fixture",
                command=("fixture-sync", "--offline"),
                input_toolchain_ids=("fixture-toolchain",),
            ),),
            network_cache_requirements=NetworkCacheRequirements(
                network_required=False,
                allowed_sources=(source,),
                cache_namespace="fixture-cache",
                offline_capable=True,
            ),
            verifier_sandbox=self.verifier_sandbox,
        )

    def assembly(self, root: Path, worker: Path, toolchain: Path, *,
                 capability_worker_backend: str | None = None) -> RunAssembly:
        def preflight_inputs(plan):
            worker_binding = plan.binding_for(Role.WORKER).binding
            if capability_worker_backend is not None:
                worker_binding = RoleBinding(
                    Role.WORKER, ExecutionCategory.EXTERNAL,
                    capability_worker_backend)
            verifier_binding = plan.binding_for(Role.VERIFIER).binding
            runner = RunnerCapabilities(
                execution_categories=(ExecutionCategory.EXTERNAL,),
                engine_sandboxes=(self.check_sandbox,),
                role_capabilities=(
                    RoleCapability(
                        worker_binding,
                        self.check_sandbox,
                        False, False, False, False,
                    ),
                    RoleCapability(
                        verifier_binding,
                        self.verifier_sandbox,
                        True, True, True, True,
                    ),
                ),
            )
            observed = ToolchainObservation(
                "fixture-toolchain",
                "lock:fixture@local/fixture.bin",
                sha256(b"fixture-toolchain-v1"),
                len(b"fixture-toolchain-v1"),
            )
            receipt = EnvironmentPreparationReceipt(
                plan.environment_preparation_digest,
                plan.network_cache_requirements,
                (observed,),
            )
            probes = tuple(CheckCapabilityProbe(
                check_id=check.check_id,
                target=target,
                command=check.command,
                command_input_digest=check.command_input_digest,
                environment_preparation_artifact_digest=receipt.artifact_digest,
                child_environment=check.environment,
                entrypoint_ready=True,
                structured_result=True,
                exit_code=0,
            ) for check in plan.required_checks for target in ProbeTarget)
            capabilities = CapabilitySet(runner, (receipt,), probes, ())
            context = RunnerContext(
                checkout_identity=sha256(b"fixture-checkout"),
                machine_identity=sha256(b"fixture-machine"),
                principal_identity=sha256(b"fixture-principal"),
                project_config_digest=sha256((root / ".waystone.yml").read_bytes()),
                profile_config_digest=sha256(
                    (root / ".waystone" / "profile.yml").read_bytes()),
                runtime_observations=self.observations(),
            )
            return PreflightInputs(
                capabilities,
                (MaterializedToolchain(
                    "fixture-toolchain",
                    "lock:fixture@local/fixture.bin",
                    toolchain,
                ),),
                context,
                record_runner_proof(context, runner),
            )

        def runner_invocations(dispatch):
            return {
                action.prepared_input_digest: RunnerInvocation(action.command, worker)
                for action in dispatch.engine_actions
            }

        def check_executor(request):
            return EngineCheckOutput(
                request.action.check_id,
                0,
                (("stderr", b""), ("stdout", b"fixture check passed\n")),
            )

        def verifier_executor(request):
            return FixtureVerifierResult(
                returncode=0,
                output=VerifierOutput(
                    actor=self.verifier,
                    result_digest=request.result.result_digest,
                    criterion_results=tuple(CriterionResult(
                        criterion,
                        True,
                        (sha256(b"criterion:" + criterion.encode("utf-8")),),
                    ) for criterion in request.owner_criteria),
                    blockers=(),
                    summary="fixture verifier accepted the exact result",
                ),
            )

        adapter = VerifierAdapter(
            RoleBinding(Role.VERIFIER, ExecutionCategory.EXTERNAL, "codex:gpt-verify"),
            self.verifier_sandbox,
            verifier_executor,
        )

        def decision_input(evidence, coordinator):
            return DecisionInput(
                actor=coordinator,
                outcome=DecisionOutcome.ACCEPT,
                criteria=tuple(item.criterion for item in evidence.criterion_results),
                result_digest=evidence.result.result_digest,
                verifier_reference_id=evidence.artifact_reference.reference_id,
                verifier_artifact_digest=evidence.artifact_reference.digest,
                engine_check_reference_id=(
                    evidence.engine_checks.artifact_reference.reference_id),
                engine_check_artifact_digest=(
                    evidence.engine_checks.artifact_reference.digest),
            )

        return RunAssembly(
            verification_plan=self.definition(),
            preflight_inputs=preflight_inputs,
            runner_invocations=runner_invocations,
            result_ref="refs/heads/worker-result",
            worker_actor_id="worker-fixture",
            verifier_actor=self.verifier,
            coordinator_actor=self.coordinator,
            check_executor=check_executor,
            verifier_adapter=adapter,
            decision_input=decision_input,
        )

    @contextlib.contextmanager
    def cli(self, root: Path, engine: RunEngine):
        old = Path.cwd()
        try:
            os.chdir(root)
            with mock.patch.object(run_group, "_engine_factory", return_value=engine):
                yield
        finally:
            os.chdir(old)

    @staticmethod
    def invoke(argv: list[str]):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = run_group.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def wait_for_marker(self, root: Path, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        directory = root / ".waystone" / "runner-completions"
        while time.monotonic() < deadline:
            if directory.is_dir() and any(directory.glob("*.json")):
                return
            time.sleep(0.02)
        self.fail("detached runner did not publish a completion marker")

    def wait_for_runtime(self, root: Path, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        directory = root / ".waystone" / "supervisors"
        while time.monotonic() < deadline:
            if directory.is_dir() and any(directory.glob("*.runtime.json")):
                return
            time.sleep(0.02)
        self.fail("detached supervisor did not publish runtime identity")

    def database_rows(self, root: Path):
        with self.supported_filesystem(), RunStore.open(root) as store:
            with store._connection_lock:  # noqa: SLF001
                tables = tuple(row["name"] for row in store._connection.execute(  # noqa: SLF001
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall())
                return {
                    table: tuple(tuple(row) for row in store._connection.execute(  # noqa: SLF001
                        f'SELECT * FROM "{table}" ORDER BY rowid').fetchall())
                    for table in tables
                }

    def test_one_task_cli_run_completes_through_supervisor_verify_decision_and_private_apply(self):
        root, worker, toolchain = self.project(delay=1.0)
        engine = RunEngine(root, self.assembly(root, worker, toolchain))
        with self.supported_filesystem(), self.cli(root, engine):
            started_at = time.monotonic()
            rc, output, _error = self.invoke(["start", "feat/example"])
            self.assertEqual(rc, 0, output)
            self.assertLess(time.monotonic() - started_at, 1.0)
            run_id = output.split()[1]
            self.wait_for_runtime(root)
            next_rc, next_output, _ = self.invoke(
                ["actions", "next", run_id, "--json"])
            self.assertEqual(next_rc, 0, next_output)
            self.assertIsNone(json.loads(next_output)["action"])
            self.assertEqual(json.loads(next_output)["engine"], "busy")
            self.wait_for_marker(root)

            completed = False
            for _ in range(5):
                resume_rc, resume_output, _ = self.invoke(["resume", run_id])
                self.assertEqual(resume_rc, 0, resume_output)
                if "completed on private integration ref" in resume_output:
                    completed = True
                    break
            self.assertTrue(completed)

        with self.supported_filesystem(), RunStore.open(root) as store:
            self.assertEqual(store.get_run(run_id).state, "completed")
            self.assertEqual(
                store.get_entity(EntityKind.JOB, f"{run_id}:job").state,
                "accepted",
            )
        result = git(root, "rev-parse", "refs/heads/worker-result").stdout.strip()
        integrated = git(
            root, "rev-parse", f"refs/waystone/integration/{run_id}").stdout.strip()
        self.assertEqual(integrated, result)
        self.assertEqual(git(root, "status", "--porcelain").stdout, "")

    def test_planned_runner_dispatch_returns_busy_without_waiting_for_completion(self):
        root, worker, toolchain = self.project(delay=1.0)
        engine = RunEngine(root, self.assembly(root, worker, toolchain))
        with self.supported_filesystem():
            started_at = time.monotonic()
            result = engine.start("feat/example")
            elapsed = time.monotonic() - started_at
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result.dispatch["engine"], "busy")
        self.assertIsNone(result.dispatch["action"])
        self.assertTrue(any(
            (root / ".waystone" / "supervisors").glob("*.launch.json")))
        self.wait_for_marker(root)

    def test_status_and_watch_cli_use_read_only_open_during_e2e(self):
        root, worker, toolchain = self.project()
        engine = RunEngine(root, self.assembly(root, worker, toolchain))
        with self.supported_filesystem(), self.cli(root, engine):
            rc, output, _ = self.invoke(["start", "feat/example"])
            self.assertEqual(rc, 0, output)
            run_id = output.split()[1]
            self.wait_for_marker(root)
            before = self.database_rows(root)
            status_rc, status_output, _ = self.invoke(["status", run_id, "--json"])
            self.assertEqual(status_rc, 0, status_output)
            self.assertEqual(json.loads(status_output)["run_state"], "dispatch-ready")
            with mock.patch.object(
                    engine, "watch", return_value=iter([engine.status_human(run_id)])):
                watch_rc, watch_output, _ = self.invoke(["watch", run_id])
            self.assertEqual(watch_rc, 0, watch_output)
            self.assertIn("Run state: dispatch-ready", watch_output)
            self.assertEqual(self.database_rows(root), before)

    def test_uninitialized_root_refuses_every_run_subcommand_without_creating_state(self):
        root = self.base / "uninitialized"
        root.mkdir()
        result_file = root / "result.json"
        result_file.write_text("{}", encoding="utf-8")
        commands = (
            ["start", "feat/example"],
            ["resume"],
            ["status"],
            ["watch"],
            ["cancel", "run", "--reason", "user-requested"],
            ["actions", "next", "run", "--json"],
            ["actions", "submit", "action", "--file", str(result_file)],
            ["deliver", "run"],
        )
        old = Path.cwd()
        try:
            os.chdir(root)
            for argv in commands:
                with self.subTest(argv=argv):
                    rc, output, _ = self.invoke(list(argv))
                    self.assertEqual(rc, 2, output)
                    envelope = json.loads(output)
                    self.assertFalse(envelope["ok"])
                    self.assertEqual(envelope["code"], "action_plan_invalid")
                    self.assertFalse((root / ".waystone").exists())
        finally:
            os.chdir(old)

    def test_unsupported_backend_preflight_refusal_reaches_typed_cli_envelope(self):
        root, worker, toolchain = self.project(worker_backend="unknown:gpt")
        assembly = self.assembly(
            root, worker, toolchain,
            capability_worker_backend="codex:gpt-test",
        )
        engine = RunEngine(root, assembly)
        with self.supported_filesystem(), self.cli(root, engine):
            rc, output, _ = self.invoke(["start", "feat/example"])
        self.assertEqual(rc, 2, output)
        envelope = json.loads(output)
        self.assertEqual(envelope["code"], "action_plan_invalid")
        self.assertFalse((root / ".waystone" / "supervisors").exists())

    def test_runner_invocation_must_match_frozen_preflight_digest(self):
        root, worker, toolchain = self.project()
        assembly = self.assembly(root, worker, toolchain)
        mismatched = replace(
            assembly,
            runner_invocations=lambda _dispatch: {
                "sha256:" + "0" * 64: RunnerInvocation(
                    (sys.executable, "fixture_runner.py"), worker),
            },
        )
        engine = RunEngine(root, mismatched)
        with self.supported_filesystem(), self.cli(root, engine):
            rc, output, _ = self.invoke(["start", "feat/example"])
        self.assertEqual(rc, 2, output)
        self.assertEqual(json.loads(output)["code"], "action_plan_invalid")
        self.assertFalse((root / ".waystone" / "supervisors").exists())

    def test_cancel_cli_records_intent_and_exposes_unknown_effect_pending(self):
        root, worker, toolchain = self.project()
        engine = RunEngine(root, self.assembly(root, worker, toolchain))
        with self.supported_filesystem(), self.cli(root, engine):
            rc, output, _ = self.invoke(["start", "feat/example"])
            self.assertEqual(rc, 0, output)
            run_id = output.split()[1]
            self.wait_for_marker(root)
            marker = next((root / ".waystone" / "runner-completions").glob("*.json"))
            marker.unlink()
            cancel_rc, cancel_output, _ = self.invoke([
                "cancel", run_id, "--reason", "user-requested",
            ])
        self.assertEqual(cancel_rc, 0, cancel_output)
        self.assertIn("cancel-pending(reason=unknown-effect)", cancel_output)
        with self.supported_filesystem(), RunStore.open(root) as store:
            self.assertEqual(
                store.get_run(run_id).state,
                "cancel-pending(reason=unknown-effect)",
            )
            store.get_artifact_reference(f"cancellation-intent:{run_id}")
        self.assertTrue((worker / "result.txt").is_file())

    def test_main_dispatcher_registers_run_without_legacy_project_state_check(self):
        with mock.patch.object(cli_main, "_module_checks_project_state", wraps=(
                cli_main._module_checks_project_state)):
            self.assertTrue(cli_main._module_checks_project_state(["run", "status"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
