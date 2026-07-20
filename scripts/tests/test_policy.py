"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


class L2CGuardTests(unittest.TestCase):
    """L2-C G8: four boundary-warn rules share lifecycle, replay, and attribution contracts."""

    def test_rule_vocabulary_contains_all_l2c_guards_at_observing_defaults(self):
        expected = {
            "delegation-scope-drift-v1": ({"delegate-run", "delegate-apply", "check"},
                                            "delegations"),
            "env-manifest-mutation-v1": ({"round-close", "check"}, "rounds"),
            "review-skipped-closes-v1": ({"round-close", "check"}, "rounds"),
            "done-without-evidence-v1": ({"round-close", "check"}, "rounds"),
        }
        for rule_id, (boundaries, corpus) in expected.items():
            self.assertEqual(overlay.RULES[rule_id]["boundaries"], boundaries)
            self.assertEqual(overlay.RULES[rule_id]["corpus"], corpus)
        self.assertEqual(
            overlay.RULES["review-skipped-closes-v1"]["default_params"]["consecutive"], 2)

    def test_scope_drift_reuses_structured_helper_and_replays(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            rec = _run_with_home(home, lambda: delegate._delegations_dir(root)) / "did-scope"
            (rec / "artifact").mkdir(parents=True)
            (rec / "packet.yaml").write_text(yaml.safe_dump({
                "task": {"id": "feat/scope", "round": "2026-07-15-r1"},
                "declared_scope": ["src"],
            }))
            (rec / "artifact" / "contract.yaml").write_text(yaml.safe_dump({
                "task_id": "feat/scope",
                "changed_files": [{"path": "src/ok.py"}, {"path": "docs/out.md"}],
            }))
            (rec / "status.json").write_text(_json.dumps({"state": "needs-review"}))
            _add_delta(root, home, delta_id="worker_scope_drift/outside",
                       rule="delegation-scope-drift-v1")

            events = _run_with_home(home, lambda: overlay.evaluate_boundary(
                root, "delegate-run", {"delegation_id": "did-scope"}))
            fire = next(event for event in events if event["event"] == "fire")
            self.assertEqual(fire["context"]["delegation_id"], "did-scope")
            self.assertEqual(fire["context"]["task_id"], "feat/scope")
            self.assertEqual(fire["context"]["round_id"], "2026-07-15-r1")
            self.assertEqual(fire["context"]["outside_scope"], ["docs/out.md"])

            replay = _run_with_home(
                home, lambda: overlay.replay(root, "worker_scope_drift/outside"))
            self.assertEqual((replay["opportunities"], replay["fires"]), (1, 1))
            self.assertEqual(_run_with_home(
                home, lambda: overlay.promote(root, "worker_scope_drift/outside"))["status"],
                             "warning")

    def test_round_manifest_and_done_guards_fire_without_blocking_close(self):
        import contextlib
        import io
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            base = git(root, "rev-parse", "HEAD").stdout.strip()
            cfg = (root / ".waystone.yml").read_text().replace(
                "last_round_commit: null", f"last_round_commit: {base}")
            (root / ".waystone.yml").write_text(cfg)
            tasks = (root / "tasks.yaml").read_text().replace(
                "    deps: []\n", "    deps: []\n    scope: [src]\n", 1)
            (root / "tasks.yaml").write_text(tasks)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "manifest mutation")
            _add_delta(root, home, delta_id="env_unpreparedness/manifest",
                       rule="env-manifest-mutation-v1")
            _add_delta(root, home, delta_id="verification_debt/done",
                       rule="done-without-evidence-v1")
            sessions = root / ".waystone" / "improve" / "sessions.jsonl"
            sessions.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl(sessions, [{
                "project": "demo", "kind": "main", "session_id": "s-main",
                "verification": {"runs": 0, "failed": 0},
                "build": {"runs": 0, "failed": 0},
            }])

            err = io.StringIO()
            round_id = f"{TEST_CURRENT_ROUND_DATE}-r1"
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "s-main"}), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: round.close(
                    root, round_id, done=["chore/close-me"], touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            rows = _read_warnings(root, home)
            fires = {row["rule"]: row for row in rows if row["event"] == "fire"}
            self.assertEqual(fires["env-manifest-mutation-v1"]["context"]["manifest_paths"],
                             ["pyproject.toml"])
            self.assertEqual(fires["done-without-evidence-v1"]["context"]["task_ids"],
                             ["chore/close-me"])
            self.assertEqual(fires["env-manifest-mutation-v1"]["context"]["round_id"],
                             round_id)

            for delta_id in ("env_unpreparedness/manifest", "verification_debt/done"):
                report = _run_with_home(home, lambda did=delta_id: overlay.replay(root, did))
                self.assertEqual(report["corpus"], "rounds")
                self.assertEqual((report["opportunities"], report["fires"]), (1, 1))

    def test_manifest_scope_reference_and_any_done_evidence_suppress_fires(self):
        round_record = {
            "round_id": "r1", "evaluable": True,
            "changed_files": ["pyproject.toml"], "manifest_paths": ["pyproject.toml"],
            "env_prep_before": None, "env_prep_after": None,
            "env_prep_change_kind": "unchanged",
            "task_scopes": {"feat/deps": ["pyproject.toml"]},
            "task_scope_coverage": {"feat/deps": "explicit"},
            "done_evidence": [{"task_id": "feat/deps", "evaluable": True,
                               "positive": True,
                               "evidence_kind": "satisfied-apply-verdict"}],
            "done_task_ids": ["feat/deps"],
        }
        self.assertEqual(overlay.evaluate_env_manifest_mutation(round_record)["fires"], [])
        self.assertEqual(overlay.evaluate_done_without_evidence(round_record)["fires"], [])

    def test_review_ingest_resets_two_close_streak_and_replay_is_deterministic(self):
        rounds = [
            {"round_id": "r1", "at": "2026-07-15T00:00:00+00:00", "review_mode": "packet"},
            {"round_id": "r2", "at": "2026-07-15T02:00:00+00:00", "review_mode": "packet"},
            {"round_id": "r3", "at": "2026-07-15T04:00:00+00:00", "review_mode": "packet"},
        ]
        ingests = [{"event": "review-feedback", "source": "packet-ingest",
                    "reviewer": "gpt-5.5-pro", "reviewer_configured": True,
                    "narrative_digest_matches": True,
                    "narrative_coverage_reason": None,
                    "rendered_request_digest_matches": True,
                    "rendered_request_coverage_reason": None,
                    "round_id": "r1", "at": "2026-07-15T01:00:00+00:00"}]
        out = overlay.evaluate_review_skipped_closes(rounds, ingests, consecutive=2)
        self.assertEqual(out["fires"], ["r3"])
        self.assertEqual(out, overlay.evaluate_review_skipped_closes(
            list(reversed(rounds)), list(reversed(ingests)), consecutive=2))

    def test_review_skipped_guard_shadow_replay_gates_promotion(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            exposure = _run_with_home(home, lambda: overlay._exposure_dir(root))
            exposure.mkdir(parents=True)
            for number in (1, 2):
                (exposure / f"round-r{number}.json").write_text(_json.dumps({
                    "schema": "waystone-round-exposure-1", "round_id": f"r{number}",
                    "at": f"2026-07-15T0{number}:00:00+00:00",
                    "review_mode": "packet",
                }))
            _add_delta(root, home, delta_id="review_association/skipped",
                       rule="review-skipped-closes-v1")
            report = _run_with_home(
                home, lambda: overlay.replay(root, "review_association/skipped"))
            self.assertEqual((report["opportunities"], report["fires"]), (2, 1))
            self.assertEqual(report["examples"], ["r2"])
            self.assertEqual(_run_with_home(
                home, lambda: overlay.promote(root, "review_association/skipped"))["status"],
                             "warning")
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                events = _run_with_home(home, lambda: overlay.evaluate_boundary(
                    root, "round-close", {"round_id": "r2"}))
            fire = next(event for event in events if event["event"] == "fire")
            self.assertEqual(fire["context"]["round_id"], "r2")
            self.assertEqual(fire["context"]["consecutive"], 2)

    def test_review_ingest_boundary_records_replay_event(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            row = _run_with_home(
                home, lambda: overlay.record_review_ingest(root, "2026-07-15-r1"))
            self.assertEqual(row["round_id"], "2026-07-15-r1")
            loaded, skipped = _run_with_home(home, lambda: overlay.load_review_ingests(root))
            self.assertEqual(skipped, 0)
            self.assertIsNone(loaded[0]["reviewer_configured"])
            self.assertEqual(loaded[0]["reviewer_coverage_reason"],
                             "feedback-file-unavailable")
            self.assertTrue(loaded[0]["source_pointer"].endswith("review-ingests.jsonl:1"))


class L2CAdversarialFixTests(unittest.TestCase):
    """L2-C adversarial findings: honest coverage, canonical joins, and stable populations."""

    def test_f1_invalid_task_scope_is_unknown_and_never_blocks_close(self):
        import contextlib
        import io
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            round_id = f"{TEST_CURRENT_ROUND_DATE}-invalid-scope"
            tasks = (root / "tasks.yaml").read_text().replace(
                "    deps: []\n", "    deps: []\n    scope: [../outside]\n", 1)
            (root / "tasks.yaml").write_text(tasks)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: round.close(
                    root, round_id, done=["chore/close-me"],
                    touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            exposure = _json.loads((overlay._exposure_dir(root) /
                                    f"round-{round_id}.json").read_text())
            self.assertIs(exposure["round_evidence"]["evaluable"], False)
            self.assertEqual(exposure["round_evidence"]["coverage_reason"],
                             "task-scope-invalid")

        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            round_id = f"{TEST_CURRENT_ROUND_DATE}-snapshot-error"
            with mock.patch.object(
                    overlay, "_capture_round_evidence", side_effect=RuntimeError("snapshot")), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: round.close(
                    root, round_id, done=["chore/close-me"],
                    touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            exposure = _json.loads((overlay._exposure_dir(root) /
                                    f"round-{round_id}.json").read_text())
            evidence = exposure["round_evidence"]
            self.assertEqual(
                {key: evidence[key] for key in ("evaluable", "fired", "coverage_reason")},
                {"evaluable": False, "fired": False,
                 "coverage_reason": "round-snapshot-error"})

    def test_f2_done_evidence_requires_satisfied_apply_or_structured_main_verification(self):
        row = {
            "round_evidence": {
                "done_task_ids": ["feat/apply", "feat/discard", "feat/direct-unknown"],
                "done_evidence": [
                    {"task_id": "feat/apply", "evaluable": True, "positive": True,
                     "evidence_kind": "satisfied-apply-verdict"},
                    {"task_id": "feat/discard", "evaluable": True, "positive": False,
                     "evidence_kind": "discard-verdict"},
                    {"task_id": "feat/direct-unknown", "evaluable": False, "positive": False,
                     "coverage_reason": "main-verification-unavailable"},
                ],
            },
        }
        out = overlay.evaluate_done_without_evidence(row)
        self.assertIs(out["evaluable"], True)
        self.assertIs(out["fired"], True)
        self.assertEqual(out["fires"], ["feat/discard"])
        self.assertEqual(out["unknown_task_ids"], ["feat/direct-unknown"])

        unknown = overlay.evaluate_done_without_evidence({
            "round_evidence": {
                "done_task_ids": ["feat/direct"],
                "done_evidence": [{"task_id": "feat/direct", "evaluable": False,
                                   "positive": False,
                                   "coverage_reason": "main-verification-unavailable"}],
            },
        })
        self.assertIs(unknown["evaluable"], False)
        self.assertIs(unknown["fired"], False)
        self.assertEqual(unknown["coverage_reason"], "main-verification-unavailable")

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            self.assertEqual(_deleg_run(root, home, _deleg_fake({"impl.py": "x\n"})), 0)
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            index, errors = overlay._delegation_evidence_index(root)
            self.assertEqual(errors, 0)
            self.assertIs(index["feat/xyz"][0]["positive"], False)
            self.assertEqual(index["feat/xyz"][0]["evidence_kind"],
                             "unresolved-apply-judgment")
            _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            index, errors = overlay._delegation_evidence_index(root)
            self.assertEqual(errors, 0)
            self.assertIs(index["feat/xyz"][0]["positive"], True)

            verdict_path = rec / "artifact" / "verdict-1.json"
            verdict = _json.loads(verdict_path.read_text())
            verdict["decision"] = "discard"
            verdict_path.write_text(_json.dumps(verdict) + "\n")
            index, errors = overlay._delegation_evidence_index(root)
            self.assertEqual(errors, 0)
            self.assertIs(index["feat/xyz"][0]["positive"], False)
            self.assertEqual(index["feat/xyz"][0]["evidence_kind"], "discard-verdict")

    def test_f3_review_feedback_is_canonical_pr_unknown_and_reclose_counts_once(self):
        rounds = [
            {"round_id": "r1", "at": "2026-07-15T00:00:00+00:00", "review_mode": "pr",
             "_file": "/x/round-r1.json"},
            {"round_id": "r1", "at": "2026-07-15T00:01:00+00:00", "review_mode": "pr",
             "_file": "/x/round-r1-2.json"},
        ]
        out = overlay.evaluate_review_skipped_closes(rounds, [], consecutive=2)
        self.assertEqual(out["opportunities"], 0)
        self.assertEqual(out["unevaluable_pr_state"], 1)
        self.assertEqual(len(out["by_round"]), 1)
        self.assertIs(out["by_round"][0]["evaluable"], False)
        self.assertEqual(out["by_round"][0]["coverage_reason"], "pr-state-unavailable")

        facts = {
            "cycle_fresh": True, "approved_at_head": True, "codex_fresh": True,
            "findings_resolved": True, "pro_result_at_head": True,
            "reviewers": ["codex", "pro-reviewer"], "round_id": "r1",
            "latest_cycle": 2, "current_head": "a" * 40,
        }
        event = review.completed_pr_feedback_event(facts, 17)
        self.assertEqual(event["event"], "review-feedback")
        self.assertEqual(event["source"], "pr-marker")
        self.assertEqual(event["round_id"], "r1")
        facts["cycle_version_skew_reason"] = "latest-v1-supersedes-v2"
        self.assertIsNone(review.completed_pr_feedback_event(facts, 17))

    def test_f4_transition_phases_dedupe_evaluations_and_findings_by_round(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            delta = _add_delta(
                root, home, delta_id="worker_scope_drift/phases",
                rule="delegation-scope-drift-v1")
            delta["created_at"] = "2026-07-15T02:00:00+00:00"
            delta["transitions"] = [
                {"to": "observing", "at": "2026-07-15T02:00:00+00:00"},
                {"to": "suspended", "at": "2026-07-15T04:00:00+00:00"},
            ]
            delta["status"] = "suspended"
            delta["replay"] = {"evaluations": [
                {"round_id": "r0", "subject_id": "did-0", "snapshot": "snap-0",
                 "opportunities": 1, "fires": 1},
                {"round_id": "r1", "subject_id": "did-1", "snapshot": "snap-1",
                 "opportunities": 1, "fires": 1},
                {"round_id": "r2", "subject_id": "did-2", "snapshot": "snap-2",
                 "opportunities": 1, "fires": 1},
            ]}
            overlay._write_delta(root, delta)
            exposures = [
                {"round_id": "r0", "at": "2026-07-15T01:00:00+00:00", "_file": "/x/r0"},
                {"round_id": "r1", "at": "2026-07-15T03:00:00+00:00", "_file": "/x/r1"},
                {"round_id": "r2", "at": "2026-07-15T05:00:00+00:00", "_file": "/x/r2"},
            ]
            warnings = [
                {"at": "2026-07-15T03:10:00+00:00", "boundary": "check",
                 "rule": "delegation-scope-drift-v1", "event": "evaluation",
                 "policy_identity": {"layer": "project", "id": delta["id"]},
                 "origin_delta_id": delta["id"],
                 "context": {"round_id": "r1", "delegation_id": "did-1",
                             "snapshot": "snap-1", "fired": True},
                 "source_pointer": "/w:1"},
                {"at": "2026-07-15T03:11:00+00:00", "boundary": "check",
                 "rule": "delegation-scope-drift-v1", "event": "evaluation",
                 "policy_identity": {"layer": "project", "id": delta["id"]},
                 "origin_delta_id": delta["id"],
                 "context": {"round_id": "r1", "delegation_id": "did-1",
                             "snapshot": "snap-1", "fired": True},
                 "source_pointer": "/w:2"},
                {"at": "2026-07-15T05:10:00+00:00", "boundary": "check",
                 "rule": "delegation-scope-drift-v1", "event": "evaluation",
                 "policy_identity": {"layer": "project", "id": delta["id"]},
                 "origin_delta_id": delta["id"],
                 "context": {"round_id": "r2", "delegation_id": "did-2",
                             "snapshot": "snap-2", "fired": True},
                 "source_pointer": "/w:3"},
            ]
            reviews = [
                {"round_id": "r0", "findings": [{"status": "REAL", "type": "scope"}]},
                {"round_id": "r1", "findings": [{"status": "REAL", "type": "scope"}]},
                {"round_id": "r2", "findings": [{"status": "REAL", "type": "scope"}]},
            ]
            observation = _run_with_home(home, lambda: improve._adaptive_feedback_observation(
                "demo", root, reviews, warnings, exposures, {}))
            fact = observation["facts"]["deltas"][0]
            self.assertEqual(fact["pre_active"]["opportunities"], 1)
            self.assertEqual(fact["active"]["opportunities"], 1)
            self.assertEqual(fact["post_suspend"]["opportunities"], 1)
            self.assertEqual(fact["active"]["finding_occurrences"]["scope"], 1)

    def test_f5_all_new_rules_report_tri_state_and_unevaluable_reasons(self):
        missing = {"schema": "waystone-round-exposure-1", "round_id": "old",
                   "at": "2026-07-15T00:00:00+00:00"}
        for evaluator in (overlay.evaluate_env_manifest_mutation,
                          overlay.evaluate_done_without_evidence):
            out = evaluator(missing)
            self.assertEqual(set(("evaluable", "fired", "coverage_reason")) - set(out), set())
            self.assertIs(out["evaluable"], False)
            self.assertIs(out["fired"], False)
        drift = common.delegation_scope_drift(Path("/definitely/missing"))
        self.assertEqual(set(("evaluable", "fired", "coverage_reason")) - set(drift), set())

    def test_f6_from_rec_is_one_to_one_timestamp_bound_and_rejections_are_factual(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            decisions = root / ".waystone" / "improve" / "decisions.jsonl"
            decisions.parent.mkdir(parents=True)
            _write_jsonl(decisions, [{
                "rec_id": "worker_scope_drift/one", "decision": "accept",
                "at": "2026-07-15T00:00:00+00:00",
            }])
            _add_delta(root, home, delta_id="worker_scope_drift/first",
                       rule="delegation-scope-drift-v1", from_rec="worker_scope_drift/one")
            with self.assertRaises(common.WorkflowError):
                _add_delta(root, home, delta_id="worker_scope_drift/second",
                           rule="delegation-scope-drift-v1", from_rec="worker_scope_drift/one")

            _write_jsonl(decisions, [{
                "rec_id": "worker_scope_drift/bad-time", "decision": "accept",
                "at": "not-an-iso-timestamp",
            }])
            loaded, skipped = improve._load_decisions(root)
            self.assertEqual(loaded, [])
            self.assertEqual(skipped, 1)

    def test_f6_accept_after_delta_is_quarantined_from_adaptive_statistics(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            decisions = root / ".waystone" / "improve" / "decisions.jsonl"
            decisions.parent.mkdir(parents=True)
            _write_jsonl(decisions, [{
                "rec_id": "worker_scope_drift/forged", "decision": "accept",
                "at": "2026-07-15T02:00:00+00:00",
            }])
            overlay._write_delta(root, {
                "schema": "waystone-delta-1", "id": "worker_scope_drift/forged",
                "rule": "delegation-scope-drift-v1", "status": "observing",
                "created_at": "2026-07-15T01:00:00+00:00", "transitions": [],
                "evidence": {"source": "improve-rec", "rec_id": "worker_scope_drift/forged"},
            })
            observation = _run_with_home(home, lambda: improve._adaptive_feedback_observation(
                "demo", root, [], [], [], {}))
            facts = observation["facts"]
            self.assertEqual(facts["deltas"], [])
            self.assertEqual(facts["coverage"]["accept_delta_conflicts"],
                             {"accept-after-delta": 1})

    def test_f7_warning_context_is_normalized_once_before_any_consumer(self):
        rows = [
            {"at": "2026-07-15T00:00:00+00:00", "boundary": "delegate-run",
             "rule": "delegation-scope-drift-v1", "event": "conflict",
             "policy_identity": {"layer": "project", "id": "d"},
             "context": {"task_id": "feat/a", "delegation_id": "did-b",
                         "policy_identities": [{"layer": "project", "id": "d"},
                                               {"layer": "project", "id": "other"}],
                         "resolution": "least-restrictive"},
             "source_pointer": "/w:1"},
            {"at": "2026-07-15T00:01:00+00:00", "boundary": "delegate-run",
             "rule": "delegation-scope-drift-v1", "event": "evaluation",
             "policy_identity": {"layer": "project", "id": "d"},
             "context": {"task_id": "feat/b", "delegation_id": "did-b", "fired": False},
             "source_pointer": "/w:2"},
        ]
        normalized, coverage = improve._normalize_warning_rows(rows, {"did-b": "feat/b"})
        self.assertEqual(len(normalized), 1)
        self.assertEqual(coverage, {"conflicting-context": 1})
        self.assertEqual(normalized[0]["task_ids"], ["feat/b"])

    def test_f8_manifest_vocabulary_and_meaningful_env_prep_semantics(self):
        for path in ("composer.lock", "pom.xml", "gradle.lockfile", "Package.resolved",
                     "go.sum", "Cargo.lock"):
            self.assertTrue(overlay._is_dependency_manifest(path), path)
        removed = overlay.evaluate_env_manifest_mutation({"round_evidence": {
            "evaluable": True, "manifest_paths": ["pom.xml"], "task_scopes": {"feat/x": ["src"]},
            "task_scope_coverage": {"feat/x": "explicit"},
            "env_prep_before": ["uv sync"], "env_prep_after": None,
            "env_prep_change_kind": "removed",
        }})
        self.assertIs(removed["evaluable"], True)
        self.assertIs(removed["fired"], True)
        self.assertEqual(removed["fires"], ["pom.xml"])
        unknown = overlay.evaluate_env_manifest_mutation({"round_evidence": {
            "evaluable": True, "manifest_paths": ["pom.xml"], "task_scopes": {},
            "task_scope_coverage": {"feat/x": "scope-unknown"},
            "env_prep_before": None, "env_prep_after": None,
            "env_prep_change_kind": "unchanged",
        }})
        self.assertIs(unknown["evaluable"], False)
        self.assertEqual(unknown["coverage_reason"], "task-scope-unknown")
        updated = overlay.evaluate_env_manifest_mutation({"round_evidence": {
            "evaluable": False, "coverage_reason": "task-scope-unknown",
            "manifest_paths": ["pom.xml"], "task_scopes": {},
            "task_scope_coverage": {"feat/x": "task-scope-unknown"},
            "env_prep_before": None, "env_prep_after": ["mvn dependency:go-offline"],
            "env_prep_change_kind": "added",
        }})
        self.assertIs(updated["evaluable"], True)
        self.assertIs(updated["fired"], False)

    def test_f9_round_snapshot_builds_delegation_index_and_capture_once(self):
        import contextlib
        import io
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            round_id = f"{TEST_CURRENT_ROUND_DATE}-one-scan"
            tasks = (root / "tasks.yaml").read_text().replace(
                "  - id: fix/finding-a", "  - id: chore/second\n    title: a second task close\n"
                "    status: active\n    deps: []\n  - id: fix/finding-a")
            (root / "tasks.yaml").write_text(tasks)
            with mock.patch.object(overlay, "_delegation_evidence_index",
                                   wraps=overlay._delegation_evidence_index) as index, \
                    mock.patch.object(overlay, "_capture_round_evidence",
                                      wraps=overlay._capture_round_evidence) as capture, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: round.close(
                    root, round_id, done=["chore/close-me", "chore/second"],
                    touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            self.assertEqual(index.call_count, 1)
            self.assertEqual(capture.call_count, 1)


class L2DPolicyMachineTests(unittest.TestCase):
    """L2-D G5/G6/G7: maturity, four policy layers, consent, and materialization."""

    def _project(self, directory: str) -> tuple[Path, Path]:
        root = Path(directory) / "repo"
        home = Path(directory) / "home"
        root.mkdir()
        home.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text(
            "version: 1\nproject: demo\nreviews_dir: docs/reviews\n"
            "state:\n  last_round_commit: null\n")
        (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
        return root, home

    @staticmethod
    def _replay(root: Path, delta_id: str) -> None:
        delta = overlay.load_delta(root, delta_id)
        delta["replay"] = {"fires": 1, "opportunities": 2, "fire_rate": 0.5,
                           "by_round": []}
        overlay._write_delta(root, delta)

    @staticmethod
    def _promotion_evidence(root: Path, home: Path, rule: str, delta_id: str) -> None:
        other = root.parent / "other-project"
        other.mkdir()
        machine = _run_with_home(home, common.machine_dir)
        machine.mkdir(parents=True, exist_ok=True)
        (machine / "projects.json").write_text(_json.dumps({"projects": [
            {"name": "demo", "path": str(root.resolve()), "aliases": []},
            {"name": "other", "path": str(other.resolve()), "aliases": []},
        ]}))
        for project in (root, other):
            warnings = project / ".waystone" / "overlay" / "warnings.jsonl"
            warnings.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl(warnings, [{
                "at": "2026-07-15T00:00:00+00:00", "boundary": "check", "rule": rule,
                "event": "evaluation", "delta_status": "observing",
                "policy_identity": {"layer": "project", "id": delta_id},
                "origin_delta_id": delta_id, "message": "evaluated", "context": {},
                "params_fingerprint": overlay._policy_params_fingerprint(
                    rule, dict(overlay.RULES[rule].get("default_params") or {})),
            }])

    def test_maturity_is_deterministic_recorded_and_recommendations_stay_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            out = root / ".waystone" / "improve"
            out.mkdir(parents=True)
            _write_jsonl(out / "sessions.jsonl", [{"project": "demo", "session_id": "s1"}])
            _write_jsonl(out / "delegations.jsonl", [{"project": "demo", "session_id": "s1"}])
            _write_jsonl(out / "reviews.jsonl", [
                {"project": "demo", "round_id": "r1", "feedback_file": "r1-feedback.md",
                 "findings": [], "counts": {}},
                {"project": "demo", "round_id": "r2", "feedback_file": None,
                 "findings": [], "counts": {}},
            ])
            (out / "decisions.jsonl").write_text(
                '{"rec_id":"retry_loops/x","decision":"accept","at":"2026-07-15T00:00:00Z"}\n')
            (out / "parse_coverage.json").write_text("{}")

            facts = _run_with_home(home, lambda: improve.run_audit(
                out, improve.PROJECT_LENS_SCOPE, project_root=root))
            self.assertEqual(facts["maturity"]["stage"], "calibrate")
            self.assertEqual(facts["maturity"]["recommendation_tier"], "always-allowed")
            self.assertEqual(facts["maturity"]["counts"], {
                "traced_sessions": 1, "rounds": 2, "review_feedback": 1,
                "findings": 0, "delegations": 1, "decisions": 1,
            })
            state_path = root / ".waystone" / "maturity.json"
            state = _json.loads(state_path.read_text())
            self.assertEqual([row["to"] for row in state["transitions"]], ["calibrate"])

            reviews = []
            for number in range(5):
                reviews.append({
                    "project": "demo", "round_id": f"r{number}",
                    "feedback_file": f"r{number}-feedback.md",
                    "findings": [{"id": f"f{number}-{finding}"} for finding in range(4)],
                    "counts": {},
                })
            _write_jsonl(out / "reviews.jsonl", reviews)
            facts = _run_with_home(home, lambda: improve.run_audit(
                out, improve.PROJECT_LENS_SCOPE, project_root=root))
            self.assertEqual(facts["maturity"]["stage"], "tune")
            state = _json.loads(state_path.read_text())
            self.assertEqual([row["to"] for row in state["transitions"]], ["calibrate", "tune"])
            self.assertIn("enforce", improve.MATURITY_STAGES)
            self.assertEqual(improve.maturity_stage({key: 10_000 for key in facts["maturity"]["counts"]}),
                             "tune")
            self.assertFalse((state_path.parent / "maturity.json.tmp").exists())

    def test_user_promotion_is_explicit_and_evidence_gated(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            machine = _run_with_home(home, common.machine_dir)
            machine.mkdir(parents=True, exist_ok=True)
            (machine / "projects.json").write_text(_json.dumps({"projects": [{
                "name": "demo", "path": str(root.resolve()), "aliases": [],
            }]}))
            _run_with_home(home, lambda: overlay.add_delta(
                root, "retry_loops/one-project", rule="done-without-evidence-v1", summary="s",
                candidate_scope="user_candidate", observed_in=["demo"]))
            with self.assertRaises(common.WorkflowError) as cm:
                _run_with_home(home, lambda: overlay.promote_user(root, "retry_loops/one-project"))
            self.assertIn("2 distinct projects", str(cm.exception))

            _run_with_home(home, lambda: overlay.add_delta(
                root, "retry_loops/shared", rule="done-without-evidence-v1", summary="s",
                candidate_scope="user_candidate"))
            self._promotion_evidence(
                root, home, "done-without-evidence-v1", "retry_loops/shared")
            promoted = _run_with_home(
                home, lambda: overlay.promote_user(root, "retry_loops/shared"))
            self.assertEqual(promoted["scope"]["kind"], "user")
            self.assertTrue(_run_with_home(
                home, lambda: overlay._user_delta_path("retry_loops/shared")).is_file())
            self.assertNotIn("accepted", overlay.DELTA_STATUSES)

    def test_composition_layers_committed_wins_and_round_override_expires(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            delta_id = "verification_debt/local"
            _run_with_home(home, lambda: overlay.add_delta(
                root, delta_id, rule="delegation-verification-evidence-v1", summary="s",
                candidate_scope="user_candidate"))
            self._promotion_evidence(
                root, home, "delegation-verification-evidence-v1", delta_id)
            _run_with_home(home, lambda: overlay.promote_user(root, delta_id))
            policy = {
                "schema": "waystone-project-policy-1",
                "policies": [{
                    "id": "delegation-verification-evidence",
                    "rule": "delegation-verification-evidence-v1", "stage": "warning",
                    "params": {}, "summary": "committed",
                }],
            }
            (root / "docs").mkdir()
            (root / "docs" / "waystone-policy.yaml").write_text(
                yaml.safe_dump(policy, sort_keys=False))
            _run_with_home(home, lambda: overlay.set_round_override(
                root, "2026-07-15-l2d", "delegation-verification-evidence-v1",
                "warning", "one-round exception"))

            composed = _run_with_home(home, lambda: overlay.compose_policy(
                root, round_id="2026-07-15-l2d"))
            self.assertEqual([layer["name"] for layer in composed["layers"]],
                             ["base", "user", "project", "round"])
            effective = next(row for row in composed["effective"]
                             if row["rule"] == "delegation-verification-evidence-v1")
            self.assertEqual((effective["layer"], effective["stage"]), ("round", "warning"))
            self.assertTrue(any(row["layer"] == "project" for row in composed["shadowed"]))

            _run_with_home(home, lambda: overlay.expire_round_overrides(root, "2026-07-15-l2d"))
            composed = _run_with_home(home, lambda: overlay.compose_policy(root))
            effective = next(row for row in composed["effective"]
                             if row["rule"] == "delegation-verification-evidence-v1")
            self.assertEqual((effective["layer"], effective["source_kind"]),
                             ("project", "committed"))
            shadow = next(row for row in composed["shadowed"]
                          if row["identity"] == {"layer": "project", "id": delta_id})
            self.assertEqual(shadow["reason"], "committed-wins")
            self.assertTrue(any(row["resolution"] == "committed-wins"
                                for row in composed["conflicts"]))
            override = _json.loads(overlay._round_override_path(root).read_text())
            self.assertIsNotNone(override["expired_at"])
            self.assertFalse(_run_with_home(
                home, lambda: overlay.expire_round_overrides(root, "2026-07-16-next")))

    def test_same_scope_conflict_is_least_restrictive_and_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            for delta_id in ("verification_debt/observe", "verification_debt/warn"):
                _run_with_home(home, lambda did=delta_id: overlay.add_delta(
                    root, did, rule="delegation-verification-evidence-v1", summary="s"))
            warning = overlay.load_delta(root, "verification_debt/warn")
            warning["status"] = "warning"
            overlay._write_delta(root, warning)
            composed = _run_with_home(home, lambda: overlay.compose_policy(root))
            effective = next(row for row in composed["effective"]
                             if row["rule"] == "delegation-verification-evidence-v1")
            self.assertEqual(effective["stage"], "observing")
            self.assertEqual(composed["conflicts"][0]["resolution"], "least-restrictive")
            _run_with_home(home, lambda: overlay.evaluate_boundary(root, "check", {}))
            rows = _read_warnings(root, home)
            self.assertTrue(any(row["event"] == "conflict" for row in rows))

    def test_consent_log_and_materialization_require_explicit_acceptance(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            delta_id = "verification_debt/materialize"
            _run_with_home(home, lambda: overlay.add_delta(
                root, delta_id, rule="delegation-verification-evidence-v1", summary="verified",
                pointers=["facts.json#verification_debt"]))
            self._replay(root, delta_id)
            with self.assertRaises(common.WorkflowError) as cm:
                _run_with_home(home, lambda: overlay.materialize(root, delta_id))
            self.assertIn("consent", str(cm.exception))

            context = _run_with_home(
                home, lambda: overlay.materialize_consent_context(root, delta_id))
            consent = _run_with_home(home, lambda: common.record_consent(
                root, "materialize", "accept", context))
            self.assertEqual(set(consent), {"surface", "choice", "at", "context"})
            path = _run_with_home(home, lambda: overlay.materialize(root, delta_id))
            document = yaml.safe_load(path.read_text())
            self.assertEqual(document["schema"], "waystone-project-policy-1")
            self.assertNotIn("origin_delta_id", document["policies"][0])
            mapping = _json.loads(overlay._materialization_map_path(root).read_text())
            self.assertEqual(mapping["mappings"][0]["origin_delta_id"], delta_id)
            self.assertNotIn("provenance", document["policies"][0])
            self.assertIn("docs/waystone-policy.yaml", git(
                root, "status", "--short", "--untracked-files=all").stdout)

    def test_consent_cli_records_standard_shape(self):
        import contextlib
        import io
        import waystone

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: waystone.main([
                    "consent", "record", "install.agents", "accept",
                    "--context", "kind=agents", "--root", str(root)]))
            self.assertEqual(rc, 0)
            rows = [_json.loads(line) for line in (root / ".waystone" / "consents.jsonl")
                    .read_text().splitlines()]
            self.assertEqual(rows[0]["context"]["kind"], "agents")
            self.assertEqual(rows[0]["context"]["stage"], "install")
            self.assertEqual(len(rows[0]["context"]["candidate_hash"]), 64)
            self.assertEqual(len(rows[0]["context"]["template_hash"]), 64)

    def test_overlay_cli_verbs_are_explicit_gated_and_never_reach_enforce(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            delta_id = "verification_debt/cli"
            _run_with_home(home, lambda: overlay.add_delta(
                root, delta_id, rule="delegation-verification-evidence-v1", summary="s",
                candidate_scope="user_candidate"))
            self._promotion_evidence(
                root, home, "delegation-verification-evidence-v1", delta_id)
            self._replay(root, delta_id)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_run_with_home(home, lambda: overlay.main([
                    "promote-user", delta_id, "--root", str(root)])), 0)
                self.assertEqual(_run_with_home(home, lambda: overlay.main([
                    "override", "delegation-verification-evidence-v1",
                    "--round", "2026-07-15-cli", "--stage", "warning",
                    "--root", str(root)])), 1)
                self.assertEqual(_run_with_home(home, lambda: overlay.main([
                    "override", "delegation-verification-evidence-v1",
                    "--round", "2026-07-15-cli", "--stage", "enforce",
                    "--reason", "must stay unreachable", "--root", str(root)])), 1)
                self.assertEqual(_run_with_home(home, lambda: overlay.main([
                    "override", "delegation-verification-evidence-v1",
                    "--round", "2026-07-15-cli", "--stage", "warning",
                    "--reason", "temporary", "--root", str(root)])), 0)
                self.assertEqual(_run_with_home(home, lambda: overlay.main([
                    "materialize", delta_id, "--consent-recorded", "--root", str(root)])), 0)
            self.assertTrue((root / "docs" / "waystone-policy.yaml").is_file())

    def test_managed_agent_and_boundary_hook_marker_installs(self):
        import contextlib
        import io
        import waystone

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            for kind in ("agents", "hooks"):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = _run_with_home(home, lambda k=kind: waystone.main([
                        "install", k, "--consent-recorded", "--root", str(root)]))
                self.assertEqual(rc, 0)
            repo_root = SCRIPTS.parent
            self.assertEqual((root / ".claude" / "agents" / "waystone-operator.md").read_bytes(),
                             (repo_root / "templates" / "waystone-operator-agent.md").read_bytes())
            self.assertTrue((root / ".waystone" / "boundary-hooks-enabled").is_file())
            self.assertFalse((root / ".claude" / "settings.json").exists())
            self.assertFalse((repo_root / "templates" / "waystone-boundary-hook.json").exists())
            status = git(root, "status", "--short", "--untracked-files=all").stdout
            self.assertIn(".claude/agents/waystone-operator.md", status)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: waystone.main([
                    "install", "agents", "--consent-recorded", "--root", str(root)]))
            self.assertEqual(rc, 1)

    def test_hook_install_reports_legacy_settings_without_modifying_them(self):
        import contextlib
        import io
        import waystone

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            settings = root / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text(_json.dumps({"hooks": {"Stop": [{"hooks": [{
                "type": "command", "command": "waystone check", "timeout": 30,
            }]}]}}))
            before = settings.read_bytes()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: waystone.main([
                    "install", "hooks", "--consent-recorded", "--root", str(root)]))
            self.assertEqual(rc, 0)
            self.assertIn("legacy", stdout.getvalue().lower())
            self.assertIn(".claude/settings.json", stdout.getvalue())
            self.assertIn("remove", stdout.getvalue().lower())
            self.assertEqual(settings.read_bytes(), before)
            self.assertTrue((root / ".waystone" / "boundary-hooks-enabled").is_file())

    def test_statusline_install_records_consent_and_preserves_other_settings(self):
        import contextlib
        import io
        import waystone

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            settings = home / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text(_json.dumps({"permissions": {"allow": ["Read"]}}))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                consent_rc = _run_with_home(home, lambda: waystone.main([
                    "consent", "record", "install.statusline", "accept",
                    "--context", "kind=statusline", "--root", str(root)]))
                install_rc = _run_with_home(home, lambda: waystone.main([
                    "install", "statusline", "--root", str(root)]))

            self.assertEqual((consent_rc, install_rc), (0, 0))
            document = _json.loads(settings.read_text())
            self.assertEqual(document["permissions"], {"allow": ["Read"]})
            self.assertEqual(document["statusLine"], {
                "type": "command", "command": "waystone statusline 2>/dev/null || true",
            })
            rows = [_json.loads(line) for line in (root / ".waystone" / "consents.jsonl")
                    .read_text().splitlines()]
            self.assertEqual(rows[-1]["surface"], "install.statusline")
            self.assertEqual(rows[-1]["choice"], "accept")
            self.assertEqual(rows[-1]["context"]["kind"], "statusline")
            self.assertIn(str(settings), stdout.getvalue())
            self.assertFalse((root / ".claude" / "settings.json").exists())

    def test_statusline_install_never_overwrites_existing_setting_and_prints_embed_guide(self):
        import contextlib
        import io
        import waystone

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            settings = home / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text(_json.dumps({
                "statusLine": {"type": "command", "command": "bash my-status.sh"},
                "keep": True,
            }, indent=2) + "\n")
            before = settings.read_bytes()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: waystone.main([
                    "install", "statusline", "--consent-recorded", "--root", str(root)]))

            self.assertEqual(rc, 0)
            self.assertEqual(settings.read_bytes(), before)
            message = stdout.getvalue()
            self.assertIn("embed", message.lower())
            self.assertIn("waystone statusline", message)
            self.assertIn('cd "$cwd"', message)
            self.assertIn(str(settings), message)

    def test_init_skill_offers_statusline_as_an_independent_optional_install(self):
        skill = (SCRIPTS.parent / "skills" / "init" / "SKILL.md").read_text()
        step = skill.split("## Step 8.5", 1)[1].split("## Step 9", 1)[0]
        for phrase in (
            "statusline", "~/.claude/settings.json", "waystone consent record install.statusline",
            "waystone install statusline", "embed", "declined",
        ):
            self.assertIn(phrase, step.lower())

    def test_round_and_delegate_exposures_capture_composed_policy(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "review_association/local", rule="round-close-open-findings-v1", summary="s"))
            _run_with_home(home, lambda: overlay.set_round_override(
                root, TEST_CLOSE_ROUND_ID, "round-close-open-findings-v1", "observing", "temporary"))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: round.close(
                    root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"], touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            exposure = _json.loads((overlay._exposure_dir(root) /
                                    f"round-{TEST_CLOSE_ROUND_ID}.json").read_text())
            effective = next(row for row in exposure["policy_composition"]["effective"]
                             if row["rule"] == "round-close-open-findings-v1")
            self.assertEqual(effective["layer"], "round")
            self.assertIsNotNone(_json.loads(overlay._round_override_path(root).read_text())["expired_at"])

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/local", rule="delegation-verification-evidence-v1",
                summary="s"))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_deleg_run(root, home, _deleg_fake({"impl.py": "x\n"})), 0)
            exposure = _json.loads((_latest_rec(root, home) / "exposure.json").read_text())
            effective = next(row for row in exposure["policy_composition"]["effective"]
                             if row["rule"] == "delegation-verification-evidence-v1")
            self.assertEqual(effective["layer"], "project")


class L2DAdversarialFindingTests(unittest.TestCase):
    """L2-D adversarial findings F1-F9: policy-machine closure invariants."""

    def _project(self, base: Path, name: str = "repo") -> Path:
        root = base / name
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text(
            f"version: 1\nproject: {name}\nreviews_dir: docs/reviews\n"
            "state:\n  last_round_commit: null\n")
        (root / "tasks.yaml").write_text(f"version: 1\nproject: {name}\ntasks: []\n")
        return root

    @staticmethod
    def _register(home: Path, *roots: Path) -> None:
        machine = _run_with_home(home, common.machine_dir)
        machine.mkdir(parents=True, exist_ok=True)
        (machine / "projects.json").write_text(_json.dumps({
            "projects": [
                {"name": root.name, "path": str(root.resolve()), "aliases": []}
                for root in roots
            ],
        }))

    @staticmethod
    def _observation(root: Path, rule: str, delta_id: str) -> None:
        path = root / ".waystone" / "overlay" / "warnings.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "at": "2026-07-15T00:00:00+00:00", "boundary": "check",
            "rule": rule, "event": "evaluation", "delta_status": "observing",
            "policy_identity": {"layer": "project", "id": delta_id},
            "origin_delta_id": delta_id,
            "params_fingerprint": overlay._policy_params_fingerprint(
                rule, dict(overlay.RULES[rule].get("default_params") or {})),
            "message": "rule evaluated at workflow boundary",
            "context": {"evaluable": True, "fired": False, "coverage_reason": None},
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(_json.dumps(row, sort_keys=True) + "\n")

    @staticmethod
    def _policy(rule: str, *, policy_id: str = "verification-evidence",
                stage: str = "warning", params: dict | None = None, **extra) -> dict:
        return {
            "id": policy_id, "rule": rule, "stage": stage,
            "params": {} if params is None else params,
            "summary": "committed policy",
            **extra,
        }

    def test_f1_every_layer_is_effective_and_overrides_the_previous_layer(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, home = self._project(base), base / "home"
            home.mkdir()
            rule = "delegation-verification-evidence-v1"
            composed = _run_with_home(home, lambda: overlay.compose_policy(root))
            effective = next(row for row in composed["effective"] if row["rule"] == rule)
            self.assertEqual(effective["identity"], {"layer": "base", "id": f"base/{rule}"})

            user_delta = {
                "schema": "waystone-delta-1", "id": "verification_debt/shared", "rule": rule,
                "status": "warning", "params": {}, "origin_delta_id": "verification_debt/shared",
            }
            _run_with_home(home, lambda: overlay._write_new_user_delta(user_delta))
            composed = _run_with_home(home, lambda: overlay.compose_policy(root))
            effective = next(row for row in composed["effective"] if row["rule"] == rule)
            self.assertEqual(effective["identity"]["layer"], "user")

            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/project", rule=rule, summary="project"))
            composed = _run_with_home(home, lambda: overlay.compose_policy(root))
            effective = next(row for row in composed["effective"] if row["rule"] == rule)
            self.assertEqual(effective["identity"]["layer"], "project")

            _run_with_home(home, lambda: overlay.set_round_override(
                root, "2026-07-15-f1", rule, "warning", "round-specific"))
            composed = _run_with_home(
                home, lambda: overlay.compose_policy(root, round_id="2026-07-15-f1"))
            effective = next(row for row in composed["effective"] if row["rule"] == rule)
            self.assertEqual(effective["identity"]["layer"], "round")

    def test_f2_observed_in_is_registry_and_evidence_derived_not_user_supplied(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base, home = Path(d), Path(d) / "home"
            home.mkdir()
            root = self._project(base, "one")
            other = self._project(base, "two")
            self._register(home, root, other)
            rule = "done-without-evidence-v1"
            delta_id = "verification_debt/cross-project"
            delta = _run_with_home(home, lambda: overlay.add_delta(
                root, delta_id, rule=rule, summary="candidate",
                candidate_scope="user_candidate", observed_in=["forged-a", "forged-b"]))
            self.assertEqual(delta["observed_in"], [])
            with self.assertRaisesRegex(common.WorkflowError, "2 distinct projects"):
                _run_with_home(home, lambda: overlay.promote_user(root, delta_id))

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_run_with_home(home, lambda: overlay.main([
                    "add", "verification_debt/cli-forgery", "--rule", rule,
                    "--summary", "candidate", "--observed-in", "forged",
                    "--root", str(root)])), 1)

            self._observation(root, rule, delta_id)
            self._observation(other, rule, delta_id)
            promoted = _run_with_home(home, lambda: overlay.promote_user(root, delta_id))
            self.assertEqual(promoted["observed_in"],
                             sorted((str(root.resolve()), str(other.resolve()))))

    def test_f3_user_write_is_atomic_machine_locked_and_delegate_reuses_snapshot(self):
        import contextlib
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            base, home = Path(d), Path(d) / "home"
            home.mkdir()
            root = self._project(base, "one")
            other = self._project(base, "two")
            self._register(home, root, other)
            rule = "done-without-evidence-v1"
            delta_id = "verification_debt/locked"
            _run_with_home(home, lambda: overlay.add_delta(
                root, delta_id, rule=rule, summary="candidate",
                candidate_scope="user_candidate"))
            self._observation(root, rule, delta_id)
            self._observation(other, rule, delta_id)

            entered = []

            @contextlib.contextmanager
            def observed_lock(path, timeout=None):
                entered.append(Path(path))
                yield

            replaced = []
            real_replace = overlay.os.replace

            def observed_replace(source, target):
                replaced.append((Path(source), Path(target)))
                return real_replace(source, target)

            def observed_project_lock(project, timeout=None):
                return observed_lock(common.project_lock_path(project), timeout=timeout)

            with mock.patch.object(overlay, "hold_lock", observed_lock), \
                    mock.patch.object(overlay, "hold_project_lock", observed_project_lock), \
                    mock.patch.object(overlay.os, "replace", observed_replace):
                _run_with_home(home, lambda: overlay.promote_user(root, delta_id))
            self.assertEqual(entered, _run_with_home(home, lambda: [
                common.registry_lock_path(), common.overlay_lock_path(),
                common.project_lock_path(root),
            ]))
            target = _run_with_home(home, lambda: overlay._user_delta_path(delta_id))
            self.assertEqual(replaced[-1][1], target)
            self.assertFalse(target.with_name(target.name + ".tmp").exists())

            composition = {"effective": [{
                "identity": {"layer": "base", "id": "base/x"}, "stage": "observing",
            }]}
            with mock.patch.object(overlay, "compose_policy",
                                   side_effect=AssertionError("must reuse supplied snapshot")):
                self.assertEqual(delegate._active_overlays(root, composition), [{
                    "identity": {"layer": "base", "id": "base/x"}, "status": "observing",
                }])

    def test_f4_layer_qualified_identity_prevents_cross_layer_attribution(self):
        with tempfile.TemporaryDirectory() as d:
            base, home = Path(d), Path(d) / "home"
            home.mkdir()
            root = self._project(base)
            rule = "delegation-scope-drift-v1"
            delta_id = "worker_scope_drift/shared"
            delta = _run_with_home(home, lambda: overlay.add_delta(
                root, delta_id, rule=rule, summary="project"))
            delta["created_at"] = "2026-07-15T01:00:00+00:00"
            delta["transitions"] = [{"to": "observing", "at": delta["created_at"]}]
            overlay._write_delta(root, delta)
            user = {**delta, "scope": {"kind": "user"}, "origin_delta_id": delta_id}
            _run_with_home(home, lambda: overlay._write_new_user_delta(user))

            composed = _run_with_home(home, lambda: overlay.compose_policy(root))
            effective = next(row for row in composed["effective"] if row["rule"] == rule)
            self.assertEqual(effective["identity"], {"layer": "project", "id": delta_id})
            shadow = next(row for row in composed["shadowed"]
                          if row["identity"] == {"layer": "user", "id": delta_id})
            self.assertEqual(shadow["shadowed_by"], {"layer": "project", "id": delta_id})

            warnings = [
                {"at": "2026-07-15T03:10:00+00:00", "boundary": "check", "rule": rule,
                 "event": "evaluation", "policy_identity": {"layer": layer, "id": delta_id},
                 "origin_delta_id": delta_id,
                 "context": {"round_id": "r1", "delegation_id": f"did-{layer}",
                             "snapshot": f"snap-{layer}", "fired": True},
                 "source_pointer": f"/{layer}"}
                for layer in ("user", "project")
            ]
            exposures = [{"round_id": "r1", "at": "2026-07-15T03:00:00+00:00",
                          "_file": "/r1"}]
            observation = _run_with_home(home, lambda: improve._adaptive_feedback_observation(
                "repo", root, [], warnings, exposures, {}))
            fact = observation["facts"]["deltas"][0]
            self.assertEqual(fact["identity"], {"layer": "project", "id": delta_id})
            self.assertEqual(fact["active"]["opportunities"], 1)

    def test_f5_compose_recovers_override_after_durable_round_close(self):
        with tempfile.TemporaryDirectory() as d:
            base, home = Path(d), Path(d) / "home"
            home.mkdir()
            root = self._project(base)
            round_id = "2026-07-15-recovery"
            rule = "done-without-evidence-v1"
            _run_with_home(home, lambda: overlay.set_round_override(
                root, round_id, rule, "warning", "temporary"))
            path = overlay._round_override_path(root)
            override = _json.loads(path.read_text())
            self.assertEqual(override["overrides"][0]["round_id"], round_id)
            exposure = overlay._exposure_dir(root) / f"round-{round_id}.json"
            exposure.parent.mkdir(parents=True, exist_ok=True)
            exposure.write_text(_json.dumps({
                "schema": "waystone-round-exposure-1", "round_id": round_id,
                "at": "2099-01-01T00:00:00+00:00",
            }))
            composed = _run_with_home(home, lambda: overlay.compose_policy(root))
            effective = next(row for row in composed["effective"] if row["rule"] == rule)
            self.assertNotEqual(effective["identity"]["layer"], "round")
            recovered = _json.loads(path.read_text())
            self.assertIsNotNone(recovered["expired_at"])
            self.assertEqual(recovered["expiry_reason"], "durable-round-close")

    def test_f6_committed_policy_schema_and_rule_params_fail_loud(self):
        with tempfile.TemporaryDirectory() as d:
            base, home = Path(d), Path(d) / "home"
            home.mkdir()
            root = self._project(base)
            docs = root / "docs"
            docs.mkdir()
            path = docs / "waystone-policy.yaml"
            invalid = [
                self._policy("unknown-rule-v1"),
                self._policy("delegation-verification-evidence-v1", unexpected=True),
                self._policy("delegation-verification-evidence-v1", params={"extra": 1}),
                self._policy("round-close-open-findings-v1", params={"severities": "major"}),
                self._policy("review-skipped-closes-v1", params={"consecutive": True}),
            ]
            for policy in invalid:
                path.write_text(yaml.safe_dump({
                    "schema": "waystone-project-policy-1", "policies": [policy],
                }, sort_keys=False))
                with self.subTest(policy=policy), self.assertRaises(common.WorkflowError):
                    _run_with_home(home, lambda: overlay.compose_policy(root))
            path.write_text(yaml.safe_dump({
                "schema": "waystone-project-policy-1", "policies": [], "unexpected": True,
            }, sort_keys=False))
            with self.assertRaises(common.WorkflowError):
                _run_with_home(home, lambda: overlay.compose_policy(root))
            schema = _json.loads((SCRIPTS.parent / "templates" /
                                  "project-policy-schema.json").read_text())
            item_schema = schema["properties"]["policies"]["items"]
            self.assertFalse(item_schema["additionalProperties"])
            self.assertIn("oneOf", item_schema)

    def test_f7_materialize_commits_only_policy_and_sanitized_description(self):
        with tempfile.TemporaryDirectory() as d:
            base, home = Path(d), Path(d) / "home"
            home.mkdir()
            root = self._project(base)
            delta_id = "verification_debt/materialize-safe"
            _run_with_home(home, lambda: overlay.add_delta(
                root, delta_id, rule="delegation-verification-evidence-v1",
                summary=f"ran secret command at {root}/private.log",
                pointers=[f"{root}/facts.json#behavior"], from_rec=None,
                title=f"Verification policy\nfor {root} and /tmp/other-secret"))
            delta = overlay.load_delta(root, delta_id)
            delta["replay"] = {"fires": 9, "opportunities": 10, "fire_rate": 0.9}
            overlay._write_delta(root, delta)
            path = _run_with_home(
                home, lambda: overlay.materialize(root, delta_id, consent_recorded=True))
            document = yaml.safe_load(path.read_text())
            policy = document["policies"][0]
            self.assertEqual(set(policy), {"id", "rule", "stage", "params", "summary"})
            mapping = _json.loads(overlay._materialization_map_path(root).read_text())
            self.assertEqual(mapping["mappings"][0]["origin_delta_id"], delta_id)
            self.assertNotIn("\n", policy["summary"])
            serialized = path.read_text()
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("private.log", serialized)
            self.assertNotIn("/tmp/other-secret", serialized)
            self.assertNotIn("fire_rate", serialized)
            self.assertNotIn("provenance", serialized)

    def test_f8_consent_is_bound_to_candidate_stage_target_and_template_hash(self):
        import contextlib
        import io
        import waystone

        with tempfile.TemporaryDirectory() as d:
            base, home = Path(d), Path(d) / "home"
            home.mkdir()
            root = self._project(base)
            missing = "verification_debt/missing"
            with self.assertRaises(common.WorkflowError):
                _run_with_home(
                    home, lambda: overlay.materialize(root, missing, consent_recorded=True))
            self.assertFalse((root / ".waystone" / "consents.jsonl").exists())

            delta_id = "verification_debt/hash-bound"
            _run_with_home(home, lambda: overlay.add_delta(
                root, delta_id, rule="delegation-verification-evidence-v1", summary="candidate"))
            delta = overlay.load_delta(root, delta_id)
            delta["replay"] = {"fires": 1, "opportunities": 1, "fire_rate": 1.0}
            overlay._write_delta(root, delta)
            context = _run_with_home(
                home, lambda: overlay.materialize_consent_context(root, delta_id))
            self.assertEqual(set(context), {
                "origin_delta_id", "target_path", "candidate_hash", "stage",
            })
            forged_context = {**context, "candidate_hash": "0" * 64}
            _run_with_home(home, lambda: common.record_consent(
                root, "materialize", "accept", forged_context))
            with self.assertRaisesRegex(common.WorkflowError, "consent"):
                _run_with_home(home, lambda: overlay.materialize(root, delta_id))
            _run_with_home(home, lambda: common.record_consent(
                root, "materialize", "accept", context))
            changed = overlay.load_delta(root, delta_id)
            changed["status"] = "warning"
            overlay._write_delta(root, changed)
            with self.assertRaisesRegex(common.WorkflowError, "consent"):
                _run_with_home(home, lambda: overlay.materialize(root, delta_id))

            target = root / ".claude" / "agents" / "waystone-operator.md"
            target.parent.mkdir(parents=True)
            target.write_text("occupied")
            before = (root / ".waystone" / "consents.jsonl").read_text()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: waystone.main([
                    "install", "agents", "--consent-recorded", "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertEqual((root / ".waystone" / "consents.jsonl").read_text(), before)
            target.unlink()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: waystone.main([
                    "install", "agents", "--consent-recorded", "--root", str(root)]))
            self.assertEqual(rc, 0)
            rows = [_json.loads(line) for line in (root / ".waystone" / "consents.jsonl")
                    .read_text().splitlines()]
            install_context = rows[-1]["context"]
            self.assertEqual(install_context["stage"], "install")
            self.assertEqual(len(install_context["candidate_hash"]), 64)
            self.assertEqual(len(install_context["template_hash"]), 64)
            self.assertEqual(install_context["target_path"], str(target.resolve()))

    def test_f9_degraded_maturity_snapshot_never_records_a_transition(self):
        with tempfile.TemporaryDirectory() as d:
            base, home = Path(d), Path(d) / "home"
            home.mkdir()
            root = self._project(base)
            out = root / ".waystone" / "improve"
            out.mkdir(parents=True)
            _write_jsonl(out / "sessions.jsonl", [{"project": "repo", "session_id": "s1"}])
            _write_jsonl(out / "delegations.jsonl", [{"project": "repo", "session_id": "s1"}])
            _write_jsonl(out / "reviews.jsonl", [
                {"project": "repo", "round_id": "r1", "feedback_file": "r1-feedback.md",
                 "findings": [], "counts": {}},
                {"project": "repo", "round_id": "r2", "feedback_file": None,
                 "findings": [], "counts": {}},
            ])
            (out / "decisions.jsonl").write_text(
                '{"rec_id":"retry_loops/x","decision":"accept",'
                '"at":"2026-07-15T00:00:00Z"}\n')
            (out / "parse_coverage.json").write_text("{}")
            complete = _run_with_home(home, lambda: improve.run_audit(
                out, improve.PROJECT_LENS_SCOPE, project_root=root))
            self.assertEqual(complete["maturity"]["stage"], "calibrate")
            state_path = root / ".waystone" / "maturity.json"
            before = state_path.read_bytes()

            (out / "reviews.jsonl").write_text("{broken")
            degraded = _run_with_home(home, lambda: improve.run_audit(
                out, improve.PROJECT_LENS_SCOPE, project_root=root))
            self.assertIs(degraded["maturity"]["degraded"], True)
            self.assertEqual(degraded["maturity"]["stage"], "calibrate")
            self.assertIn("reviews", degraded["maturity"]["degraded_inputs"])
            self.assertEqual(state_path.read_bytes(), before)

            _write_jsonl(out / "reviews.jsonl", [])
            (out / "decisions.jsonl").unlink()
            degraded = _run_with_home(home, lambda: improve.run_audit(
                out, improve.PROJECT_LENS_SCOPE, project_root=root))
            self.assertIn("decisions", degraded["maturity"]["degraded_inputs"])
            self.assertEqual(state_path.read_bytes(), before)


class CodexPluginContractTests(unittest.TestCase):
    def test_dual_manifests_and_host_surfaces(self):
        root = SCRIPTS.parent
        claude = _json.loads((root / ".claude-plugin" / "plugin.json").read_text())
        codex = _json.loads((root / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual((claude["name"], claude["version"]),
                         (codex["name"], codex["version"]))
        self.assertEqual(codex["version"], "0.11.1")
        self.assertEqual(codex["skills"], "./skills/")
        self.assertNotIn("hooks", codex)
        for field in ("logo", "logoDark"):
            self.assertTrue((root / codex["interface"][field]).is_file())
        claude_hooks = _json.loads((root / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertEqual(set(claude_hooks),
                         {"PreToolUse", "SessionStart", "PreCompact", "SessionEnd", "PostToolUse",
                          "Stop"})
        commands = [hook["command"] for groups in claude_hooks.values()
                    for group in groups for hook in group["hooks"]]
        for command in commands:
            self.assertIn('"${CLAUDE_PLUGIN_ROOT}', command)
            self.assertNotRegex(command, r"(^|[ ;])waystone(?:[ ;]|$)")
        self.assertTrue((root / "bin" / "waystone-codex").stat().st_mode & 0o111)

    def test_boundary_hook_noops_without_config_or_enable_marker(self):
        import os

        script = SCRIPTS.parent / "hooks" / "scripts" / "boundary_check.sh"
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            fake_bin = base / "bin"
            fake_bin.mkdir()
            calls = base / "uv-calls"
            uv = fake_bin / "uv"
            uv.write_text("#!/usr/bin/env bash\nprintf called >> \"$UV_CALLS\"\nexit 0\n")
            uv.chmod(0o755)
            env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
                   "UV_CALLS": str(calls)}

            missing_config = base / "missing-config"
            (missing_config / ".waystone").mkdir(parents=True)
            (missing_config / ".waystone" / "boundary-hooks-enabled").touch()
            result = subprocess.run(
                ["bash", str(script)], input=_json.dumps({"cwd": str(missing_config)}),
                cwd=missing_config, capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)

            missing_marker = base / "missing-marker"
            missing_marker.mkdir()
            (missing_marker / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            result = subprocess.run(
                ["bash", str(script)], input=_json.dumps({"cwd": str(missing_marker)}),
                cwd=missing_marker, capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(calls.exists())

    def test_boundary_hook_preserves_stderr_and_never_blocks(self):
        import os

        script = SCRIPTS.parent / "hooks" / "scripts" / "boundary_check.sh"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            marker = root / ".waystone" / "boundary-hooks-enabled"
            marker.parent.mkdir(parents=True)
            marker.touch()
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            fake_bin = Path(d) / "bin"
            fake_bin.mkdir()
            uv = fake_bin / "uv"
            uv.write_text(
                "#!/usr/bin/env bash\nprintf 'launcher stdout\\n'\n"
                "printf 'launcher stderr\\n' >&2\nexit 17\n")
            uv.chmod(0o755)
            env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
            result = subprocess.run(
                ["bash", str(script)], input=_json.dumps({"cwd": str(root)}), cwd=root,
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "launcher stdout\n")
            self.assertEqual(result.stderr, "launcher stderr\n")

    def test_machine_data_root_is_host_neutral(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            old_host = os.environ.get("WAYSTONE_HOST")
            old_codex_home = os.environ.get("CODEX_HOME")
            try:
                os.environ["WAYSTONE_HOST"] = "codex"
                os.environ.pop("CODEX_HOME", None)
                self.assertEqual(_run_with_home(home, common.machine_dir), home / ".waystone")
                self.assertEqual(_run_with_home(
                    home, common.migrate_home_data, isolate_storage=False),
                                 home / ".waystone")
                self.assertFalse((home / ".claude" / "waystone").exists())
                os.environ["CODEX_HOME"] = str(home / "custom-codex")
                self.assertEqual(_run_with_home(home, common.machine_dir), home / ".waystone")
                self.assertEqual(_run_with_home(
                    home, common.migrate_home_data, isolate_storage=False),
                                 home / ".waystone")
            finally:
                if old_host is None:
                    os.environ.pop("WAYSTONE_HOST", None)
                else:
                    os.environ["WAYSTONE_HOST"] = old_host
                if old_codex_home is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = old_codex_home


class L3GapClosureAcceptanceTests(unittest.TestCase):
    """L3-3 acceptance failures: every new contract has a deterministic consumer."""

    def test_routing_questions_render_and_budget_note_reaches_packet(self):
        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            rendered = "\n".join(session_context._routing_block(root))
            policy = yaml.safe_load(session_context.ROUTING_POLICY_PATH.read_text())
            for question in policy["questions"]:
                self.assertIn(question["id"], rendered)
            self.assertLessEqual(len(session_context._routing_block(root)), 12)
            data = {"project": "demo", "tasks": [{
                "id": "feat/route", "title": "route", "status": "active",
                "accept": ["routed"],
            }]}
            packet, _acceptance = delegate._build_packet(
                data, "feat/route", [], root,
                routing_note="budget favors deterministic workflow")
            self.assertEqual(packet["routing_note"], {
                "provenance": "main-session",
                "note": "budget favors deterministic workflow",
            })
        skill = (SCRIPTS.parent / "skills" / "delegate" / "SKILL.md").read_text()
        for question_id in (
            "reasoning", "context-inheritance", "independent-perspective", "bounded-scope",
            "repetitive-tools", "retry-cost", "independent-verification", "budget-sensitivity",
        ):
            self.assertIn(question_id, skill)
        self.assertIn("--routing-note", skill)
        self.assertIn("external-runner", skill)
        self.assertIn("host-guided", skill)

    def test_routing_note_reaches_prompt_projection_and_opportunity_rebuttal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            data = {"project": "demo", "tasks": [{
                "id": "feat/route", "title": "route", "status": "active",
                "accept": ["routed"],
            }]}
            packet, _acceptance = delegate._build_packet(
                data, "feat/route", [], root,
                routing_note="budget favors direct execution")
            prompt = delegate._render_prompt(packet, "a" * 40)
            self.assertIn("- routing_note: budget favors direct execution", prompt)

            record = common.project_state_path(root) / "delegations" / "did-route"
            record.mkdir(parents=True)
            (record / "claim.json").write_text(_json.dumps({"task_id": "feat/route"}))
            (record / "exposure.json").write_text(_json.dumps({"task_id": "feat/route"}))
            (record / "status.json").write_text(_json.dumps({"state": "failed-runner"}))
            (record / "packet.yaml").write_text(yaml.safe_dump(packet, sort_keys=False))
            rows, skipped, _verdicts, _verifications = improve._project_delegation_rows(root)
            self.assertEqual(skipped, 0)
            self.assertEqual(rows[0]["routing_note"], packet["routing_note"])

            sessions = [{
                "project": "demo", "kind": "main", "session_id": "main-1",
                "tools": {"by_category": {"file_write": 4, "shell": 3}},
                "retry_loops": {"count": 1},
                "context_heavy": {"max_result_bytes": 150000},
                "usage": {"input": 120000},
            }]
            evidence = [{
                "project": "demo", "task_id": "feat/route",
                "task_context": {"session_id": "main-1", "acceptance_criteria": 1,
                                 "declared_scope_count": 1},
                "delegations": [{"did": "did-route", "routing_note": packet["routing_note"]}],
            }]
            lens = improve._lens_delegation_opportunity(sessions, evidence)
            self.assertEqual(lens["per_project"]["demo"]["candidates"], 0)
            self.assertEqual(lens["coverage"]["routing_note_rebuttal"], 1)

    def test_host_guided_routes_project_into_join_and_role_lens(self):
        routes = round._parse_route_notes([
            "implementer,forked-subagent,codex:gpt-test",
        ])
        self.assertEqual(routes, [{
            "role": "implementer", "execution": "forked-subagent",
            "backend": "codex:gpt-test", "provenance": "main-session",
        }])
        exposure = {"round_id": "r1", "at": "2026-07-15T00:00:00Z", "routes": routes}
        projected = improve._round_exposure_projection(
            {"round": "r1"}, {"r1": exposure})
        self.assertEqual(projected["routes"], routes)
        lens = improve._lens_finding_concentration([{
            "project": "demo", "round_id": "r1", "routes": routes,
            "findings": [{"id": "f1", "status": "REAL", "type": "correctness"}],
        }], [])
        self.assertEqual(lens["per_project"]["demo"]["by_role"], {"implementer": 1})

    def test_new_reviewer_default_is_role_and_skills_are_role_first(self):
        legacy = common.normalize_config({"version": 1, "project": "demo"})
        self.assertEqual(legacy["review"]["reviewers"], ["codex", "gpt-5.5-pro"])
        init_skill = (SCRIPTS.parent / "skills" / "init" / "SKILL.md").read_text()
        review_skill = (SCRIPTS.parent / "skills" / "review" / "SKILL.md").read_text()
        self.assertIn("reviewers: [role:reviewer]", init_skill)
        self.assertIn("role:reviewer", review_skill)
        self.assertNotIn('--reviewer "<resolved reviewer backend>"', review_skill)
        self.assertIn("reply itself must begin", review_skill)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            with self.assertRaisesRegex(common.WorkflowError, "profile") as raised:
                review.resolve_reviewers(root, ["role:reviewer"])
            self.assertIn("reviewers: [codex", str(raised.exception))

    def test_review_skill_requires_taxonomy_or_reasoned_unknown(self):
        text = (SCRIPTS.parent / "skills" / "review" / "SKILL.md").read_text()
        for finding_type in improve.FINDING_TYPES:
            self.assertIn(finding_type, text)
        self.assertIn("unknown", text)
        self.assertIn("reason", text.lower())

    def test_exposure_fingerprints_cover_full_config_policy_and_routing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreview: {mode: packet}\n"
                "delegation: {env_prep: null}\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            _path, exposure = overlay.write_round_exposure(
                root, "2026-07-15-fp", "a" * 40, "b" * 40)
            for field in (
                "config_fingerprint", "committed_policy_fingerprint",
                "routing_policy_fingerprint",
            ):
                self.assertIn(field, exposure)
            before = [{**exposure, "at": "2026-07-15T00:00:00Z"}]
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreview: {mode: pr}\n"
                "delegation: {env_prep: [uv sync]}\n")
            events, _coverage = improve._staleness_change_events(root, before)
            self.assertIn("current-config-fingerprint-mismatch", {reason for _at, reason in events})

    def test_f1_run_records_patch_digest_and_apply_rejects_post_verdict_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            patch_path = rec / "artifact" / "changes.patch"
            self.assertEqual(
                contract["patch_sha256"],
                "sha256:" + hashlib.sha256(patch_path.read_bytes()).hexdigest(),
            )
            _write_apply_verdict(rec)
            original = patch_path.read_bytes()
            replaced = original.replace(b"+x\n", b"+tampered\n")
            self.assertNotEqual(replaced, original)
            patch_path.write_bytes(replaced)
            with self.assertRaisesRegex(common.WorkflowError, "digest"):
                _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            self.assertFalse((root / "impl.py").exists())

    def test_f1_verify_proves_base_plus_patch_matches_result_and_pre_digest_cannot_apply(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _write_profile(root, (
                'schema: waystone-profile-1\nbindings:\n'
                '  implementer: {execution: external-runner, backend: "codex:gpt-test"}\n'
                '  verifier: {backend: "codex:gpt-test"}\n'
            ))
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            contract_path = rec / "artifact" / "contract.yaml"
            contract = yaml.safe_load(contract_path.read_text())
            contract["result_sha"] = contract["base_sha"]
            contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))
            with self.assertRaisesRegex(common.WorkflowError, "result_sha"):
                _run_with_home(home, lambda: delegate.verify_delegation(root, rec.name))
            self.assertEqual(list((rec / "artifact").glob("verify-*.json")), [])

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            contract_path = rec / "artifact" / "contract.yaml"
            contract = yaml.safe_load(contract_path.read_text())
            contract.pop("patch_sha256", None)
            contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))
            with self.assertRaisesRegex(common.WorkflowError, "pre-digest record"):
                _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))

    def test_f1_apply_rechecks_contract_and_verify_artifact_digests(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _write_profile(root, (
                'schema: waystone-profile-1\nbindings:\n'
                '  implementer: {execution: external-runner, backend: "codex:gpt-test"}\n'
                '  verifier: {backend: "codex:gpt-test"}\n'
            ))
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            contract_path = rec / "artifact" / "contract.yaml"
            contract_bytes = contract_path.read_bytes()
            contract = yaml.safe_load(contract_bytes)
            exposure = _json.loads((rec / "exposure.json").read_text())
            verify_path = rec / "artifact" / "verify-1.json"
            verify = {
                "schema": "waystone-verify-1", "at": "2026-07-15T00:00:00+00:00",
                "transport": "codex-exec:read-only", "backend": "codex:gpt-test",
                "provenance": "independent-verifier",
                "payload": {"summary": "reviewed", "findings": [], "limitations": []},
                "profile_fingerprint": exposure["profile_fingerprint"],
                "base_sha": contract["base_sha"], "result_sha": contract["result_sha"],
                "patch_sha256": contract["patch_sha256"],
                "requested_effort": None, "effective_effort": None,
                "effective_tool_policy": {"tools": ["synthetic"]},
            }
            verify_path.write_text(_json.dumps(verify) + "\n")
            verify_bytes = verify_path.read_bytes()
            _write_apply_verdict(rec)

            verify_path.write_bytes(verify_bytes + b" \n")
            with self.assertRaisesRegex(common.WorkflowError, "verify artifact digest"):
                _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            verify_path.write_bytes(verify_bytes)
            contract_path.write_bytes(contract_bytes + b"\n# replaced after verdict\n")
            with self.assertRaisesRegex(common.WorkflowError, "contract digest"):
                _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            self.assertFalse((root / "impl.py").exists())

    def test_f2_promote_user_requires_exact_candidate_evaluations(self):
        with tempfile.TemporaryDirectory() as d:
            base, home = Path(d), Path(d) / "home"
            home.mkdir()
            source = base / "source"
            source.mkdir()
            root, _unused = _deleg_project(source)
            other = base / "other"
            other.mkdir()
            machine = _run_with_home(home, common.machine_dir)
            machine.mkdir(parents=True, exist_ok=True)
            (machine / "projects.json").write_text(_json.dumps({"projects": [
                {"name": "source", "path": str(root.resolve()), "aliases": []},
                {"name": "other", "path": str(other.resolve()), "aliases": []},
            ]}))
            delta_id = "verification_debt/exact-candidate"
            rule = "done-without-evidence-v1"
            delta = _run_with_home(home, lambda: overlay.add_delta(
                root, delta_id, rule=rule, summary="candidate",
                candidate_scope="user_candidate"))
            fingerprint = common.canonical_payload_hash({
                "rule": rule, "params": delta.get("params") or {},
            })

            def observation(project: Path, *, identity: dict, event: str,
                            origin: str | None, status: str = "observing",
                            params_fingerprint: str = fingerprint) -> None:
                path = project / ".waystone" / "overlay" / "warnings.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                row = {
                    "at": "2026-07-15T00:00:00+00:00", "boundary": "check",
                    "rule": rule, "event": event, "delta_status": status,
                    "policy_identity": identity, "params_fingerprint": params_fingerprint,
                    "message": "evaluated", "context": {},
                }
                if origin is not None:
                    row["origin_delta_id"] = origin
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(_json.dumps(row) + "\n")

            observation(root, identity={"layer": "project", "id": delta_id},
                        event="evaluation", origin=delta_id)
            observation(other, identity={"layer": "base", "id": f"base/{rule}"},
                        event="evaluation", origin=None)
            observation(other, identity={"layer": "project", "id": delta_id},
                        event="evaluation", origin=delta_id, status="suspended")
            observation(other, identity={"layer": "project", "id": "other/candidate"},
                        event="evaluation", origin="other/candidate")
            observation(other, identity={"layer": "project", "id": delta_id},
                        event="evaluation", origin=delta_id,
                        params_fingerprint="sha256:" + "0" * 64)
            candidate_fire = other / ".waystone" / "overlay" / "warnings.jsonl"
            with candidate_fire.open("a", encoding="utf-8") as stream:
                stream.write(_json.dumps({
                    "at": "2026-07-15T00:01:00+00:00", "boundary": "check",
                    "rule": rule, "event": "fire", "delta_status": "observing",
                    "policy_identity": {"layer": "project", "id": delta_id},
                    "origin_delta_id": delta_id, "params_fingerprint": fingerprint,
                    "message": "fired", "context": {},
                }) + "\n")
            with self.assertRaisesRegex(common.WorkflowError, "1 distinct project"):
                _run_with_home(home, lambda: overlay.promote_user(root, delta_id))

    def test_f3_materialization_ids_are_stable_unique_and_composition_rejects_duplicate_identity(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            ids = ("verification_debt/first", "verification_debt/second")
            for delta_id in ids:
                _run_with_home(home, lambda delta_id=delta_id: overlay.add_delta(
                    root, delta_id, rule="delegation-verification-evidence-v1",
                    summary="candidate"))
                delta = overlay.load_delta(root, delta_id)
                delta["replay"] = {"fires": 1, "opportunities": 1, "fire_rate": 1.0}
                overlay._write_delta(root, delta)
                _run_with_home(home, lambda delta_id=delta_id: overlay.materialize(
                    root, delta_id, consent_recorded=True))
            document = yaml.safe_load((root / "docs" / "waystone-policy.yaml").read_text())
            policy_ids = [row["id"] for row in document["policies"]]
            self.assertEqual(len(policy_ids), 2)
            self.assertEqual(len(set(policy_ids)), 2)
            composed = _run_with_home(home, lambda: overlay.compose_policy(root))
            identities = [
                (policy["identity"]["layer"], policy["identity"]["id"])
                for layer in composed["layers"] for policy in layer["policies"]
            ]
            self.assertEqual(len(identities), len(set(identities)))
            self.assertTrue(all(row["identity"] != row["shadowed_by"]
                                for row in composed["shadowed"]))
            project_policies = composed["layers"][2]["policies"]
            self.assertEqual({row["source_kind"] for row in project_policies},
                             {"overlay", "committed"})
            by_source = {
                source: {(row["identity"]["layer"], row["identity"]["id"])
                         for row in project_policies if row["source_kind"] == source}
                for source in ("overlay", "committed")
            }
            self.assertTrue(by_source["overlay"].isdisjoint(by_source["committed"]))
            warning_rows = {
                policy["source_kind"]: overlay._emit(
                    root, "test", policy, policy["rule"], policy["status"],
                    "evaluation", "attribution", {})
                for policy in project_policies
            }
            self.assertEqual(warning_rows["overlay"]["policy_source_kind"], "overlay")
            self.assertEqual(warning_rows["committed"]["policy_source_kind"], "committed")
            self.assertNotEqual(
                warning_rows["overlay"]["policy_identity"],
                warning_rows["committed"]["policy_identity"],
            )

            duplicate = overlay._strict_delta_directory(
                overlay._deltas_dir(root), layer="project", source_kind="overlay")[0]
            with mock.patch.object(overlay, "_load_project_policy", return_value=[duplicate]):
                with self.assertRaisesRegex(common.WorkflowError, "duplicate policy identity"):
                    _run_with_home(home, lambda: overlay.compose_policy(root))

    def test_f4_status_recovers_trusted_pr_contract_and_improve_projects_reviewer_identity(self):
        from unittest import mock
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n"
                "review:\n  mode: pr\n  reviewers: [macro-reviewer]\n"
                "  operators: [owner]\n  approvers: [owner]\n")
            target, base_sha = "a" * 40, "b" * 40
            marker = review.emit_marker("review-cycle", {
                "round_id": "2026-07-15-trusted", "cycle": 3,
                "target_sha": target, "base_sha": base_sha,
                "reviewers": ["macro-reviewer"],
                "profile_fingerprint": "sha256:profile",
                "rendered_request_digest": TEST_RENDERED_REQUEST_DIGEST,
            }, version=2)
            bundle = {
                "bodies": [{"body": marker, "author": "owner",
                            "at": "2026-07-15T00:00:00Z"}],
                "reviews": [], "head": target, "base_sha": base_sha,
                "state": "OPEN", "is_draft": False, "checks": [],
                "base": "main", "merge_state": "CLEAN",
            }
            policy = common.normalize_config(yaml.safe_load((root / ".waystone.yml").read_text()))
            context = {"repo": "owner/repo", "pr": 9, "bundle": bundle,
                       "head": target, "base_sha": base_sha, "base": "main",
                       "policy": policy}
            with mock.patch.object(review, "pr_context", return_value=context), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.status(root, 9), 0)
            sidecars = improve._round_review_sidecars(root / "docs" / "reviews")
            self.assertIn("2026-07-15-trusted", sidecars)
            rows = improve._project_review_rows("demo", root, policy)
            row = next(item for item in rows if item["round_id"] == "2026-07-15-trusted")
            self.assertEqual(row["review_cycle"], 3)
            self.assertEqual(row["reviewers"], ["macro-reviewer"])
            self.assertEqual(row["review_profile_fingerprint"], "sha256:profile")
            self.assertEqual(row["review_binding_provenance"], "explicit")
            self.assertEqual(row["rendered_request_digest"],
                             TEST_RENDERED_REQUEST_DIGEST)
            self.assertEqual(row["review_request_binding_provenance"], "unknown")
            self.assertEqual(row["review_request_binding_reason"],
                             "missing-pr-request-generation")

    def test_pr_improve_joins_freeze_with_v2_request_digest_provenance(self):
        from unittest import mock
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, round_id = _pr_prepared_round(base, "macro-reviewer")
            cfg = common.load_config(root)
            context = {
                "repo": "owner/repo", "pr": 9,
                "bundle": {"head": target, "base_sha": "b" * 40, "bodies": []},
                "head": target, "base_sha": "b" * 40, "base": "main",
                "policy": cfg,
            }
            with mock.patch.object(review, "pr_context", return_value=context), \
                    mock.patch.object(review, "_gh", return_value=(0, "ok")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.freeze(root, 9, round_id), 0)

            row = next(item for item in improve._project_review_rows("demo", root, cfg)
                       if item["round_id"] == round_id)
            request_binding = review.read_round_request_binding(next(
                (common.project_state_path(root) / "review-requests").glob(
                    f"{round_id}-request.binding*.json")))
            self.assertEqual(row["review_binding_provenance"], "explicit")
            self.assertEqual(row["review_binding_source"], "pr-freeze-sidecar")
            self.assertEqual(row["review_request_binding_provenance"], "explicit")
            self.assertEqual(row["review_binding_schema"],
                             review.ROUND_REQUEST_BINDING_SCHEMA)
            self.assertEqual(row["narrative_digest"],
                             request_binding["narrative_digest"])
            self.assertEqual(row["rendered_request_digest"],
                             request_binding["rendered_request_digest"])

    def test_pr_cycle_freeze_binds_exact_request_generation_after_reprepare(self):
        from unittest import mock
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, round_id = _pr_prepared_round(base, "macro-reviewer")
            cfg = common.load_config(root)
            request_dir = common.project_state_path(root) / "review-requests"
            first_path = next(request_dir.glob(f"{round_id}-request.binding*.json"))
            first = review.read_round_request_binding(first_path)
            posted = []
            context = {
                "repo": "owner/repo", "pr": 9,
                "bundle": {"head": target, "base_sha": "b" * 40, "bodies": []},
                "head": target, "base_sha": "b" * 40, "base": "main",
                "policy": cfg,
            }
            with mock.patch.object(review, "pr_context", return_value=context), \
                    mock.patch.object(
                        review, "_gh",
                        side_effect=lambda _root, *args: (posted.append(args) or (0, "ok"))), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.freeze(root, 9, round_id), 0)

            body = posted[0][posted[0].index("--body") + 1]
            marker = review.parse_markers(body, "review-cycle")[0]
            self.assertEqual(marker["_version"], 2)
            self.assertEqual(marker["rendered_request_digest"],
                             first["rendered_request_digest"])
            freeze_binding = review.read_pr_freeze_binding(next(
                (root / cfg["reviews_dir"]).glob(f"{round_id}-freeze-*.binding*.json")))
            self.assertEqual(freeze_binding["schema"], review.PR_FREEZE_BINDING_SCHEMA)
            self.assertEqual(freeze_binding["rendered_request_digest"],
                             first["rendered_request_digest"])

            narrative = base / "narrative.md"
            narrative.write_text(_PR_NARRATIVE.replace(
                "Freeze republishes only rendered bytes.",
                "Freeze binds the exact rendered generation."))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.prepare_review_request(root, round_id, narrative), 0)
            latest_path, latest = review.latest_round_request_binding(
                list(request_dir.glob(f"{round_id}-request.binding*.json")),
                expected_round_id=round_id)
            self.assertIsNotNone(latest_path)
            self.assertNotEqual(latest["rendered_request_digest"],
                                first["rendered_request_digest"])

            binding, reason = review.ingest_round_binding(root, round_id, cfg)
            self.assertIsNone(reason)
            self.assertEqual(binding["rendered_request_digest"],
                             first["rendered_request_digest"])
            self.assertEqual(Path(binding["request_binding_source"]), first_path)
            row = next(item for item in improve._project_review_rows("demo", root, cfg)
                       if item["round_id"] == round_id)
            self.assertEqual(row["rendered_request_digest"],
                             first["rendered_request_digest"])
            self.assertEqual(Path(row["review_request_binding_source"]), first_path)

    def test_status_recovers_marker_generation_and_missing_request_stays_unknown(self):
        from unittest import mock
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, round_id = _pr_prepared_round(base, "macro-reviewer")
            cfg = common.load_config(root)
            request_dir = common.project_state_path(root) / "review-requests"
            first_path = next(request_dir.glob(f"{round_id}-request.binding*.json"))
            first = review.read_round_request_binding(first_path)
            posted = []
            freeze_context = {
                "repo": "owner/repo", "pr": 9,
                "bundle": {"head": target, "base_sha": "b" * 40, "bodies": []},
                "head": target, "base_sha": "b" * 40, "base": "main",
                "policy": cfg,
            }
            with mock.patch.object(review, "pr_context", return_value=freeze_context), \
                    mock.patch.object(
                        review, "_gh",
                        side_effect=lambda _root, *args: (posted.append(args) or (0, "ok"))), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.freeze(root, 9, round_id), 0)
            body = posted[0][posted[0].index("--body") + 1]
            for path in (root / cfg["reviews_dir"]).glob(
                    f"{round_id}-freeze-*.binding*.json"):
                path.unlink()

            narrative = base / "narrative.md"
            narrative.write_text(_PR_NARRATIVE.replace(
                "Full suite green.", "Generation B is not part of cycle A."))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.prepare_review_request(root, round_id, narrative), 0)
            status_bundle = {
                "bodies": [{"body": body, "author": "owner",
                            "at": "2026-07-19T00:00:00Z"}],
                "reviews": [], "head": target, "base_sha": "b" * 40,
                "state": "OPEN", "is_draft": False, "checks": [],
                "base": "main", "merge_state": "CLEAN",
            }
            status_context = {**freeze_context, "bundle": status_bundle}
            with mock.patch.object(review, "pr_context", return_value=status_context), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.status(root, 9), 0)

            row = next(item for item in improve._project_review_rows("demo", root, cfg)
                       if item["round_id"] == round_id)
            self.assertEqual(row["rendered_request_digest"],
                             first["rendered_request_digest"])
            self.assertEqual(Path(row["review_request_binding_source"]), first_path)

            first_path.unlink()
            row = next(item for item in improve._project_review_rows("demo", root, cfg)
                       if item["round_id"] == round_id)
            self.assertEqual(row["rendered_request_digest"],
                             first["rendered_request_digest"])
            self.assertEqual(row["review_request_binding_provenance"], "unknown")
            self.assertEqual(row["review_request_binding_reason"],
                             "missing-pr-request-generation")
            binding, reason = review.ingest_round_binding(root, round_id, cfg)
            self.assertEqual(reason, "missing-pr-request-generation")
            self.assertEqual(binding["rendered_request_digest"],
                             first["rendered_request_digest"])
            self.assertEqual(binding["request_binding_provenance"], "unknown")
            self.assertIsNone(binding["narrative_digest"])

    def test_legacy_pr_freeze_does_not_adopt_latest_request_generation(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, round_id = _pr_prepared_round(base, "macro-reviewer")
            cfg = common.load_config(root)
            write_legacy_pr_freeze_binding(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"])

            binding, reason = review.ingest_round_binding(root, round_id, cfg)
            self.assertEqual(reason, "legacy-pre-digest")
            self.assertEqual(binding["request_binding_provenance"], "legacy-pre-digest")
            self.assertIsNone(binding.get("narrative_digest"))
            self.assertIsNone(binding.get("rendered_request_digest"))
            row = next(item for item in improve._project_review_rows("demo", root, cfg)
                       if item["round_id"] == round_id)
            self.assertEqual(row["review_request_binding_provenance"],
                             "legacy-pre-digest")
            self.assertEqual(row["review_request_binding_reason"], "legacy-pre-digest")
            self.assertIsNone(row["narrative_digest"])
            self.assertIsNone(row["rendered_request_digest"])

    def test_mixed_v1_v2_freeze_sidecars_report_skew_and_recover_v2(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, round_id = _pr_prepared_round(base, "macro-reviewer")
            cfg = common.load_config(root)
            request_path = next((common.project_state_path(root) / "review-requests").glob(
                f"{round_id}-request.binding*.json"))
            request = review.read_round_request_binding(request_path)
            v1_path = write_legacy_pr_freeze_binding(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"])
            v2_path = review.write_pr_freeze_binding(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"],
                None, cfg["reviews_dir"],
                rendered_request_digest=request["rendered_request_digest"])
            set_binding_timestamp(v1_path, "2026-07-19T00:00:00+00:00")
            set_binding_timestamp(v2_path, "2026-07-19T00:00:01+00:00")

            binding, reason = review.ingest_round_binding(root, round_id, cfg)
            self.assertEqual(reason, "pr-freeze-version-skew")
            self.assertTrue(binding["pr_freeze_version_skew"])
            self.assertEqual(binding["rendered_request_digest"],
                             request["rendered_request_digest"])
            row = next(item for item in improve._project_review_rows("demo", root, cfg)
                       if item["round_id"] == round_id)
            self.assertEqual(row["review_binding_reason"], "pr-freeze-version-skew")
            self.assertEqual(row["review_request_binding_provenance"], "explicit")

    def test_ingest_round_binding_later_v1_demotes_v2_generation(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, round_id = _pr_prepared_round(base, "macro-reviewer")
            cfg = common.load_config(root)
            request_path = next((common.project_state_path(root) / "review-requests").glob(
                f"{round_id}-request.binding*.json"))
            request = review.read_round_request_binding(request_path)
            v1_path = write_legacy_pr_freeze_binding(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"])
            v2_path = review.write_pr_freeze_binding(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"],
                None, cfg["reviews_dir"],
                rendered_request_digest=request["rendered_request_digest"])
            set_binding_timestamp(v2_path, "2026-07-19T00:00:00+00:00")
            set_binding_timestamp(v1_path, "2026-07-19T00:00:01+00:00")

            binding, reason = review.ingest_round_binding(root, round_id, cfg)
            self.assertEqual(reason, "latest-v1-supersedes-v2")
            self.assertTrue(binding["pr_freeze_version_skew"])
            self.assertEqual(binding["request_binding_provenance"], "unknown")
            self.assertEqual(binding["request_binding_reason"],
                             "latest-v1-supersedes-v2")
            self.assertIsNone(binding["narrative_digest"])
            self.assertIsNone(binding["rendered_request_digest"])

    def test_improve_review_binding_later_v1_demotes_v2_generation(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, round_id = _pr_prepared_round(base, "macro-reviewer")
            cfg = common.load_config(root)
            request_path = next((common.project_state_path(root) / "review-requests").glob(
                f"{round_id}-request.binding*.json"))
            request = review.read_round_request_binding(request_path)
            v1_path = write_legacy_pr_freeze_binding(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"])
            v2_path = review.write_pr_freeze_binding(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"],
                None, cfg["reviews_dir"],
                rendered_request_digest=request["rendered_request_digest"])
            set_binding_timestamp(v2_path, "2026-07-19T00:00:00+00:00")
            set_binding_timestamp(v1_path, "2026-07-19T00:00:01+00:00")

            sidecars = improve._round_review_sidecars(
                root / cfg["reviews_dir"])[round_id]
            row = improve._review_binding(None, round_id, "pr", sidecars)
            self.assertEqual(row["review_binding_reason"],
                             "latest-v1-supersedes-v2")
            self.assertTrue(row["review_cycle_version_skew"])
            self.assertEqual(row["review_request_binding_provenance"], "unknown")
            self.assertEqual(row["review_request_binding_reason"],
                             "latest-v1-supersedes-v2")
            self.assertIsNone(row["narrative_digest"])
            self.assertIsNone(row["rendered_request_digest"])

    def test_same_timestamp_v1_v2_freeze_tie_fails_closed_across_projections(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, round_id = _pr_prepared_round(base, "macro-reviewer")
            cfg = common.load_config(root)
            request_path = next((common.project_state_path(root) / "review-requests").glob(
                f"{round_id}-request.binding*.json"))
            request = review.read_round_request_binding(request_path)
            v1_path = write_legacy_pr_freeze_binding(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"])
            v2_path = review.write_pr_freeze_binding(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"],
                None, cfg["reviews_dir"],
                rendered_request_digest=request["rendered_request_digest"])
            set_binding_timestamp(v1_path, "2026-07-19T00:00:00Z")
            set_binding_timestamp(v2_path, "2026-07-19T00:00:00+00:00")

            binding, reason = review.ingest_round_binding(root, round_id, cfg)
            self.assertEqual(reason, "v1-v2-timestamp-tie")
            self.assertEqual(binding["request_binding_provenance"], "unknown")
            self.assertIsNone(binding["rendered_request_digest"])
            sidecars = improve._round_review_sidecars(
                root / cfg["reviews_dir"])[round_id]
            row = improve._review_binding(None, round_id, "pr", sidecars)
            self.assertEqual(row["review_binding_reason"], "v1-v2-timestamp-tie")
            self.assertEqual(row["review_request_binding_provenance"], "unknown")
            self.assertIsNone(row["rendered_request_digest"])

    def test_digest_era_pr_rejects_later_v1_only_cycle_even_with_fresh_evidence(self):
        head, base_sha = "a" * 40, "b" * 40
        bodies = [
            {"body": review.emit_marker("review-cycle", {
                "round_id": "2026-07-19-digest-era", "cycle": 1,
                "target_sha": head, "base_sha": base_sha,
                "reviewers": ["macro-reviewer"],
                "rendered_request_digest": TEST_RENDERED_REQUEST_DIGEST,
            }, version=2), "author": "owner", "at": "2026-07-19T00:00:00Z"},
            {"body": review.emit_marker("review-cycle", {
                "round_id": "(unset)", "cycle": 1, "target_sha": head,
                "base_sha": base_sha, "reviewers": ["macro-reviewer"],
            }), "author": "owner", "at": "2026-07-19T00:01:00Z"},
            {"body": review.emit_marker("review-cycle", {
                "round_id": "(unset)", "cycle": 2, "target_sha": head,
                "base_sha": base_sha, "reviewers": ["macro-reviewer"],
            }), "author": "owner", "at": "2026-07-19T00:02:00Z"},
            {"body": review.emit_marker("review-result", {
                "reviewer": "macro-reviewer", "review_cycle": 2,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": [],
            }), "author": "owner", "at": "2026-07-19T00:03:00Z"},
            {"body": review.emit_marker("findings", {
                "cycle": 2, "resolved": True,
            }), "author": "owner", "at": "2026-07-19T00:04:00Z"},
            {"body": review.emit_marker("approval", {
                "sha": head, "base_sha": base_sha, "cycle": 2, "by": "owner",
            }), "author": "owner", "at": "2026-07-19T00:05:00Z"},
        ]

        facts = review.classify(
            review.parse_bodies(bodies), head, macro_reviewers=("macro-reviewer",),
            approvers=("owner",), operators=("owner",), current_base=base_sha)
        self.assertEqual(facts["cycle_version_skew_reason"],
                         "latest-v1-supersedes-v2")
        self.assertTrue(facts["cycle_version_skew"])
        self.assertIsNone(facts["rendered_request_digest"])
        self.assertFalse(facts["cycle_fresh"])
        self.assertTrue(facts["pro_result_at_head"])
        self.assertTrue(facts["findings_resolved"])
        self.assertTrue(facts["approved_at_head"])
        gate = {**PASS, **facts, "want_codex": False}
        ok, failures = merge.merge_gate(gate)
        self.assertFalse(ok)
        self.assertTrue(any("v2 marker" in failure for failure in failures), failures)

    def test_pure_v1_only_pr_keeps_legacy_cycle_path(self):
        head, base_sha = "a" * 40, "b" * 40
        for round_id in ("2026-07-18-legacy", "(unset)", "legacy-round"):
            with self.subTest(round_id=round_id):
                markers = review.parse_bodies([
                    {"body": review.emit_marker("review-cycle", {
                        "round_id": round_id, "cycle": 1,
                        "target_sha": head, "base_sha": base_sha,
                    }), "author": "owner", "at": "2026-07-18T00:00:00Z"},
                    {"body": review.emit_marker("review-cycle", {
                        "round_id": round_id, "cycle": 2,
                        "target_sha": head, "base_sha": base_sha,
                    }), "author": "owner", "at": "2026-07-18T00:01:00Z"},
                ])

                facts = review.classify(
                    markers, head, operators=("owner",), current_base=base_sha)
                self.assertTrue(facts["cycle_fresh"])
                self.assertEqual(facts["cycle_marker_version"], 1)
                self.assertFalse(facts["cycle_version_skew"])
                self.assertIsNone(facts["cycle_version_skew_reason"])

    def test_roundless_freeze_in_new_worktree_cannot_mint_v1_gate_bypass(self):
        from unittest import mock
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target, base_sha = "a" * 40, "b" * 40
            other_pr_v2 = review.parse_markers(review.emit_marker("review-cycle", {
                "round_id": "2026-07-19-other-pr", "cycle": 1,
                "target_sha": "c" * 40, "base_sha": base_sha,
                "reviewers": ["macro-reviewer"],
                "rendered_request_digest": TEST_RENDERED_REQUEST_DIGEST,
            }, version=2), "review-cycle")[0]
            self.assertEqual(other_pr_v2["_version"], 2)
            self.assertEqual(list(root.rglob("*.binding*.json")), [])
            policy = common.normalize_config({
                "version": 1, "project": "demo", "reviews_dir": "docs/reviews",
                "review": {"mode": "pr", "reviewers": ["macro-reviewer"]},
            })
            context = {
                "repo": "owner/repo", "pr": 9,
                "bundle": {"head": target, "base_sha": base_sha, "bodies": []},
                "head": target, "base_sha": base_sha, "base": "main",
                "policy": policy,
            }
            posted = []
            err = io.StringIO()
            with mock.patch.object(review, "pr_context", return_value=context), \
                    mock.patch.object(
                        review, "_gh",
                        side_effect=lambda _root, *args: (posted.append(args) or (0, "ok"))), \
                    contextlib.redirect_stderr(err):
                rc = review.freeze(root, 9, None)
            if rc == 0:
                marker_body = posted[0][posted[0].index("--body") + 1]
                marker = review.parse_markers(marker_body, "review-cycle")[0]
                self.assertEqual(marker["_version"], 1)
                bodies = [
                    {"body": marker_body, "author": "owner",
                     "at": "2026-07-19T00:00:00Z"},
                    {"body": review.emit_marker("review-result", {
                        "reviewer": "macro-reviewer", "review_cycle": 1,
                        "reviewed_sha": target, "verdict": "shipped",
                        "decision_required": [],
                    }), "author": "owner", "at": "2026-07-19T00:01:00Z"},
                    {"body": review.emit_marker("approval", {
                        "sha": target, "base_sha": base_sha, "cycle": 1, "by": "owner",
                    }), "author": "owner", "at": "2026-07-19T00:02:00Z"},
                ]
                facts = review.classify(
                    review.parse_bodies(bodies), target,
                    macro_reviewers=("macro-reviewer",), approvers=("owner",),
                    operators=("owner",), current_base=base_sha)
                gate = {**PASS, **facts, "want_codex": False}
                self.assertTrue(merge.merge_gate(gate)[0], merge.merge_gate(gate)[1])
            self.assertEqual(
                rc, 1,
                "roundless freeze minted a v1 marker whose completed cycle passed merge gate")
            self.assertEqual(posted, [])
            self.assertIn("--round", err.getvalue())
            self.assertIn("prepare", err.getvalue())

    def test_digest_capable_freeze_without_round_fails_before_minting_v1(self):
        from unittest import mock
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, target, _round_id = _pr_prepared_round(Path(d), "macro-reviewer")
            cfg = common.load_config(root)
            context = {
                "repo": "owner/repo", "pr": 9,
                "bundle": {"head": target, "base_sha": "b" * 40, "bodies": []},
                "head": target, "base_sha": "b" * 40, "base": "main",
                "policy": cfg,
            }
            posted = []
            err = io.StringIO()
            with mock.patch.object(review, "pr_context", return_value=context), \
                    mock.patch.object(
                        review, "_gh",
                        side_effect=lambda _root, *args: (posted.append(args) or (0, "ok"))), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(review.freeze(root, 9, None), 1)
            self.assertEqual(posted, [])
            self.assertIn("--round", err.getvalue())
            self.assertIn("digest", err.getvalue().lower())

    def test_post_cutoff_v1_only_cycle_blocks_complete_fresh_pr(self):
        head, base_sha = "a" * 40, "b" * 40
        bodies = [
            {"body": review.emit_marker("review-cycle", {
                "round_id": "2026-07-19-old-host", "cycle": 1,
                "target_sha": head, "base_sha": base_sha,
                "reviewers": ["macro-reviewer"],
            }), "author": "owner", "at": "2026-07-19T00:00:00Z"},
            {"body": review.emit_marker("review-result", {
                "reviewer": "macro-reviewer", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": [],
            }), "author": "owner", "at": "2026-07-19T00:01:00Z"},
            {"body": review.emit_marker("approval", {
                "sha": head, "base_sha": base_sha, "cycle": 1, "by": "owner",
            }), "author": "owner", "at": "2026-07-19T00:02:00Z"},
        ]

        facts = review.classify(
            review.parse_bodies(bodies), head, macro_reviewers=("macro-reviewer",),
            approvers=("owner",), operators=("owner",), current_base=base_sha)
        self.assertEqual(facts["cycle_version_skew_reason"], "digest-era-v1-freeze")
        self.assertFalse(facts["cycle_fresh"])
        self.assertIsNone(facts["rendered_request_digest"])
        gate = {**PASS, **facts, "want_codex": False}
        ok, failures = merge.merge_gate(gate)
        self.assertFalse(ok)
        self.assertTrue(any("digest-era-v1-freeze" in failure for failure in failures), failures)

    def test_status_persists_mixed_host_v1_demotion_for_offline_projections(self):
        from unittest import mock
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, target, round_id = _pr_prepared_round(Path(d), "macro-reviewer")
            cfg = common.load_config(root)
            request_path = next((common.project_state_path(root) / "review-requests").glob(
                f"{round_id}-request.binding*.json"))
            request = review.read_round_request_binding(request_path)
            review.write_pr_freeze_binding(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"],
                None, cfg["reviews_dir"],
                rendered_request_digest=request["rendered_request_digest"])
            v2 = review.emit_marker("review-cycle", {
                "round_id": round_id, "cycle": 1, "target_sha": target,
                "base_sha": "b" * 40, "reviewers": ["macro-reviewer"],
                "rendered_request_digest": request["rendered_request_digest"],
            }, version=2)
            later_v1 = review.emit_marker("review-cycle", {
                "round_id": "(unset)", "cycle": 1, "target_sha": target,
                "base_sha": "b" * 40, "reviewers": ["macro-reviewer"],
            })
            bundle = {
                "bodies": [
                    {"body": v2, "author": "owner", "at": "2026-07-19T00:00:00Z"},
                    {"body": later_v1, "author": "owner", "at": "2026-07-19T00:01:00Z"},
                ],
                "reviews": [], "head": target, "base_sha": "b" * 40,
                "state": "OPEN", "is_draft": False, "checks": [],
                "base": "main", "merge_state": "CLEAN",
            }
            context = {"repo": "owner/repo", "pr": 9, "bundle": bundle,
                       "head": target, "base_sha": "b" * 40, "base": "main",
                       "policy": cfg}
            with mock.patch.object(review, "pr_context", return_value=context), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.status(root, 9), 0)

            demotions = list((root / cfg["reviews_dir"]).glob(
                f"{round_id}-freeze-1.demotion*.json"))
            self.assertEqual(len(demotions), 1)
            demotion = review.read_pr_freeze_demotion(
                demotions[0], expected_round_id=round_id, expected_cycle=1)
            self.assertEqual(demotion["reason"], "latest-v1-supersedes-v2")
            binding, reason = review.ingest_round_binding(root, round_id, cfg)
            self.assertEqual(reason, "latest-v1-supersedes-v2")
            self.assertEqual(binding["request_binding_provenance"], "unknown")
            self.assertIsNone(binding["rendered_request_digest"])
            sidecars = improve._round_review_sidecars(root / cfg["reviews_dir"])[round_id]
            row = improve._review_binding(None, round_id, "pr", sidecars)
            self.assertEqual(row["review_binding_reason"], "latest-v1-supersedes-v2")
            self.assertEqual(row["review_request_binding_provenance"], "unknown")
            self.assertIsNone(row["rendered_request_digest"])

    def test_pr_freeze_demotion_reader_enforces_filename_identity(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = review.write_pr_freeze_demotion(
                root, "2026-07-19-r1", 7, 2, "a" * 40, "b" * 40,
                ["macro-reviewer"], None, "docs/reviews",
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST,
                superseding_cycle=3,
                superseding_marker_at="2026-07-19T00:01:00Z")
            renamed = path.with_name(path.name.replace("freeze-2", "freeze-3"))
            path.rename(renamed)
            with self.assertRaisesRegex(common.WorkflowError, "does not match"):
                review.read_pr_freeze_demotion(renamed)

    def test_pr_freeze_demotion_write_is_atomic_and_preserves_immutable_sequences(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            directory = root / "docs/reviews"
            kwargs = {
                "rendered_request_digest": TEST_RENDERED_REQUEST_DIGEST,
                "superseding_cycle": 3,
                "superseding_marker_at": "2026-07-19T00:01:00Z",
            }
            with mock.patch.object(
                    common.os, "replace", side_effect=OSError("injected rename failure")):
                with self.assertRaisesRegex(OSError, "injected rename failure"):
                    review.write_pr_freeze_demotion(
                        root, "2026-07-19-r1", 7, 2, "a" * 40, "b" * 40,
                        ["macro-reviewer"], None, "docs/reviews", **kwargs)
            self.assertEqual(list(directory.iterdir()), [])

            first = review.write_pr_freeze_demotion(
                root, "2026-07-19-r1", 7, 2, "a" * 40, "b" * 40,
                ["macro-reviewer"], None, "docs/reviews", **kwargs)
            first_bytes = first.read_bytes()
            duplicate = review.write_pr_freeze_demotion(
                root, "2026-07-19-r1", 7, 2, "a" * 40, "b" * 40,
                ["macro-reviewer"], None, "docs/reviews", **kwargs)
            self.assertEqual(duplicate, first)
            second = review.write_pr_freeze_demotion(
                root, "2026-07-19-r1", 7, 2, "a" * 40, "b" * 40,
                ["macro-reviewer"], None, "docs/reviews",
                **{**kwargs, "superseding_cycle": 4})
            self.assertEqual(second.name, "2026-07-19-r1-freeze-2.demotion-2.json")
            self.assertEqual(first.read_bytes(), first_bytes)
            self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_corrupt_pr_freeze_demotion_is_quarantined_as_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            root, target, round_id = _pr_prepared_round(Path(d), "macro-reviewer")
            cfg = common.load_config(root)
            request_path = next((common.project_state_path(root) / "review-requests").glob(
                f"{round_id}-request.binding*.json"))
            request = review.read_round_request_binding(request_path)
            review.write_pr_freeze_binding(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"],
                None, cfg["reviews_dir"],
                rendered_request_digest=request["rendered_request_digest"])
            demotion = review.write_pr_freeze_demotion(
                root, round_id, 9, 1, target, "b" * 40, ["macro-reviewer"],
                None, cfg["reviews_dir"],
                rendered_request_digest=request["rendered_request_digest"],
                superseding_cycle=1,
                superseding_marker_at="2026-07-19T00:01:00Z")
            demotion.write_text("{corrupt")

            binding, reason = review.ingest_round_binding(root, round_id, cfg)
            self.assertIsNone(binding)
            self.assertEqual(reason, "corrupt-round-binding:WorkflowError")
            sidecars = improve._round_review_sidecars(root / cfg["reviews_dir"])[round_id]
            row = improve._review_binding(None, round_id, "pr", sidecars)
            self.assertIsNone(row["rendered_request_digest"])
            self.assertEqual(row["review_binding_provenance"], "unknown")
            self.assertEqual(row["review_binding_reason"],
                             "corrupt-pr-freeze-demotion-sidecar")

    def test_cross_round_freeze_prefix_collisions_are_excluded_across_projections(self):
        for kind in ("freeze", "demotion"):
            for foreign_corrupt in (False, True):
                with self.subTest(kind=kind, foreign_corrupt=foreign_corrupt), \
                        tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    (root / ".waystone.yml").write_text(
                        "version: 1\nproject: demo\nreviews_dir: docs/reviews\n"
                        "review:\n  mode: pr\n")
                    round_id = "2026-07-19-a"
                    foreign_round_id = f"{round_id}-freeze-b"
                    target, foreign_target = "a" * 40, "c" * 40
                    review.write_pr_freeze_binding(
                        root, round_id, 7, 1, target, "b" * 40,
                        ["macro-reviewer"], None, "docs/reviews",
                        rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
                    if kind == "freeze":
                        foreign = review.write_pr_freeze_binding(
                            root, foreign_round_id, 8, 3, foreign_target, "d" * 40,
                            ["macro-reviewer"], None, "docs/reviews",
                            rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
                    else:
                        foreign = review.write_pr_freeze_demotion(
                            root, foreign_round_id, 8, 3, foreign_target, "d" * 40,
                            ["macro-reviewer"], None, "docs/reviews",
                            rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST,
                            superseding_cycle=3,
                            superseding_marker_at="2026-07-19T00:01:00Z")
                    if foreign_corrupt:
                        foreign.write_text("{corrupt")

                    sidecars = improve._round_review_sidecars(root / "docs/reviews")
                    projected = improve._review_binding(
                        None, round_id, "pr", sidecars[round_id])
                    self.assertEqual(projected["target_sha"], target)
                    self.assertEqual(projected["review_cycle"], 1)
                    self.assertEqual(projected["review_binding_provenance"], "explicit")
                    self.assertNotIn(
                        foreign, [Path(row["_file"]) for row in sidecars[round_id]])

                    binding, reason = review.ingest_round_binding(
                        root, round_id, common.load_config(root))
                    self.assertIsNotNone(binding, reason)
                    self.assertEqual(binding["target_sha"], projected["target_sha"])
                    self.assertEqual(binding["cycle"], projected["review_cycle"])
                    self.assertEqual(
                        binding["request_binding_provenance"],
                        projected["review_request_binding_provenance"])
                    self.assertEqual(reason, projected["review_request_binding_reason"])

    def test_corrupt_latest_pr_freeze_sidecar_blocks_stale_cycle_across_projections(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n"
                "review:\n  mode: pr\n")
            round_id = "2026-07-19-corrupt-latest-freeze"
            review.write_pr_freeze_binding(
                root, round_id, 7, 1, "a" * 40, "b" * 40,
                ["macro-reviewer"], None, "docs/reviews",
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
            corrupt = (root / "docs/reviews" /
                       f"{round_id}-freeze-2.binding.json")
            corrupt.write_text('{"schema":')

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                sidecars = improve._round_review_sidecars(
                    root / "docs/reviews")[round_id]
            projected = improve._review_binding(None, round_id, "pr", sidecars)

            self.assertIsNone(projected["target_sha"])
            self.assertEqual(projected["review_binding_provenance"], "unknown")
            self.assertEqual(projected["review_binding_reason"],
                             "corrupt-pr-freeze-sidecar")
            sentinels = [
                row for row in sidecars
                if row.get("_binding_error") == "corrupt-pr-freeze-sidecar"
            ]
            self.assertEqual(len(sentinels), 1)
            self.assertEqual(sentinels[0]["round_id"], round_id)
            self.assertEqual(sentinels[0]["cycle"], 2)
            self.assertEqual(sentinels[0]["_binding_kind"], "freeze")
            self.assertIn(str(corrupt), err.getvalue())
            self.assertIn("quarantined as unknown", err.getvalue())

            binding, reason = review.ingest_round_binding(
                root, round_id, common.load_config(root))
            self.assertIsNone(binding)
            self.assertEqual(reason, "corrupt-round-binding:WorkflowError")

    def test_pr_freeze_binding_reader_enforces_filename_round_and_cycle_identity(self):
        for field, value in (
                ("round_id", "2026-07-19-other-round"), ("cycle", 3)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                path = review.write_pr_freeze_binding(
                    root, "2026-07-19-r1", 7, 2, "a" * 40, "b" * 40,
                    ["macro-reviewer"], None, "docs/reviews",
                    rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
                row = _json.loads(path.read_text())
                row[field] = value
                path.write_text(_json.dumps(row) + "\n")

                with self.assertRaisesRegex(common.WorkflowError, "filename identity"):
                    review.read_pr_freeze_binding(path)

    def test_unparseable_pr_freeze_filename_quarantines_only_its_round(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n"
                "review:\n  mode: pr\n")
            damaged_round = "2026-07-19-invalid-freeze-name"
            healthy_round = "2026-07-19-healthy-freeze"
            for round_id, cycle, target in (
                    (damaged_round, 1, "a" * 40),
                    (healthy_round, 3, "c" * 40)):
                review.write_pr_freeze_binding(
                    root, round_id, 7, cycle, target, "b" * 40,
                    ["macro-reviewer"], None, "docs/reviews",
                    rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
            invalid = (root / "docs/reviews" /
                       f"{damaged_round}-freeze-latest.binding.json")
            invalid.write_text("{corrupt")

            sidecars = improve._round_review_sidecars(root / "docs/reviews")
            damaged = improve._review_binding(
                None, damaged_round, "pr", sidecars[damaged_round])
            healthy = improve._review_binding(
                None, healthy_round, "pr", sidecars[healthy_round])

            self.assertIsNone(damaged["target_sha"])
            self.assertEqual(damaged["review_binding_reason"],
                             "corrupt-pr-freeze-sidecar")
            sentinel = next(
                row for row in sidecars[damaged_round]
                if row.get("_binding_error") == "corrupt-pr-freeze-sidecar")
            self.assertIsNone(sentinel["cycle"])
            self.assertEqual(sentinel["_file"], str(invalid))
            self.assertEqual(healthy["target_sha"], "c" * 40)
            self.assertEqual(healthy["review_cycle"], 3)
            self.assertEqual(healthy["review_binding_provenance"], "explicit")

            binding, reason = review.ingest_round_binding(
                root, damaged_round, common.load_config(root))
            self.assertIsNone(binding)
            self.assertEqual(reason, "corrupt-round-binding:WorkflowError")

    def test_older_corrupt_pr_freeze_does_not_override_newer_valid_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n"
                "review:\n  mode: pr\n")
            round_id = "2026-07-19-newer-valid-freeze"
            directory = root / "docs/reviews"
            directory.mkdir(parents=True)
            (directory / f"{round_id}-freeze-1.binding.json").write_text("{corrupt")
            review.write_pr_freeze_binding(
                root, round_id, 7, 2, "c" * 40, "b" * 40,
                ["macro-reviewer"], None, "docs/reviews",
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)

            sidecars = improve._round_review_sidecars(directory)[round_id]
            projected = improve._review_binding(None, round_id, "pr", sidecars)
            self.assertEqual(projected["target_sha"], "c" * 40)
            self.assertEqual(projected["review_cycle"], 2)
            self.assertEqual(projected["review_binding_provenance"], "explicit")

            binding, reason = review.ingest_round_binding(
                root, round_id, common.load_config(root))
            self.assertEqual(binding["target_sha"], "c" * 40)
            self.assertEqual(binding["cycle"], 2)
            self.assertEqual(reason, "missing-pr-request-generation")

    def test_unparseable_mixed_version_timestamp_fails_closed(self):
        head = "a" * 40
        rows = [
            {"body": review.emit_marker("review-cycle", {
                "cycle": 1, "target_sha": head,
            }, version=1), "author": "owner", "at": "not-a-time"},
            {"body": review.emit_marker("review-cycle", {
                "cycle": 1, "target_sha": head,
                "rendered_request_digest": TEST_RENDERED_REQUEST_DIGEST,
            }, version=2), "author": "owner", "at": "2026-07-19T00:00:00Z"},
        ]

        facts = review.classify(review.parse_bodies(rows), head, operators=("owner",))
        self.assertTrue(facts["cycle_conflict"])
        self.assertEqual(facts["cycle_version_skew_reason"], "v1-v2-timestamp-tie")
        self.assertFalse(facts["cycle_fresh"])

    def test_conflicting_same_cycle_pr_freeze_digests_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n"
                "review:\n  mode: pr\n")
            round_id = "2026-07-19-digest-conflict"
            common.ensure_project_state_dir(root)
            for digest in ("sha256:" + "a" * 64, "sha256:" + "b" * 64):
                review.write_pr_freeze_binding(
                    root, round_id, 7, 2, "a" * 40, "b" * 40,
                    ["codex:test"], "sha256:abc", "docs/reviews",
                    rendered_request_digest=digest)

            binding, reason = review.ingest_round_binding(
                root, round_id, common.load_config(root))
            self.assertIsNone(binding)
            self.assertEqual(reason, "conflicting-pr-freeze-sidecars")
            sidecars = improve._round_review_sidecars(
                root / "docs" / "reviews")[round_id]
            projected = improve._review_binding(None, round_id, "pr", sidecars)
            self.assertIsNone(projected["target_sha"])
            self.assertEqual(projected["review_binding_reason"],
                             "conflicting-pr-freeze-sidecars")

    def test_conflicting_same_cycle_pr_freeze_sidecars_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
            review.write_pr_freeze_binding(
                root, "2026-07-15-r", 7, 2, "a" * 40, "b" * 40,
                ["codex:test"], "sha256:abc", "docs/reviews",
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
            sidecars = improve._round_review_sidecars(root / "docs" / "reviews")["2026-07-15-r"]
            binding = improve._review_binding(None, "2026-07-15-r", "pr", sidecars)
            self.assertEqual(binding["target_sha"], "a" * 40)
            self.assertEqual(binding["review_binding_provenance"], "explicit")
            self.assertEqual(binding["review_binding_source"], "pr-freeze-sidecar")
            conflict = {**sidecars[0], "target_sha": "c" * 40, "_file": "conflict.json"}
            conflicted = improve._review_binding(
                None, "2026-07-15-r", "pr", [*sidecars, conflict])
            self.assertIsNone(conflicted["target_sha"])
            self.assertEqual(
                conflicted["review_binding_reason"], "conflicting-pr-freeze-sidecars")
            self.assertEqual(conflicted["review_binding_provenance"], "unknown")

    def test_f5_apply_judgment_is_unresolved_until_applied_and_acceptance_uses_applied_transition(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            verdict = _json.loads((rec / "artifact" / "verdict-1.json").read_text())
            self.assertIn("judged_at", verdict)
            self.assertNotIn("at", verdict)
            index, errors = overlay._delegation_evidence_index(root)
            self.assertEqual(errors, 0)
            self.assertIs(index["feat/xyz"][0]["positive"], False)
            self.assertEqual(index["feat/xyz"][0]["evidence_kind"],
                             "unresolved-apply-judgment")
            (root / "impl.py").write_text("live conflict\n")
            with self.assertRaisesRegex(common.WorkflowError, "live tree has drifted"):
                _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            index, errors = overlay._delegation_evidence_index(root)
            self.assertEqual(errors, 0)
            self.assertIs(index["feat/xyz"][0]["positive"], False)
            (root / "impl.py").unlink()
            _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            status = delegate._read_status(rec)
            self.assertEqual(status["state"], "applied")
            self.assertIsNotNone(common.parse_iso_timestamp(status["accepted_at"]))
            index, errors = overlay._delegation_evidence_index(root)
            self.assertEqual(errors, 0)
            self.assertIs(index["feat/xyz"][0]["positive"], True)

    def test_f5_discard_judgment_never_overrides_direct_round_close_acceptance(self):
        task = {"status": "done", "round": "r1"}
        delegations = [{"did": "d1", "acceptance": {
            "event": "delegation-verdict", "judged_at": "2026-07-15T01:00:00Z",
            "decision": "discard", "resolved": False, "provenance": "explicit",
        }}]
        exposures = {"r1": {
            "round_id": "r1", "at": "2026-07-15T02:00:00Z", "_file": "round-r1.json",
        }}
        acceptance = improve._task_acceptance(task, delegations, exposures)
        self.assertEqual(acceptance["event"], "round-close")
        self.assertEqual(acceptance["accepted_at"], "2026-07-15T02:00:00Z")

    def test_fresh_missing_reviews_and_decisions_is_bootstrap_not_degraded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            out = root / ".waystone" / "improve"
            out.mkdir(parents=True)
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            _write_jsonl(out / "sessions.jsonl", [])
            _write_jsonl(out / "delegations.jsonl", [])
            (out / "parse_coverage.json").write_text(_json.dumps({"row_totals": {}}))
            facts = improve.run_audit(
                out, improve.PROJECT_LENS_SCOPE, project_root=root)
            self.assertEqual(facts["maturity"]["stage"], "bootstrap")
            self.assertIs(facts["maturity"]["degraded"], False)

    def test_committed_policy_candidate_is_sanitized_and_mapping_stays_local(self):
        candidate = overlay._materialized_candidate(Path("/tmp/project"), {
            "id": "verification_debt/local-name", "title": "/tmp/project secret",
            "rule": "delegation-verification-evidence-v1", "status": "observing", "params": {},
        })
        self.assertNotIn("origin_delta_id", candidate)
        self.assertRegex(candidate["id"], r"^delegation-verification-evidence-[0-9a-f]{12}$")
        self.assertEqual(candidate, overlay._materialized_candidate(Path("/tmp/project"), {
            "id": "verification_debt/local-name", "title": "changed display text",
            "rule": "delegation-verification-evidence-v1", "status": "observing", "params": {},
        }))
        self.assertNotIn("verification_debt/local-name", _json.dumps(candidate))
        self.assertEqual(candidate["summary"],
                         "Project policy for delegation verification evidence.")

    def test_init_consent_fields_are_validated_and_documented(self):
        cfg = common.normalize_config({"version": 1, "project": "demo"})
        self.assertEqual(cfg["policy"]["start_level"], "warn-allowed")
        self.assertIs(cfg["delegation"]["enabled"], True)
        with self.assertRaisesRegex(ValueError, "start_level"):
            common.normalize_config({"policy": {"start_level": "enforce"}})
        with self.assertRaisesRegex(ValueError, "delegation.enabled"):
            common.normalize_config({"delegation": {"enabled": "yes"}})
        skill = (SCRIPTS.parent / "skills" / "init" / "SKILL.md").read_text()
        for text in (
            "observe-only", "warn-allowed", "delegation.enabled",
            "init.start-level", "init.delegation",
        ):
            self.assertIn(text, skill)

    def test_install_skill_previews_target_effect_rollback_before_consent(self):
        skill = (SCRIPTS.parent / "skills" / "init" / "SKILL.md").read_text()
        step = skill.split("## Step 8.5", 1)[1].split("## Step 9", 1)[0]
        for phrase in ("target path", "effect", "rollback", "delete"):
            self.assertIn(phrase, step.lower())
        self.assertLess(step.index("target path"), step.index("consent record"))
        self.assertIn(".waystone/boundary-hooks-enabled", step)
        self.assertIn("both Claude Code and Codex", step)
        self.assertIn("have been shared since v0.9", skill)

    def test_direct_delegable_work_signal_and_high_risk_single_skip(self):
        sessions = [{
            "project": "demo", "kind": "main", "session_id": "s1",
            "tools": {"by_category": {"file_write": 1, "shell": 0}},
            "retry_loops": {"count": 0}, "context_heavy": {"max_result_bytes": 0},
            "usage": {"input": 1},
        }]
        evidence = [{
            "project": "demo", "task_id": "feat/direct", "delegations": [],
            "task_context": {"session_id": "s1", "acceptance_criteria": 1,
                             "declared_scope_count": 1},
        }]
        lens = improve._lens_delegation_opportunity(sessions, evidence)
        candidate = lens["_projection_rows"][0]
        self.assertIn("delegable-direct-work", candidate["triggered_by"])
        rounds = [{
            "schema": "waystone-round-exposure-1", "round_id": "r1",
            "at": "2026-07-15T00:00:00Z", "review_mode": "packet",
            "round_evidence": {"changed_files": [f"f{i}" for i in range(20)],
                               "open_blocker_task_ids": []},
        }]
        result = overlay.evaluate_review_skipped_closes(
            rounds, [], consecutive=2, diff_files_threshold=20, open_blocker_threshold=1)
        self.assertEqual(result["fires"], ["r1"])
        self.assertEqual(result["by_round"][0]["risk_reason"], "diff-files-threshold")

    def test_remaining_section15_metrics_are_actual_and_longitudinal(self):
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            _write_jsonl(out / "sessions.jsonl", [{
                "project": "demo", "kind": "main", "session_id": "s1",
                "tools": {"by_category": {"file_write": 2, "shell": 3}},
                "context_heavy": {"max_result_bytes": 2048, "tool_results_over_100kb": 0},
                "usage": {"input": 100, "output": 10, "cache_read": 0, "cache_creation": 0},
            }])
            _write_jsonl(out / "delegations.jsonl", [])
            _write_jsonl(out / "reviews.jsonl", [
                {"project": "demo", "round_id": "r1", "findings": [
                    {"id": "f1", "status": "REAL", "type": "verification", "severity": "major"}]},
                {"project": "demo", "round_id": "r2", "findings": [
                    {"id": "f2", "status": "REAL", "type": "verification", "severity": "major"}]},
            ])
            _write_jsonl(out / "evidence.jsonl", [{
                "project": "demo", "task_id": "feat/x", "findings": [],
                "delegations": [{
                    "did": "d1", "state": "applied", "verification_runs": [
                        {"number": 1, "judgment_set_hash": "same", "findings": 1},
                        {"number": 2, "judgment_set_hash": "same", "findings": 1},
                    ],
                }],
            }, {"coverage": {"warning_observations": [{
                "project": "demo", "records": 0, "fire": 0, "conflict": 0,
                "by_rule": {}, "by_boundary": {}, "by_rule_boundary": {},
                "recent_rounds": [],
                "coverage": {"warnings_file_present": True},
            }]}}])
            _write_jsonl(out / "evidence_warnings.jsonl", [
                {"project": "demo", "event": "fire", "rule": "done-without-evidence-v1",
                 "policy_identity": {"layer": "project", "id": "p1"}, "context": {}},
                {"project": "demo", "event": "fire", "rule": "done-without-evidence-v1",
                 "policy_identity": {"layer": "project", "id": "p1"}, "context": {}},
            ])
            (out / "adaptive_feedback.json").write_text(_json.dumps([{
                "project": "demo", "facts": {"deltas": [
                    {"identity": {"layer": "project", "id": "p1"}, "status": "observing"},
                    {"identity": {"layer": "project", "id": "p2"}, "status": "retired"},
                ], "coverage": {"accept_delta_conflicts": {}}},
            }]))
            improve.run_audit(out, improve.PROJECT_LENS_SCOPE)
            snap = improve.run_metrics(
                out, improve.PROJECT_LENS_SCOPE,
                now=datetime(2026, 7, 15, tzinfo=timezone.utc))
            self.assertEqual(snap["metrics"]["quality"]["severe_finding_recurrence_rate"]["value"], .5)
            self.assertEqual(snap["metrics"]["quality"]["verification_finding_trend"]["value"],
                             {"first": 1, "last": 1, "delta": 0})
            main_direct = snap["metrics"]["delegation_effectiveness"]["main_direct_work"]
            self.assertIsNone(main_direct["value"])
            self.assertEqual(main_direct["unavailable_reason"], "lens-not-computed")
            self.assertEqual(snap["metrics"]["delegation_effectiveness"]["main_context_inflow"]["value"], 100)
            self.assertEqual(snap["metrics"]["governance"]["repeated_warning_exposure_count"]["value"], 1)
            self.assertEqual(snap["metrics"]["governance"]["retained_delta_count"]["value"], 1)
            self.assertEqual(snap["metrics"]["reproducibility_environment"]
                             ["acceptance_reproducibility"]["value"], 1.0)
