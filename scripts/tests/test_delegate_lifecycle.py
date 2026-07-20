"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


class DelegateEffortTests(unittest.TestCase):
    """0.8.0 M2 §20 — optional profile effort is explicit in execution and exposure."""

    def test_ultra_effort_reaches_codex_runner_and_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: \"codex:gpt-test\", "
                "effort: ultra}\n"))
            seen = {}

            def fake(worktree, model, prompt_path, record_dir, *, effort=None):
                seen["effort"] = effort
                (worktree / "impl.py").write_text("x\n")
                (record_dir / "last_message.md").write_text("summary")
                return (0, 0.1)

            _deleg_run(root, home, fake)
            rec = _latest_rec(root, home)
            exposure = _json.loads((rec / "exposure.json").read_text())
            self.assertEqual(seen["effort"], "ultra")
            self.assertEqual(exposure["binding"]["effort"], "ultra")

    def test_effort_routes_are_documented(self):
        readme = (SCRIPTS.parent / "README.md").read_text()
        conventions = (
            SCRIPTS.parent / "references" / "conventions.md").read_text()
        project_conventions = (
            SCRIPTS.parent / "docs" / "CONVENTIONS.md").read_text()
        for text in (readme, conventions, project_conventions):
            self.assertNotIn("`pro`", text)
            self.assertNotIn("web ChatGPT", text)
            self.assertIn("`ultra`", text)
            self.assertIn("model_reasoning_effort", text)
            self.assertIn("external-runner", text)


class DelegateApplyTests(unittest.TestCase):
    """0.8.0 M1 §12 — plain `git apply`, atomic drift failure, discard cleanup, re-apply refusal."""

    def test_apply_non_utf8_patch_preserves_bytes(self):
        # H1: a latin-1 text patch must apply byte-exact to the live tree
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)

            def fake(worktree, model, prompt_path, record_dir):
                (worktree / "cafe.txt").write_bytes(b"caf\xe9 au lait\n")
                (record_dir / "last_message.md").write_text("x")
                return (0, 0.1)
            _deleg_run(root, home, fake)
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            self.assertEqual(rc, 0)
            self.assertEqual((root / "cafe.txt").read_bytes(), b"caf\xe9 au lait\n")

    def test_apply_success_and_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "print('x')\n"}))
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            wt = _run_with_home(home, lambda: delegate._worktree_path(root, rec.name))
            rc = _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            self.assertEqual(rc, 0)
            self.assertTrue((root / "impl.py").exists())                     # patch landed on live tree
            self.assertEqual(delegate._read_status(rec)["state"], "applied")
            self.assertFalse(wt.exists())                                    # worktree removed
            self.assertTrue((rec / "artifact" / "contract.yaml").exists())   # record preserved
            for suffix in ("", "-result"):
                self.assertEqual(git(root, "rev-parse", "--verify",
                                     f"refs/waystone/delegations/{rec.name}{suffix}").returncode, 0)

    def test_apply_cli_holds_project_then_record_lock_during_mutation(self):
        import contextlib
        import fcntl
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "print('x')\n"}))
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            original_apply = delegate.apply_delegation
            original_hold_lock = delegate.hold_lock
            original_hold_project_lock = delegate.hold_project_lock
            original_resolve_root = delegate._resolve_root
            acquired = []

            def recording_project_lock(project, timeout=None):
                acquired.append("project")
                return original_hold_project_lock(project, timeout=timeout)

            @contextlib.contextmanager
            def recording_lock(path, timeout=None):
                label = "project" if Path(path) == common.project_lock_path(root) else "record"
                acquired.append(label)
                with original_hold_lock(path, timeout=timeout):
                    yield

            def assert_held(path):
                with Path(path).open("a+", encoding="utf-8") as stream:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return True
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    return False

            def checked_apply(project, did):
                self.assertTrue(assert_held(common.project_lock_path(project)))
                self.assertTrue(assert_held(rec / "record.lock"))
                return original_apply(project, did)

            delegate.apply_delegation = checked_apply
            delegate.hold_lock = recording_lock
            delegate.hold_project_lock = recording_project_lock
            delegate._resolve_root = lambda _explicit: root
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = _run_with_home(home, lambda: delegate.main(
                        ["apply", rec.name, "--root", str(root)]))
            finally:
                delegate.apply_delegation = original_apply
                delegate.hold_lock = original_hold_lock
                delegate.hold_project_lock = original_hold_project_lock
                delegate._resolve_root = original_resolve_root
            self.assertEqual(rc, 0)
            self.assertEqual(acquired, ["project", "record"])
            self.assertTrue((root / "impl.py").is_file())

    def test_apply_cli_times_out_when_project_lock_is_preempted(self):
        import contextlib
        import io
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "print('x')\n"}))
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"WAYSTONE_LOCK_TIMEOUT": "0.02"}, clear=False), \
                    common.hold_lock(common.project_lock_path(root), timeout=0.2), \
                    contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["apply", rec.name, "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("is held by pid", err.getvalue())
            self.assertFalse((root / "impl.py").exists())
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")

    def test_apply_drift_is_atomic_exit1(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"a.py": "AAA\n", "b.py": "BBB\n"}))
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            (root / "a.py").write_text("conflicting live content\n")  # drift on a patch target
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: delegate.main(["apply", rec.name, "--root", str(root)]))
            self.assertEqual(rc, 1)                                   # not a raw git rc
            self.assertIn("drifted", err.getvalue())
            self.assertFalse((root / "b.py").exists())               # atomic: other target untouched
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")  # unchanged

    def test_apply_unrelated_dirty_ok(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            (root / "f.txt").write_text("locally dirtied but unrelated")
            rc = _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            self.assertEqual(rc, 0)
            self.assertTrue((root / "impl.py").exists())

    def test_apply_empty_patch_noop(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({}))  # no changes
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            rc = _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            self.assertEqual(rc, 0)
            self.assertEqual(delegate._read_status(rec)["state"], "applied")

    def test_reapply_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            with self.assertRaises(delegate.WorkflowError):
                _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))

    def test_discard_cleanup_and_accepts_running(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            wt = _run_with_home(home, lambda: delegate._worktree_path(root, rec.name))
            delegate._set_state(rec, "running")  # simulate a crash remnant (R1)
            rc = _run_with_home(
                home, lambda: delegate.discard_delegation(root, rec.name, "clear crash remnant"))
            self.assertEqual(rc, 0)
            self.assertEqual(delegate._read_status(rec)["state"], "discarded")
            self.assertFalse(wt.exists())
            self.assertTrue((rec / "exposure.json").exists())  # record preserved

    def test_apply_requires_verdict_and_forbids_no_verdict_override(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["apply", rec.name, "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("run 'waystone delegate verdict' first", err.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_run_with_home(home, lambda: delegate.main([
                    "apply", rec.name, "--root", str(root), "--override-no-verdict"])), 1)
            self.assertEqual(_run_with_home(home, lambda: delegate.main([
                "apply", rec.name, "--root", str(root), "--override-no-verdict",
                "--reason", "emergency owner approval"])), 1)
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            self.assertFalse((root / "impl.py").exists())

    def test_apply_uses_latest_verdict_and_rejects_discard_decision(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            discarded = _json.loads((rec / "artifact" / "verdict-1.json").read_text())
            discarded["decision"] = "discard"
            (rec / "artifact" / "verdict-2.json").write_text(
                _json.dumps(discarded) + "\n", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: delegate.main([
                    "apply", rec.name, "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("latest verdict decision is discard", err.getvalue())
            self.assertFalse((root / "impl.py").exists())

    def test_apply_revalidates_directly_planted_verdict_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            verdict_path = rec / "artifact" / "verdict-1.json"
            verdict = _json.loads(verdict_path.read_text())
            verdict["criteria"][0]["evidence"] = ["fabricated-check"]
            verdict_path.write_text(_json.dumps(verdict) + "\n")

            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            self.assertIn("evidence", str(cm.exception))
            self.assertFalse((root / "impl.py").exists())
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")

    def test_apply_revalidates_verify_schema_before_blocker_gate(self):
        verifier_profile = (
            'schema: waystone-profile-1\nbindings:\n'
            '  implementer: {execution: external-runner, backend: "codex:gpt-test"}\n'
            '  verifier: {backend: "codex:gpt-test"}\n'
        )
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _write_profile(root, verifier_profile)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            invalid_verify = {
                "schema": "waystone-verify-1", "at": "2026-07-15T00:00:00+00:00",
                "transport": "codex-exec:read-only", "backend": "codex:gpt-test",
                "provenance": "independent-verifier",
                "payload": {"summary": "reviewed", "findings": [{"severity": "minor"}],
                            "limitations": []},
            }
            (rec / "artifact" / "verify-1.json").write_text(
                _json.dumps(invalid_verify) + "\n")
            _write_apply_verdict(rec)
            verdict_path = rec / "artifact" / "verdict-1.json"
            verdict = _json.loads(verdict_path.read_text())
            verdict["verify_number"] = 1
            verdict_path.write_text(_json.dumps(verdict) + "\n")

            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate.apply_delegation(root, rec.name))
            self.assertIn("verify artifact schema", str(cm.exception))
            self.assertFalse((root / "impl.py").exists())

    def test_discard_cli_requires_reason_and_records_it(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["discard", rec.name, "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("--reason", err.getvalue())
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            self.assertEqual(_run_with_home(home, lambda: delegate.main([
                "discard", rec.name, "--root", str(root),
                "--reason", "does not meet acceptance"])), 0)
            transition = delegate._read_status(rec)["at_transitions"][-1]
            self.assertEqual(transition["reason"], "does not meet acceptance")

    def test_discard_orphan_cleans_refs_and_cache_without_record(self):
        import shutil

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            did = rec.name
            wt = _run_with_home(home, lambda: delegate._worktree_path(root, did))
            shutil.rmtree(rec)
            self.assertTrue(wt.exists())
            for suffix in ("", "-result"):
                self.assertEqual(git(root, "rev-parse", "--verify",
                                     f"refs/waystone/delegations/{did}{suffix}").returncode, 0)
            self.assertEqual(_run_with_home(home, lambda: delegate.main([
                "discard", "--orphan", did, "--root", str(root),
                "--reason", "remove orphaned delegation storage"])), 0)
            self.assertFalse(wt.exists())
            for suffix in ("", "-result"):
                self.assertNotEqual(git(root, "rev-parse", "--verify",
                                        f"refs/waystone/delegations/{did}{suffix}").returncode, 0)

    def test_discard_records_intent_and_resumes_with_new_or_inherited_reason(self):
        for resume_reason, final_reason in (("updated conclusion", "updated conclusion"),
                                            (None, "review rejected")):
            with self.subTest(resume_reason=resume_reason), tempfile.TemporaryDirectory() as d:
                root, home = _deleg_project(d)
                _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
                rec = _latest_rec(root, home)
                original_cleanup = delegate._cleanup
                calls = {"value": 0}

                def interrupted(*args, **kwargs):
                    calls["value"] += 1
                    raise delegate.WorkflowError("injected cleanup interruption")

                delegate._cleanup = interrupted
                try:
                    with self.assertRaises(delegate.WorkflowError):
                        _run_with_home(home, lambda: delegate.discard_delegation(
                            root, rec.name, "review rejected"))
                finally:
                    delegate._cleanup = original_cleanup

                status = delegate._read_status(rec)
                self.assertEqual(status["state"], "discarding")
                self.assertEqual(status["at_transitions"][-1]["reason"], "review rejected")
                args = ["discard", rec.name, "--root", str(root)]
                if resume_reason is not None:
                    args += ["--reason", resume_reason]
                self.assertEqual(_run_with_home(home, lambda: delegate.main(args)), 0)
                transitions = delegate._read_status(rec)["at_transitions"]
                self.assertEqual([item["state"] for item in transitions[-2:]],
                                 ["discarding", "discarded"])
                self.assertEqual(transitions[-1]["reason"], final_reason)
                self.assertEqual(calls["value"], 1)

    def test_discard_orphan_is_project_locked_and_cleanup_failures_are_loud(self):
        import contextlib
        import fcntl
        import io
        import shutil

        for mode in ("git-failure", "lying-success"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                root, home = _deleg_project(d)
                _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
                rec = _latest_rec(root, home)
                did = rec.name
                shutil.rmtree(rec)
                original_git = delegate._git
                lock_observed = []

                def project_lock_held():
                    path = common.project_lock_path(root)
                    with path.open("a+", encoding="utf-8") as stream:
                        try:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            return True
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                        return False

                def injected_git(cwd, *args, **kwargs):
                    if args[:2] == ("update-ref", "-d"):
                        lock_observed.append(project_lock_held())
                        if mode == "git-failure":
                            return 1, "", "injected update-ref failure"
                        return 0, "", ""
                    return original_git(cwd, *args, **kwargs)

                delegate._git = injected_git
                err = io.StringIO()
                try:
                    with contextlib.redirect_stderr(err):
                        rc = _run_with_home(home, lambda: delegate.main([
                            "discard", "--orphan", did, "--root", str(root),
                            "--reason", "remove orphaned delegation storage"]))
                finally:
                    delegate._git = original_git
                self.assertEqual(rc, 1)
                self.assertTrue(lock_observed)
                self.assertTrue(all(lock_observed))
                self.assertIn("cleanup", err.getvalue())
                self.assertEqual(git(root, "rev-parse", "--verify",
                                     f"refs/waystone/delegations/{did}").returncode, 0)


class DelegateVerdictTests(unittest.TestCase):
    """0.9.0-c: verdict schema, G1-G5, overrides, and append-only artifacts."""

    _VERIFIER_PROFILE = (
        'schema: waystone-profile-1\nbindings:\n'
        '  implementer: {execution: external-runner, backend: "codex:gpt-test"}\n'
        '  verifier: {backend: "codex:gpt-test"}\n'
    )

    def _record(self, d, *, verifier=False):
        root, home = _deleg_project(d)
        if verifier:
            _write_profile(root, self._VERIFIER_PROFILE)
        _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
        return root, home, _latest_rec(root, home)

    def _payload(self, rec, *, decision="apply", decided_by="main-session", met=True,
                 checks=True, criterion=None, overrides=None):
        packet = yaml.safe_load((rec / "packet.yaml").read_text())
        criteria = [{
            "criterion": criterion if criterion is not None else item,
            "met": met,
            "evidence": ["agent_checks[0]"] if checks else ["verify-1#summary"],
        } for item in packet["acceptance"]]
        payload = {
            "schema": "waystone-verdict-1",
            "decision": decision,
            "decided_by": decided_by,
            "criteria": criteria,
            "agent_checks": ([{"cmd": "tests", "exit": 0, "summary": "passed"}]
                             if checks else []),
            "warnings_seen": [],
            "rationale": "evidence supports the decision",
            "limitations": [],
        }
        if overrides is not None:
            payload["overrides"] = overrides
        return payload

    def _input(self, d, payload):
        path = Path(d) / "verdict-input.json"
        path.write_text(_json.dumps(payload), encoding="utf-8")
        return path

    def _record_verdict(self, root, home, rec, path, *flags):
        return _run_with_home(home, lambda: delegate.main([
            "verdict", rec.name, "--file", str(path), "--root", str(root), *flags]))

    def _verify(self, rec, findings=None):
        contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
        exposure = _json.loads((rec / "exposure.json").read_text())
        artifact = {
            "schema": "waystone-verify-1",
            "at": "2026-07-15T00:00:00+00:00",
            "transport": "codex-exec:read-only",
            "backend": "codex:gpt-test",
            "provenance": "independent-verifier",
            "payload": {"summary": "reviewed", "findings": findings or [], "limitations": []},
            "profile_fingerprint": exposure["profile_fingerprint"],
            "base_sha": contract["base_sha"], "result_sha": contract["result_sha"],
            "patch_sha256": contract["patch_sha256"],
            "requested_effort": None, "effective_effort": None,
            "effective_tool_policy": {
                "tools": ["codex-exec"], "sandbox": "read-only", "bash": False,
                "filesystem_postcondition": "git-status+untracked-content-unchanged",
            },
        }
        (rec / "artifact" / "verify-1.json").write_text(
            _json.dumps(artifact) + "\n", encoding="utf-8")

    def test_schema_file_and_g1_wrong_state_refused(self):
        templates = SCRIPTS.parent / "templates"
        stored = _json.loads((templates / "verdict-schema.json").read_text())
        user_input = _json.loads((templates / "verdict-input-schema.json").read_text())
        self.assertEqual(stored["properties"]["schema"]["const"], "waystone-verdict-1")
        self.assertIn("agent_checks", user_input["required"])
        for field in ("judged_at", "provenance", "verify_number", "profile_fingerprint",
                      "artifact_digests"):
            self.assertIn(field, stored["required"])
            self.assertNotIn(field, user_input["properties"])
        with tempfile.TemporaryDirectory() as d:
            root, home, rec = self._record(d)
            delegate._set_state(rec, "running")
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate.record_verdict(
                    root, rec.name, self._input(d, self._payload(rec))))
            self.assertIn("only a needs-review delegation", str(cm.exception))

    def test_g2_requires_exact_criterion_set(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec = self._record(d)
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate.record_verdict(
                    root, rec.name,
                    self._input(d, self._payload(rec, criterion="criterion alpha"))))
            self.assertIn("exactly match", str(cm.exception))
            self.assertEqual(list((rec / "artifact").glob("verdict-*.json")), [])

    def test_g3_without_verifier_requires_agent_checks(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec = self._record(d)
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate.record_verdict(
                    root, rec.name, self._input(d, self._payload(rec, checks=False))))
            self.assertIn("agent_checks", str(cm.exception))
            self.assertEqual(self._record_verdict(
                root, home, rec, self._input(d, self._payload(rec))), 0)

    def test_g3_with_verifier_requires_verify_artifact_and_records_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec = self._record(d, verifier=True)
            path = self._input(d, self._payload(rec, checks=False))
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate.record_verdict(root, rec.name, path))
            self.assertIn("verify-N.json", str(cm.exception))
            self._verify(rec)
            self.assertEqual(_run_with_home(
                home, lambda: delegate.record_verdict(root, rec.name, path)), 0)
            verdict = _json.loads((rec / "artifact" / "verdict-1.json").read_text())
            exposure = _json.loads((rec / "exposure.json").read_text())
            self.assertEqual(verdict["provenance"], "main-session")
            self.assertEqual(verdict["verify_number"], 1)
            self.assertEqual(verdict["profile_fingerprint"], exposure["profile_fingerprint"])
            self.assertIn("judged_at", verdict)
            self.assertEqual(
                set(verdict["artifact_digests"]),
                {"contract_sha256", "patch_sha256", "verify_sha256"})
            self.assertIsNotNone(verdict["artifact_digests"]["verify_sha256"])
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertNotIn("verdict", contract)  # contract has no verdict

    def test_g3_uses_run_recorded_requirement_not_later_warning_rows(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec = self._record(d)
            warnings = overlay._warnings_path(root)
            warnings.parent.mkdir(parents=True, exist_ok=True)
            warnings.write_text(_json.dumps({
                "boundary": "delegate-run", "rule": "delegation-verification-evidence-v1",
                "event": "fire", "context": {"delegation_id": rec.name},
            }) + "\n")
            path = self._input(d, self._payload(rec))
            self.assertEqual(_run_with_home(
                home, lambda: delegate.record_verdict(root, rec.name, path)), 0)

    def test_g3_actual_run_atomically_records_verify_requirement_before_publication(self):
        import contextlib
        import fcntl
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/required",
                rule="delegation-verification-evidence-v1", summary="s"))
            original_runner = delegate._run_codex
            original_warn = delegate._warn_boundary
            observed = {}

            def checked_warn(project, boundary, context):
                rec = delegate._record_dir(project, context["delegation_id"])
                observed["state_during_rule"] = delegate._read_status(rec).get("state")
                with (rec / "record.lock").open("a+", encoding="utf-8") as stream:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        observed["record_lock_held"] = True
                    else:
                        observed["record_lock_held"] = False
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                return original_warn(project, boundary, context)

            delegate._run_codex = _deleg_fake({"impl.py": "x\n"})
            delegate._warn_boundary = checked_warn
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(_run_with_home(home, lambda: delegate.main([
                        "run", "feat/xyz", "--root", str(root)])), 0)
            finally:
                delegate._run_codex = original_runner
                delegate._warn_boundary = original_warn

            rec = _latest_rec(root, home)
            status = delegate._read_status(rec)
            self.assertTrue(observed["record_lock_held"])
            self.assertEqual(observed["state_during_rule"], "running")
            self.assertEqual(status["state"], "needs-review")
            self.assertIs(status["verification_required"], True)
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate.record_verdict(
                    root, rec.name, self._input(d, self._payload(rec))))
            self.assertIn("verify-N.json", str(cm.exception))

    def test_g3_verifier_binding_is_recorded_by_actual_run(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _write_profile(root, self._VERIFIER_PROFILE)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            self.assertIs(delegate._read_status(rec)["verification_required"], True)

    def test_agent_checks_and_met_evidence_are_semantically_nonempty(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec = self._record(d)
            for field in ("cmd", "summary"):
                payload = self._payload(rec)
                payload["agent_checks"][0][field] = " \t"
                with self.subTest(field=field), self.assertRaises(delegate.WorkflowError) as cm:
                    _run_with_home(home, lambda p=payload: delegate.record_verdict(
                        root, rec.name, self._input(d, p)))
                self.assertIn(field, str(cm.exception))

            for evidence in ([], ["fabricated"], ["agent_checks[9]"], ["verify-9#finding-0"]):
                payload = self._payload(rec)
                payload["criteria"][0]["evidence"] = evidence
                with self.subTest(evidence=evidence), self.assertRaises(delegate.WorkflowError) as cm:
                    _run_with_home(home, lambda p=payload: delegate.record_verdict(
                        root, rec.name, self._input(d, p)))
                self.assertIn("evidence", str(cm.exception))

            payload = self._payload(rec)
            payload["agent_checks"][0]["exit"] = 7
            self.assertEqual(_run_with_home(home, lambda: delegate.record_verdict(
                root, rec.name, self._input(d, payload))), 0)

    def test_verify_schema_and_numbering_are_fail_loud(self):
        valid = {
            "schema": "waystone-verify-1", "at": "2026-07-15T00:00:00+00:00",
            "transport": "codex-exec:read-only", "backend": "codex:gpt-test",
            "provenance": "independent-verifier",
            "payload": {"summary": "reviewed", "findings": [], "limitations": []},
            "profile_fingerprint": "sha256:123456789abc",
            "base_sha": "a" * 40, "result_sha": "b" * 40,
            "patch_sha256": "sha256:" + "c" * 64,
            "requested_effort": None, "effective_effort": None,
            "effective_tool_policy": {"tools": ["synthetic"]},
        }
        cases = (
            ("verify-final.json", valid, "non-canonical"),
            ("verify-2.json", valid, "contiguous"),
            ("verify-1.json", {**valid, "payload": {"findings": [{"severity": "minor"}]}},
             "schema"),
        )
        for name, artifact, needle in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                root, home, rec = self._record(d, verifier=True)
                (rec / "artifact" / name).write_text(_json.dumps(artifact) + "\n")
                with self.assertRaises(delegate.WorkflowError) as cm:
                    _run_with_home(home, lambda: delegate.record_verdict(
                        root, rec.name, self._input(d, self._payload(rec))))
                self.assertIn(needle, str(cm.exception))

    def test_verdict_numbering_rejects_sparse_and_noncanonical_files(self):
        for name, needle in (("verdict-7.json", "contiguous"),
                             ("verdict-final.json", "non-canonical"),
                             ("verdict-01.json", "non-canonical")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                root, home, rec = self._record(d)
                (rec / "artifact" / name).write_text("{}\n")
                with self.assertRaises(delegate.WorkflowError) as cm:
                    _run_with_home(home, lambda: delegate.record_verdict(
                        root, rec.name, self._input(d, self._payload(rec, decision="discard"))))
                self.assertIn(needle, str(cm.exception))

    def test_g4_blocker_and_main_session_refuted_by_gate(self):
        blocker = {"title": "false positive", "severity": "blocker", "evidence": "x",
                   "recommendation": "change it"}
        with tempfile.TemporaryDirectory() as d:
            root, home, rec = self._record(d, verifier=True)
            self._verify(rec, [blocker])
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate.record_verdict(
                    root, rec.name, self._input(d, self._payload(rec))))
            self.assertIn("unresolved blocker", str(cm.exception))
            self.assertEqual(self._record_verdict(
                root, home, rec, self._input(d, self._payload(
                    rec, overrides=[{"refuted_by": [0]}])), "--override-blocker"), 1)
            for overrides in (None, [{"refuted_by": []}]):
                path = self._input(d, self._payload(rec, overrides=overrides))
                with self.assertRaises(delegate.WorkflowError) as cm:
                    _run_with_home(home, lambda p=path: delegate.record_verdict(
                        root, rec.name, p, override_blocker_reason="reviewed false positive"))
                self.assertIn("refuted_by", str(cm.exception))
            path = self._input(d, self._payload(rec, overrides=[{"refuted_by": [0]}]))
            self.assertEqual(self._record_verdict(
                root, home, rec, path, "--override-blocker", "--reason",
                "reviewed false positive"), 0)
            verdict = _json.loads((rec / "artifact" / "verdict-1.json").read_text())
            self.assertEqual(verdict["overrides"][0]["gate"], "blocker")
            self.assertEqual(verdict["overrides"][0]["finding_index"], 0)
            self.assertEqual(verdict["overrides"][0]["refuted_by"], [0])

    def test_g4_user_blocker_override_also_requires_concrete_refutation(self):
        blocker = {"title": "known risk", "severity": "blocker", "evidence": "x",
                   "recommendation": "change it"}
        with tempfile.TemporaryDirectory() as d:
            root, home, rec = self._record(d, verifier=True)
            self._verify(rec, [blocker])
            payload = self._payload(rec, decided_by="user")
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate.record_verdict(
                    root, rec.name, self._input(d, payload),
                    override_blocker_reason="user accepts known risk"))
            self.assertIn("refuted_by", str(cm.exception))
            payload["overrides"] = [{"refuted_by": [0]}]
            self.assertEqual(_run_with_home(home, lambda: delegate.record_verdict(
                root, rec.name, self._input(d, payload),
                override_blocker_reason="user accepts known risk")), 0)
            verdict = _json.loads((rec / "artifact" / "verdict-1.json").read_text())
            self.assertEqual(verdict["overrides"][0]["reason"], "user accepts known risk")
            self.assertEqual(verdict["overrides"][0]["refuted_by"], [0])

    def test_g5_unmet_blocks_apply_and_override_records_reason(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec = self._record(d)
            payload = self._payload(rec, met=False)
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate.record_verdict(
                    root, rec.name, self._input(d, payload)))
            self.assertIn("unmet", str(cm.exception))
            self.assertEqual(self._record_verdict(
                root, home, rec, self._input(d, payload), "--override-unmet", "--reason",
                "criterion waived by owner"), 0)
            verdict = _json.loads((rec / "artifact" / "verdict-1.json").read_text())
            self.assertEqual(verdict["overrides"][0]["gate"], "unmet")
            self.assertEqual(verdict["overrides"][0]["reason"], "criterion waived by owner")

    def test_verdict_numbering_never_overwrites_and_state_does_not_change(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec = self._record(d)
            payload = self._payload(rec, decision="discard")
            self.assertEqual(_run_with_home(home, lambda: delegate.record_verdict(
                root, rec.name, self._input(d, payload))), 0)
            first = (rec / "artifact" / "verdict-1.json").read_bytes()
            self.assertEqual(_run_with_home(home, lambda: delegate.record_verdict(
                root, rec.name, self._input(d, payload))), 0)
            self.assertEqual((rec / "artifact" / "verdict-1.json").read_bytes(), first)
            self.assertTrue((rec / "artifact" / "verdict-2.json").is_file())
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")


class DelegateCorruptRecordTests(unittest.TestCase):
    """H3 — corrupt record JSON fails safe (named file, lock held, list survives), never a traceback."""

    def test_owner_lock_scan_fail_safe_on_corrupt_status(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            (rec / "status.json").write_text("{ corrupt", encoding="utf-8")
            with self.assertRaises(delegate.WorkflowError) as cm:
                _deleg_run(root, home, _deleg_fake({"impl.py": "y\n"}))
            msg = str(cm.exception)
            self.assertIn("corrupt", msg)
            self.assertIn("discard", msg)  # the clearing path is named

    def test_status_list_marks_corrupt_row_and_keeps_healthy(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks:\n"
                '  - id: feat/xyz\n    title: "implement xyz feature"\n    status: active\n'
                '    accept:\n      - "criterion alpha here"\n'
                '  - id: feat/two\n    title: "the second task here"\n    status: active\n'
                '    accept:\n      - "criterion beta here"\n')
            orig = delegate._make_did
            try:
                delegate._make_did = lambda tid: "20260713T000001Z-" + tid.replace("/", "-")
                _deleg_run(root, home, _deleg_fake({"a.py": "x\n"}), task="feat/xyz")
                delegate._make_did = lambda tid: "20260713T000002Z-" + tid.replace("/", "-")
                _deleg_run(root, home, _deleg_fake({"b.py": "y\n"}), task="feat/two")
            finally:
                delegate._make_did = orig
            recs = _run_with_home(home, lambda: sorted(delegate._delegations_dir(root).iterdir()))
            (recs[0] / "status.json").write_text("{ corrupt", encoding="utf-8")
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = _run_with_home(home, lambda: delegate.main(["status", "--root", str(root)]))
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("[corrupt]", out)          # the broken row is surfaced, not fatal
            self.assertIn("feat/two", out)           # ...and the healthy row still renders
            self.assertIn("needs-review", out)

    def test_show_corrupt_status_exit1(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            (rec / "status.json").write_text("{ corrupt", encoding="utf-8")
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: delegate.main(["show", rec.name, "--root", str(root)]))
            self.assertEqual(rc, 1)                  # WorkflowError, never a traceback
            self.assertIn("status.json", err.getvalue())

    def test_apply_corrupt_contract_exit1(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            _write_apply_verdict(rec)
            (rec / "artifact" / "contract.yaml").write_text("{invalid: [unclosed", encoding="utf-8")
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: delegate.main(["apply", rec.name, "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("contract.yaml", err.getvalue())
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")  # unchanged

    def test_discard_accepts_corrupt_record(self):
        # the cleanup path must not block itself on the very corruption it is meant to clear
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            wt = _run_with_home(home, lambda: delegate._worktree_path(root, rec.name))
            (rec / "status.json").write_text("{ corrupt", encoding="utf-8")
            (rec / "exposure.json").write_text("{ corrupt", encoding="utf-8")
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(
                    home, lambda: delegate.discard_delegation(root, rec.name, "clear corrupt record"))
            self.assertEqual(rc, 0)
            self.assertEqual(delegate._read_status(rec)["state"], "discarded")
            self.assertFalse(wt.exists())
