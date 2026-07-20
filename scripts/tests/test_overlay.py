"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


class OverlayStoreTests(unittest.TestCase):
    """0.8.0 M2 §3 — delta store, id grammar, lifecycle transitions, corrupt handling."""

    def test_add_creates_observing_delta(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            delta = _add_delta(root, home)
            self.assertEqual(delta["status"], "observing")
            self.assertEqual(delta["schema"], "waystone-delta-1")
            self.assertEqual(delta["candidate_scope"], "unresolved")
            self.assertEqual(delta["evidence"]["source"], "manual")
            self.assertIsNone(delta["evidence"]["rec_id"])
            self.assertEqual(delta["evidence"]["summary"], "observed 3/5 delegations without verification")
            # proposed -> observing recorded as a transition (add IS the acceptance)
            self.assertEqual([t["to"] for t in delta["transitions"]], ["observing"])
            self.assertEqual(delta["observed_in"], [])
            # persisted, slash -> double-dash filename
            p = _run_with_home(home, lambda: overlay._delta_path(root, "verification_debt/skip"))
            self.assertTrue(p.exists())
            self.assertEqual(p.name, "verification_debt--skip.json")

    def test_add_from_rec_sets_provenance(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            decisions = root / ".waystone" / "improve" / "decisions.jsonl"
            decisions.parent.mkdir(parents=True)
            _write_jsonl(decisions, [{
                "rec_id": "verification_debt/heavy-solo", "decision": "accept",
                "at": "2026-07-15T00:00:00+00:00",
            }])
            delta = _add_delta(root, home, from_rec="verification_debt/heavy-solo",
                               pointers=["a.py:1", "b.py:2"],
                               candidate_scope="project_candidate")
            self.assertEqual(delta["evidence"]["source"], "improve-rec")
            self.assertEqual(delta["evidence"]["rec_id"], "verification_debt/heavy-solo")
            self.assertEqual(delta["evidence"]["pointers"], ["a.py:1", "b.py:2"])
            self.assertEqual(delta["candidate_scope"], "project_candidate")
            self.assertEqual(delta["observed_in"], [])

    def test_add_invalid_delta_id_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            with self.assertRaises(delegate.WorkflowError):
                _add_delta(root, home, delta_id="Bad Id/Nope")

    def test_add_unknown_rule_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            with self.assertRaises(delegate.WorkflowError):
                _add_delta(root, home, rule="nonexistent-rule-v9")

    def test_add_missing_flags_exit1(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                # missing --summary
                self.assertEqual(_run_with_home(home, lambda: overlay.main(
                    ["add", "verification_debt/x", "--rule", "delegation-verification-evidence-v1",
                     "--root", str(root)])), 1)
                # missing --rule
                self.assertEqual(_run_with_home(home, lambda: overlay.main(
                    ["add", "verification_debt/x", "--summary", "s", "--root", str(root)])), 1)

    def test_add_bad_candidate_scope_exit1(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_run_with_home(home, lambda: overlay.main(
                    ["add", "verification_debt/x", "--rule", "delegation-verification-evidence-v1",
                     "--summary", "s", "--candidate-scope", "bogus", "--root", str(root)])), 1)

    def test_promote_requires_replay(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            _add_delta(root, home)
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: overlay.promote(root, "verification_debt/skip"))
            self.assertIn("replay", str(cm.exception))
            # inject a replay result then promote succeeds observing -> warning
            p = _run_with_home(home, lambda: overlay._delta_path(root, "verification_debt/skip"))
            import json as _j
            delta = _j.loads(p.read_text())
            delta["replay"] = {"fires": 2, "opportunities": 5, "replayed_at": "2026-07-14T00:00:00+00:00"}
            p.write_text(_j.dumps(delta))
            out = _run_with_home(home, lambda: overlay.promote(root, "verification_debt/skip"))
            self.assertEqual(out["status"], "warning")

    def test_promote_observe_only_warns_that_runtime_emission_is_suppressed(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\npolicy:\n  start_level: observe-only\n")
            _add_delta(root, home)
            path = _run_with_home(
                home, lambda: overlay._delta_path(root, "verification_debt/skip"))
            delta = _json.loads(path.read_text())
            delta["replay"] = {"fires": 2, "opportunities": 5,
                               "replayed_at": "2026-07-14T00:00:00+00:00"}
            path.write_text(_json.dumps(delta))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                promoted = _run_with_home(
                    home, lambda: overlay.promote(root, "verification_debt/skip"))
            self.assertEqual(promoted["status"], "warning")
            self.assertIn("start_level", err.getvalue())
            self.assertIn("suppressed", err.getvalue())

    def test_demote_warning_to_observing(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            _add_delta(root, home)
            p = _run_with_home(home, lambda: overlay._delta_path(root, "verification_debt/skip"))
            import json as _j
            delta = _j.loads(p.read_text())
            delta["status"] = "warning"
            p.write_text(_j.dumps(delta))
            out = _run_with_home(home, lambda: overlay.demote(root, "verification_debt/skip"))
            self.assertEqual(out["status"], "observing")

    def test_suspend_and_retire_unconditional_and_terminal(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            _add_delta(root, home)
            out = _run_with_home(home, lambda: overlay.suspend(root, "verification_debt/skip", note="pause"))
            self.assertEqual(out["status"], "suspended")
            # retire from suspended is fine (#9 — teardown always open)
            out = _run_with_home(home, lambda: overlay.retire(root, "verification_debt/skip"))
            self.assertEqual(out["status"], "retired")
            # retired is terminal — any further transition refused
            for verb in (overlay.promote, overlay.demote, overlay.suspend, overlay.retire):
                with self.assertRaises(delegate.WorkflowError):
                    _run_with_home(home, lambda v=verb: v(root, "verification_debt/skip"))

    def test_add_leaves_no_tmp_file(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            _add_delta(root, home)
            ddir = _run_with_home(home, lambda: overlay._deltas_dir(root))
            self.assertEqual(sorted(p.name for p in ddir.iterdir()), ["verification_debt--skip.json"])

    def test_corrupt_delta_marked_in_list_strict_in_show(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            _add_delta(root, home)
            _add_delta(root, home, delta_id="verification_debt/other")
            ddir = _run_with_home(home, lambda: overlay._deltas_dir(root))
            (ddir / "verification_debt--skip.json").write_text("{ corrupt")
            listed = _run_with_home(home, lambda: overlay.list_deltas(root))
            corrupt = [x for x in listed if x.get("corrupt")]
            healthy = [x for x in listed if not x.get("corrupt")]
            self.assertEqual(len(corrupt), 1)
            self.assertTrue(any(h["id"] == "verification_debt/other" for h in healthy))
            # single-record path fails loud, naming the file
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: overlay.load_delta(root, "verification_debt/skip"))
            self.assertIn("verification_debt--skip.json", str(cm.exception))

    def test_unknown_delta_id_exit1(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            with self.assertRaises(delegate.WorkflowError):
                _run_with_home(home, lambda: overlay.load_delta(root, "verification_debt/nope"))

    def test_waystone_dispatcher_routes_overlay(self):
        import waystone
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: waystone.main(["overlay", "list", "--root", str(root)]))
            self.assertEqual(rc, 0)


class OverlayRuleTests(unittest.TestCase):
    """0.8.0 M2 §4 — rule vocabulary v1 fire predicates (both status axes pinned, R3)."""

    def test_rule1_fire_predicate(self):
        # present True + non-empty verification -> no fire
        self.assertFalse(overlay.rule1_fires(
            {"delegate_report": {"present": True, "verification": [{"cmd": "pytest", "rc": 0}]}}))
        # present True but empty verification -> fire
        self.assertTrue(overlay.rule1_fires(
            {"delegate_report": {"present": True, "verification": []}}))
        # report absent -> fire
        self.assertTrue(overlay.rule1_fires({"delegate_report": {"present": False}}))
        # invalid report -> fire
        self.assertTrue(overlay.rule1_fires({"delegate_report": {"present": "invalid"}}))

    def test_rule2_open_severe_fires_excludes_done_rejected_minor(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _rule2_project(d)
            cfg = common.load_config(root)
            out = overlay.evaluate_rule2(root, cfg, ["blocker", "major"])
            fired_ids = sorted(f["task_id"] for f in out["fires"])
            # fix/finding-a fires (open blocker, REAL); b is done; c is REJECTED; d is minor
            self.assertEqual(fired_ids, ["fix/finding-a"])
            # JW-GPT-004 has no linked task -> unlinked, not a fire
            self.assertEqual(out["unlinked"], 1)

    def test_rule2_closing_done_override_suppresses_fire(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _rule2_project(d)
            cfg = common.load_config(root)
            out = overlay.evaluate_rule2(root, cfg, ["blocker", "major"],
                                            closing_done={"fix/finding-a"})
            self.assertEqual(out["fires"], [])  # a is being closed in this round


class BoundaryWarnTests(unittest.TestCase):
    """0.8.0 M2 §6 — boundary warn engine: observing logs silently, warning also stderr, host exit
    never changes, engine exceptions never propagate, warnings.jsonl row schema, check pin (R4)."""

    def _deleg_needs_review(self, d, report=None):
        root, home = _deleg_project(d)
        _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}, report=report))
        return root, home, _latest_rec(root, home).name

    def test_delegate_run_observing_logs_no_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, did = self._deleg_needs_review(d, report=None)  # no verification -> fires
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/skip", rule="delegation-verification-evidence-v1", summary="s"))
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                events = _run_with_home(home, lambda: overlay.evaluate_boundary(
                    root, "delegate-run", {"delegation_id": did}))
            fires = [e for e in events if e["event"] == "fire"]
            self.assertEqual(len(fires), 1)
            self.assertEqual(fires[0]["delta_status"], "observing")
            self.assertEqual(err.getvalue(), "")  # observing suppresses stderr
            rows = _read_warnings(root, home)
            self.assertTrue(any(r["event"] == "fire" for r in rows))
            # row schema
            r = next(r for r in rows if r["event"] == "fire")
            for key in ("at", "boundary", "policy_identity", "origin_delta_id", "rule",
                        "delta_status", "event", "message", "context"):
                self.assertIn(key, r)
            self.assertEqual(r["policy_identity"], {
                "layer": "project", "id": "verification_debt/skip"})
            self.assertEqual(r["context"]["delegation_id"], did)

    def test_delegate_run_warning_emits_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, did = self._deleg_needs_review(d, report=None)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/skip", rule="delegation-verification-evidence-v1", summary="s"))
            _force_status(root, home, "verification_debt/skip", "warning")
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                events = _run_with_home(home, lambda: overlay.evaluate_boundary(
                    root, "delegate-run", {"delegation_id": did}))
            self.assertIn("waystone warn", err.getvalue())
            fire = [e for e in events if e["event"] == "fire"][0]
            self.assertEqual(fire["delta_status"], "warning")
            self.assertIs(fire["suppressed_by_start_level"], False)

    def test_observe_only_suppresses_warning_stderr_but_records_and_projects_it(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\npolicy:\n  start_level: observe-only\n")
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}, report=None))
            rec = _latest_rec(root, home)
            delegation_exposure = _json.loads((rec / "exposure.json").read_text())
            self.assertEqual(delegation_exposure["start_level"], "observe-only")
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/skip",
                rule="delegation-verification-evidence-v1", summary="s"))
            _force_status(root, home, "verification_debt/skip", "warning")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                events = _run_with_home(home, lambda: overlay.evaluate_boundary(
                    root, "delegate-run", {"delegation_id": rec.name}))
            self.assertEqual(err.getvalue(), "")
            fire = next(event for event in events if event["event"] == "fire")
            self.assertEqual(fire["start_level"], "observe-only")
            self.assertIs(fire["suppressed_by_start_level"], True)
            projected, skipped, present = improve._load_warning_rows(root)
            self.assertEqual((skipped, present), (0, True))
            projected_fire = next(row for row in projected if row["event"] == "fire")
            self.assertIs(projected_fire["suppressed_by_start_level"], True)

            _path, round_exposure = _run_with_home(
                home, lambda: overlay.write_round_exposure(root, "r1", None, None))
            self.assertEqual(round_exposure["start_level"], "observe-only")

    def test_observe_only_still_emits_conflict_stderr(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home, did = self._deleg_needs_review(d, report=None)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\npolicy:\n  start_level: observe-only\n")
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/one",
                rule="delegation-verification-evidence-v1", summary="s"))
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/two",
                rule="delegation-verification-evidence-v1", summary="s"))
            _force_status(root, home, "verification_debt/one", "warning")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                events = _run_with_home(home, lambda: overlay.evaluate_boundary(
                    root, "delegate-run", {"delegation_id": did}))
            conflict = next(event for event in events if event["event"] == "conflict")
            self.assertIn("waystone warn conflict", err.getvalue())
            self.assertIs(conflict["suppressed_by_start_level"], False)
            fire = next(event for event in events if event["event"] == "fire")
            self.assertEqual(fire["delta_status"], "observing")
            self.assertIs(fire["suppressed_by_start_level"], False)

    def test_no_fire_when_verification_present(self):
        with tempfile.TemporaryDirectory() as d:
            report = ("verification:\n  - {cmd: \"pytest\", rc: 0, summary: \"ok\"}\n"
                      "limitations: []\nrisks: []\nescalations: []\n")
            root, home, did = self._deleg_needs_review(d, report=report)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/skip", rule="delegation-verification-evidence-v1", summary="s"))
            events = _run_with_home(home, lambda: overlay.evaluate_boundary(
                root, "delegate-run", {"delegation_id": did}))
            self.assertEqual([e for e in events if e["event"] == "fire"], [])

    def test_apply_exit_unchanged_despite_warning(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, did = self._deleg_needs_review(d, report=None)
            _write_apply_verdict(delegate._record_dir(root, did))
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/skip", rule="delegation-verification-evidence-v1", summary="s"))
            _force_status(root, home, "verification_debt/skip", "warning")
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: delegate.apply_delegation(root, did))
            self.assertEqual(rc, 0)  # warn never changes host exit (S5)
            self.assertTrue((root / "impl.py").exists())

    def test_engine_exception_does_not_propagate(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            orig = overlay._evaluate_boundary
            overlay._evaluate_boundary = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
            import contextlib
            import io
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    events = _run_with_home(home, lambda: overlay.evaluate_boundary(root, "check", {}))
            finally:
                overlay._evaluate_boundary = orig
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "evaluation-error")
            self.assertEqual(events[0]["context"], {
                "evaluable": False, "fired": False, "coverage_reason": "evaluation-error"})

    def test_unknown_active_rule_logs_evaluation_error_and_notice(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/future", rule="delegation-verification-evidence-v1",
                summary="s"))
            delta = _run_with_home(
                home, lambda: overlay.load_delta(root, "verification_debt/future"))
            delta["rule"] = "future-rule-v9"
            _run_with_home(home, lambda: overlay._write_delta(root, delta))
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = _run_with_home(
                    home, lambda: overlay.main(["check", "--root", str(root)]))
            self.assertEqual(rc, 0)
            rows = _read_warnings(root, home)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event"], "evaluation-error")
            self.assertEqual(rows[0]["rule"], "future-rule-v9")
            self.assertIn("future-rule-v9", err.getvalue())

    def test_conflict_least_restrictive(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home, did = self._deleg_needs_review(d, report=None)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/one", rule="delegation-verification-evidence-v1", summary="s"))
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/two", rule="delegation-verification-evidence-v1", summary="s"))
            _force_status(root, home, "verification_debt/one", "warning")  # two stays observing
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                events = _run_with_home(home, lambda: overlay.evaluate_boundary(
                    root, "delegate-run", {"delegation_id": did}))
            # effective status is least-restrictive (observing wins) + a conflict event recorded
            conflict = next(e for e in events if e["event"] == "conflict")
            self.assertEqual(conflict["context"]["delegation_id"], did)
            self.assertIn("waystone warn conflict", err.getvalue())
            self.assertEqual([e for e in events if e["event"] == "fire"][0]["delta_status"], "observing")

    def test_check_multi_delegation_multi_finding_pin(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _check_project(d)
            report = ("verification:\n  - {cmd: \"pytest\", rc: 0, summary: \"ok\"}\n"
                      "limitations: []\nrisks: []\nescalations: []\n")
            _deleg_run(root, home, _deleg_fake({"a.py": "x\n"}, report=None), task="feat/xyz")     # fires
            _deleg_run(root, home, _deleg_fake({"b.py": "y\n"}, report=report), task="feat/two")   # verified
            with self.assertRaises(delegate.WorkflowError):  # failed-runner -> no contract, excluded
                _deleg_run(root, home, _deleg_fake({"c.py": "z\n"}, rc=3), task="feat/three")
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/skip", rule="delegation-verification-evidence-v1", summary="s"))
            _run_with_home(home, lambda: overlay.add_delta(
                root, "review_association/open", rule="round-close-open-findings-v1", summary="s"))
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                events = _run_with_home(home, lambda: overlay.evaluate_boundary(root, "check", {}))
            r1 = [e for e in events if e["rule"] == "delegation-verification-evidence-v1" and e["event"] == "fire"]
            self.assertEqual(len(r1), 1)                        # only the unverified needs-review one
            self.assertIn("feat-xyz", r1[0]["context"]["delegation_id"])
            r2 = [e for e in events if e["rule"] == "round-close-open-findings-v1" and e["event"] == "fire"]
            self.assertEqual(len(r2), 1)
            self.assertIn("fix/finding-a", r2[0]["message"])

    def test_check_cli_exit0_even_with_fires(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, did = self._deleg_needs_review(d, report=None)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/skip", rule="delegation-verification-evidence-v1", summary="s"))
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: overlay.main(["check", "--root", str(root)]))
            self.assertEqual(rc, 0)

    def test_round_close_boundary_fires_and_records_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "review_association/open", rule="round-close-open-findings-v1", summary="s"))
            _force_status(root, home, "review_association/open", "warning")
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: round.close(
                    root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"], touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)  # warn does not block close
            self.assertIn("waystone warn", err.getvalue())
            rows = _read_warnings(root, home)
            self.assertTrue(any(r["boundary"] == "round-close" and r["event"] == "fire" for r in rows))

    def test_round_close_warn_engine_failure_keeps_committed_close_success(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            orig = overlay.evaluate_boundary
            overlay.evaluate_boundary = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("synthetic warn crash"))
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: round.close(
                        root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"], touched=[], commit="HEAD"))
            finally:
                overlay.evaluate_boundary = orig
            self.assertEqual(rc, 0)
            self.assertEqual(common.load_tasks(root)["tasks"][0]["status"], "done")
            self.assertIn("overlay warning", err.getvalue())
            self.assertIn("synthetic warn crash", err.getvalue())

    def test_round_close_warn_import_failure_keeps_committed_close_success(self):
        import builtins
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            orig_import = builtins.__import__
            overlay_imports = {"count": 0}

            def fake_import(name, *args, **kwargs):
                if name == "overlay":
                    overlay_imports["count"] += 1
                    if overlay_imports["count"] > 1:
                        raise ImportError("synthetic overlay import failure")
                return orig_import(name, *args, **kwargs)

            err = io.StringIO()
            builtins.__import__ = fake_import
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: round.close(
                        root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"], touched=[], commit="HEAD"))
            finally:
                builtins.__import__ = orig_import
            self.assertEqual(rc, 0)
            self.assertEqual(common.load_tasks(root)["tasks"][0]["status"], "done")
            self.assertIn("overlay warning", err.getvalue())
            self.assertIn("synthetic overlay import failure", err.getvalue())

    def test_delegate_warn_failures_are_noticed_without_changing_host_exit(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            orig = overlay.evaluate_boundary
            overlay.evaluate_boundary = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("synthetic warn crash"))
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    self.assertEqual(
                        _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"})), 0)
                    rec = _latest_rec(root, home)
                    _write_apply_verdict(rec)
                    self.assertEqual(
                        _run_with_home(
                            home, lambda: delegate.apply_delegation(root, rec.name)), 0)
            finally:
                overlay.evaluate_boundary = orig
            self.assertIn("delegate-run", err.getvalue())
            self.assertIn("delegate-apply", err.getvalue())
            self.assertEqual(err.getvalue().count("synthetic warn crash"), 2)

    def test_review_ingest_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "review_association/open", rule="round-close-open-findings-v1", summary="s"))
            events = _run_with_home(home, lambda: overlay.evaluate_boundary(
                root, "review-ingest", {"round_id": "2026-01-01-r1"}))
            fires = [e for e in events if e["event"] == "fire"]
            self.assertEqual(len(fires), 1)
            self.assertIn("fix/finding-a", fires[0]["message"])


class DelegateExposureOverlayTests(unittest.TestCase):
    """0.8.0 M2 §9 — delegation exposure `overlays` filled with active deltas at run time."""

    def test_exposure_overlays_populated(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/skip", rule="delegation-verification-evidence-v1", summary="s"))
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            exp = _json.loads((rec / "exposure.json").read_text())
            project_policy = next(
                row for row in exp["overlays"] if row["identity"]["layer"] == "project")
            self.assertEqual(project_policy, {
                "identity": {"layer": "project", "id": "verification_debt/skip"},
                "origin_delta_id": "verification_debt/skip", "status": "observing",
            })
            self.assertEqual(sum(row["identity"]["layer"] == "base"
                                 for row in exp["overlays"]), len(overlay.RULES) - 1)

    def test_corrupt_delta_refuses_exposure_capture_and_names_file(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/healthy", rule="delegation-verification-evidence-v1",
                summary="s"))
            corrupt = _run_with_home(home, lambda: overlay._deltas_dir(root) / "corrupt.json")
            corrupt.write_text("{not-json")
            called = {"n": 0}

            def fake(*args):
                called["n"] += 1
                return (0, 0.1)

            with self.assertRaises(delegate.WorkflowError) as cm:
                _deleg_run(root, home, fake)
            self.assertIn(str(corrupt), str(cm.exception))
            self.assertEqual(called["n"], 0)


class ReplayTests(unittest.TestCase):
    """0.8.0 M2 §5 — deterministic shadow replay and the replay-backed promote gate."""

    def _delegation_corpus(self, root, home):
        ddir = _run_with_home(home, lambda: delegate._delegations_dir(root))
        verified = ddir / "d-verified" / "artifact"
        missing = ddir / "d-missing" / "artifact"
        corrupt = ddir / "d-corrupt" / "artifact"
        failed = ddir / "d-failed" / "artifact"
        for p in (verified, missing, corrupt, failed):
            p.mkdir(parents=True)
        (verified / "contract.yaml").write_text(
            "delegate_report:\n  present: true\n  verification:\n    - {cmd: pytest, rc: 0}\n")
        (missing / "contract.yaml").write_text(
            "delegate_report:\n  present: false\n")
        (corrupt / "contract.yaml").write_text("delegate_report: [\n")
        # d-failed models failed-env/-runner/-artifact: record dir exists, contract does not.

    def test_delegation_replay_counts_only_evaluable_contracts(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            _add_delta(root, home)
            self._delegation_corpus(root, home)
            report = _run_with_home(
                home, lambda: overlay.replay(root, "verification_debt/skip"))
            self.assertEqual(report["corpus"], "delegations")
            self.assertEqual(report["corpus_size"], 3)  # contract-bearing records, corrupt included
            self.assertEqual(report["opportunities"], 2)  # corrupt excluded from the denominator
            self.assertEqual(report["fires"], 1)
            self.assertEqual(report["fire_rate"], 0.5)
            self.assertEqual(report["evaluation_errors"], 1)
            self.assertEqual(report["examples"], ["d-missing/artifact/contract.yaml"])
            self.assertIsNone(report["estimated_nuisance_rate"])
            self.assertEqual(report["nuisance_provenance"], "unlabeled")
            delta = _run_with_home(home, lambda: overlay.load_delta(root, "verification_debt/skip"))
            self.assertIn("replayed_at", delta["replay"])
            self.assertNotIn("replayed_at", report)

    def test_replay_stdout_is_byte_identical_and_uses_neutral_vocabulary(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            _add_delta(root, home)
            self._delegation_corpus(root, home)
            import contextlib
            import io

            def run_once():
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = _run_with_home(home, lambda: overlay.main(
                        ["replay", "verification_debt/skip", "--root", str(root)]))
                self.assertEqual(rc, 0)
                return out.getvalue().encode()

            first, second = run_once(), run_once()
            self.assertEqual(first, second)
            text = first.decode().lower()
            self.assertIn("would have fired 1/2 times", text)
            self.assertIn("nuisance rate requires labeling", text)
            for forbidden in ("prevented", "improved", "benefit"):
                self.assertNotIn(forbidden, text)

    def test_empty_corpus_has_null_rate_and_explicit_marker(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            _add_delta(root, home)
            report = _run_with_home(
                home, lambda: overlay.replay(root, "verification_debt/skip"))
            self.assertEqual(report["corpus_size"], 0)
            self.assertEqual(report["opportunities"], 0)
            self.assertIsNone(report["fire_rate"])
            self.assertEqual(report["status"], "empty-corpus")

    def test_review_replay_is_round_based_and_promote_accepts_real_result(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _rule2_project(d)
            _add_delta(root, home, delta_id="review_association/open",
                       rule="round-close-open-findings-v1")
            report = _run_with_home(
                home, lambda: overlay.replay(root, "review_association/open"))
            self.assertEqual(report["corpus"], "reviews")
            self.assertEqual(report["corpus_size"], 1)
            self.assertEqual(report["opportunities"], 1)
            self.assertEqual(report["fires"], 1)
            self.assertEqual(report["unlinked_findings"], 1)
            self.assertEqual(report["resolution_provenance"], "current-task-state-approximation")
            promoted = _run_with_home(
                home, lambda: overlay.promote(root, "review_association/open"))
            self.assertEqual(promoted["status"], "warning")
