"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


class DelegateFanoutPlanTests(unittest.TestCase):
    """§4.1 — `delegate plan --json` fan-out manifest and its fail-loud gates."""

    def test_plan_emits_canonical_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            rc, _out, plan = _run_plan(root, home, ["feat/xyz", "feat/two", "--json",
                                                    "--routing-note", "budget: normal"])
            self.assertEqual(rc, 0)
            self.assertEqual(plan["schema"], "waystone-fanout-plan-1")
            self.assertEqual(plan["root"], str(root.resolve()))
            self.assertRegex(plan["correlation_id"], r"^\d{8}T\d{6}Z-fanout-[0-9a-f]{6}$")
            self.assertNotIn("registry_fingerprint", plan)
            self.assertEqual(plan["carrier"], {
                "orchestrator": {"execution": "deterministic-workflow",
                                 "backend": "claude:fable-5", "effort": "high"},
                "clerk": {"backend": "claude:haiku-4.5", "effort": "low"},
                "implementer": {"execution": "external-runner",
                                "backend": "codex:gpt-5.6-sol", "effort": "ultra"}})
            self.assertEqual([t["task_id"] for t in plan["tasks"]], ["feat/xyz", "feat/two"])
            for t in plan["tasks"]:
                self.assertTrue(t["deps_ok"])
                self.assertRegex(t["packet_sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(plan["overlap_pairs"], [])
            self.assertEqual(plan["unknown_scope_tasks"], [])
            self.assertEqual(plan["routing_note"], "budget: normal")
            # packet_sha256 equals the run-side canonical digest of the same rebuilt packet
            data = common.load_tasks(root)
            packet, _ = delegate._build_packet(data, "feat/xyz", [], root)
            self.assertEqual(plan["tasks"][0]["packet_sha256"],
                             delegate._packet_core_digest(packet))
            # _fingerprint matches the live profile
            _profile, fingerprint = _run_with_home(home, lambda: delegate._load_profile(root))
            self.assertEqual(plan["profile_fingerprint"], fingerprint)

    def test_fanout_correlation_id_format_and_uniqueness(self):
        # the -fanout-<6 hex> suffix is a uniqueness discriminator: two plans in the same UTC second
        # must not collide, since this value becomes each delegation's immutable carrier.instance_id
        a = delegate._make_fanout_correlation_id()
        b = delegate._make_fanout_correlation_id()
        rx = r"^\d{8}T\d{6}Z-fanout-[0-9a-f]{6}$"
        self.assertRegex(a, rx)
        self.assertRegex(b, rx)
        self.assertNotEqual(a, b)

    def test_plan_reports_scope_overlap_and_unknown_scope(self):
        tasks_yaml = (
            "version: 1\nproject: demo\ntasks:\n"
            '  - id: feat/a\n    title: "a"\n    status: active\n'
            '    scope: [src]\n    accept: ["crit a"]\n'
            '  - id: feat/b\n    title: "b"\n    status: active\n'
            '    scope: [src/sub]\n    accept: ["crit b"]\n'
            '  - id: fix/c\n    title: "c"\n    status: active\n    accept: ["crit c"]\n')
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d, tasks_yaml=tasks_yaml)
            rc, _out, plan = _run_plan(root, home, ["feat/a", "feat/b", "fix/c", "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(plan["overlap_pairs"], [["feat/a", "feat/b"]])
            self.assertEqual(plan["unknown_scope_tasks"], ["fix/c"])

    def test_plan_refuses_unmet_dependency(self):
        tasks_yaml = (
            "version: 1\nproject: demo\ntasks:\n"
            '  - id: feat/base\n    title: "base"\n    status: active\n    accept: ["crit"]\n'
            '  - id: feat/child\n    title: "child"\n    status: active\n'
            '    deps: [feat/base]\n    scope: [src/c]\n    accept: ["crit c"]\n')
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d, tasks_yaml=tasks_yaml)
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["plan", "feat/child", "--json", "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("unmet dependencies", err.getvalue())

    def test_plan_fails_closed_on_corrupt_record(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}), task="feat/xyz")
            rec = _latest_rec(root, home)
            (rec / "status.json").write_text("{ corrupt", encoding="utf-8")
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["plan", "feat/two", "--json", "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("corrupt delegation record", err.getvalue())

    def test_plan_refuses_when_target_has_non_terminal_delegation(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}), task="feat/xyz")
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["plan", "feat/xyz", "--json", "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("non-terminal delegation", err.getvalue())

    def test_plan_refuses_binding_without_explicit_effort(self):
        profile = (
            "schema: waystone-profile-1\nbindings:\n"
            "  orchestrator: {execution: deterministic-workflow, backend: 'claude:fable-5'}\n"
            "  clerk: {execution: clean-subagent, backend: 'claude:haiku-4.5', effort: low}\n"
            "  implementer: {execution: external-runner, backend: 'codex:gpt-5.6-sol', effort: ultra}\n")
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d, profile=profile)
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["plan", "feat/xyz", "--json", "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("explicit effort", err.getvalue())

    def test_plan_refuses_orchestrator_not_deterministic_workflow(self):
        profile = (
            "schema: waystone-profile-1\nbindings:\n"
            "  orchestrator: {execution: main-session, backend: 'claude:fable-5', effort: high}\n"
            "  clerk: {execution: clean-subagent, backend: 'claude:haiku-4.5', effort: low}\n"
            "  implementer: {execution: external-runner, backend: 'codex:gpt-5.6-sol', effort: ultra}\n")
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d, profile=profile)
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["plan", "feat/xyz", "--json", "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("deterministic-workflow", err.getvalue())

    def test_plan_requires_json_flag(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["plan", "feat/xyz", "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("--json", err.getvalue())


class DelegatePacketDigestTests(unittest.TestCase):
    """§4.1/§4.2 — canonical packet-core digest determinism and carrier exclusion."""

    def _packet(self, root):
        return delegate._build_packet(common.load_tasks(root), "feat/xyz", [], root)[0]

    def test_digest_is_deterministic_and_worktree_stable(self):
        with tempfile.TemporaryDirectory() as d:
            root, _home = _fanout_project(d)
            packet_a = self._packet(root)
            packet_b = self._packet(root)
            self.assertEqual(delegate._packet_core_digest(packet_a),
                             delegate._packet_core_digest(packet_b))
            self.assertRegex(delegate._packet_core_digest(packet_a), r"^sha256:[0-9a-f]{64}$")
            # the absolute project root is NOT in the core — a different root hashes identically
            packet_moved = dict(packet_a)
            packet_moved["project"] = {"name": packet_a["project"]["name"],
                                       "root": "/somewhere/else"}
            self.assertEqual(delegate._packet_core_digest(packet_a),
                             delegate._packet_core_digest(packet_moved))

    def test_carrier_and_volatile_fields_excluded_from_digest(self):
        with tempfile.TemporaryDirectory() as d:
            root, _home = _fanout_project(d)
            packet = self._packet(root)
            base = delegate._packet_core_digest(packet)
            packet["carrier"] = {"name": "claude-workflow", "instance_id": "cid.1"}
            packet["routing_note"] = {"provenance": "main-session", "note": "x"}
            packet["retry_context"] = {"provenance": "main-session", "note": "y"}
            self.assertEqual(delegate._packet_core_digest(packet), base)

    def test_digest_changes_when_acceptance_changes(self):
        with tempfile.TemporaryDirectory() as d:
            root, _home = _fanout_project(d)
            base = delegate._packet_core_digest(self._packet(root))
            data = common.load_tasks(root)
            mutated = delegate._build_packet(data, "feat/xyz", ["a fresh criterion"], root)[0]
            self.assertNotEqual(delegate._packet_core_digest(mutated), base)


class DelegateExpectAndCarrierTests(unittest.TestCase):
    """§4.2 — --expect-packet-sha / --expect-profile / --carrier binding and validation."""

    def _assert_registry_mutation_refuses_before_claim(self, old, new, *, tasks_yaml=_FANOUT_TASKS):
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d, tasks_yaml=tasks_yaml)
            rc, _out, manifest = _run_plan(root, home, ["feat/xyz", "--json"])
            self.assertEqual(rc, 0)
            sha = manifest["tasks"][0]["packet_sha256"]
            self.assertIn(old, tasks_yaml)
            (root / "tasks.yaml").write_text(tasks_yaml.replace(old, new), encoding="utf-8")
            ddir = _run_with_home(home, lambda: delegate._delegations_dir(root))
            self.assertFalse(ddir.exists())
            with self.assertRaisesRegex(delegate.WorkflowError, "digest mismatch"):
                _run_with_home(home, lambda: delegate.run_delegation(
                    root, "feat/xyz", "implementer", [], expect_packet_sha=sha))
            self.assertFalse(ddir.exists())

    def test_expect_packet_sha_refuses_stale_milestone_before_claim(self):
        self._assert_registry_mutation_refuses_before_claim(
            'milestone: "milestone one"', 'milestone: "milestone two"')

    def test_expect_packet_sha_refuses_stale_round_before_claim(self):
        self._assert_registry_mutation_refuses_before_claim(
            'round: "round one"', 'round: "round two"')

    def test_expect_packet_sha_refuses_stale_anchor_before_claim(self):
        self._assert_registry_mutation_refuses_before_claim(
            'anchor: "src/a.py:10"', 'anchor: "src/a.py:20"')

    def test_expect_packet_sha_refuses_stale_notes_before_claim(self):
        self._assert_registry_mutation_refuses_before_claim(
            'notes: "original dispatch note"', 'notes: "changed dispatch note"')

    def test_expect_packet_sha_tracks_prompt_visible_mapping_order_before_claim(self):
        original = _FANOUT_TASKS.replace(
            '    notes: "original dispatch note"\n',
            "    notes:\n      nested:\n        first: one\n        second: two\n")
        self._assert_registry_mutation_refuses_before_claim(
            "      nested:\n        first: one\n        second: two\n",
            "      nested:\n        second: two\n        first: one\n",
            tasks_yaml=original)

    def test_claim_run_refuses_prompt_visible_mapping_reorder_after_prepare(self):
        # The prepare->claim window: _prepare_run pins plan["packet"] (order X); reordering a
        # mapping-typed notes to order Y before _claim_run must be refused. Dict equality is
        # order-insensitive, so only the order-sensitive core digest catches this reorder.
        original = _FANOUT_TASKS.replace(
            '    notes: "original dispatch note"\n',
            "    notes:\n      nested:\n        first: one\n        second: two\n")
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d, tasks_yaml=original)
            plan = _run_with_home(home, lambda: delegate._prepare_run(
                root, "feat/xyz", "implementer", []))
            (root / "tasks.yaml").write_text(original.replace(
                "      nested:\n        first: one\n        second: two\n",
                "      nested:\n        second: two\n        first: one\n"), encoding="utf-8")
            def claim():
                with common.hold_lock(common.project_lock_path(root)):
                    return delegate._claim_run(root, plan)
            with self.assertRaisesRegex(delegate.WorkflowError, "changed while preparing"):
                _run_with_home(home, claim)
            self.assertFalse(
                _run_with_home(home, lambda: delegate._delegations_dir(root)).exists())

    def test_expect_packet_sha_match_runs_and_stale_refuses(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            packet = delegate._build_packet(common.load_tasks(root), "feat/xyz", [], root)[0]
            sha = delegate._packet_core_digest(packet)
            orig = delegate._run_codex
            delegate._run_codex = _deleg_fake({"impl.py": "x\n"})
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = _run_with_home(home, lambda: delegate.run_delegation(
                        root, "feat/xyz", "implementer", [], expect_packet_sha=sha))
                self.assertEqual(rc, 0)
                # mutate the task, then dispatch with the now-stale sha → refused before claim
                (root / "tasks.yaml").write_text(_FANOUT_TASKS.replace(
                    "implement xyz feature", "a materially different title"))
                with self.assertRaisesRegex(delegate.WorkflowError, "digest mismatch"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        _run_with_home(home, lambda: delegate.run_delegation(
                            root, "feat/xyz", "implementer", [], expect_packet_sha=sha))
            finally:
                delegate._run_codex = orig

    def test_expect_profile_mismatch_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            with self.assertRaisesRegex(delegate.WorkflowError, "profile fingerprint mismatch"):
                _run_with_home(home, lambda: delegate.run_delegation(
                    root, "feat/xyz", "implementer", [],
                    expect_profile="sha256:deadbeefdead"))

    def test_carrier_recorded_in_packet_and_excluded_from_digest(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            orig = delegate._run_codex
            delegate._run_codex = _deleg_fake({"impl.py": "x\n"})
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = _run_with_home(home, lambda: delegate.run_delegation(
                        root, "feat/xyz", "implementer", [],
                        carrier="claude-workflow", carrier_instance="20260717T000000Z-fanout"))
                self.assertEqual(rc, 0)
            finally:
                delegate._run_codex = orig
            rec = _latest_rec(root, home)
            packet = yaml.safe_load((rec / "packet.yaml").read_text())
            self.assertEqual(packet["carrier"],
                             {"name": "claude-workflow", "instance_id": "20260717T000000Z-fanout"})
            # the same packet without carrier hashes identically (carrier never enters the core)
            no_carrier = {k: v for k, v in packet.items() if k != "carrier"}
            self.assertEqual(delegate._packet_core_digest(packet),
                             delegate._packet_core_digest(no_carrier))

    def test_carrier_flag_validation(self):
        self.assertIsNone(delegate._validate_carrier(None, None))
        self.assertEqual(
            delegate._validate_carrier("claude-workflow", "cid.1"),
            {"name": "claude-workflow", "instance_id": "cid.1"})
        with self.assertRaisesRegex(delegate.WorkflowError, "must be one of"):
            delegate._validate_carrier("codex-workflow", "cid.1")
        with self.assertRaisesRegex(delegate.WorkflowError, "carrier-instance"):
            delegate._validate_carrier("claude-workflow", "bad id with spaces")
        with self.assertRaisesRegex(delegate.WorkflowError, "given together"):
            delegate._validate_carrier("claude-workflow", None)
        with self.assertRaisesRegex(delegate.WorkflowError, "given together"):
            delegate._validate_carrier(None, "cid.1")


class DelegateJsonEventsTests(unittest.TestCase):
    """§4.2 — --json-events NDJSON stream: pure stdout, claimed-before-worktree, finished on every path."""

    def _events(self, text):
        import json as _json
        events = []
        for line in text.splitlines():
            if line.strip():
                events.append(_json.loads(line))  # raises if any human line leaked to stdout
        return events

    def test_json_events_order_purity_and_claimed_precedes_worktree(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            out = io.StringIO()
            seen = {}
            orig_codex = delegate._run_codex
            orig_add = delegate._add_worktree
            delegate._run_codex = _deleg_fake({"impl.py": "x\n"})

            def spy_add(root_, wt, base):
                # the claimed event must already be on stdout before any worktree is created
                seen["claimed_before_worktree"] = "claimed" in out.getvalue()
                return orig_add(root_, wt, base)

            delegate._add_worktree = spy_add
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    rc = _run_with_home(home, lambda: delegate.run_delegation(
                        root, "feat/xyz", "implementer", [], json_events=True))
            finally:
                delegate._run_codex = orig_codex
                delegate._add_worktree = orig_add
            self.assertEqual(rc, 0)
            self.assertTrue(seen["claimed_before_worktree"])
            events = self._events(out.getvalue())  # pure NDJSON — no "base_sha:" human lines
            self.assertEqual(events[0]["event"], "claimed")
            self.assertEqual(events[-1]["event"], "finished")
            self.assertLess([e["event"] for e in events].index("claimed"),
                            [e["event"] for e in events].index("finished"))
            claimed, finished = events[0], events[-1]
            self.assertEqual(claimed["task_id"], "feat/xyz")
            self.assertRegex(claimed["packet_sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(finished["state"], "needs-review")
            self.assertEqual(claimed["delegation_id"], finished["delegation_id"])
            self.assertEqual(finished["changed_file_count"], 1)
            self.assertFalse(finished["patch_empty"])
            self.assertTrue(finished["artifact"].endswith("contract.yaml"))

    def test_json_events_finished_on_failed_runner(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            out = io.StringIO()
            orig = delegate._run_codex
            delegate._run_codex = _deleg_fake({"impl.py": "x\n"}, rc=9)
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(delegate.WorkflowError):
                        _run_with_home(home, lambda: delegate.run_delegation(
                            root, "feat/xyz", "implementer", [], json_events=True))
            finally:
                delegate._run_codex = orig
            events = self._events(out.getvalue())
            self.assertEqual(events[0]["event"], "claimed")
            finished = events[-1]
            self.assertEqual(finished["event"], "finished")
            self.assertEqual(finished["state"], "failed-runner")
            self.assertIsNone(finished["artifact"])
            self.assertIsNone(finished["base_sha"])
            self.assertIsNone(finished["changed_file_count"])
            self.assertIsNone(finished["patch_sha256"])
            self.assertIsNone(finished["patch_empty"])
            self.assertIsNone(finished["delegate_report_present"])
            self.assertIsInstance(finished["error"], str)


class DelegateStatusJsonTests(unittest.TestCase):
    """§4.3 — `delegate status --json` structured rows, incl. a corrupt row with recovered task_id."""

    def test_status_json_reports_rows_and_corrupt_row(self):
        import contextlib
        import io
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            orig = delegate._make_did
            try:
                delegate._make_did = lambda tid: "20260713T000001Z-" + tid.replace("/", "-")
                _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}), task="feat/xyz")
                delegate._make_did = lambda tid: "20260713T000002Z-" + tid.replace("/", "-")
                _deleg_run(root, home, _deleg_fake({"impl2.py": "y\n"}), task="feat/two")
            finally:
                delegate._make_did = orig
            recs = _run_with_home(home, lambda: sorted(delegate._delegations_dir(root).iterdir()))
            # corrupt BOTH status and exposure on the first record → task_id recovered from claim.json
            (recs[0] / "status.json").write_text("{ corrupt", encoding="utf-8")
            (recs[0] / "exposure.json").write_text("{ corrupt", encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["status", "--root", str(root), "--json"]))
            self.assertEqual(rc, 0)
            rows = _json.loads(buf.getvalue())
            self.assertIsInstance(rows, list)
            by_did = {r["delegation_id"]: r for r in rows}
            corrupt = by_did["20260713T000001Z-feat-xyz"]
            self.assertTrue(corrupt["corrupt"])
            self.assertEqual(corrupt["task_id"], "feat/xyz")  # recovered from claim.json, no heuristics
            healthy = by_did["20260713T000002Z-feat-two"]
            self.assertFalse(healthy["corrupt"])
            self.assertEqual(healthy["state"], "needs-review")
            self.assertEqual(healthy["task_id"], "feat/two")
            self.assertTrue(healthy["base_sha"])

    def test_status_json_claim_only_record_is_claimed_not_corrupt(self):
        # a readable claim.json with no exposure/status yet (the claim→exposure window or a crash
        # remnant) is a healthy active hold in state "claimed" — the same classification the owner
        # lock gives it — never {state: null, corrupt: true}.
        import contextlib
        import io
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            ddir = _run_with_home(home, lambda: delegate._delegations_dir(root))
            rec = ddir / "20260713T000003Z-feat-xyz"
            rec.mkdir(parents=True)
            (rec / "claim.json").write_text(_json.dumps(
                {"schema": "waystone-delegation-claim-1", "task_id": "feat/xyz",
                 "at": "2026-07-13T00:00:03Z"}) + "\n", encoding="utf-8")
            self.assertFalse((rec / "exposure.json").exists())
            self.assertFalse((rec / "status.json").exists())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["status", "--root", str(root), "--json"]))
            self.assertEqual(rc, 0)
            rows = _json.loads(buf.getvalue())
            row = {r["delegation_id"]: r for r in rows}["20260713T000003Z-feat-xyz"]
            self.assertFalse(row["corrupt"])            # healthy claim-only hold, not corrupt
            self.assertEqual(row["state"], "claimed")   # owner-lock semantics
            self.assertEqual(row["task_id"], "feat/xyz")  # recovered from claim.json
            self.assertIsNone(row["base_sha"])

    def test_status_json_unreadable_claim_only_record_is_corrupt(self):
        # genuinely unreadable binding (claim.json unparseable, no exposure) stays corrupt: True —
        # corrupt is reserved for records the owner-lock scan itself treats as corrupt.
        import contextlib
        import io
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            root, home = _fanout_project(d)
            ddir = _run_with_home(home, lambda: delegate._delegations_dir(root))
            rec = ddir / "20260713T000004Z-feat-xyz"
            rec.mkdir(parents=True)
            (rec / "claim.json").write_text("{ corrupt", encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["status", "--root", str(root), "--json"]))
            self.assertEqual(rc, 0)
            rows = _json.loads(buf.getvalue())
            row = {r["delegation_id"]: r for r in rows}["20260713T000004Z-feat-xyz"]
            self.assertTrue(row["corrupt"])
            self.assertIsNone(row["state"])


class DelegateFanoutTemplateLintTests(unittest.TestCase):
    """§7.1 — node --check is a secondary syntax lint for the carrier template (skip if node absent)."""

    def test_fanout_workflow_template_passes_node_check(self):
        import shutil
        node = shutil.which("node")
        template = (SCRIPTS.parent / "templates" / "hosts" / "claude-code"
                    / "delegate-fanout.workflow.js")
        if node is None:
            self.skipTest("node not available — validateOnly engine call is the primary gate")
        if not template.is_file():
            self.skipTest("carrier template not present yet (owned by a parallel agent)")
        proc = subprocess.run([node, "--check", str(template)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class DelegateMainContractTests(unittest.TestCase):
    """§5.3 — the binding-precedence line pinned in references/main-contract.md."""

    def test_main_contract_last_line_is_binding_precedence(self):
        path = SCRIPTS.parent / "references" / "main-contract.md"
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(
            lines[-1], "Session execution modes never override a role's declared binding.")


class DelegateCliTests(unittest.TestCase):
    """0.8.0 M1 §2 — arg parsing, exit codes, status/show surfaces (incl. R11 no-artifact refusal)."""

    def test_run_subprocess_rejects_unsatisfied_dependency_before_runner_claim(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks:\n"
                "  - id: feat/xyz\n    title: implement xyz feature\n    status: active\n"
                "    deps: [feat/parent]\n"
                "    accept:\n      - criterion alpha here\n"
                "  - id: feat/parent\n    title: intentionally parked parent task\n"
                "    status: parked\n")
            fake_bin = Path(d) / "fake-bin"
            fake_bin.mkdir()
            runner_called = Path(d) / "runner-called"
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                f"#!/bin/sh\ntouch {runner_called}\nexit 99\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            (home / ".codex").mkdir(parents=True)
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "WAYSTONE_HOME": str(home / ".waystone"),
                "PATH": str(fake_bin) + os.pathsep + env["PATH"],
            })
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "waystone.py"), "delegate", "run", "feat/xyz",
                 "--root", str(root)],
                env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("feat/parent (parked)", result.stderr)
            self.assertFalse(runner_called.exists())
            self.assertFalse(delegate._delegations_dir(root).exists())

    def test_run_via_main_and_status_list(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            import contextlib
            import io
            orig = delegate._run_codex
            delegate._run_codex = _deleg_fake({"impl.py": "x\n"})
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = _run_with_home(home, lambda: delegate.main(
                        ["run", "feat/xyz", "--root", str(root), "--accept", "extra criterion"]))
                self.assertEqual(rc, 0)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    _run_with_home(home, lambda: delegate.main(["status", "--root", str(root)]))
            finally:
                delegate._run_codex = orig
            self.assertIn("feat/xyz", out.getvalue())
            self.assertIn("needs-review", out.getvalue())

    def test_run_note_and_accept_provenance_are_recorded_in_packet(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            import contextlib
            import io
            orig = delegate._run_codex
            delegate._run_codex = _deleg_fake({"impl.py": "x\n"})
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = _run_with_home(home, lambda: delegate.main([
                        "run", "feat/xyz", "--root", str(root),
                        "--accept", "ad-hoc exact criterion",
                        "--note", "retry after transient registry failure",
                    ]))
            finally:
                delegate._run_codex = orig
            self.assertEqual(rc, 0)
            rec = _latest_rec(root, home)
            packet = yaml.safe_load((rec / "packet.yaml").read_text())
            self.assertEqual(packet["retry_context"], {
                "provenance": "main-session",
                "note": "retry after transient registry failure",
            })
            self.assertEqual(packet["accept_provenance"][-1], {
                "criterion": "ad-hoc exact criterion", "source": "delegate run --accept"})

    def test_show_failure_limits_output_to_status_error_and_stderr_tail(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            with self.assertRaises(delegate.WorkflowError):
                _deleg_run(root, home, _deleg_fake({}, rc=7))
            rec = _latest_rec(root, home)
            (rec / "runner.stderr").write_text(
                "\n".join(f"stderr line {i}" for i in range(1, 61)) + "\n", encoding="utf-8")
            (rec / "runner.jsonl").write_text("RUNNER-JSONL-SECRET\n", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = _run_with_home(home, lambda: delegate.main([
                    "show", rec.name, "--failure", "--root", str(root)]))
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("runner rc 7", text)
            self.assertIn("stderr line 11", text)
            self.assertIn("stderr line 60", text)
            self.assertNotIn("stderr line 10\n", text)
            self.assertNotIn("RUNNER-JSONL-SECRET", text)

    def test_show_failure_diagnoses_runner_sandbox_mechanisms(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            with self.assertRaises(delegate.WorkflowError):
                _deleg_run(root, home, _deleg_fake({}, rc=7))
            rec = _latest_rec(root, home)
            cases = (
                ("bwrap: Creating new namespace failed: Operation not permitted", "/etc/apparmor.d/bwrap"),
                ("AppArmor denied unprivileged userns creation", "/etc/apparmor.d/bwrap"),
                ("failed to apply Landlock sandbox rules", "Landlock"),
            )
            for stderr, hint in cases:
                with self.subTest(stderr=stderr):
                    (rec / "runner.stderr").write_text(stderr + "\n", encoding="utf-8")
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        rc = _run_with_home(home, lambda: delegate.main([
                            "show", rec.name, "--failure", "--root", str(root)]))
                    self.assertEqual(rc, 0)
                    self.assertIn("diagnostic hint:", out.getvalue())
                    self.assertIn(hint, out.getvalue())

            # A failed-env WITHOUT a recognized sandbox-mechanism line must still point at the
            # one-time verification key (a broken second machine skipped the probe).
            delegate._set_state(rec, "failed-env", error="runner wrote nothing")
            (rec / "runner.stderr").write_text(
                "some opaque environment error\n", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = _run_with_home(home, lambda: delegate.main([
                    "show", rec.name, "--failure", "--root", str(root)]))
            self.assertEqual(rc, 0)
            self.assertIn(".waystone/codex-runner-verified", out.getvalue())

    def test_unknown_subcommand(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(delegate.main(["frobnicate"]), 1)

    def test_waystone_dispatcher_routes_delegate(self):
        import waystone
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: waystone.main(["delegate", "status", "--root", str(root)]))
            self.assertEqual(rc, 0)

    def test_unknown_delegation_id(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["show", "nope-not-real", "--root", str(root)]))
            self.assertEqual(rc, 1)

    def test_show_surfaces(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            report = "verification: []\nlimitations: []\nrisks: []\nescalations: []\n"
            _deleg_run(root, home, _deleg_fake({"impl.py": "hello\n"}, report=report))
            rec = _latest_rec(root, home)
            import contextlib
            import io
            for opt, needle in (("--patch", "hello"), ("--report", "waystone-artifact-1"),
                                ("--exposure", "waystone-exposure-1")):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = _run_with_home(home, lambda o=opt: delegate.main(
                        ["show", rec.name, o, "--root", str(root)]))
                self.assertEqual(rc, 0)
                self.assertIn(needle, buf.getvalue())

    def test_show_patch_report_refused_when_no_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\ndelegation:\n  env_prep:\n    - \"false\"\n")
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError):
                    _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))  # -> failed-env
            rec = _latest_rec(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "failed-env")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_run_with_home(home, lambda: delegate.main(
                    ["show", rec.name, "--patch", "--root", str(root)])), 1)
                self.assertEqual(_run_with_home(home, lambda: delegate.main(
                    ["show", rec.name, "--report", "--root", str(root)])), 1)
            # exposure + summary always available (recorded at start)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(_run_with_home(home, lambda: delegate.main(
                    ["show", rec.name, "--exposure", "--root", str(root)])), 0)
                self.assertEqual(_run_with_home(home, lambda: delegate.main(
                    ["show", rec.name, "--root", str(root)])), 0)
