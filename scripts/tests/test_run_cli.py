#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Focused production run CLI ingress and ProjectContext ordering contracts."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import time
from contextlib import contextmanager
from unittest import mock

import yaml

from test_work_brief import init_project, item, payload
from waystone.cli import review_group, run_group
from waystone.features.review_layout import new_run_id
from waystone.jobs import completion
from waystone.jobs.domain import Role
from waystone.jobs.profile import read_profile
from waystone.runs.artifacts import ArtifactStore
from waystone.runs.spec import load_run_spec
from waystone.runs.store import (
    EngineOwnedPathUnverifiableError, EntityKind, FilesystemInfo, RunStore)


class RunCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.head, self.frame = init_project(self.root)
        (self.root / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks:\n"
            "  - id: feat/semantic-brief\n"
            "    title: Compare candidate approaches\n"
            "    status: pending\n"
            "    scope: [src.py]\n"
            "    deps: []\n",
            encoding="utf-8",
        )
        git(self.root, "add", "tasks.yaml")
        self.assertEqual(git(self.root, "commit", "-qm", "task").returncode, 0)
        state = self.root / ".waystone"
        state.mkdir()
        state.joinpath("profile.yml").write_text(
            "schema: waystone-profile-2\nbindings:\n"
            "  coordinator: {execution: in-session, backend: 'host:current'}\n"
            "  worker: {execution: external, backend: 'codex:worker'}\n"
            "  verifier: {execution: external, backend: 'codex:verifier'}\n"
            "  reviewer: {execution: external, backend: 'codex:reviewer'}\n",
            encoding="utf-8",
        )
        self.machine = self.base / "machine"
        self.machine.mkdir()
        self.machine.joinpath("projects.json").write_text(json.dumps({"projects": [{
            "project_id": "project:run-cli",
            "name": "demo",
            "path": str(self.root.resolve()),
        }]}), encoding="utf-8")
        self.brief_path = self.base / "work-brief.json"
        self.brief_path.write_bytes(completion.canonical_json(
            payload(self.head, self.frame, new_run_id())))

    def install_fixture_codex(self) -> Path:
        binary = self.base / "bin" / "codex"
        binary.parent.mkdir()
        binary.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys\n"
            "from pathlib import Path\n"
            "if os.environ.get('FIXTURE_CODEX_HTTP_400') == '1':\n"
            "  sys.stderr.write(\"HTTP 400: invalid_json_schema for attempt_id\\n\")\n"
            "  raise SystemExit(1)\n"
            "args = sys.argv[1:]\n"
            "schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())\n"
            "properties = schema['properties']\n"
            "assert properties['attempt_id']['type'] == 'string'\n"
            "summary = properties['result_summary']['anyOf'][0]\n"
            "evaluation = 'enum' in summary\n"
            "if not evaluation:\n"
            "  Path('candidate.txt').write_text('fixture candidate\\n', encoding='utf-8')\n"
            "  subprocess.run(['git', 'add', 'candidate.txt'], check=True)\n"
            "  subprocess.run(['git', 'commit', '-qm', 'fixture candidate'], check=True)\n"
            "result = {\n"
            "  'schema': properties['schema']['const'],\n"
            "  'status': 'completed',\n"
            "  'run_spec_digest': properties['run_spec_digest']['const'],\n"
            "  'attempt_id': properties['attempt_id']['const'],\n"
            "  'result_summary': 'pass' if evaluation else 'Candidate explored.',\n"
            "  'evidence_refs': [],\n"
            "  'context_request': None,\n"
            "}\n"
            "Path(args[args.index('-o') + 1]).write_text(json.dumps(result), encoding='utf-8')\n",
            encoding="utf-8",
        )
        binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return binary

    @contextmanager
    def runtime(self, cwd: Path | None = None):
        old = Path.cwd()
        target = self.root if cwd is None else cwd
        output = io.StringIO()
        try:
            os.chdir(target)
            with mock.patch.dict(os.environ, {"WAYSTONE_HOME": str(self.machine)}), mock.patch(
                    "waystone.runs.store._probe_state_filesystem",
                    return_value=FilesystemInfo(
                        filesystem="apfs", mount_point=Path("/"), writable=True)), \
                    contextlib.redirect_stdout(output):
                yield output
        finally:
            os.chdir(old)

    def test_start_uses_production_assembly_and_freezes_typed_ingress(self):
        binary = self.install_fixture_codex()
        with self.runtime() as output, mock.patch.dict(
                os.environ, {"PATH": f"{binary.parent}{os.pathsep}{os.environ['PATH']}"}):
            result = run_group.main([
                "start",
                "feat/semantic-brief",
                "--work-brief",
                str(self.brief_path),
                "--stage",
                "explore",
            ])
            run_id = output.getvalue().split()[1]
            deadline = time.monotonic() + 10
            marker = self.root / ".waystone" / "runner-completions"
            while time.monotonic() < deadline and not tuple(marker.glob("*.json")):
                time.sleep(0.02)

        self.assertEqual(result, 0, output.getvalue())
        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)):
            spec = load_run_spec(run_id, start=self.root)
            with RunStore.open(self.root) as store:
                self.assertEqual(store.get_run(run_id).state, "dispatch-ready")
                self.assertEqual(
                    store.get_entity(
                        EntityKind.ATTEMPT, f"{run_id}:attempt:1").state,
                    "running",
                )
        self.assertEqual(spec.revision, 1)
        self.assertEqual(spec.lifecycle_stage.value, "explore")

    def test_e2e5_public_explore_completes_and_closes_published_worker_result(self):
        binary = self.install_fixture_codex()
        with self.runtime() as output, mock.patch.dict(
                os.environ, {"PATH": f"{binary.parent}{os.pathsep}{os.environ['PATH']}"}):
            result = run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(self.brief_path),
                "--stage", "explore",
            ])
            self.assertEqual(result, 0, output.getvalue())
            run_id = output.getvalue().split()[1]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                output.seek(0)
                output.truncate(0)
                resumed = run_group.main(["resume", run_id])
                if resumed == 0 and "run_closeout_ready" in output.getvalue():
                    break
                time.sleep(0.02)
            else:
                self.fail(f"public explore did not reach closeout-ready: {output.getvalue()}")

            spec = load_run_spec(run_id, start=self.root)
            with mock.patch(
                    "waystone.runs.store._probe_state_filesystem",
                    return_value=FilesystemInfo(
                        filesystem="apfs", mount_point=Path("/"), writable=True)), \
                    RunStore.open(self.root) as store:
                attempt_id = f"{run_id}:attempt:1"
                result_ref = store.get_artifact_reference(f"worker-result:{attempt_id}")
                self.assertEqual(store.get_run(run_id).state, "closeout-ready")
                self.assertEqual(
                    store.get_entity(EntityKind.ATTEMPT, attempt_id).state, "completed")
                self.assertEqual(
                    store.get_entity(EntityKind.JOB, spec.job_id).state, "completed")
            candidate_oid = git(
                self.root, "rev-parse", f"refs/waystone/candidates/{run_id}").stdout.strip()
            self.assertEqual(candidate_oid, git(self.root, "rev-parse", "HEAD").stdout.strip())

            binding = read_profile(
                self.root / ".waystone" / "profile.yml").binding_for(
                    Role.COORDINATOR).binding_digest
            outcome = {
                "schema": "waystone-outcome-delta-1",
                "run_id": run_id,
                "run_spec_digest": spec.run_spec_digest,
                "lifecycle_stage": "explore",
                "objective_ref": spec.objective_ref.to_dict(),
                "kind": "no-objective-delta",
                "summary": "The public explore path published a candidate.",
                "result_digest": result_ref.digest,
                "evidence_refs": [],
                "finding_refs": [],
                "recorded_by": {
                    "role": "coordinator", "binding_digest": binding, "principal": None,
                },
                "rationale": "The candidate ref is locally reachable and immutable.",
            }
            outcome_path = self.base / "outcome.yaml"
            outcome_path.write_bytes(yaml.safe_dump(outcome, sort_keys=False).encode())
            output.seek(0)
            output.truncate(0)
            self.assertEqual(
                run_group.main(["close", run_id, "--outcome", str(outcome_path)]),
                0,
                output.getvalue(),
            )

        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            self.assertEqual(store.get_run(run_id).state, "completed")

    def test_e2e6_public_evaluate_then_promote_executes_frozen_full_chain(self):
        evaluation_body = {
            "schema": "waystone-evaluation-spec-1",
            "evaluation_id": new_run_id(),
            "generation": 1,
            "objective_ref": self.frame.fact_ref("commitment/outcome").to_dict(),
            "criteria": [{
                "id": "representative", "metric": "exact-match",
                "operator": "gte", "threshold": 1,
            }],
            "datasets": [{
                "id": "fixture", "artifact_reference_id": "dataset:fixture",
                "digest": "sha256:" + "d" * 64, "visibility": "harness-only",
            }],
            "seed": 7,
            "supersedes_spec_digest": None,
        }
        evaluation_bytes = yaml.safe_dump(evaluation_body, sort_keys=True).encode()
        evaluation_path = self.root / "docs/evaluations/fixture/spec.yaml"
        evaluation_path.parent.mkdir(parents=True)
        evaluation_path.write_bytes(evaluation_bytes)
        git(self.root, "add", str(evaluation_path.relative_to(self.root)))
        self.assertEqual(git(self.root, "commit", "-qm", "evaluation fixture").returncode, 0)
        evaluation_commit = git(self.root, "rev-parse", "HEAD").stdout.strip()
        evaluation_source = {
            "kind": "evaluation-spec",
            "commit": evaluation_commit,
            "path": str(evaluation_path.relative_to(self.root)),
            "digest": "sha256:" + hashlib.sha256(evaluation_bytes).hexdigest(),
            "generation": 1,
        }
        candidate_worktree = self.base / "candidate-worktree"
        self.assertEqual(git(
            self.root, "worktree", "add", "-q", "-b", "fixture-candidate",
            str(candidate_worktree)).returncode, 0)
        binary = self.install_fixture_codex()
        path = f"{binary.parent}{os.pathsep}{os.environ['PATH']}"

        with self.runtime(candidate_worktree) as output, mock.patch.dict(
                os.environ, {"PATH": path}):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(self.brief_path),
                "--stage", "explore", "--from-worktree", str(candidate_worktree),
            ]), 0, output.getvalue())
            explore_id = output.getvalue().split()[1]
            self._resume_until_closeout(output, explore_id)

        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            candidate_ref = store.get_artifact_reference(f"candidate:{explore_id}")

        evaluate_payload = payload(self.head, self.frame, new_run_id())
        evaluate_payload["lifecycle_stage"] = "evaluate"
        evaluate_payload["objective"]["ref"] = self.frame.fact_ref(
            "commitment/outcome").to_dict()
        evaluate_payload["current_state"].append(item(
            "The explore run published the frozen candidate.",
            "harness-observation",
            source={
                "kind": "evidence", "reference_id": f"candidate:{explore_id}",
                "digest": candidate_ref.digest,
            },
        ))
        evaluate_payload["evidence_expected"] = [{
            "criterion_id": "representative",
            "kind": "evaluation-evidence",
            "text": "The candidate passes the representative fixture.",
            "source": evaluation_source,
        }]
        evaluate_path = self.base / "evaluate-brief.json"
        evaluate_path.write_bytes(completion.canonical_json(evaluate_payload))
        with self.runtime() as output, mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(evaluate_path),
                "--stage", "evaluate",
            ]), 0, output.getvalue())
            evaluate_id = output.getvalue().split()[1]
            self._resume_until_closeout(output, evaluate_id)

        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            evidence_ref = store.get_artifact_reference(
                f"evaluation-evidence:{evaluate_id}")
        evidence_source = {
            "kind": "evaluation-evidence",
            "reference_id": f"evaluation-evidence:{evaluate_id}",
            "candidate_digest": candidate_ref.digest,
            "generation": 1,
            "digest": evidence_ref.digest,
        }
        artifacts = ArtifactStore(self.root)
        records = {
            "regression-contract:fixture": artifacts.write(b"representative regression\n"),
            "supported-scope:fixture": artifacts.write(b"candidate.txt\n"),
            "accepted-risks:fixture": artifacts.write(b"public-contract\n"),
        }
        promote_payload = payload(self.head, self.frame, new_run_id())
        promote_payload["lifecycle_stage"] = "promote"
        promote_payload["objective"]["ref"] = self.frame.fact_ref(
            "commitment/outcome").to_dict()
        promote_payload["current_state"].append(item(
            "The candidate and passed evaluation generation are frozen with promotion records.",
            "harness-observation",
            sources=[
                {"kind": "evidence", "reference_id": f"candidate:{explore_id}",
                 "digest": candidate_ref.digest},
                evidence_source,
                *[
                    {"kind": "evidence", "reference_id": reference_id,
                     "digest": artifact.digest}
                    for reference_id, artifact in records.items()
                ],
            ],
        ))
        promote_payload["evidence_expected"] = [{
            "criterion_id": "representative",
            "kind": "regression-contract",
            "text": "Promote only the passed representative generation.",
            "source": evidence_source,
        }]
        promote_path = self.base / "promote-brief.json"
        promote_path.write_bytes(completion.canonical_json(promote_payload))
        expected_target = git(self.root, "rev-parse", "HEAD").stdout.strip()
        candidate_oid = git(
            self.root, "rev-parse", f"refs/waystone/candidates/{explore_id}").stdout.strip()
        self.assertNotEqual(expected_target, candidate_oid)

        with self.runtime() as output, mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(promote_path),
                "--stage", "promote",
            ]), 0, output.getvalue())
            promote_id = output.getvalue().split()[1]
            promote_spec = load_run_spec(promote_id, start=self.root)
            reviewer_binding = read_profile(
                self.root / ".waystone" / "profile.yml").binding_for(
                    Role.REVIEWER).binding_digest
            review_run_id = new_run_id()
            feedback_path = self.base / "promotion-review.yaml"
            feedback_path.write_text(yaml.safe_dump({
                "target": {
                    "run_spec_digest": promote_spec.run_spec_digest,
                    "result_digest": promote_spec.candidate["producer_result_digest"],
                },
                "binding_digest": reviewer_binding,
                "reported_by": {
                    "role": "reviewer",
                    "binding_digest": reviewer_binding,
                    "principal": None,
                },
                "findings": [],
            }, sort_keys=False), encoding="utf-8")
            review_group.ingest_feedback(
                self.root, review_run_id, feedback_path,
                binding_digest=reviewer_binding,
            )
            review_group.attach_review(self.root, promote_id, review_run_id)
            self._resume_until_closeout(output, promote_id)

        self.assertEqual(git(self.root, "rev-parse", "HEAD").stdout.strip(), candidate_oid)
        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            self.assertEqual(store.get_run(promote_id).state, "closeout-ready")
            verifier_ref = store.get_artifact_reference(
                f"verifier-evidence:{promote_id}:typed-independent-verify")
            decision_ref = store.get_artifact_reference(
                f"integration-decision:{promote_id}:integration-decision")
            review_ref = store.get_artifact_reference(
                f"review-cycle:{promote_spec.promotion_lineage.id}:1")
            self.assertEqual(len({
                verifier_ref.digest, decision_ref.digest, review_ref.digest,
            }), 3)
            decision = json.loads(ArtifactStore(self.root).read_reference(
                decision_ref).decode("utf-8"))
            self.assertEqual(
                decision["candidate_digest"], promote_spec.candidate["digest"])
            self.assertEqual(
                decision["evaluation_evidence_digest"],
                promote_spec.evaluation["evidence"]["digest"],
            )
            self.assertEqual(len(decision["reviewer_artifact_digests"]), 1)
            attempts = store._connection.execute(  # noqa: SLF001
                "SELECT attempt_id, state FROM attempts WHERE run_id = ?", (promote_id,)
            ).fetchall()
            self.assertEqual([(row["attempt_id"], row["state"]) for row in attempts], [
                (f"{promote_id}:attempt:1", "completed"),
            ])

    def test_g01304_http_400_child_failure_terminalizes_marker_and_run_state(self):
        binary = self.install_fixture_codex()
        environment = {
            "PATH": f"{binary.parent}{os.pathsep}{os.environ['PATH']}",
            "FIXTURE_CODEX_HTTP_400": "1",
        }
        with self.runtime() as output, mock.patch.dict(os.environ, environment):
            self.assertEqual(run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(self.brief_path),
                "--stage", "explore",
            ]), 0, output.getvalue())
            run_id = output.getvalue().split()[1]
            # A transient typed refusal (e.g. lease contention with the detached
            # supervisor) also returns a nonzero envelope, so the loop must key on
            # the run reaching its terminal state rather than on the first nonzero rc.
            deadline = time.monotonic() + 10
            result = 0
            state = None
            with mock.patch(
                    "waystone.runs.store._probe_state_filesystem",
                    return_value=FilesystemInfo(
                        filesystem="apfs", mount_point=Path("/"), writable=True)):
                while time.monotonic() < deadline:
                    output.seek(0)
                    output.truncate(0)
                    result = run_group.main(["resume", run_id])
                    try:
                        with RunStore.open(self.root) as store:
                            state = store.get_run(run_id).state
                    except EngineOwnedPathUnverifiableError:
                        # WAL sidecar verification can race with the detached
                        # supervisor's connection teardown; retry within the deadline.
                        time.sleep(0.02)
                        continue
                    if state == "failed":
                        break
                    time.sleep(0.02)
            self.assertEqual(state, "failed", output.getvalue())
            self.assertEqual(result, 2, output.getvalue())

        marker_paths = tuple((
            self.root / ".waystone" / "runner-completions").glob("*.json"))
        self.assertEqual(len(marker_paths), 1)
        marker = json.loads(marker_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(marker["schema"], "waystone-runner-completion-1")
        self.assertEqual(marker["returncode"], 1)
        self.assertNotIn("worker_result_digest", marker)
        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            attempt_id = f"{run_id}:attempt:1"
            self.assertEqual(store.get_run(run_id).state, "failed")
            self.assertEqual(
                store.get_entity(EntityKind.JOB, f"{run_id}:job").state, "failed")
            self.assertEqual(
                store.get_entity(EntityKind.ATTEMPT, attempt_id).state, "failed")
            self.assertEqual(
                store.get_entity(
                    EntityKind.ACTION, f"{attempt_id}:worker").state,
                "completed",
            )
            failure_ref = store.get_artifact_reference(f"runner-failure:{attempt_id}")
        failure = json.loads(
            ArtifactStore(self.root).read(failure_ref.digest).decode("utf-8"))
        self.assertEqual(failure["failure_class"], "runner-exit-nonzero")
        stderr = ArtifactStore(self.root).read(failure["stderr_artifact_digest"])
        self.assertIn(b"HTTP 400", stderr)

    def _resume_until_closeout(self, output: io.StringIO, run_id: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            output.seek(0)
            output.truncate(0)
            result = run_group.main(["resume", run_id])
            if result == 0 and "run_closeout_ready" in output.getvalue():
                return
            time.sleep(0.02)
        self.fail(f"run {run_id} did not reach closeout-ready: {output.getvalue()}")

    def test_stage_is_only_an_assertion_and_mismatch_creates_no_run(self):
        with self.runtime() as output:
            result = run_group.main([
                "start",
                "feat/semantic-brief",
                "--work-brief",
                str(self.brief_path),
                "--stage",
                "promote",
            ])

        self.assertEqual(result, 2, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["code"], "action_plan_invalid")
        with mock.patch(
                "waystone.runs.store._probe_state_filesystem",
                return_value=FilesystemInfo(
                    filesystem="apfs", mount_point=Path("/"), writable=True)), \
                RunStore.open(self.root) as store:
            count = store._connection.execute("SELECT count(*) FROM runs").fetchone()[0]  # noqa: SLF001
        self.assertEqual(count, 0)

    def test_linked_start_without_explicit_selector_refuses_before_ingress_or_db_open(self):
        linked = self.base / "linked"
        self.assertEqual(
            git(self.root, "worktree", "add", "-q", "-b", "cli-linked", str(linked)).returncode,
            0,
        )
        missing = self.base / "must-not-be-read.json"
        with self.runtime(linked) as output:
            result = run_group.main([
                "start", "feat/semantic-brief", "--work-brief", str(missing),
            ])

        self.assertEqual(result, 2, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["code"], "action_plan_invalid")
        self.assertFalse((self.root / ".waystone" / "state.db").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
