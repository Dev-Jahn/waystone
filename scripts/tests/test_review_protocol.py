"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


class LockPrimitiveTests(unittest.TestCase):
    def test_hold_lock_records_diagnostics_and_never_unlinks_marker(self):
        import json as _json
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "state" / "lock"
            with mock.patch.dict(os.environ, {"WAYSTONE_HOST": "codex"}, clear=False), \
                    mock.patch.object(sys, "argv", ["waystone.py", "round", "close"]):
                with common.hold_lock(lock, timeout=0.2):
                    holder = _json.loads(lock.read_text())
                    self.assertEqual(holder["pid"], os.getpid())
                    self.assertEqual(holder["host"], "codex")
                    self.assertEqual(holder["verb"], "round close")
                    self.assertIn("at", holder)
            self.assertTrue(lock.is_file())

    def test_hold_lock_timeout_reports_holder_and_env_default(self):
        import fcntl
        import json as _json
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "lock"
            holder = {"pid": 4242, "host": "codex", "verb": "round close",
                      "at": "2026-07-15T12:03:11+00:00"}
            stream = lock.open("a+", encoding="utf-8")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                stream.write(_json.dumps(holder) + "\n")
                stream.flush()
                with mock.patch.dict(os.environ, {"WAYSTONE_LOCK_TIMEOUT": "0.02"}, clear=False):
                    with self.assertRaises(common.WorkflowError) as cm:
                        with common.hold_lock(lock):
                            self.fail("contended lock must never enter the protected section")
                message = str(cm.exception)
                self.assertIn(str(lock), message)
                self.assertIn("pid 4242", message)
                self.assertIn("codex, round close, since 12:03:11", message)
                self.assertIn("raise WAYSTONE_LOCK_TIMEOUT", message)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()


class LockWiringTests(unittest.TestCase):
    def test_task_set_times_out_with_holder_details_and_no_write(self):
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            home = Path(d) / "home"
            root.mkdir()
            home.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            before = (root / "tasks.yaml").read_bytes()
            lock = common.project_state_path(root) / "lock"
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "WAYSTONE_HOME": str(home / ".waystone"),
                "WAYSTONE_HOST": "codex",
                "WAYSTONE_LOCK_TIMEOUT": "0.2",
            })
            with mock.patch.dict(os.environ, {"WAYSTONE_HOST": "codex"}, clear=False), \
                    mock.patch.object(sys, "argv", ["waystone.py", "round", "close"]), \
                    common.hold_lock(lock, timeout=0.2):
                result = subprocess.run([
                    sys.executable, str(SCRIPTS / "waystone.py"), "task", "set",
                    "feat/alpha", "status", "done", str(root),
                ], capture_output=True, text=True, env=env, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn(str(lock), result.stderr)
            self.assertIn("pid ", result.stderr)
            self.assertIn("codex, round close", result.stderr)
            self.assertEqual((root / "tasks.yaml").read_bytes(), before)

    def test_project_register_times_out_on_registry_lock(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            home = Path(d) / "home"
            root.mkdir()
            home.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: x\ntasks: []\n")
            lock = home / ".waystone" / "registry.lock"
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "WAYSTONE_HOME": str(home / ".waystone"),
                "WAYSTONE_LOCK_TIMEOUT": "0.02",
            })
            with common.hold_lock(lock, timeout=0.2):
                result = subprocess.run([
                    sys.executable, str(SCRIPTS / "waystone.py"),
                    "project", "register", str(root),
                ], capture_output=True, text=True, env=env, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn("registry.lock is held by pid", result.stderr)
            self.assertFalse((home / ".waystone" / "projects.json").exists())


class MarkerTests(unittest.TestCase):
    def test_emit_parse_roundtrip(self):
        s = review.emit_marker("review-cycle", {"round_id": "2026-06-15-x", "cycle": 1,
                                                   "target_sha": "a" * 40, "reviewers": ["codex", "gpt-5.5-pro"]})
        got = review.parse_markers(s)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["_kind"], "review-cycle")
        self.assertEqual(got[0]["cycle"], 1)
        self.assertEqual(got[0]["target_sha"], "a" * 40)

    def test_latest_and_next_cycle(self):
        text = (review.emit_marker("review-cycle", {"cycle": 1, "target_sha": "a" * 40})
                + "\n" + review.emit_marker("review-cycle", {"cycle": 2, "target_sha": "b" * 40}))
        ms = review.parse_markers(text)
        self.assertEqual(review.latest_cycle(ms)["cycle"], 2)
        self.assertEqual(review.next_cycle_number(ms), 3)
        self.assertEqual(review.next_cycle_number([]), 1)

    def test_next_cycle_uses_only_trusted_operator_markers(self):
        bodies = [
            {"body": review.emit_marker("review-cycle", {
                "cycle": 2, "target_sha": "a" * 40,
            }), "author": "owner"},
            {"body": review.emit_marker("review-cycle", {
                "cycle": 99, "target_sha": "b" * 40,
            }), "author": "attacker"},
        ]
        markers = review.parse_bodies(bodies)
        self.assertEqual(review.next_cycle_number(markers, ("owner",)), 3)

    def test_freeze_ignores_untrusted_high_cycle_marker(self):
        from unittest import mock
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, target, round_id = _pr_prepared_round(Path(d), "macro-reviewer")
            policy = common.load_config(root)
            attacker_marker = review.emit_marker("review-cycle", {
                "cycle": 99, "target_sha": "f" * 40,
            })
            posted = []
            context = {
                "repo": "owner/repo", "pr": 9,
                "bundle": {
                    "head": target, "base_sha": "b" * 40,
                    "bodies": [{"body": attacker_marker, "author": "attacker"}],
                },
                "head": target, "base_sha": "b" * 40, "base": "main",
                "policy": policy,
            }
            with mock.patch.object(review, "pr_context", return_value=context), \
                    mock.patch.object(
                        review, "_gh",
                        side_effect=lambda _root, *args: (posted.append(args) or (0, "ok"))), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.freeze(root, 9, round_id), 0)
            body = posted[0][posted[0].index("--body") + 1]
            self.assertEqual(
                review.parse_markers(body, "review-cycle")[0]["cycle"], 1)

    def test_classify_fresh_vs_stale(self):
        head = "b" * 40
        # cycle frozen at a different sha => stale
        ms = review.parse_markers(review.emit_marker("review-cycle", {"cycle": 1, "target_sha": "a" * 40}))
        self.assertFalse(review.classify(ms, head)["cycle_fresh"])
        # frozen at head => fresh
        ms = review.parse_markers(review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}))
        self.assertTrue(review.classify(ms, head)["cycle_fresh"])

    def _bodies(self, head, *, reviewer="gpt-5.5-pro", cycle=1, verdict="shipped",
                approver="owner", decision=None):
        return [
            {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner", "at": "2026-06-01T00:00:00Z"},
            {"body": review.emit_marker("review-result", {"reviewer": reviewer, "review_cycle": cycle,
                                                             "reviewed_sha": head, "verdict": verdict,
                                                             "decision_required": decision or []}), "author": reviewer, "at": "2026-06-01T01:00:00Z"},
            {"body": review.emit_marker("approval", {"sha": head, "cycle": 1, "by": approver}), "author": approver, "at": "2026-06-01T03:00:00Z"},
            {"body": review.emit_marker("findings", {"cycle": 1, "resolved": True}), "author": "owner", "at": "2026-06-01T02:00:00Z"},
        ]

    def test_classify_valid_binding(self):
        head = "c" * 40
        c = review.classify(review.parse_bodies(self._bodies(head)), head,
                               macro_reviewers=("gpt-5.5-pro",), approvers=("owner",))
        self.assertTrue(c["pro_result_at_head"])
        self.assertTrue(c["approved_at_head"])
        self.assertTrue(c["findings_resolved"])
        # different head invalidates all three (SHA-binding)
        c2 = review.classify(review.parse_bodies(self._bodies(head)), "d" * 40,
                                macro_reviewers=("gpt-5.5-pro",), approvers=("owner",))
        self.assertFalse(c2["pro_result_at_head"])
        self.assertFalse(c2["approved_at_head"])

    def test_classify_rejects_bad_provenance(self):
        head = "c" * 40
        mr, ap = ("gpt-5.5-pro",), ("owner",)
        # wrong reviewer
        c = review.classify(review.parse_bodies(self._bodies(head, reviewer="random-user")), head, macro_reviewers=mr, approvers=ap)
        self.assertFalse(c["pro_result_at_head"])
        # wrong cycle (result for cycle 99, latest is 1)
        c = review.classify(review.parse_bodies(self._bodies(head, cycle=99)), head, macro_reviewers=mr, approvers=ap)
        self.assertFalse(c["pro_result_at_head"])
        # not-shipped verdict
        c = review.classify(review.parse_bodies(self._bodies(head, verdict="not-shipped")), head, macro_reviewers=mr, approvers=ap)
        self.assertFalse(c["pro_result_at_head"])
        # decision required
        c = review.classify(review.parse_bodies(self._bodies(head, decision=["stop"])), head, macro_reviewers=mr, approvers=ap)
        self.assertFalse(c["pro_result_at_head"])
        # approval by untrusted author
        c = review.classify(review.parse_bodies(self._bodies(head, approver="anyone")), head, macro_reviewers=mr, approvers=ap)
        self.assertFalse(c["approved_at_head"])

    def test_fenced_marker_ignored(self):
        head = "c" * 40
        fenced = "```yaml\n" + review.emit_marker("approval", {"sha": head, "by": "owner"}) + "\n```"
        self.assertEqual(review.parse_markers(fenced), [])
        c = review.classify(review.parse_bodies([{"body": fenced, "author": "owner"}]), head, approvers=("owner",))
        self.assertFalse(c["approved_at_head"])

    def test_findings_resolved_strict_bool(self):
        # a non-True 'resolved' (e.g. arbitrary string) must not count as resolved
        m = review.parse_markers(review.emit_marker("findings", {"cycle": 1, "resolved": "maybe"}))
        c = review.classify([{"_kind": "review-cycle", "cycle": 1, "target_sha": "x"}, *m], "x")
        self.assertFalse(c["findings_resolved"])

    def test_ci_strict(self):
        for bad in ("ACTION_REQUIRED", "NEUTRAL", "SKIPPED", "STALE", "WHATEVER"):
            self.assertEqual(review.ci_state({"checks": [{"conclusion": bad}]}), "failing", bad)
        self.assertEqual(review.ci_state({"checks": [{"conclusion": "SUCCESS"}]}), "passing")
        self.assertEqual(review.ci_state({"checks": [{"conclusion": "PENDING"}]}), "pending")

    def _op_bodies(self, head, *, result_author="owner", findings_author="owner",
                   cycle_author="owner", reviewer="gpt-5.5-pro", cycle=1, verdict="shipped",
                   approver="owner", resolved=True):
        """Bodies where the GitHub author (who POSTED) is distinct from the logical reviewer id —
        the realistic PR-mode case (a human operator posts the macro reviewer's reply)."""
        return [
            {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": cycle_author, "at": "2026-06-01T00:00:00Z"},
            {"body": review.emit_marker("review-result", {"reviewer": reviewer, "review_cycle": cycle,
                "reviewed_sha": head, "verdict": verdict, "decision_required": []}), "author": result_author, "at": "2026-06-01T01:00:00Z"},
            {"body": review.emit_marker("approval", {"sha": head, "cycle": 1, "by": approver}), "author": approver, "at": "2026-06-01T03:00:00Z"},
            {"body": review.emit_marker("findings", {"cycle": 1, "resolved": resolved}), "author": findings_author, "at": "2026-06-01T02:00:00Z"},
        ]

    def test_classify_operator_provenance(self):
        head = "e" * 40
        ops, mr, ap = ("owner",), ("gpt-5.5-pro",), ("owner",)
        c = review.classify(review.parse_bodies(self._op_bodies(head)), head,
                               macro_reviewers=mr, approvers=ap, operators=ops)
        self.assertTrue(c["pro_result_at_head"])
        self.assertTrue(c["findings_resolved"])
        self.assertTrue(c["cycle_fresh"])
        # a non-operator forging the macro result (still claiming reviewer gpt-5.5-pro) is ignored
        c = review.classify(review.parse_bodies(self._op_bodies(head, result_author="attacker")),
                               head, macro_reviewers=mr, approvers=ap, operators=ops)
        self.assertFalse(c["pro_result_at_head"])
        # a non-operator forging findings-resolved is ignored
        c = review.classify(review.parse_bodies(self._op_bodies(head, findings_author="attacker")),
                               head, macro_reviewers=mr, approvers=ap, operators=ops)
        self.assertFalse(c["findings_resolved"])
        # a non-operator can't hijack the latest cycle with a higher-numbered freeze
        bodies = self._op_bodies(head)
        bodies.append({"body": review.emit_marker("review-cycle", {"cycle": 9, "target_sha": "f" * 40}),
                       "author": "attacker"})
        c = review.classify(review.parse_bodies(bodies), head, macro_reviewers=mr, approvers=ap, operators=ops)
        self.assertEqual(c["latest_cycle"], 1)
        self.assertTrue(c["cycle_fresh"])

    def test_approval_by_must_match_author(self):
        head = "e" * 40
        # an approval whose claimed `by` differs from who actually posted it is rejected
        bodies = [{"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner"},
                  {"body": review.emit_marker("approval", {"sha": head, "cycle": 1, "by": "owner"}), "author": "impersonator"}]
        c = review.classify(review.parse_bodies(bodies), head, approvers=("owner", "impersonator"))
        self.assertFalse(c["approved_at_head"])

    def test_cycle_conflict_fails_closed(self):
        head = "e" * 40
        # two operator freeze markers for the same latest cycle, different SHA → not fresh
        bodies = [
            {"body": review.emit_marker("review-cycle", {"cycle": 2, "target_sha": head}), "author": "owner"},
            {"body": review.emit_marker("review-cycle", {"cycle": 2, "target_sha": "f" * 40}), "author": "owner"},
        ]
        c = review.classify(review.parse_bodies(bodies), head, operators=("owner",))
        self.assertTrue(c["cycle_conflict"])
        self.assertFalse(c["cycle_fresh"])

    def test_cycle_digest_conflict_fails_closed(self):
        head = "e" * 40
        bodies = [
            {"body": review.emit_marker("review-cycle", {
                "cycle": 2, "target_sha": head,
                "rendered_request_digest": "sha256:" + "a" * 64,
            }, version=2), "author": "owner"},
            {"body": review.emit_marker("review-cycle", {
                "cycle": 2, "target_sha": head,
                "rendered_request_digest": "sha256:" + "b" * 64,
            }, version=2), "author": "owner"},
        ]
        c = review.classify(review.parse_bodies(bodies), head, operators=("owner",))
        self.assertTrue(c["cycle_conflict"])
        self.assertFalse(c["cycle_fresh"])

    def test_cycle_v1_v2_mix_is_version_skew_not_digest_conflict(self):
        head = "e" * 40
        bodies = [
            {"body": review.emit_marker("review-cycle", {
                "cycle": 2, "target_sha": head,
            }), "author": "owner", "at": "2026-07-19T00:00:01Z"},
            {"body": review.emit_marker("review-cycle", {
                "cycle": 2, "target_sha": head,
                "rendered_request_digest": TEST_RENDERED_REQUEST_DIGEST,
            }, version=2), "author": "owner", "at": "2026-07-19T00:00:00Z"},
        ]
        c = review.classify(review.parse_bodies(bodies), head, operators=("owner",))
        self.assertFalse(c["cycle_conflict"])
        self.assertTrue(c["cycle_version_skew"])
        self.assertIsNone(c["rendered_request_digest"])
        self.assertEqual(c.get("cycle_version_skew_reason"), "latest-v1-supersedes-v2")
        self.assertEqual(c["cycle_marker_version"], 1)
        self.assertFalse(c["cycle_fresh"])

    def test_cycle_v1_then_later_v2_keeps_digest_authority(self):
        head = "e" * 40
        bodies = [
            {"body": review.emit_marker("review-cycle", {
                "cycle": 2, "target_sha": head,
            }), "author": "owner", "at": "2026-07-19T00:00:00Z"},
            {"body": review.emit_marker("review-cycle", {
                "cycle": 2, "target_sha": head,
                "rendered_request_digest": TEST_RENDERED_REQUEST_DIGEST,
            }, version=2), "author": "owner", "at": "2026-07-19T00:00:01Z"},
        ]
        c = review.classify(review.parse_bodies(bodies), head, operators=("owner",))
        self.assertFalse(c["cycle_conflict"])
        self.assertTrue(c["cycle_version_skew"])
        self.assertIsNone(c.get("cycle_version_skew_reason"))
        self.assertEqual(c["cycle_marker_version"], 2)
        self.assertEqual(c["rendered_request_digest"], TEST_RENDERED_REQUEST_DIGEST)
        self.assertTrue(c["cycle_fresh"])

    def test_cycle_v1_v2_same_timestamp_fails_closed(self):
        head = "e" * 40
        bodies = [
            {"body": review.emit_marker("review-cycle", {
                "cycle": 2, "target_sha": head,
            }), "author": "owner", "at": "2026-07-19T00:00:00Z"},
            {"body": review.emit_marker("review-cycle", {
                "cycle": 2, "target_sha": head,
                "rendered_request_digest": TEST_RENDERED_REQUEST_DIGEST,
            }, version=2), "author": "owner", "at": "2026-07-19T00:00:00+00:00"},
        ]
        c = review.classify(review.parse_bodies(bodies), head, operators=("owner",))
        self.assertTrue(c["cycle_conflict"])
        self.assertTrue(c["cycle_version_skew"])
        self.assertEqual(c.get("cycle_version_skew_reason"), "v1-v2-timestamp-tie")
        self.assertIsNone(c["cycle_marker_version"])
        self.assertIsNone(c["rendered_request_digest"])
        self.assertFalse(c["cycle_fresh"])

    def test_cycle_marker_schema_requires_digest_exactly_in_v2(self):
        v1_with_digest = review.parse_markers(review.emit_marker("review-cycle", {
            "cycle": 1, "target_sha": "a" * 40,
            "rendered_request_digest": TEST_RENDERED_REQUEST_DIGEST,
        }))[0]
        v2_without_digest = review.parse_markers(review.emit_marker("review-cycle", {
            "cycle": 1, "target_sha": "a" * 40,
        }, version=2))[0]
        self.assertFalse(review.marker_valid(v1_with_digest))
        self.assertFalse(review.marker_valid(v2_without_digest))

    def test_findings_latest_trusted_state_reblocks(self):
        head = "e" * 40
        # an earlier resolved:true followed by a later resolved:false must re-block
        bodies = [
            {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner"},
            {"body": review.emit_marker("findings", {"cycle": 1, "resolved": True}), "author": "owner", "at": "2026-06-19T01:00:00Z"},
            {"body": review.emit_marker("findings", {"cycle": 1, "resolved": False}), "author": "owner", "at": "2026-06-19T02:00:00Z"},
        ]
        c = review.classify(review.parse_bodies(bodies), head, operators=("owner",))
        self.assertFalse(c["findings_resolved"])

    def test_codex_fresh_commit_binding(self):
        head = "a" * 40
        # (1) formal review whose commit_id == head
        self.assertTrue(review.codex_fresh(
            [{"author": review.CODEX_BOT, "commit_id": head, "state": "COMMENTED"}], [], head))
        # a review of a DIFFERENT commit does not count for this head
        self.assertFalse(review.codex_fresh(
            [{"author": review.CODEX_BOT, "commit_id": "b" * 40, "state": "COMMENTED"}], [], head))
        # a non-codex author does not count (formal-review path)
        self.assertFalse(review.codex_fresh(
            [{"author": "someone", "commit_id": head, "state": "APPROVED"}], [], head))
        # (2) the connector's no-issue COMMENT naming the head short-SHA counts (real codex path).
        # GraphQL (gh pr view) drops the [bot] suffix — must still match.
        comment = {"author": "chatgpt-codex-connector", "body": f"Codex Review: no issues.\nReviewed commit: `{head[:10]}`"}
        self.assertTrue(review.codex_fresh([], [comment], head))
        # a codex comment naming a DIFFERENT (old) head does not count
        stale = {"author": review.CODEX_BOT, "body": "Reviewed commit: `" + ("b" * 10) + "`"}
        self.assertFalse(review.codex_fresh([], [stale], head))
        # a non-codex commenter naming the head can't forge it (login is GitHub-verified)
        forged = {"author": "attacker", "body": f"Reviewed commit: `{head[:10]}`"}
        self.assertFalse(review.codex_fresh([], [forged], head))
        # nothing at all (bare 👍 reaction) → fail-closed
        self.assertFalse(review.codex_fresh([], [], head))

    def test_file_at_ref_uses_explicit_get(self):
        import base64 as _b64
        captured = {}

        def fake_gh(root, *args):
            captured["args"] = args
            return (0, _b64.b64encode(b"hello: world\n").decode())

        orig = review._gh
        review._gh = fake_gh
        try:
            out = review.file_at_ref(Path("/x"), "o/r", "tasks.yaml", "sha123")
        finally:
            review._gh = orig
        self.assertEqual(out, "hello: world\n")
        self.assertIn("--method", captured["args"])
        self.assertEqual(captured["args"][captured["args"].index("--method") + 1], "GET")

    def test_base_sha_binding(self):
        # B4: a cycle frozen at (head H, base B1) is stale once the base moves to B2
        head = "f" * 40
        cyc = {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head, "base_sha": "b1" + "0" * 38}),
               "author": "owner"}
        ms = review.parse_bodies([cyc])
        self.assertTrue(review.classify(ms, head, operators=("owner",), current_base="b1" + "0" * 38)["cycle_fresh"])
        self.assertFalse(review.classify(ms, head, operators=("owner",), current_base="b2" + "0" * 38)["cycle_fresh"])

    def test_result_uses_latest_not_any(self):
        # B2: a later not-shipped (with a stop decision) cancels an earlier shipped
        head = "c" * 40
        bodies = [
            {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner"},
            {"body": review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": []}),
             "author": "owner", "at": "2026-06-19T01:00:00Z"},
            {"body": review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "not-shipped", "decision_required": ["stop"]}),
             "author": "owner", "at": "2026-06-19T02:00:00Z"},
        ]
        c = review.classify(review.parse_bodies(bodies), head, macro_reviewers=("gpt-5.5-pro",), operators=("owner",))
        self.assertFalse(c["pro_result_at_head"])

    def test_all_macro_reviewers_required(self):
        # B2: with two configured macro reviewers, one passing result is not enough
        head = "c" * 40
        bodies = [
            {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner"},
            {"body": review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": []}), "author": "owner"},
        ]
        c = review.classify(review.parse_bodies(bodies), head,
                               macro_reviewers=("gpt-5.5-pro", "other-reviewer"), operators=("owner",))
        self.assertFalse(c["pro_result_at_head"])  # 'other-reviewer' has no result

    def test_new_codex_signal_reblocks_findings_and_approval(self):
        # B3: a Codex signal newer than the findings resolution / approval re-blocks both
        head = "c" * 40
        bodies = [
            {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner"},
            {"body": review.emit_marker("findings", {"cycle": 1, "resolved": True}), "author": "owner", "at": "2026-06-19T01:00:00Z"},
            {"body": review.emit_marker("approval", {"sha": head, "cycle": 1, "by": "owner"}), "author": "owner", "at": "2026-06-19T01:30:00Z"},
        ]
        ms = review.parse_bodies(bodies)
        # no later codex signal → both hold
        ok = review.classify(ms, head, approvers=("owner",), operators=("owner",), codex_signal_at=None)
        self.assertTrue(ok["findings_resolved"]); self.assertTrue(ok["approved_at_head"])
        # a Codex signal at T03:00 (after resolution & approval) → both go stale
        blocked = review.classify(ms, head, approvers=("owner",), operators=("owner",),
                                     codex_signal_at="2026-06-19T03:00:00Z")
        self.assertFalse(blocked["findings_resolved"]); self.assertFalse(blocked["approved_at_head"])

    def test_codex_comment_negative_context_not_fresh(self):
        # M5: a SHA appearing in prose (not the 'Reviewed commit' field) must not count
        head = "1234567890" + "a" * 30
        neg = {"author": "chatgpt-codex-connector",
               "body": f"Reviewed commit: `deadbeef00`.\nI did NOT review {head[:10]}; rerun required."}
        self.assertFalse(review.codex_fresh([], [neg], head))
        pos = {"author": "chatgpt-codex-connector", "body": f"**Reviewed commit:** `{head[:10]}`"}
        self.assertTrue(review.codex_fresh([], [pos], head))

    def test_ci_completed_not_passing(self):
        # M7: COMPLETED is a run status, not a success conclusion
        self.assertEqual(review.ci_state({"checks": [{"conclusion": "COMPLETED"}]}), "failing")
        self.assertEqual(review.ci_state({"checks": [{"state": "COMPLETED"}]}), "failing")
        self.assertEqual(review.ci_state({"checks": [{"conclusion": "SUCCESS"}, {"conclusion": "COMPLETED"}]}), "failing")

    def test_rest_reviews_flattens_slurped_pages(self):
        # M6: --slurp returns an array of per-page arrays; rest_reviews must flatten them
        import json as _json
        pages = [[{"id": 1, "user": {"login": "a"}, "commit_id": "x", "state": "COMMENTED", "submitted_at": "t1"}],
                 [{"id": 2, "user": {"login": "b"}, "commit_id": "y", "state": "APPROVED", "submitted_at": "t2"}]]
        orig = review._gh
        review._gh = lambda root, *a: (0, _json.dumps(pages))
        try:
            out = review.rest_reviews(Path("/x"), "o/r", 5)
        finally:
            review._gh = orig
        self.assertEqual([r["id"] for r in out], [1, 2])
        self.assertEqual(out[1]["author"], "b")

    # ---- v0.2.5: cycle-bound evidence (no reuse across a re-freeze) ----
    def test_old_codex_signal_stale_after_refreeze(self):
        head = "f" * 40
        reviews = [{"author": review.CODEX_BOT, "commit_id": head, "state": "COMMENTED",
                    "at": "2026-06-19T01:00:00Z", "id": 1}]
        self.assertTrue(review.codex_fresh(reviews, [], head))  # no freeze gate → counts
        # re-freeze at a later time → the pre-freeze Codex review no longer counts
        self.assertEqual(review.codex_signals_at_head(reviews, [], head, since_at="2026-06-20T05:00:00Z"), [])

    def test_old_approval_rejected_for_new_cycle_and_base(self):
        head, B2 = "f" * 40, "b2" + "0" * 38
        cyc2 = {"body": review.emit_marker("review-cycle", {"cycle": 2, "target_sha": head, "base_sha": B2}),
                "author": "owner", "at": "2026-06-20T05:00:00Z"}
        old = {"body": review.emit_marker("approval", {"sha": head, "cycle": 1, "by": "owner"}),
               "author": "owner", "at": "2026-06-19T02:00:00Z"}  # cycle 1, no base
        c = review.classify(review.parse_bodies([cyc2, old]), head,
                               approvers=("owner",), operators=("owner",), current_base=B2)
        self.assertFalse(c["approved_at_head"])
        # a fresh approval bound to (cycle 2, head, base B2) is accepted
        new = {"body": review.emit_marker("approval", {"sha": head, "base_sha": B2, "cycle": 2, "by": "owner"}),
               "author": "owner", "at": "2026-06-20T06:00:00Z"}
        c2 = review.classify(review.parse_bodies([cyc2, new]), head,
                                approvers=("owner",), operators=("owner",), current_base=B2)
        self.assertTrue(c2["approved_at_head"])

    def test_approval_before_evidence_invalid(self):
        head = "c" * 40
        bodies = [
            {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner", "at": "2026-06-19T00:00:00Z"},
            {"body": review.emit_marker("approval", {"sha": head, "cycle": 1, "by": "owner"}),
             "author": "owner", "at": "2026-06-19T01:00:00Z"},  # approved early
            {"body": review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": []}),
             "author": "owner", "at": "2026-06-19T02:00:00Z"},  # evidence arrived later
        ]
        c = review.classify(review.parse_bodies(bodies), head,
                               macro_reviewers=("gpt-5.5-pro",), approvers=("owner",), operators=("owner",))
        self.assertTrue(c["pro_result_at_head"])
        self.assertFalse(c["approved_at_head"])  # approval predates the result it claims to clear

    def test_same_timestamp_conflicting_results_fail_closed(self):
        head = "c" * 40
        T = "2026-06-20T05:00:00Z"
        bodies = [
            {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner", "at": "2026-06-19T00:00:00Z"},
            {"body": review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": []}), "author": "owner", "at": T},
            {"body": review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "not-shipped", "decision_required": ["stop"]}), "author": "owner", "at": T},
        ]
        c = review.classify(review.parse_bodies(bodies), head, macro_reviewers=("gpt-5.5-pro",), operators=("owner",))
        self.assertFalse(c["pro_result_at_head"])

    def test_base_conflict_same_cycle_fails_closed(self):
        head, B1, B2 = "f" * 40, "b1" + "0" * 38, "b2" + "0" * 38
        bodies = [
            {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head, "base_sha": B1}), "author": "owner", "at": "2026-06-19T00:00:00Z"},
            {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head, "base_sha": B2}), "author": "owner", "at": "2026-06-19T00:00:01Z"},
        ]
        c = review.classify(review.parse_bodies(bodies), head, operators=("owner",), current_base=B2)
        self.assertTrue(c["cycle_conflict"])
        self.assertFalse(c["cycle_fresh"])

    # ---- v0.2.6: strict ordering + canonical paginated comment log ----
    def test_strict_ordering_equal_timestamp_fails(self):
        head, B, T = "c" * 40, "b" * 40, "2026-06-22T00:00:00Z"
        # a Codex review AT the freeze time is not strictly after → stale
        revs = [{"author": review.CODEX_BOT, "commit_id": head, "state": "COMMENTED", "at": T, "id": 1}]
        self.assertEqual(review.codex_signals_at_head(revs, [], head, since_at=T), [])
        # an approval at the SAME second as its evidence is order-ambiguous → invalid
        bodies = [
            {"body": review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head, "base_sha": B}), "author": "owner", "at": "2026-06-21T00:00:00Z"},
            {"body": review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": []}), "author": "owner", "at": T},
            {"body": review.emit_marker("findings", {"cycle": 1, "resolved": True}), "author": "owner", "at": T},
            {"body": review.emit_marker("approval", {"sha": head, "base_sha": B, "cycle": 1, "by": "owner"}), "author": "owner", "at": T},
        ]
        c = review.classify(review.parse_bodies(bodies), head, macro_reviewers=("gpt-5.5-pro",),
                               approvers=("owner",), operators=("owner",), current_base=B, codex_signal_at=None)
        self.assertTrue(c["pro_result_at_head"])
        self.assertFalse(c["approved_at_head"])

    def test_refreeze_same_cycle_advances_boundary(self):
        head, B = "c" * 40, "b" * 40
        bodies = [
            {"body": review.emit_marker("review-cycle", {"cycle": 2, "target_sha": head, "base_sha": B}), "author": "owner", "at": "2026-06-22T00:00:00Z"},
            {"body": review.emit_marker("review-cycle", {"cycle": 2, "target_sha": head, "base_sha": B}), "author": "owner", "at": "2026-06-22T02:00:00Z"},
        ]
        ms = review.parse_bodies(bodies)
        self.assertEqual(review.latest_cycle(ms, ("owner",))["_at"], "2026-06-22T02:00:00Z")  # later re-freeze wins
        # a Codex review between the two freezes is stale vs the advanced boundary
        revs = [{"author": review.CODEX_BOT, "commit_id": head, "state": "COMMENTED", "at": "2026-06-22T01:00:00Z", "id": 1}]
        self.assertEqual(review.codex_signals_at_head(revs, [], head, since_at="2026-06-22T02:00:00Z"), [])
        c = review.classify(ms, head, operators=("owner",), current_base=B)
        self.assertFalse(c["cycle_conflict"])  # same head/base → re-freeze, not a conflict
        self.assertTrue(c["cycle_fresh"])

    def test_rest_comments_paginates_and_uses_updated_at(self):
        import json as _json
        pages = [[{"id": 1, "user": {"login": "a"}, "body": "first", "created_at": "t0", "updated_at": "t0"}],
                 [{"id": 2, "user": {"login": "b"}, "body": "edited later", "created_at": "t0", "updated_at": "t5"}]]
        orig = review._gh
        review._gh = lambda root, *a: (0, _json.dumps(pages))
        try:
            out = review.rest_comments(Path("/x"), "o/r", 9)
        finally:
            review._gh = orig
        self.assertEqual([c["id"] for c in out], [1, 2])           # both pages flattened
        self.assertEqual(out[1]["at"], "t5")                       # effective time = updated_at

    def test_codex_regex_anchored_rejects_prose(self):
        head = "9b896a84c0" + "0" * 30  # valid 40-hex
        bot = "chatgpt-codex-connector"
        for neg in (f"I did not review this. Previous Reviewed commit: `{head[:10]}`",
                    f"Not reviewed commit: `{head[:10]}`",
                    f"> **Reviewed commit:** `{head[:10]}` stale quote",
                    f"foo reviewed commit:** `{head[:10]}` bar"):
            self.assertFalse(review.codex_fresh([], [{"author": bot, "body": neg}], head), neg)
        self.assertTrue(review.codex_fresh([], [{"author": bot, "body": f"**Reviewed commit:** `{head[:10]}`"}], head))

    def test_freeze_request_lists_custom_macro_reviewer(self):
        captured = {}
        # freeze reads the BASE policy via pr_context; a custom non-codex reviewer must be
        # prompted. Round-bound: the frozen carrier path requires a real prepared round.
        with tempfile.TemporaryDirectory() as d:
            root, head, rid = _pr_prepared_round(Path(d), "codex, research-auditor")
            ctx = {"repo": "o/r", "pr": 3, "head": head, "base_sha": "b" * 40, "base": "main",
                   "bundle": {"head": head, "base_sha": "b" * 40, "bodies": []},
                   "policy": common.normalize_config(
                       {"version": 1, "project": "x",
                        "review": {"mode": "pr", "reviewers": ["codex", "research-auditor"]}})}

            def fake_gh(root, *args):
                if len(args) >= 2 and args[0] == "pr" and args[1] == "comment":
                    captured["body"] = args[args.index("--body") + 1]
                return (0, "")

            saved = (review.pr_context, review._gh)
            review.pr_context = lambda root, pr: ctx
            review._gh = fake_gh
            try:
                import contextlib
                import io
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(review.freeze(root, 3, rid), 0)
            finally:
                review.pr_context, review._gh = saved
        self.assertIn("research-auditor", captured.get("body", ""))  # custom reviewer prompted, not name-guessed
        self.assertIn("@codex review", captured["body"])
        self.assertIn(f"- Reviewing: {head}", captured["body"])  # round-bound carrier present

    def test_reviewer_role_freezes_full_backend_identity_and_profile_fingerprint(self):
        captured = {}
        with tempfile.TemporaryDirectory() as d:
            def write_profile(root: Path) -> None:
                state = common.ensure_project_state_dir(root)
                (state / "profile.yml").write_text(
                    "schema: waystone-profile-1\nbindings:\n"
                    "  reviewer: {execution: forked-subagent, backend: 'claude:opus-4.1'}\n")

            root, head, rid = _pr_prepared_round(
                Path(d), "codex, role:reviewer", pre_close=write_profile)
            ctx = {
                "repo": "o/r", "pr": 3, "head": head, "base_sha": "b" * 40,
                "base": "main", "bundle": {
                    "head": head, "base_sha": "b" * 40, "bodies": []},
                "policy": common.normalize_config({
                    "version": 1, "project": "x", "review": {
                        "mode": "pr", "reviewers": ["codex", "role:reviewer"]}}),
            }

            def fake_gh(_root, *args):
                captured["body"] = args[args.index("--body") + 1]
                return (0, "")

            saved = (review.pr_context, review._gh)
            review.pr_context = lambda _root, _pr: ctx
            review._gh = fake_gh
            try:
                import contextlib
                import io
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(review.freeze(root, 3, rid), 0)
            finally:
                review.pr_context, review._gh = saved

        self.assertIn("Macro reviewer(s) — claude:opus-4.1", captured["body"])
        self.assertNotIn("role:reviewer", captured["body"])
        cycle = review.parse_markers(captured["body"], "review-cycle")[0]
        self.assertEqual(cycle["reviewers"], ["codex", "claude:opus-4.1"])
        self.assertRegex(cycle["profile_fingerprint"], r"^sha256:[0-9a-f]{12}$")

    def test_reviewer_role_resolves_before_facts_and_role_marker_is_rejected(self):
        head = "c" * 40
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            state = common.ensure_project_state_dir(root)
            (state / "profile.yml").write_text(
                "schema: waystone-profile-1\nbindings:\n"
                "  reviewer: {execution: external-runner, backend: 'claude:opus-4.1'}\n")
            _profile, fingerprint = delegate._load_profile(root)
            cfg = common.normalize_config({
                "version": 1, "project": "x",
                "review": {"reviewers": ["role:reviewer"]},
            })
            bundle = {
                "head": head, "base_sha": "", "bodies": self._bodies(
                    head, reviewer="claude:opus-4.1"),
                "reviews": [], "checks": [], "state": "OPEN", "is_draft": False,
                "base": "main", "merge_state": "CLEAN",
            }
            bundle["bodies"][0]["body"] = review.emit_marker("review-cycle", {
                "cycle": 1, "target_sha": head, "reviewers": ["claude:opus-4.1"],
                "profile_fingerprint": fingerprint,
            })
            facts = review.facts_from_bundle(bundle, cfg, None, root=root)
            self.assertTrue(facts["pro_result_at_head"])

            bundle["bodies"] = self._bodies(head, reviewer="role:reviewer")
            bundle["bodies"][0]["body"] = review.emit_marker("review-cycle", {
                "cycle": 1, "target_sha": head, "reviewers": ["claude:opus-4.1"],
                "profile_fingerprint": fingerprint,
            })
            facts = review.facts_from_bundle(bundle, cfg, None, root=root)
            self.assertFalse(facts["pro_result_at_head"])

    def test_frozen_reviewer_identity_survives_profile_drift_but_cycle_is_stale(self):
        head = "c" * 40
        base = "d" * 40
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            state = common.ensure_project_state_dir(root)
            profile_path = state / "profile.yml"
            profile_path.write_text(
                "schema: waystone-profile-1\nbindings:\n"
                "  reviewer: {execution: external-runner, backend: 'claude:opus-old'}\n")
            _profile, fingerprint = delegate._load_profile(root)
            cfg = common.normalize_config({
                "version": 1, "project": "x",
                "review": {"reviewers": ["role:reviewer"], "operators": ["owner"]},
            })
            bodies = [
                {"body": review.emit_marker("review-cycle", {
                    "cycle": 1, "target_sha": head, "base_sha": base,
                    "reviewers": ["claude:opus-old"],
                    "profile_fingerprint": fingerprint,
                }), "author": "owner", "at": "2026-07-15T00:00:00Z"},
                {"body": review.emit_marker("review-result", {
                    "reviewer": "claude:opus-old", "review_cycle": 1,
                    "reviewed_sha": head, "verdict": "shipped", "decision_required": [],
                }), "author": "owner", "at": "2026-07-15T01:00:00Z"},
            ]
            bundle = {
                "head": head, "base_sha": base, "bodies": bodies, "reviews": [],
                "checks": [], "state": "OPEN", "is_draft": False, "base": "main",
                "merge_state": "CLEAN",
            }
            profile_path.write_text(
                "schema: waystone-profile-1\nbindings:\n"
                "  reviewer: {execution: external-runner, backend: 'claude:opus-new'}\n")
            facts = review.facts_from_bundle(bundle, cfg, "owner/repo", root=root)
            self.assertEqual(facts["reviewers"], ["claude:opus-old"])
            self.assertTrue(facts["pro_result_at_head"])
            self.assertTrue(facts["reviewer_profile_drift"])
            self.assertFalse(facts["cycle_fresh"])

    def test_codex_backend_identity_does_not_alias_codex_trust_sentinel(self):
        head = "c" * 40
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            state = common.ensure_project_state_dir(root)
            (state / "profile.yml").write_text(
                "schema: waystone-profile-1\nbindings:\n"
                "  reviewer: {execution: external-runner, backend: 'codex:gpt-review'}\n")
            _profile, fingerprint = delegate._load_profile(root)
            cfg = common.normalize_config({
                "version": 1, "project": "x", "review": {
                    "reviewers": ["codex", "role:reviewer"], "operators": ["owner"]},
            })
            bundle = {
                "head": head, "base_sha": "", "reviews": [], "checks": [],
                "state": "OPEN", "is_draft": False, "base": "main", "merge_state": "CLEAN",
                "bodies": [
                    {"body": review.emit_marker("review-cycle", {
                        "cycle": 1, "target_sha": head,
                        "reviewers": ["codex", "codex:gpt-review"],
                        "profile_fingerprint": fingerprint,
                    }), "author": "owner", "at": "2026-07-15T00:00:00Z"},
                    {"body": review.emit_marker("review-result", {
                        "reviewer": "codex:gpt-review", "review_cycle": 1,
                        "reviewed_sha": head, "verdict": "shipped", "decision_required": [],
                    }), "author": "owner", "at": "2026-07-15T01:00:00Z"},
                ],
            }
            facts = review.facts_from_bundle(bundle, cfg, "owner/repo", root=root)
        self.assertEqual(facts["reviewers"], ["codex", "codex:gpt-review"])
        self.assertTrue(facts["pro_result_at_head"])
        self.assertFalse(facts["codex_fresh"])

    def test_reviewer_role_without_profile_binding_fails_loud(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            state = common.ensure_project_state_dir(root)
            (state / "profile.yml").write_text(
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: 'codex:gpt'}\n")
            with self.assertRaisesRegex(
                    common.WorkflowError, "profile has no binding for role 'reviewer'"):
                review.resolve_reviewers(root, ["role:reviewer"])

    def test_review_cli_reports_missing_reviewer_binding_without_traceback(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "home"
            home.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: x\ntasks: []\n")
            state = common.ensure_project_state_dir(root)
            (state / "profile.yml").write_text(
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: 'codex:gpt'}\n")
            ctx = {
                "repo": "o/r", "pr": 3, "head": "a" * 40, "base_sha": "b" * 40,
                "base": "main", "bundle": {
                    "head": "a" * 40, "base_sha": "b" * 40, "bodies": []},
                "policy": common.normalize_config({
                    "version": 1, "project": "x", "review": {
                        "mode": "pr", "reviewers": ["role:reviewer"]}}),
            }
            original = review.pr_context
            review.pr_context = lambda _root, _pr: ctx
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: review.main([
                        "freeze", "--pr", "3", "--round", "2026-07-19-review", str(root)]))
            finally:
                review.pr_context = original
            self.assertEqual(rc, 1)
            self.assertIn("profile has no binding for role 'reviewer'", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())


class MergeGateTests(unittest.TestCase):
    def test_all_pass(self):
        ok, fails = merge.merge_gate(dict(PASS))
        self.assertTrue(ok, fails)
        self.assertEqual(fails, [])

    def test_each_condition_blocks(self):
        cases = {
            "cycle_fresh": (False, "stale"),
            "codex_fresh": (False, "Codex"),
            "findings_resolved": (False, "findings"),
            "pro_result_at_head": (False, "external"),
            "approved_at_head": (False, "approval"),
        }
        for key, (val, needle) in cases.items():
            g = dict(PASS); g[key] = val
            ok, fails = merge.merge_gate(g)
            self.assertFalse(ok, key)
            self.assertTrue(any(needle.lower() in f.lower() for f in fails), (key, fails))

    def test_latest_v1_supersession_requires_new_cycle_v2_refreeze(self):
        g = dict(PASS)
        g["cycle_version_skew_reason"] = "latest-v1-supersedes-v2"
        ok, fails = merge.merge_gate(g)
        self.assertFalse(ok)
        self.assertTrue(any("new cycle" in failure and "v2" in failure
                            for failure in fails), fails)

    def test_ci_only_blocks_when_required(self):
        g = dict(PASS); g["ci"] = "failing"
        self.assertFalse(merge.merge_gate(g)[0])
        g["require_ci"] = False
        self.assertTrue(merge.merge_gate(g)[0])
        # ci 'none' with require_ci blocks
        g2 = dict(PASS); g2["ci"] = "none"
        self.assertFalse(merge.merge_gate(g2)[0])

    def test_blockers_and_decisions_block(self):
        g = dict(PASS); g["open_blockers"] = ["fix/x"]
        self.assertFalse(merge.merge_gate(g)[0])
        g = dict(PASS); g["open_decisions"] = ["decision/y"]
        self.assertFalse(merge.merge_gate(g)[0])

    def test_unpushed_local_head_blocks(self):
        g = dict(PASS); g["remote_contains_head"] = False
        self.assertFalse(merge.merge_gate(g)[0])

    def test_gate_only_requires_configured_reviewers(self):
        # codex not wanted: a missing/false codex review must not block
        g = dict(PASS); g["want_codex"] = False; g["codex_fresh"] = False; g["findings_resolved"] = False
        self.assertTrue(merge.merge_gate(g)[0], merge.merge_gate(g)[1])
        # pro not wanted: a missing pro result must not block
        g = dict(PASS); g["want_pro"] = False; g["pro_result_at_head"] = False
        self.assertTrue(merge.merge_gate(g)[0], merge.merge_gate(g)[1])
        # but when wanted, they still block
        g = dict(PASS); g["want_codex"] = True; g["codex_fresh"] = False
        self.assertFalse(merge.merge_gate(g)[0])

    def test_pr_state_and_head_read_block(self):
        g = dict(PASS); g["head_read_ok"] = False
        ok, fails = merge.merge_gate(g)
        self.assertFalse(ok); self.assertTrue(any("policy@base" in f or "tasks@head" in f for f in fails))
        for key, val in (("pr_state", "MERGED"), ("is_draft", True)):
            g = dict(PASS); g[key] = val
            self.assertFalse(merge.merge_gate(g)[0], key)
        g = dict(PASS); g["base"] = "feature"; g["expected_base"] = "main"
        self.assertFalse(merge.merge_gate(g)[0])


class TasksGateTests(unittest.TestCase):
    def test_counts(self):
        data = {"tasks": [
            {"id": "fix/a", "severity": "blocker", "status": "pending"},
            {"id": "fix/b", "severity": "blocker", "status": "done"},
            {"id": "decision/c", "status": "pending"},
            {"id": "decision/d", "status": "done"},
            {"id": "feat/e", "status": "active"},
        ]}
        c = merge.tasks_gate_counts(data)
        self.assertEqual(c["open_blockers"], ["fix/a"])
        self.assertEqual(c["open_decisions"], ["decision/c"])

    def test_defensive_on_malformed(self):
        # a non-list `tasks` must not crash and must not silently report zero open items as valid
        for bad in ({"tasks": "not-a-list"}, {"tasks": 5}, "garbage", None):
            self.assertEqual(merge.tasks_gate_counts(bad), {"open_blockers": [], "open_decisions": []}, bad)
        # such a registry also fails schema validation (the gate's head_read_ok hook)
        self.assertTrue(validate.validate({"version": 1, "project": "x", "tasks": "not-a-list"}))

    def test_validator_malformed_deps_no_crash(self):
        # M8: a non-list `deps` must be a clean validation error, never a process crash
        for bad in (5, "feat/x", None, {"a": 1}):
            data = {"version": 1, "project": "proj", "tasks": [
                {"id": "feat/foo", "title": "a properly explained task", "deps": bad}]}
            errs = validate.validate(data)  # must not raise
            if bad is not None:  # None == absent → no deps error
                self.assertTrue(any("deps" in e for e in errs), bad)


class RemoteTests(unittest.TestCase):
    def test_pushed_vs_unpushed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bare = d / "remote.git"
            work = d / "work"
            subprocess.run(["git", "init", "-q", "--bare", str(bare)])
            work.mkdir()
            init_repo(work)
            git(work, "remote", "add", "origin", str(bare))
            git(work, "push", "-q", "-u", "origin", "main")
            pushed, info = common.head_pushed(work, fetch=True)
            self.assertTrue(pushed, info)
            offline, offline_info = common.head_pushed(work, fetch=False)
            self.assertFalse(offline)
            self.assertIn("live remote fetch required", offline_info.get("reason", ""))
            # new local commit, not pushed
            (work / "f.txt").write_text("1")
            git(work, "commit", "-aqm", "c1")
            pushed2, info2 = common.head_pushed(work, fetch=True)
            self.assertFalse(pushed2, info2)
            self.assertEqual(info2.get("behind"), 0)

    def test_no_upstream(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            init_repo(work)
            pushed, info = common.head_pushed(work, fetch=False)
            self.assertFalse(pushed)
            self.assertIn("reason", info)

    def test_fetch_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bare = d / "remote.git"; work = d / "work"
            subprocess.run(["git", "init", "-q", "--bare", str(bare)])
            work.mkdir(); init_repo(work)
            git(work, "remote", "add", "origin", str(bare))
            git(work, "push", "-q", "-u", "origin", "main")
            import shutil
            shutil.rmtree(bare)  # remote now unreachable
            pushed, info = common.head_pushed(work, fetch=True)
            self.assertFalse(pushed)  # must NOT trust the stale ref
            self.assertIn("fetch failed", info.get("reason", ""))

    def test_empty_temporary_fetch_ref_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bare, work = d / "remote.git", d / "work"
            subprocess.run(
                ["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
            work.mkdir()
            init_repo(work)
            git(work, "remote", "add", "origin", str(bare))
            git(work, "push", "-q", "-u", "origin", "main")

            original_git_rc = common.git_rc

            def empty_temporary_ref(root, *args):
                if (len(args) == 3 and args[:2] == ("rev-parse", "--verify")
                        and args[2].startswith("refs/waystone/verify-fetch-")
                        and args[2].endswith("^{commit}")):
                    return (0, "", "")
                return original_git_rc(root, *args)

            common.git_rc = empty_temporary_ref
            try:
                pushed, info = common.head_pushed(work, fetch=True)
            finally:
                common.git_rc = original_git_rc
            self.assertFalse(pushed)
            self.assertIn("temporary fetch ref", info.get("reason", ""))

    def test_next_fetch_sweeps_only_stale_generated_refs(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bare, work = d / "remote.git", d / "work"
            subprocess.run(
                ["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
            work.mkdir()
            init_repo(work)
            git(work, "remote", "add", "origin", str(bare))
            git(work, "push", "-q", "-u", "origin", "main")
            tip = git(work, "rev-parse", "HEAD").stdout.strip()

            stale_pid = 2147483647
            with self.assertRaises(ProcessLookupError):
                os.kill(stale_pid, 0)
            stale_ref = f"refs/waystone/verify-fetch-{stale_pid}-{'a' * 32}"
            live_ref = f"refs/waystone/verify-fetch-{os.getpid()}-{'b' * 32}"
            user_ref = "refs/waystone/verify-fetch-user-owned"
            for ref in (stale_ref, live_ref, user_ref):
                self.assertEqual(git(work, "update-ref", ref, tip).returncode, 0)

            try:
                fetched, info = common.fetch_upstream_head(work)
                self.assertEqual(fetched, tip, info)
                self.assertEqual(
                    git(work, "show-ref", "--verify", "--quiet", stale_ref).returncode, 1)
                self.assertEqual(
                    git(work, "show-ref", "--verify", "--quiet", live_ref).returncode, 0)
                self.assertEqual(
                    git(work, "show-ref", "--verify", "--quiet", user_ref).returncode, 0)
            finally:
                git(work, "update-ref", "-d", live_ref)
                git(work, "update-ref", "-d", user_ref)

    def test_live_fetch_evidence_ignores_fetch_head_overwrite_and_cleans_ref(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bare, work = d / "remote.git", d / "work"
            subprocess.run(
                ["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
            work.mkdir()
            init_repo(work)
            git(work, "remote", "add", "origin", str(bare))
            git(work, "push", "-q", "-u", "origin", "main")
            remote_tip = git(work, "rev-parse", "HEAD").stdout.strip()
            (work / "local.txt").write_text("not pushed\n")
            git(work, "add", "-A")
            git(work, "commit", "-qm", "local only")
            other_sha = git(work, "rev-parse", "HEAD").stdout.strip()

            original_git_rc = common.git_rc

            def overwrite_fetch_head(root, *args):
                result = original_git_rc(root, *args)
                if args and args[0] == "fetch" and result[0] == 0:
                    (Path(root) / ".git" / "FETCH_HEAD").write_text(f"{other_sha}\n")
                return result

            common.git_rc = overwrite_fetch_head
            try:
                fetched, info = common.fetch_upstream_head(work)
            finally:
                common.git_rc = original_git_rc
            self.assertEqual(fetched, remote_tip, info)
            self.assertEqual(git(work, "rev-parse", "FETCH_HEAD").stdout.strip(), other_sha)
            refs = git(
                work, "for-each-ref", "--format=%(refname)",
                "refs/waystone/verify-fetch-*").stdout.strip()
            self.assertEqual(refs, "")


class PacketPublicationTests(unittest.TestCase):
    ROUND_ID = f"{TEST_CURRENT_ROUND_DATE}-packet"

    NARRATIVE = """## What changed and why

The round made packet rendering deterministic.

## Read these first

1. `scripts/review.py` — renderer and binding checks

## Claims to attack

1. Narrative text cannot override protocol fields.

## Evidence already produced (mine — inspect, don't trust)

| Claim | Command / artifact | My reading | Where it lives |
|---|---|---|---|
| renderer | `tests` | deterministic | `scripts/tests/run_tests.py` |

## Known weak spots

PR publication has a different carrier.

## Domain lens

Fail-loud protocol boundaries.
"""

    def _request(self, root: Path, target: str, base: str = "(root)") -> Path:
        request = root / "docs" / "reviews" / f"{self.ROUND_ID}-request.md"
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(
            f"# Review Request — {self.ROUND_ID}\n\n"
            f"- Reviewing: {target}   (diff against {base})\n")
        return request

    def _closed_project(self, base: Path, *, mode: str = "packet") -> tuple[Path, str, Path]:
        root = base / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text(
            "version: 1\nproject: demo\nreviews_dir: docs/reviews\n"
            f"review:\n  mode: {mode}\n  reviewers: [reviewer-x]\n"
            "state:\n  last_round_commit: null\n")
        (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "setup")
        target = git(root, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(round.close(
            root, self.ROUND_ID, done=[], touched=[], commit="HEAD"), 0)
        narrative = base / "narrative.md"
        narrative.write_text(self.NARRATIVE)
        return root, target, narrative

    def test_prepare_renders_template_from_round_exposure_and_narrative(self):
        import re

        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)

            request = root / "docs" / "reviews" / f"{self.ROUND_ID}-request.md"
            rendered = request.read_text()
            for line in (
                    "- Project: demo", "- Branch: main", "- Reviewer: reviewer-x",
                    f"- Reviewing: {target}   (diff against (root))"):
                self.assertEqual(rendered.count(line), 1, line)
            for heading in review.NARRATIVE_HEADINGS:
                self.assertEqual(rendered.count(heading), 1, heading)
            for key in ("model:", "effort:", "review-target:"):
                self.assertEqual(len(re.findall(rf"(?m)^{re.escape(key)}", rendered)), 1, key)
            self.assertIn(f"review-target: {target}", rendered)
            self.assertNotRegex(rendered, r"\[\[[A-Z_]+\]\]")
            for old_placeholder in (
                    "{round-id}", "{project}", "{branch}",
                    "{40-lowercase-hex-closeout-sha}"):
                self.assertNotIn(old_placeholder, rendered)

            binding = review.read_round_request_binding(next(
                (root / "docs" / "reviews").glob(
                    f"{self.ROUND_ID}-request.binding*.json")))
            self.assertEqual(binding["target_sha"], target)
            self.assertIsNone(binding["base_sha"])
            self.assertEqual(binding["reviewers"], ["reviewer-x"])
            self.assertEqual(binding["schema"], review.ROUND_REQUEST_BINDING_SCHEMA)
            self.assertRegex(binding["narrative_digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(
                binding["rendered_request_digest"],
                review._canonical_rendered_request_digest(rendered))

    def test_rendered_request_exposes_self_digest_and_canonicalizer_is_header_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            root, _target, narrative = self._closed_project(Path(d))
            narrative.write_text(self.NARRATIVE.replace(
                "Fail-loud protocol boundaries.",
                "request-digest: this is narrative prose, not the reply header."))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)

            request = root / "docs/reviews" / f"{self.ROUND_ID}-request.md"
            rendered = request.read_text()
            binding = review.read_round_request_binding(next(
                request.parent.glob(f"{self.ROUND_ID}-request.binding*.json")))
            self.assertIn(
                f"request-digest: {binding['rendered_request_digest']}", rendered)
            self.assertIn(
                "request-digest: this is narrative prose, not the reply header.", rendered)

            narrative_changed = rendered.replace(
                "request-digest: this is narrative prose, not the reply header.",
                "request-digest: changed narrative prose.")
            self.assertNotEqual(
                review._canonical_rendered_request_digest(narrative_changed),
                binding["rendered_request_digest"])
            displayed_changed = rendered.replace(
                f"request-digest: {binding['rendered_request_digest']}",
                f"request-digest: {'sha256:' + 'f' * 64}")
            self.assertEqual(
                review._canonical_rendered_request_digest(displayed_changed),
                binding["rendered_request_digest"])

    def test_delayed_echo_stamps_named_generation_and_stays_pending_after_reprepare(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, narrative = self._closed_project(base)
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            rdir = root / "docs/reviews"
            first_path, first = review.latest_round_request_binding(
                list(rdir.glob(f"{self.ROUND_ID}-request.binding*.json")),
                expected_round_id=self.ROUND_ID)
            self.assertIsNotNone(first_path)
            self.assertIsNotNone(first)

            reply = base / "reply.md"
            reply.write_text(
                "model: reviewer-x\neffort: high\n"
                f"review-target: {target}\n"
                f"request-digest: {first['rendered_request_digest']}\n\nreviewed\n")
            narrative.write_text(self.NARRATIVE.replace(
                "Fail-loud protocol boundaries.", "A newer narrative generation."))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            _latest_path, latest = review.latest_round_request_binding(
                list(rdir.glob(f"{self.ROUND_ID}-request.binding*.json")),
                expected_round_id=self.ROUND_ID)
            self.assertIsNotNone(latest)
            self.assertNotEqual(
                first["rendered_request_digest"], latest["rendered_request_digest"])

            self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)
            pending = review.pending_reviews(root)
            self.assertEqual([row["round_id"] for row in pending], [self.ROUND_ID])
            self.assertEqual(
                pending[0]["reason"], "feedback-request-digest-stale-generation")

            feedback = rdir / f"{self.ROUND_ID}-feedback.md"
            metadata = review.read_feedback_reply_metadata(
                feedback, expected_round_id=self.ROUND_ID, binding=latest)
            self.assertEqual(metadata["narrative_digest"], first["narrative_digest"])
            self.assertEqual(
                metadata["rendered_request_digest"], first["rendered_request_digest"])
            self.assertIs(metadata["rendered_request_digest_matches"], False)
            self.assertEqual(
                metadata["rendered_request_coverage_reason"],
                "request-digest-stale-generation")

            events, skipped = overlay.load_review_ingests(root)
            self.assertEqual(skipped, 0)
            self.assertIs(events[0]["narrative_digest_matches"], False)
            self.assertEqual(
                events[0]["rendered_request_coverage_reason"],
                "request-digest-stale-generation")
            projected_guard = overlay.evaluate_review_skipped_closes(
                [{"round_id": self.ROUND_ID,
                  "at": "2099-01-01T00:00:00+00:00",
                  "review_mode": "packet"}],
                [{**events[0], "at": "2098-01-01T00:00:00+00:00"}],
                consecutive=1)
            self.assertEqual(projected_guard["fires"], [self.ROUND_ID])
            self.assertIsNone(projected_guard["by_round"][0]["feedback_observed"])

            out = base / "out"
            improve.run_reviews(base / "unused-registry.json", out, project_root=root)
            row = next(row for row in (
                _json.loads(line) for line in (out / "reviews.jsonl").read_text().splitlines()
            ) if row["round_id"] == self.ROUND_ID)
            self.assertEqual(
                row["reply_rendered_request_digest"], first["rendered_request_digest"])
            self.assertIs(row["rendered_request_digest_matches"], False)
            self.assertEqual(
                row["rendered_request_coverage_reason"],
                "request-digest-stale-generation")

    def test_feedback_cache_digest_edit_cannot_reassign_verbatim_reply(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, narrative = self._closed_project(base)
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            rdir = root / "docs/reviews"
            _first_path, first = review.latest_round_request_binding(
                list(rdir.glob(f"{self.ROUND_ID}-request.binding*.json")),
                expected_round_id=self.ROUND_ID)
            self.assertIsNotNone(first)

            reply = base / "reply.md"
            reply.write_text(
                "model: reviewer-x\neffort: high\n"
                f"review-target: {target}\n"
                f"request-digest: {first['rendered_request_digest']}\n\nreviewed\n")
            self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)

            narrative.write_text(self.NARRATIVE.replace(
                "Fail-loud protocol boundaries.", "A newer narrative generation."))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            _latest_path, latest = review.latest_round_request_binding(
                list(rdir.glob(f"{self.ROUND_ID}-request.binding*.json")),
                expected_round_id=self.ROUND_ID)
            self.assertIsNotNone(latest)
            self.assertNotEqual(
                first["rendered_request_digest"], latest["rendered_request_digest"])

            feedback = rdir / f"{self.ROUND_ID}-feedback.md"
            content = feedback.read_bytes()
            header, tail = content.split(review.FEEDBACK_HEADER_SEPARATOR, 1)
            lines = header.decode().splitlines()
            metadata_index = next(
                index for index, line in enumerate(lines)
                if line.startswith("reply-metadata-json: "))
            payload = _json.loads(lines[metadata_index].removeprefix(
                "reply-metadata-json: "))
            payload["narrative_digest"] = latest["narrative_digest"]
            payload["rendered_request_digest"] = latest["rendered_request_digest"]
            lines[metadata_index] = "reply-metadata-json: " + _json.dumps(
                payload, sort_keys=True, separators=(",", ":"))
            feedback.write_bytes(
                "\n".join(lines).encode() + review.FEEDBACK_HEADER_SEPARATOR + tail)

            pending = review.pending_reviews(root)
            self.assertEqual([row["round_id"] for row in pending], [self.ROUND_ID])
            self.assertEqual(pending[0]["reason"], "feedback-cache-body-mismatch")
            projected = review.read_feedback_reply_metadata(
                feedback, expected_round_id=self.ROUND_ID, binding=latest)
            self.assertIsNone(projected["rendered_request_digest_matches"])
            self.assertEqual(
                projected["rendered_request_coverage_reason"],
                "feedback-cache-body-mismatch")

            out = base / "out"
            improve.run_reviews(base / "unused-registry.json", out, project_root=root)
            row = next(row for row in (
                _json.loads(line) for line in (out / "reviews.jsonl").read_text().splitlines()
            ) if row["round_id"] == self.ROUND_ID)
            self.assertIsNone(row["rendered_request_digest_matches"])
            self.assertEqual(
                row["rendered_request_coverage_reason"], "feedback-cache-body-mismatch")

    def test_feedback_cache_coverage_edit_cannot_enable_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, narrative = self._closed_project(base)
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            reply = base / "reply.md"
            reply.write_text(
                "model: reviewer-x\neffort: high\n"
                f"review-target: {target}\n\nreviewed\n")
            self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)

            rdir = root / "docs/reviews"
            feedback = rdir / f"{self.ROUND_ID}-feedback.md"
            header, tail = feedback.read_bytes().split(
                review.FEEDBACK_HEADER_SEPARATOR, 1)
            lines = header.decode().splitlines()
            metadata_index = next(
                index for index, line in enumerate(lines)
                if line.startswith("reply-metadata-json: "))
            payload = _json.loads(lines[metadata_index].removeprefix(
                "reply-metadata-json: "))
            payload["rendered_request_coverage_reason"] = (
                "request-digest-missing-legacy-fallback")
            lines[metadata_index] = "reply-metadata-json: " + _json.dumps(
                payload, sort_keys=True, separators=(",", ":"))
            feedback.write_bytes(
                "\n".join(lines).encode() + review.FEEDBACK_HEADER_SEPARATOR + tail)

            _binding_path, binding = review.latest_round_request_binding(
                list(rdir.glob(f"{self.ROUND_ID}-request.binding*.json")),
                expected_round_id=self.ROUND_ID)
            projected = review.read_feedback_reply_metadata(
                feedback, expected_round_id=self.ROUND_ID, binding=binding)
            self.assertIs(projected["rendered_request_digest_matches"], False)
            self.assertEqual(
                projected["rendered_request_coverage_reason"], "request-digest-missing")
            self.assertEqual(
                review.pending_reviews(root)[0]["reason"],
                "feedback-request-digest-missing")

    def test_missing_or_corrupt_named_generation_is_unknown_not_receipt_corrupt(self):
        for mutation in ("missing", "corrupt"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as d:
                base = Path(d)
                root, target, narrative = self._closed_project(base)
                self.assertEqual(review.prepare_review_request(
                    root, self.ROUND_ID, narrative), 0)
                rdir = root / "docs/reviews"
                first_path, first = review.latest_round_request_binding(
                    list(rdir.glob(f"{self.ROUND_ID}-request.binding*.json")),
                    expected_round_id=self.ROUND_ID)
                self.assertIsNotNone(first_path)
                self.assertIsNotNone(first)
                reply = base / "reply.md"
                reply.write_text(
                    "model: reviewer-x\neffort: high\n"
                    f"review-target: {target}\n"
                    f"request-digest: {first['rendered_request_digest']}\n\nreviewed\n")
                self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)

                narrative.write_text(self.NARRATIVE.replace(
                    "Fail-loud protocol boundaries.", "A newer narrative generation."))
                self.assertEqual(review.prepare_review_request(
                    root, self.ROUND_ID, narrative), 0)
                _latest_path, latest = review.latest_round_request_binding(
                    list(rdir.glob(f"{self.ROUND_ID}-request.binding*.json")),
                    expected_round_id=self.ROUND_ID)
                self.assertIsNotNone(latest)
                if mutation == "missing":
                    first_path.unlink()
                else:
                    first_path.write_text("{}\n")

                feedback = rdir / f"{self.ROUND_ID}-feedback.md"
                projected = review.read_feedback_reply_metadata(
                    feedback, expected_round_id=self.ROUND_ID, binding=latest)
                self.assertIsNone(projected["narrative_digest_matches"])
                self.assertEqual(
                    projected["narrative_coverage_reason"], "request-digest-unknown")
                self.assertIs(projected["rendered_request_digest_matches"], False)
                self.assertEqual(
                    projected["rendered_request_coverage_reason"],
                    "request-digest-unknown")
                events, skipped = overlay.load_review_ingests(root)
                self.assertEqual(skipped, 0)
                self.assertIsNone(events[0]["narrative_digest_matches"])
                self.assertEqual(
                    events[0]["rendered_request_coverage_reason"],
                    "request-digest-unknown")
                projected_guard = overlay.evaluate_review_skipped_closes(
                    [{"round_id": self.ROUND_ID,
                      "at": "2099-01-01T00:00:00+00:00",
                      "review_mode": "packet"}],
                    [{**events[0], "at": "2098-01-01T00:00:00+00:00"}],
                    consecutive=1)
                self.assertEqual(projected_guard["fires"], [self.ROUND_ID])
                self.assertIsNone(projected_guard["by_round"][0]["feedback_observed"])
                self.assertEqual(
                    review.pending_reviews(root)[0]["reason"],
                    "feedback-request-digest-unknown")

    def test_invalid_stored_round_is_isolated_before_generation_lookup(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, narrative = self._closed_project(base)
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            rdir = root / "docs/reviews"
            _binding_path, binding = review.latest_round_request_binding(
                list(rdir.glob(f"{self.ROUND_ID}-request.binding*.json")),
                expected_round_id=self.ROUND_ID)
            reply = base / "reply.md"
            reply.write_text(
                "model: reviewer-x\neffort: high\n"
                f"review-target: {target}\n"
                f"request-digest: {binding['rendered_request_digest']}\n\nreviewed\n")
            self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)

            feedback = rdir / f"{self.ROUND_ID}-feedback.md"
            pristine = feedback.read_bytes()
            header, tail = pristine.split(
                review.FEEDBACK_HEADER_SEPARATOR, 1)
            lines = ["round: /" if line.startswith("round: ") else line
                     for line in header.decode().splitlines()]
            feedback.write_bytes(
                "\n".join(lines).encode() + review.FEEDBACK_HEADER_SEPARATOR + tail)
            with mock.patch.object(
                    review, "_request_generation_in_directory",
                    side_effect=AssertionError("generation lookup must not run")):
                projected = review.read_feedback_reply_metadata(
                    feedback, expected_round_id=self.ROUND_ID, binding=binding)
            self.assertEqual(
                projected["rendered_request_coverage_reason"],
                "feedback-receipt-corrupt")

            header, tail = pristine.split(review.FEEDBACK_HEADER_SEPARATOR, 1)
            lines = header.decode().splitlines()
            metadata_index = next(
                index for index, line in enumerate(lines)
                if line.startswith("reply-metadata-json: "))
            payload = _json.loads(lines[metadata_index].removeprefix(
                "reply-metadata-json: "))
            payload["metadata"] = []
            lines[metadata_index] = "reply-metadata-json: " + _json.dumps(
                payload, sort_keys=True, separators=(",", ":"))
            feedback.write_bytes(
                "\n".join(lines).encode() + review.FEEDBACK_HEADER_SEPARATOR + tail)
            with mock.patch.object(
                    review, "_request_generation_in_directory",
                    side_effect=AssertionError("generation lookup must not run")):
                projected = review.read_feedback_reply_metadata(
                    feedback, expected_round_id=self.ROUND_ID, binding=binding)
            self.assertEqual(
                projected["rendered_request_coverage_reason"],
                "feedback-receipt-corrupt")
            self.assertEqual(
                review.pending_reviews(root)[0]["reason"], "feedback-receipt-corrupt")

            out = base / "out"
            improve.run_reviews(base / "unused-registry.json", out, project_root=root)
            row = next(row for row in (
                _json.loads(line) for line in (out / "reviews.jsonl").read_text().splitlines()
            ) if row["round_id"] == self.ROUND_ID)
            self.assertEqual(
                row["rendered_request_coverage_reason"], "feedback-receipt-corrupt")

    def test_stale_echo_recovers_when_its_generation_becomes_latest_again(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, narrative = self._closed_project(base)
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            rdir = root / "docs/reviews"
            _first_path, first = review.latest_round_request_binding(
                list(rdir.glob(f"{self.ROUND_ID}-request.binding*.json")),
                expected_round_id=self.ROUND_ID)
            self.assertIsNotNone(first)

            reply = base / "reply.md"
            reply.write_text(
                "model: reviewer-x\neffort: high\n"
                f"review-target: {target}\n"
                f"request-digest: {first['rendered_request_digest']}\n\nreviewed\n")
            narrative.write_text(self.NARRATIVE.replace(
                "Fail-loud protocol boundaries.", "A newer narrative generation."))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)
            self.assertEqual(
                review.pending_reviews(root)[0]["reason"],
                "feedback-request-digest-stale-generation")

            narrative.write_text(self.NARRATIVE)
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            _latest_path, latest = review.latest_round_request_binding(
                list(rdir.glob(f"{self.ROUND_ID}-request.binding*.json")),
                expected_round_id=self.ROUND_ID)
            self.assertEqual(
                latest["rendered_request_digest"], first["rendered_request_digest"])
            self.assertEqual(review.pending_reviews(root), [])

            feedback = rdir / f"{self.ROUND_ID}-feedback.md"
            receipt = review.read_feedback_reply_metadata(
                feedback, expected_round_id=self.ROUND_ID, binding=latest)
            self.assertIs(receipt["rendered_request_digest_matches"], True)
            self.assertIsNone(receipt["rendered_request_coverage_reason"])

            out = base / "out"
            improve.run_reviews(base / "unused-registry.json", out, project_root=root)
            row = next(row for row in (
                _json.loads(line) for line in (out / "reviews.jsonl").read_text().splitlines()
            ) if row["round_id"] == self.ROUND_ID)
            self.assertIs(row["rendered_request_digest_matches"], True)
            self.assertIsNone(row["rendered_request_coverage_reason"])

    def test_v2_reply_without_digest_stays_pending_with_resubmission_guidance(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            reply = Path(d) / "reply.md"
            reply.write_text(
                "model: reviewer-x\neffort: high\n"
                f"review-target: {target}\n\nreviewed\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)

            binding, reason = review.ingest_round_binding(
                root, self.ROUND_ID, common.load_config(root))
            self.assertIsNone(reason)
            guidance = err.getvalue()
            self.assertIn("request-digest", guidance)
            self.assertIn("request you reviewed", guidance)
            self.assertIn("resubmit", guidance.lower())
            self.assertNotIn(binding["rendered_request_digest"], guidance)
            pending = review.pending_reviews(root)
            self.assertEqual([row["round_id"] for row in pending], [self.ROUND_ID])
            self.assertEqual(pending[0]["reason"], "feedback-request-digest-missing")

            metadata = review.read_feedback_reply_metadata(
                root / "docs/reviews" / f"{self.ROUND_ID}-feedback.md",
                expected_round_id=self.ROUND_ID, binding=binding)
            self.assertIsNone(metadata["rendered_request_digest"])
            self.assertEqual(
                metadata["rendered_request_coverage_reason"], "request-digest-missing")

    def test_unknown_echo_is_distinct_from_stale_generation(self):
        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            unknown = "sha256:" + "f" * 64
            reply = Path(d) / "reply.md"
            reply.write_text(
                "model: reviewer-x\neffort: high\n"
                f"review-target: {target}\nrequest-digest: {unknown}\n\nreviewed\n")

            self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)
            pending = review.pending_reviews(root)
            self.assertEqual(pending[0]["reason"], "feedback-request-digest-unknown")
            binding, reason = review.ingest_round_binding(
                root, self.ROUND_ID, common.load_config(root))
            self.assertIsNone(reason)
            receipt = review.read_feedback_reply_metadata(
                root / "docs/reviews" / f"{self.ROUND_ID}-feedback.md",
                expected_round_id=self.ROUND_ID, binding=binding)
            self.assertIsNone(receipt["rendered_request_digest"])
            self.assertEqual(
                receipt["rendered_request_coverage_reason"], "request-digest-unknown")

    def test_v2_binding_missing_or_invalid_digest_is_corrupt(self):
        for field, value in (
                ("narrative_digest", None),
                ("rendered_request_digest", None),
                ("narrative_digest", "sha256:not-a-digest"),
                ("rendered_request_digest", "sha256:not-a-digest")):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as d:
                root, _target, narrative = self._closed_project(Path(d))
                self.assertEqual(review.prepare_review_request(
                    root, self.ROUND_ID, narrative), 0)
                binding_path = next((root / "docs/reviews").glob(
                    f"{self.ROUND_ID}-request.binding*.json"))
                binding = _json.loads(binding_path.read_text())
                if value is None:
                    binding.pop(field)
                else:
                    binding[field] = value
                binding_path.write_text(_json.dumps(binding) + "\n")

                with self.assertRaisesRegex(common.WorkflowError, "corrupt review binding"):
                    review.read_round_request_binding(binding_path)

    def test_round_request_binding_rejects_duplicate_json_fields(self):
        with tempfile.TemporaryDirectory() as d:
            root, _target, narrative = self._closed_project(Path(d))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            binding_path = next((root / "docs/reviews").glob(
                f"{self.ROUND_ID}-request.binding*.json"))
            shadow_target = "f" * 40
            binding_path.write_text(binding_path.read_text().replace(
                '"target_sha":',
                f'"target_sha": "{shadow_target}", "target_sha":', 1))

            with self.assertRaisesRegex(common.WorkflowError, "corrupt review binding"):
                review.read_round_request_binding(
                    binding_path, expected_round_id=self.ROUND_ID)

    def test_v1_binding_cutoff_includes_real_2026_07_18_artifacts(self):
        for name in (
                "2026-07-18-carrier-lanes-request.binding.json",
                "2026-07-18-carrier-lanes-fixes-request.binding.json"):
            binding = review.read_round_request_binding(
                SCRIPTS.parent / "docs/reviews" / name)
            self.assertEqual(binding["schema"], review.ROUND_REQUEST_BINDING_V1_SCHEMA)

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            later = write_legacy_round_request_binding(
                root, "2026-07-19-digest-era", "a" * 40, None, ["reviewer-x"])
            with self.assertRaisesRegex(common.WorkflowError, "corrupt review binding"):
                review.read_round_request_binding(later)

    def test_v1_binding_with_digest_fields_is_not_accepted_as_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            binding_path = write_legacy_round_request_binding(
                root, "2026-07-18-genuine-legacy", "a" * 40, None, ["reviewer-x"])
            binding = _json.loads(binding_path.read_text())
            binding["narrative_digest"] = TEST_NARRATIVE_DIGEST
            binding["rendered_request_digest"] = TEST_RENDERED_REQUEST_DIGEST
            binding_path.write_text(_json.dumps(binding) + "\n")

            with self.assertRaisesRegex(common.WorkflowError, "corrupt review binding"):
                review.read_round_request_binding(binding_path)

    def test_binding_rejects_nonexistent_calendar_date(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_legacy_round_request_binding(
                Path(d), "2026-99-99-impossible", "a" * 40, None, ["reviewer-x"])
            with self.assertRaisesRegex(common.WorkflowError, "corrupt review binding"):
                review.read_round_request_binding(path)

    def test_pr_binding_paths_reject_nonexistent_calendar_date(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = review.write_pr_freeze_binding(
                root, "2026-99-99-r1", 7, 1, "a" * 40, "b" * 40,
                ["reviewer-x"], None, "docs/reviews",
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
            with self.assertRaisesRegex(common.WorkflowError, "corrupt review binding"):
                review.read_pr_freeze_binding(path)
            with self.assertRaisesRegex(common.WorkflowError, "real YYYY-MM-DD"):
                review._round_requires_digest_binding("2026-99-99-r1")

    def test_pr_freeze_binding_schema_requires_digest_exactly_in_v2(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = review.write_pr_freeze_binding(
                root, "2026-07-19-r1", 7, 1, "a" * 40, "b" * 40,
                ["reviewer-x"], None, "docs/reviews",
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
            binding = _json.loads(path.read_text())
            binding.pop("rendered_request_digest")
            path.write_text(_json.dumps(binding) + "\n")
            with self.assertRaisesRegex(common.WorkflowError, "corrupt review binding"):
                review.read_pr_freeze_binding(path)

            binding["schema"] = review.PR_FREEZE_BINDING_V1_SCHEMA
            binding["rendered_request_digest"] = TEST_RENDERED_REQUEST_DIGEST
            path.write_text(_json.dumps(binding) + "\n")
            with self.assertRaisesRegex(common.WorkflowError, "corrupt review binding"):
                review.read_pr_freeze_binding(path)

    def test_prepare_rejects_narrative_protocol_lookalikes(self):
        lookalikes = (
            "- Reviewing: deadbeef", "- Reviewer: somebody", "- Project: foreign",
            "- Branch: other", "model: other", "effort: low", "review-target: deadbeef",
        )
        for index, lookalike in enumerate(lookalikes):
            with self.subTest(lookalike=lookalike), tempfile.TemporaryDirectory() as d:
                root, _target, narrative = self._closed_project(Path(d))
                narrative.write_text(self.NARRATIVE.replace(
                    "Fail-loud protocol boundaries.",
                    f"Fail-loud protocol boundaries.\n{lookalike}"))
                import contextlib
                import io
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(review.prepare_review_request(
                        root, self.ROUND_ID, narrative), 1, index)
                self.assertIn("narrative", err.getvalue().lower())
                self.assertIn("protocol", err.getvalue().lower())
                self.assertFalse((root / "docs" / "reviews" /
                                  f"{self.ROUND_ID}-request.md").exists())

    def test_prepare_rejects_wrapped_narrative_lookalikes(self):
        wrapped = (
            "  - Reviewer: somebody",
            "> - Reviewing: deadbeef",
            "* model: other",
            ">   effort: low",
            "```\n- Reviewing: deadbeef\n```",
            "- -Reviewer: tight",
            "1. model: other",
            "- [ ] review-target: deadbeef",
        )
        for index, lookalike in enumerate(wrapped):
            with self.subTest(lookalike=lookalike), tempfile.TemporaryDirectory() as d:
                root, _target, narrative = self._closed_project(Path(d))
                narrative.write_text(self.NARRATIVE.replace(
                    "Fail-loud protocol boundaries.",
                    f"Fail-loud protocol boundaries.\n{lookalike}"))
                import contextlib
                import io
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(review.prepare_review_request(
                        root, self.ROUND_ID, narrative), 1, index)
                self.assertIn("lookalike", err.getvalue().lower())
                self.assertFalse((root / "docs" / "reviews" /
                                  f"{self.ROUND_ID}-request.md").exists())

    def test_prepare_allows_ordinary_bullets_tables_and_quotes_in_narrative(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, _target, narrative = self._closed_project(Path(d))
            narrative.write_text(self.NARRATIVE.replace(
                "Fail-loud protocol boundaries.",
                "Fail-loud protocol boundaries.\n- 일반 불릿: 설명\n"
                "| model 열 | 값 |\n> 인용 문장입니다."))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    review.prepare_review_request(root, self.ROUND_ID, narrative), 0)

    def test_reclose_refuses_dirty_tracked_tree_even_at_same_head(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, _target, _narrative = self._closed_project(Path(d))
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks: []\n# dirty\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(round.reclose(root, self.ROUND_ID), 1)
            self.assertIn("commit the closeout", err.getvalue())

    def test_pr_freeze_republishes_rendered_request_not_disk_file(self):
        import contextlib
        import io

        captured = {}
        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d), mode="pr")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.prepare_review_request(
                    root, self.ROUND_ID, narrative), 0)
            request = review.prepared_request_path(root, self.ROUND_ID, mode="pr")
            request.write_text(request.read_text() + "\nTAMPERED-AFTER-PREPARE\n")
            ctx = {"repo": "o/r", "pr": 9, "head": target, "base_sha": "b" * 40,
                   "base": "main",
                   "bundle": {"head": target, "base_sha": "b" * 40, "bodies": []},
                   "policy": common.normalize_config(
                       {"version": 1, "project": "demo",
                        "review": {"mode": "pr", "reviewers": ["reviewer-x"]}})}

            def fake_gh(_root, *args):
                if len(args) >= 2 and args[0] == "pr" and args[1] == "comment":
                    captured["body"] = args[args.index("--body") + 1]
                return (0, "")

            saved = (review.pr_context, review._gh)
            review.pr_context = lambda _root, _pr: ctx
            review._gh = fake_gh
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(review.freeze(root, 9, self.ROUND_ID), 0)
            finally:
                review.pr_context, review._gh = saved
        self.assertIn(f"- Reviewing: {target}", captured["body"])
        self.assertNotIn("TAMPERED-AFTER-PREPARE", captured["body"])

    def test_pr_freeze_rejects_exposure_only_binding_field_tamper(self):
        import contextlib
        import io
        import json

        mutations = {
            "target_sha": lambda exposure: exposure.update(head_sha="a" * 40),
            "base_sha": lambda exposure: exposure.update(base_sha="b" * 40),
            "reviewers": lambda exposure: exposure.update(reviewers=["reviewer-y"]),
            "mode": lambda exposure: exposure.update(review_mode="packet"),
            "project": lambda exposure: exposure["project"].update(name="other-project"),
            "branch": lambda exposure: exposure["project"].update(branch="other-branch"),
        }
        for field, mutate in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as d:
                root, target, narrative = self._closed_project(Path(d), mode="pr")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(review.prepare_review_request(
                        root, self.ROUND_ID, narrative), 0)
                exposure_path, exposure = review.read_round_closeout_exposure(
                    root, self.ROUND_ID)
                mutate(exposure)
                exposure_path.write_text(json.dumps(exposure) + "\n")
                cfg = common.load_config(root)
                ctx = {
                    "repo": "o/r", "pr": 9, "head": target, "base_sha": "c" * 40,
                    "base": "main", "bundle": {
                        "head": target, "base_sha": "c" * 40, "bodies": []},
                    "policy": cfg,
                }
                posted = []
                saved = review.pr_context, review._gh
                review.pr_context = lambda _root, _pr: ctx
                review._gh = lambda _root, *args: (posted.append(args) or (0, "ok"))
                err = io.StringIO()
                try:
                    with contextlib.redirect_stderr(err):
                        self.assertEqual(review.freeze(root, 9, self.ROUND_ID), 1)
                finally:
                    review.pr_context, review._gh = saved
                self.assertEqual(posted, [])
                self.assertTrue(
                    "round exposure" in err.getvalue()
                    or "rendered request digest" in err.getvalue())

    def test_pr_freeze_keeps_explicit_prepared_pair_pr_mode_check(self):
        import inspect
        import re

        source = re.sub(r"\s+", "", inspect.getsource(review.freeze))
        self.assertIn('request_sidecar["mode"]!="pr"', source)

    def test_prepare_rejects_stale_exposure_with_reclose_guidance(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, _target, narrative = self._closed_project(Path(d))
            (root / "after.txt").write_text("new head\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "advance after close")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.prepare_review_request(
                    root, self.ROUND_ID, narrative), 1)
            self.assertIn("reclose", err.getvalue().lower())
            self.assertFalse((root / "docs" / "reviews" /
                              f"{self.ROUND_ID}-request.md").exists())

    def test_prepare_rejects_existing_mismatched_sidecar_without_supersede(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d))
            foreign = "a" * 40 if target != "a" * 40 else "b" * 40
            existing = review.write_round_request_binding(
                root, self.ROUND_ID, foreign, None, ["reviewer-x"], mode="packet",
                narrative_digest=TEST_NARRATIVE_DIGEST,
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.prepare_review_request(
                    root, self.ROUND_ID, narrative), 1)
            self.assertIn("new round id", err.getvalue().lower())
            self.assertEqual(list((root / "docs" / "reviews").glob(
                f"{self.ROUND_ID}-request.binding*.json")), [existing])
            self.assertFalse((root / "docs" / "reviews" /
                              f"{self.ROUND_ID}-request.md").exists())

    def test_prepare_rejects_noncanonical_binding_instead_of_reporting_success(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, _target, narrative = self._closed_project(Path(d))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.prepare_review_request(
                    root, self.ROUND_ID, narrative), 0)
            directory = root / "docs/reviews"
            canonical = next(directory.glob(
                f"{self.ROUND_ID}-request.binding*.json"))
            alias = canonical.with_name(
                f"{self.ROUND_ID}-request.binding-02.json")
            canonical.rename(alias)

            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = review.prepare_review_request(root, self.ROUND_ID, narrative)

            self.assertEqual(rc, 1)
            self.assertIn(str(alias), err.getvalue())
            self.assertNotIn("prepared review request binding", out.getvalue())
            self.assertFalse(canonical.exists())

    def test_prepare_supports_pr_mode_without_unrendered_placeholders(self):
        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d), mode="pr")
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            request = review.prepared_request_path(root, self.ROUND_ID, mode="pr")
            rendered = request.read_text()
            self.assertIn(f"- Reviewing: {target}   (diff against (root))", rendered)
            self.assertNotRegex(rendered, r"\[\[[A-Z_]+\]\]")
            binding = review.read_round_request_binding(next(
                request.parent.glob(f"{self.ROUND_ID}-request.binding*.json")))
            self.assertEqual(binding["mode"], "pr")
            self.assertEqual(binding["target_sha"], target)

    def test_round_reclose_rebinds_only_host_local_exposure_to_current_head(self):
        with tempfile.TemporaryDirectory() as d:
            root, _target, _narrative = self._closed_project(Path(d), mode="pr")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "closeout state")
            closeout = git(root, "rev-parse", "HEAD").stdout.strip()
            config_before = (root / ".waystone.yml").read_bytes()

            self.assertEqual(round.reclose(root, self.ROUND_ID), 0)
            _path, exposure = review.read_round_closeout_exposure(root, self.ROUND_ID)
            self.assertEqual(exposure["head_sha"], closeout)
            self.assertIsNone(exposure["base_sha"])
            self.assertEqual((root / ".waystone.yml").read_bytes(), config_before)
            self.assertEqual(git(root, "status", "--short").stdout, "")

    def test_waystone_dispatches_round_reclose(self):
        with tempfile.TemporaryDirectory() as d:
            root, _target, _narrative = self._closed_project(Path(d), mode="pr")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "closeout state")
            closeout = git(root, "rev-parse", "HEAD").stdout.strip()
            env = os.environ.copy()
            env.update({"HOME": str(Path(d) / "home"),
                        "WAYSTONE_HOME": str(Path(d) / "home" / ".waystone")})
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "waystone.py"), "round", "reclose",
                 str(root), "--round", self.ROUND_ID],
                cwd=root, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            _path, exposure = review.read_round_closeout_exposure(root, self.ROUND_ID)
            self.assertEqual(exposure["head_sha"], closeout)

    def test_pr_freeze_posts_prepared_request_as_the_comment_carrier(self):
        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d), mode="pr")
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            cfg = common.load_config(root)
            ctx = {
                "repo": "o/r", "pr": 7, "head": target, "base_sha": "b" * 40,
                "base": "main", "bundle": {
                    "head": target, "base_sha": "b" * 40, "bodies": []},
                "policy": cfg,
            }
            posted = []
            original_context, original_gh = review.pr_context, review._gh
            review.pr_context = lambda _root, _pr: ctx
            review._gh = lambda _root, *args: (posted.append(args) or (0, "ok"))
            try:
                self.assertEqual(review.freeze(root, 7, self.ROUND_ID), 0)
            finally:
                review.pr_context, review._gh = original_context, original_gh
            body = posted[0][posted[0].index("--body") + 1]
            self.assertIn(f"# Review Request — {self.ROUND_ID}", body)
            self.assertIn(f"- Reviewing: {target}   (diff against (root))", body)
            self.assertEqual(body.count("## Response wanted"), 1)

    def test_pr_freeze_posts_the_exact_digest_verified_request_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, target, narrative = self._closed_project(base, mode="pr")
            original_template = review.REVIEW_REQUEST_TEMPLATE
            trailing_template = base / "review-request.md"
            trailing_template.write_text(original_template.read_text().rstrip("\n") + "   \n")
            posted = []
            try:
                review.REVIEW_REQUEST_TEMPLATE = trailing_template
                self.assertEqual(review.prepare_review_request(
                    root, self.ROUND_ID, narrative), 0)
                request_text = review.prepared_request_path(
                    root, self.ROUND_ID, mode="pr").read_text()
                cfg = common.load_config(root)
                ctx = {
                    "repo": "o/r", "pr": 7, "head": target, "base_sha": "b" * 40,
                    "base": "main", "bundle": {
                        "head": target, "base_sha": "b" * 40, "bodies": []},
                    "policy": cfg,
                }
                original_context, original_gh = review.pr_context, review._gh
                review.pr_context = lambda _root, _pr: ctx
                review._gh = lambda _root, *args: (posted.append(args) or (0, "ok"))
                try:
                    self.assertEqual(review.freeze(root, 7, self.ROUND_ID), 0)
                finally:
                    review.pr_context, review._gh = original_context, original_gh
            finally:
                review.REVIEW_REQUEST_TEMPLATE = original_template

            body = posted[0][posted[0].index("--body") + 1]
            self.assertIn(request_text + "\n<!-- waystone-review-cycle:v2", body)
            self.assertTrue(request_text.endswith("   \n"))

    def test_reviewing_line_is_exact_and_malformed_input_warns(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            request = Path(d) / "request.md"
            target, base = "a" * 40, "b" * 40
            request.write_text(
                f"- Reviewing: {target}   (diff against {base})\n")
            self.assertEqual(review.parse_packet_request_binding(request), (target, base))

            malformed = (
                f"- Reviewing:  {target}   (diff against {base})\n",
                f"- Reviewing: {target}  (diff against {base})\n",
                f"- Reviewing: {target}\t(diff against {base})\n",
                f"- Reviewing: {target}   (diff against {base}) extra\n",
                f"- Reviewing: {target}   (diff against\n{base})\n",
                f"- Reviewing: {target}   (diff against {base})\r\n",
                (f"- Reviewing: {target}   (diff against {base})\n" * 2),
            )
            for text in malformed:
                request.write_text(text)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertIsNone(review.parse_packet_request_binding(request), text)
                self.assertIn("exactly one line", err.getvalue())
                self.assertIn(str(request), err.getvalue())

    def test_prepare_binds_request_to_closeout_head_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root, head, narrative = self._closed_project(Path(d))

            self.assertEqual(review.prepare_packet_request(
                root, self.ROUND_ID, narrative), 0)
            bindings = list((root / "docs" / "reviews").glob(
                f"{self.ROUND_ID}-request.binding*.json"))
            self.assertEqual(len(bindings), 1)
            self.assertEqual(review.prepare_packet_request(
                root, self.ROUND_ID, narrative), 0)
            self.assertEqual(list((root / "docs" / "reviews").glob(
                f"{self.ROUND_ID}-request.binding*.json")), bindings)

            (root / "advance.txt").write_text(head)
            git(root, "add", "-A")
            git(root, "commit", "-qm", "advance")
            self.assertEqual(review.prepare_packet_request(
                root, self.ROUND_ID, narrative), 1)

    def test_narrative_only_reprepare_reissues_binding_and_reopens_pending(self):
        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            first_path = next((root / "docs/reviews").glob(
                f"{self.ROUND_ID}-request.binding*.json"))
            first = review.read_round_request_binding(first_path)
            self.assertRegex(first["narrative_digest"], r"^sha256:[0-9a-f]{64}$")

            reply = Path(d) / "reply.md"
            reply.write_text(
                "model: reviewer-x\neffort: high\n"
                f"review-target: {target}\n"
                f"request-digest: {first['rendered_request_digest']}\n\nreviewed\n")
            self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)
            self.assertEqual(review.pending_reviews(root), [])

            narrative.write_text(self.NARRATIVE.replace(
                "Fail-loud protocol boundaries.",
                "Fail-loud protocol boundaries after a narrative correction."))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            paths = sorted((root / "docs/reviews").glob(
                f"{self.ROUND_ID}-request.binding*.json"))
            self.assertEqual(len(paths), 2)
            _latest_path, latest = review.latest_round_request_binding(
                paths, expected_round_id=self.ROUND_ID)
            self.assertIsNotNone(latest)
            self.assertNotEqual(first["narrative_digest"], latest["narrative_digest"])
            self.assertEqual(
                [(row["round_id"], row["target_sha"]) for row in review.pending_reviews(root)],
                [(self.ROUND_ID, target)],
            )

    def test_reprepare_crash_after_each_projection_write_stays_pending(self):
        from unittest import mock

        class SimulatedCrash(BaseException):
            pass

        for stop_after in (1, 2):
            with self.subTest(stop_after=stop_after), tempfile.TemporaryDirectory() as d:
                root, target, narrative = self._closed_project(Path(d))
                self.assertEqual(review.prepare_review_request(
                    root, self.ROUND_ID, narrative), 0)
                request = root / "docs/reviews" / f"{self.ROUND_ID}-request.md"
                stored_narrative = review.stored_narrative_path(root, self.ROUND_ID)
                old_request = request.read_bytes()
                old_narrative = stored_narrative.read_bytes()
                request_digest = review._rendered_request_digest_echo(request.read_text())
                reply = Path(d) / "reply.md"
                reply.write_text(
                    "model: reviewer-x\neffort: high\n"
                    f"review-target: {target}\n"
                    f"request-digest: {request_digest}\n\nreviewed\n")
                self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)
                self.assertEqual(review.pending_reviews(root), [])

                narrative.write_text(self.NARRATIVE.replace(
                    "Fail-loud protocol boundaries.",
                    "Fail-loud protocol boundaries after interrupted reprepare."))
                new_narrative = review._read_review_narrative(narrative)
                original_write = review.write_bytes_atomic
                writes = []

                def interrupt_after_write(path, content):
                    original_write(path, content)
                    writes.append(Path(path))
                    if len(writes) == stop_after:
                        raise SimulatedCrash

                with mock.patch.object(
                        review, "write_bytes_atomic", side_effect=interrupt_after_write):
                    with self.assertRaises(SimulatedCrash):
                        review.prepare_review_request(root, self.ROUND_ID, narrative)

                self.assertNotEqual(request.read_bytes(), old_request)
                self.assertEqual(
                    stored_narrative.read_bytes() != old_narrative, stop_after == 2)
                latest_path, latest = review.latest_round_request_binding(
                    list(request.parent.glob(f"{self.ROUND_ID}-request.binding*.json")),
                    expected_round_id=self.ROUND_ID)
                self.assertIsNotNone(latest_path)
                self.assertIsNotNone(latest)
                self.assertEqual(
                    latest["narrative_digest"],
                    review._canonical_narrative_digest(new_narrative))
                pending = review.pending_reviews(root)
                self.assertEqual([row["round_id"] for row in pending], [self.ROUND_ID])

    def test_pending_exposes_latest_binding_projection_mismatches(self):
        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            request = root / "docs/reviews" / f"{self.ROUND_ID}-request.md"
            request_digest = review._rendered_request_digest_echo(request.read_text())
            reply = Path(d) / "reply.md"
            reply.write_text(
                "model: reviewer-x\neffort: high\n"
                f"review-target: {target}\n"
                f"request-digest: {request_digest}\n\nreviewed\n")
            self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)
            self.assertEqual(review.pending_reviews(root), [])

            original_request = request.read_bytes()
            request.write_bytes(original_request + b"mutated request\n")
            pending = review.pending_reviews(root)
            self.assertEqual(pending[0]["reason"], "request-digest-mismatch")
            self.assertIn("request-digest-mismatch", review.format_pending_review(pending[0]))

            request.write_bytes(original_request)
            displayed = review._rendered_request_digest_echo(request.read_text())
            request.write_text(request.read_text().replace(
                f"request-digest: {displayed}", f"request-digest: {'sha256:' + 'f' * 64}"))
            self.assertEqual(
                review.pending_reviews(root)[0]["reason"], "request-digest-mismatch")

            request.write_bytes(original_request)
            stored_narrative = review.stored_narrative_path(root, self.ROUND_ID)
            stored_narrative.write_bytes(stored_narrative.read_bytes() + b"mutated narrative\n")
            pending = review.pending_reviews(root)
            self.assertEqual(pending[0]["reason"], "stored-narrative-digest-mismatch")

    def test_render_only_reprepare_invalidates_old_feedback(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            request = root / "docs/reviews" / f"{self.ROUND_ID}-request.md"
            request_digest = review._rendered_request_digest_echo(request.read_text())
            reply = Path(d) / "reply.md"
            reply.write_text(
                "model: reviewer-x\neffort: high\n"
                f"review-target: {target}\n"
                f"request-digest: {request_digest}\n\nreviewed\n")
            self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)
            self.assertEqual(review.pending_reviews(root), [])

            revised_template = Path(d) / "review-request.md"
            revised_template.write_text(
                review.REVIEW_REQUEST_TEMPLATE.read_text().replace(
                    "This is a domain/code review", "This is a revised domain/code review"))
            with mock.patch.object(review, "REVIEW_REQUEST_TEMPLATE", revised_template):
                self.assertEqual(review.prepare_review_request(
                    root, self.ROUND_ID, narrative), 0)
                pending = review.pending_reviews(root)

            self.assertEqual([row["round_id"] for row in pending], [self.ROUND_ID])
            self.assertEqual(
                pending[0]["reason"], "feedback-request-digest-stale-generation")

    def test_stamped_feedback_blocks_legacy_fallback_if_latest_digest_is_stripped(self):
        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            reply = Path(d) / "reply.md"
            reply.write_text(
                "model: reviewer-x\neffort: high\n"
                f"review-target: {target}\n\nreviewed\n")
            self.assertEqual(review.ingest(root, self.ROUND_ID, src=reply), 0)

            narrative.write_text(self.NARRATIVE.replace(
                "Fail-loud protocol boundaries.", "A second valid narrative."))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            latest_path, _latest = review.latest_round_request_binding(
                list((root / "docs/reviews").glob(
                    f"{self.ROUND_ID}-request.binding*.json")),
                expected_round_id=self.ROUND_ID)
            self.assertIsNotNone(latest_path)
            latest = _json.loads(latest_path.read_text())
            latest.pop("narrative_digest")
            latest_path.write_text(_json.dumps(latest) + "\n")

            with self.assertRaisesRegex(common.WorkflowError, "corrupt review binding"):
                review.read_round_request_binding(latest_path)
            self.assertEqual([row["round_id"] for row in review.pending_reviews(root)],
                             [self.ROUND_ID])

    def test_ingest_rejects_digest_strip_and_v1_downgrade_for_digest_era_round(self):
        import contextlib
        import io

        for downgrade in (False, True):
            with self.subTest(downgrade=downgrade), tempfile.TemporaryDirectory() as d:
                root = Path(d) / "repo"
                root.mkdir()
                init_repo(root)
                (root / ".waystone.yml").write_text(
                    "version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
                round_id = "2026-07-19-digest-era"
                rdir = root / "docs/reviews"
                rdir.mkdir(parents=True)
                (rdir / f"{round_id}-request.md").write_text("request\n")
                target, base_sha = "a" * 40, "b" * 40
                binding_path = rdir / f"{round_id}-request.binding.json"
                binding = {
                    "schema": "waystone-round-request-binding-2",
                    "round_id": round_id, "target_sha": target, "base_sha": base_sha,
                    "reviewers": ["reviewer-x"], "mode": "packet",
                    "canonical_store": "local-packet",
                    "narrative_digest": TEST_NARRATIVE_DIGEST,
                    "rendered_request_digest": TEST_RENDERED_REQUEST_DIGEST,
                    "at": "2026-07-19T00:00:00+00:00",
                }
                binding_path.write_text(_json.dumps(binding) + "\n")
                bare = Path(d) / "remote.git"
                subprocess.run(
                    ["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
                git(root, "remote", "add", "origin", str(bare))
                git(root, "add", "-A")
                git(root, "commit", "-qm", "publish digest-bound packet")
                git(root, "push", "-q", "-u", "origin", "main")
                binding.pop("narrative_digest")
                binding.pop("rendered_request_digest")
                if downgrade:
                    binding["schema"] = "waystone-round-request-binding-1"
                binding_path.write_text(_json.dumps(binding) + "\n")
                reply = Path(d) / "reply.md"
                reply.write_text(
                    "model: reviewer-x\neffort: high\n"
                    f"review-target: {base_sha[:12]}-{target[:12]}\n\nreviewed\n")
                err = io.StringIO()

                with contextlib.redirect_stderr(err):
                    self.assertEqual(review.ingest(root, round_id, src=reply), 1)

                self.assertIn("corrupt", err.getvalue())
                self.assertFalse((rdir / f"{round_id}-feedback.md").exists())
                self.assertEqual(
                    [row["round_id"] for row in review.pending_reviews(root)], [round_id])

    def test_pr_freeze_rejects_stored_narrative_digest_mismatch(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, target, narrative = self._closed_project(Path(d), mode="pr")
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative), 0)
            review.stored_narrative_path(root, self.ROUND_ID).write_text(
                self.NARRATIVE.replace(
                    "Fail-loud protocol boundaries.", "A different stored narrative."))
            cfg = common.load_config(root)
            ctx = {
                "repo": "o/r", "pr": 9, "head": target, "base_sha": "b" * 40,
                "base": "main", "bundle": {
                    "head": target, "base_sha": "b" * 40, "bodies": []},
                "policy": cfg,
            }
            posted = []
            saved = review.pr_context, review._gh
            review.pr_context = lambda _root, _pr: ctx
            review._gh = lambda _root, *args: (posted.append(args) or (0, "ok"))
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    self.assertEqual(review.freeze(root, 9, self.ROUND_ID), 1)
            finally:
                review.pr_context, review._gh = saved
            self.assertEqual(posted, [])
            self.assertIn("narrative digest", err.getvalue())

    def test_packet_gate_rejects_post_prepare_narrative_edit(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, _target, _narrative = self._remote_project(Path(d))
            request = root / "docs/reviews" / f"{self.ROUND_ID}-request.md"
            request.write_text(request.read_text().replace(
                "The round made packet rendering deterministic.",
                "The packet narrative was altered after prepare."))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.verify_packet_publication(root, self.ROUND_ID), 1)
            self.assertIn("does not reproduce", err.getvalue())

    def test_packet_gate_rejects_live_template_change_against_stored_render_digest(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, _target, _narrative = self._remote_project(Path(d))
            original_template = review.REVIEW_REQUEST_TEMPLATE
            changed_template = Path(d) / "review-request.md"
            changed_template.write_text(
                original_template.read_text().replace(
                    "# Review Request", "# Changed Review Request", 1))
            err = io.StringIO()
            try:
                review.REVIEW_REQUEST_TEMPLATE = changed_template
                with contextlib.redirect_stderr(err):
                    self.assertEqual(
                        review.verify_packet_publication(root, self.ROUND_ID), 1)
            finally:
                review.REVIEW_REQUEST_TEMPLATE = original_template

            self.assertIn("rendered request digest", err.getvalue())

    def test_packet_gate_rejects_legacy_digestless_binding(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            round_id = "2026-07-18-genuine-legacy"
            target = "a" * 40
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n"
                "review:\n  mode: packet\n  reviewers: [reviewer-x]\n")
            request = root / "docs/reviews" / f"{round_id}-request.md"
            request.parent.mkdir(parents=True)
            request.write_text(
                f"# Review Request — {round_id}\n\n"
                f"- Reviewing: {target}   (diff against (root))\n")
            write_legacy_round_request_binding(
                root, round_id, target, None, ["reviewer-x"])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.verify_packet_publication(root, round_id), 1)
            self.assertIn("legacy-pre-digest", err.getvalue())

    def test_packet_publication_gate_uses_real_remote_and_rejects_partial_commit(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            bare, root, home = base / "remote.git", base / "work", base / "home"
            subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
            root.mkdir()
            home.mkdir()
            init_repo(root)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n"
                "review:\n  mode: packet\n  reviewers: [reviewer-x]\n"
                "state:\n  last_round_commit: null\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "closeout")
            self.assertEqual(round.close(
                root, self.ROUND_ID, done=[], touched=[], commit="HEAD"), 0)
            git(root, "remote", "add", "origin", str(bare))
            git(root, "push", "-q", "-u", "origin", "main")
            narrative = base / "narrative.md"
            narrative.write_text(self.NARRATIVE)
            env = os.environ.copy()
            env.update({"HOME": str(home), "WAYSTONE_HOME": str(home / ".waystone")})

            def cli(*args: str):
                return subprocess.run(
                    [sys.executable, str(SCRIPTS / "waystone.py"), *args],
                    cwd=root, env=env, capture_output=True, text=True, timeout=15)

            prepared = cli(
                "review", "prepare", "--round", self.ROUND_ID,
                "--narrative", str(narrative), str(root))
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            request = root / "docs" / "reviews" / f"{self.ROUND_ID}-request.md"
            binding = next((root / "docs" / "reviews").glob(
                f"{self.ROUND_ID}-request.binding*.json"))

            untracked = cli("remote", "verify", str(root), "--round", self.ROUND_ID)
            self.assertNotEqual(untracked.returncode, 0)
            self.assertIn("not published", untracked.stderr)

            git(root, "add", str(request.relative_to(root)))
            git(root, "commit", "-qm", "publish request without binding")
            git(root, "push", "-q")
            partial = cli("remote", "verify", str(root), "--round", self.ROUND_ID)
            self.assertNotEqual(partial.returncode, 0)
            self.assertIn("binding", partial.stderr)

            git(root, "add", str(binding.relative_to(root)))
            git(root, "commit", "--amend", "-qm", "publish review request")
            git(root, "push", "-q", "--force-with-lease")
            published = cli("remote", "verify", str(root), "--round", self.ROUND_ID)
            self.assertEqual(published.returncode, 0, published.stderr)
            self.assertIn("request and binding", published.stdout)

    def _remote_project(self, base: Path) -> tuple[Path, str, Path]:
        bare = base / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        root, target, narrative = self._closed_project(base)
        git(root, "remote", "add", "origin", str(bare))
        git(root, "push", "-q", "-u", "origin", "main")
        self.assertEqual(review.prepare_review_request(root, self.ROUND_ID, narrative), 0)
        return root, target, narrative

    def _publish_remote_packet(self, base: Path) -> tuple[Path, Path, str]:
        publisher, target, _narrative = self._remote_project(base)
        git(publisher, "add", "-A")
        git(publisher, "commit", "-qm", "publish packet")
        git(publisher, "push", "-q")
        bare = base / "remote.git"
        validator = base / "validator"
        subprocess.run(["git", "clone", "-q", str(bare), str(validator)], check=True)
        shutil.copytree(publisher / ".waystone", validator / ".waystone", dirs_exist_ok=True)
        return validator, bare, target

    def test_round_verify_rejects_deleted_upstream_despite_stale_tracking_ref(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, bare, _target = self._publish_remote_packet(base)
            stale = git(root, "rev-parse", "refs/remotes/origin/main").stdout.strip()
            deleter = base / "deleter"
            subprocess.run(["git", "clone", "-q", str(bare), str(deleter)], check=True)
            git(bare, "config", "receive.denyDeleteCurrent", "ignore")
            self.assertEqual(
                git(deleter, "push", "-q", "origin", "--delete", "main").returncode, 0)
            self.assertEqual(
                git(root, "rev-parse", "refs/remotes/origin/main").stdout.strip(), stale)

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(remote.verify(root, self.ROUND_ID), 3)
            self.assertIn("upstream branch", err.getvalue())
            self.assertIn("absent", err.getvalue())

    def test_round_verify_ignores_excluded_stale_tracking_ref(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, bare, target = self._publish_remote_packet(base)
            stale = git(root, "rev-parse", "refs/remotes/origin/main").stdout.strip()
            git(root, "config", "--add", "remote.origin.fetch", "^refs/heads/main")

            publisher = base / "publisher"
            subprocess.run(["git", "clone", "-q", str(bare), str(publisher)], check=True)
            git(publisher, "config", "user.email", "t@t")
            git(publisher, "config", "user.name", "t")
            self.assertEqual(
                git(publisher, "checkout", "-q", "--orphan", "replacement").returncode, 0)
            git(publisher, "rm", "-qrf", ".")
            (publisher / "replacement.txt").write_text("unrelated remote history\n")
            git(publisher, "add", "-A")
            git(publisher, "commit", "-qm", "replace upstream")
            replacement = git(publisher, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(
                git(publisher, "push", "-q", "--force", "origin", "HEAD:main").returncode, 0)
            self.assertNotEqual(replacement, target)
            self.assertEqual(git(root, "fetch", "-q", "origin").returncode, 0)
            self.assertEqual(
                git(root, "rev-parse", "refs/remotes/origin/main").stdout.strip(), stale)

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(remote.verify(root, self.ROUND_ID), 3)
            self.assertIn("not contained", err.getvalue())

    def test_round_verify_rejects_local_dot_upstream(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, _bare, _target = self._publish_remote_packet(Path(d))
            git(root, "config", "branch.main.remote", ".")
            git(root, "config", "branch.main.merge", "refs/heads/main")

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(remote.verify(root, self.ROUND_ID), 3)
            self.assertIn("local repository", err.getvalue())
            self.assertIn("not remote publication", err.getvalue())

    def test_shallow_validator_reports_unknown_ancestry(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            full_validator, bare, _target = self._publish_remote_packet(base)
            shallow = base / "shallow-validator"
            subprocess.run([
                "git", "clone", "-q", "--depth=1", f"file://{bare}", str(shallow),
            ], check=True)
            shutil.copytree(
                full_validator / ".waystone", shallow / ".waystone", dirs_exist_ok=True)

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.verify_packet_publication(shallow, self.ROUND_ID), 1)
            self.assertIn("cannot determine", err.getvalue())
            self.assertIn("shallow", err.getvalue())

    def test_shallow_rc_one_is_unverifiable_but_full_history_is_definitive(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, bare = base / "source", base / "remote.git"
            shallow, full = base / "shallow", base / "full"
            source.mkdir()
            init_repo(source)
            ancestor = git(source, "rev-parse", "HEAD").stdout.strip()
            git(source, "branch", "ancestor", ancestor)
            for value in ("1", "2"):
                (source / "f.txt").write_text(value)
                git(source, "commit", "-qam", f"c{value}")
            tip = git(source, "rev-parse", "HEAD").stdout.strip()
            subprocess.run(["git", "clone", "-q", "--bare", str(source), str(bare)], check=True)
            subprocess.run(["git", "clone", "-q", str(bare), str(full)], check=True)
            subprocess.run([
                "git", "clone", "-q", "--depth=1", "--branch", "main",
                f"file://{bare}", str(shallow),
            ], check=True)
            self.assertEqual(git(
                shallow, "fetch", "-q", "--depth=1", "origin",
                "ancestor:refs/remotes/origin/ancestor").returncode, 0)

            self.assertEqual(
                git(shallow, "rev-parse", "--is-shallow-repository").stdout.strip(), "true")
            self.assertEqual(
                git(shallow, "merge-base", "--is-ancestor", ancestor, tip).returncode, 1)
            contained, reason = common.ancestry_status(shallow, ancestor, tip)
            self.assertIsNone(contained)
            self.assertIn("shallow", reason)
            self.assertFalse(common.is_ancestor(shallow, ancestor, tip))

            self.assertEqual(
                git(full, "rev-parse", "--is-shallow-repository").stdout.strip(), "false")
            self.assertEqual(common.ancestry_status(full, ancestor, tip), (True, ""))
            self.assertEqual(
                git(full, "merge-base", "--is-ancestor", tip, ancestor).returncode, 1)
            self.assertEqual(common.ancestry_status(full, tip, ancestor), (False, ""))

    def test_direct_binding_rejects_committed_but_unpushed_packet(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, _target, _narrative = self._remote_project(Path(d))
            git(root, "add", "-A")
            git(root, "commit", "-qm", "publish packet locally only")

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.verify_packet_publication(root, self.ROUND_ID), 1)
            self.assertIn("not published", err.getvalue())

            git(root, "push", "-q")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(review.verify_packet_publication(root, self.ROUND_ID), 0)

    def test_latest_unpublished_binding_is_the_judged_packet(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            bare = base / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
            root, target, narrative_path = self._closed_project(base)
            git(root, "remote", "add", "origin", str(bare))
            self.assertEqual(review.prepare_review_request(
                root, self.ROUND_ID, narrative_path), 0)
            git(root, "add", "-A")
            git(root, "commit", "-qm", "publish first packet")
            git(root, "push", "-q", "-u", "origin", "main")

            _exposure_path, exposure = review.read_round_closeout_exposure(
                root, self.ROUND_ID)
            narrative = review._read_review_narrative(narrative_path).replace(
                "Fail-loud protocol boundaries.", "Reissued narrative contract.")
            review.stored_narrative_path(root, self.ROUND_ID).write_text(narrative)
            request = review.prepared_request_path(root, self.ROUND_ID, mode="packet")
            request.write_text(review._render_review_request(
                self.ROUND_ID, exposure, narrative))
            review.write_round_request_binding(
                root, self.ROUND_ID, target, None, ["reviewer-x"], mode="packet",
                narrative_digest=review._canonical_narrative_digest(narrative),
                rendered_request_digest=review._canonical_rendered_request_digest(
                    request.read_text()))
            git(root, "add", "-A")
            git(root, "commit", "-qm", "reissued packet committed locally only")

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.verify_packet_publication(root, self.ROUND_ID), 1)
            # The LATEST binding's target is what gets judged — the stale published packet
            # must not be the publication evidence.
            self.assertIn("differs from the published copy", err.getvalue())

            git(root, "push", "-q")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(review.verify_packet_publication(root, self.ROUND_ID), 0)

    def test_symlinked_packet_artifacts_are_rejected(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, _target, _narrative = self._remote_project(Path(d))
            git(root, "add", "-A")
            git(root, "commit", "-qm", "publish packet")
            git(root, "push", "-q")
            request = root / "docs" / "reviews" / f"{self.ROUND_ID}-request.md"
            real = root / "docs" / "reviews" / "elsewhere.md"
            request.rename(real)
            request.symlink_to(real.name)

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.verify_packet_publication(root, self.ROUND_ID), 1)
            self.assertIn("regular file", err.getvalue())

    def test_gate_source_has_no_ancestry_topology_checks(self):
        import inspect
        import re

        source = inspect.getsource(review.verify_packet_publication)
        # Whitespace-normalized so `is_ancestor (` spellings cannot slip past the count.
        flat = re.sub(r"\s+", "", source)
        topology_flat = flat.replace("fetch_upstream_head", "")
        for banned in ("HEAD", "first_parent", "parents", "head_pushed"):
            self.assertNotIn(
                banned, topology_flat.replace("mergeparents,first-parentchains,HEAD", ""))
        # Exactly ONE containment primitive, and it binds the two pinned literals — any second
        # ancestry_status (or one anchored to a symbolic ref) is topology inference creeping back.
        self.assertEqual(flat.count("ancestry_status("), 1)
        self.assertIn(
            'contained,ancestry_error=ancestry_status('
            'root,binding["target_sha"],remote_sha)', flat)
        # The CLI boundary must not re-introduce a HEAD-topology precheck on the --round path:
        # head_pushed belongs to the no-round question only (asserted as exactly one call site).
        remote_flat = re.sub(r"\s+", "", inspect.getsource(remote.verify))
        self.assertEqual(remote_flat.count("head_pushed("), 1)
        round_branch = remote_flat.split("pushed,info=head_pushed(")[0]
        self.assertIn("verify_packet_publication", round_branch)

    def test_round_verify_judges_remote_not_local_head(self):
        with tempfile.TemporaryDirectory() as d:
            root, target, _narrative = self._remote_project(Path(d))
            git(root, "add", "-A")
            git(root, "commit", "-qm", "publish packet")
            git(root, "push", "-q")
            published_head = git(root, "rev-parse", "HEAD").stdout.strip()
            # Diverge the local HEAD away from the published history (force-push shape): the
            # remote still carries the closeout and the packet bytes, so --round must pass.
            git(root, "reset", "-q", "--hard", target)
            git(root, "checkout", "-q", str(published_head), "--", "docs/reviews")
            (root / "diverged.txt").write_text("local rewrite\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "diverged local history")

            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = remote.verify(root, self.ROUND_ID)
            self.assertEqual(rc, 0, out.getvalue())
            rendered = out.getvalue()
            self.assertIn("request and binding", rendered)
            self.assertIn(target, rendered)          # the verified closeout (binding literal)
            self.assertIn(published_head[:12], rendered)  # the pinned remote tip
            local_head = git(root, "rev-parse", "HEAD").stdout.strip()
            self.assertNotEqual(local_head, published_head)
            self.assertNotIn(local_head[:12], rendered)   # local HEAD absent from the verdict

    def test_published_stale_sidecar_cannot_stand_in_for_local_latest(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, _target, _narrative = self._remote_project(Path(d))
            git(root, "add", "-A")
            git(root, "commit", "-qm", "publish first packet")
            git(root, "push", "-q")
            # Reissue mid-state: the REWRITTEN request is published, but the
            # newest binding-2 exists only locally — the stale published sidecar must not be
            # accepted as the publication evidence.
            _exposure_path, exposure = review.read_round_closeout_exposure(
                root, self.ROUND_ID)
            narrative = review._read_review_narrative(
                review.stored_narrative_path(root, self.ROUND_ID)).replace(
                    "Fail-loud protocol boundaries.", "Reissued narrative contract.")
            review.stored_narrative_path(root, self.ROUND_ID).write_text(narrative)
            request = review.prepared_request_path(root, self.ROUND_ID, mode="packet")
            request.write_text(review._render_review_request(
                self.ROUND_ID, exposure, narrative))
            git(root, "add", "-A")
            git(root, "commit", "-qm", "publish rewritten request only")
            git(root, "push", "-q")
            review.write_round_request_binding(
                root, self.ROUND_ID, exposure["head_sha"], None, ["reviewer-x"], mode="packet",
                narrative_digest=review._canonical_narrative_digest(narrative),
                rendered_request_digest=review._canonical_rendered_request_digest(
                    request.read_text()))

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.verify_packet_publication(root, self.ROUND_ID), 1)
            self.assertIn("binding-2", err.getvalue())
            self.assertIn("not published", err.getvalue())

    def test_improve_preserves_corrupt_latest_request_binding_as_unknown(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "reviews"
            rdir.mkdir()
            corrupt = rdir / f"{self.ROUND_ID}-request.binding.json"
            corrupt.write_text("{not-json")
            corrupt_freeze = rdir / f"{self.ROUND_ID}-freeze-1.binding.json"
            corrupt_freeze.write_text("{also-not-json")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                sidecars = improve._round_review_sidecars(rdir)
            self.assertEqual(sidecars[self.ROUND_ID][0]["_binding_error"],
                             "corrupt-round-request-sidecar")
            self.assertIn("corrupt review binding", err.getvalue())
            self.assertIn(str(corrupt), err.getvalue())
            self.assertIn(str(corrupt_freeze), err.getvalue())
            with self.assertRaisesRegex(common.WorkflowError, "corrupt review binding"):
                review.write_pr_freeze_binding(
                    Path(d), self.ROUND_ID, 1, 1, "a" * 40, "b" * 40,
                    ["codex:test"], None, "reviews",
                    rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)


class RoundExposureTests(unittest.TestCase):
    """Round exposure is immutable, session-bound, and required for a successful close."""

    def _exposure_dir(self, root, home):
        return _run_with_home(home, lambda: overlay._exposure_dir(root))

    def test_close_records_round_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            _write_profile(root)  # profile present -> bindings/fingerprint non-null
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: round.close(
                    root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"], touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            p = self._exposure_dir(root, home) / f"round-{TEST_CLOSE_ROUND_ID}.json"
            self.assertTrue(p.exists())
            exp = _json.loads(p.read_text())
            self.assertEqual(exp["schema"], "waystone-round-exposure-1")
            self.assertEqual(exp["round_id"], TEST_CLOSE_ROUND_ID)
            self.assertIsNotNone(exp["profile_fingerprint"])
            self.assertEqual(exp["bindings"]["implementer"], "codex:gpt-5.4-codex")
            self.assertEqual(exp["guards"], None)
            self.assertEqual(exp["waivers"], [])
            bindings = list((root / "docs" / "reviews").glob(
                f"{TEST_CLOSE_ROUND_ID}-request.binding*.json"))
            self.assertEqual(bindings, [])

            closeout = git(root, "rev-parse", "HEAD").stdout.strip()
            narrative = Path(d) / "narrative.md"
            narrative.write_text(PacketPublicationTests.NARRATIVE)
            self.assertEqual(review.prepare_packet_request(
                root, TEST_CLOSE_ROUND_ID, narrative), 0)
            binding_path = next((root / "docs" / "reviews").glob(
                f"{TEST_CLOSE_ROUND_ID}-request.binding*.json"))
            binding = _json.loads(binding_path.read_text())
            self.assertEqual(binding["round_id"], TEST_CLOSE_ROUND_ID)
            self.assertEqual(binding["target_sha"], closeout)
            self.assertEqual(binding["canonical_store"], "local-packet")

    def test_profile_absent_null_bindings(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)  # no profile written
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                _run_with_home(home, lambda: round.close(
                    root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"], touched=[], commit="HEAD"))
            p = self._exposure_dir(root, home) / f"round-{TEST_CLOSE_ROUND_ID}.json"
            exp = _json.loads(p.read_text())
            self.assertIsNone(exp["profile_fingerprint"])
            self.assertIsNone(exp["bindings"])

    def test_reclose_gets_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            import contextlib
            import io
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    _run_with_home(home, lambda: round.close(
                        root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"], touched=[], commit="HEAD"))
            edir = self._exposure_dir(root, home)
            self.assertTrue((edir / f"round-{TEST_CLOSE_ROUND_ID}.json").exists())
            self.assertTrue((edir / f"round-{TEST_CLOSE_ROUND_ID}-2.json").exists())

    def test_same_round_reclose_preserves_original_previous_round_diff_base(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            previous_round_tip = git(root, "rev-parse", "HEAD").stdout.strip()
            config_path = root / ".waystone.yml"
            config_path.write_text(round.set_config_scalar(
                config_path.read_text(), "last_round_commit", previous_round_tip, section="state"))
            (root / "round-work.txt").write_text("round work\n")
            git(root, "add", ".waystone.yml", "round-work.txt")
            git(root, "commit", "-qm", "round work")
            first_close_tip = git(root, "rev-parse", "HEAD").stdout.strip()

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: round.close(
                    root, TEST_CLOSE_ROUND_ID,
                    done=["chore/close-me"], touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            _first_path, first = _run_with_home(
                home, lambda: review.read_round_closeout_exposure(root, TEST_CLOSE_ROUND_ID))
            self.assertEqual(first["head_sha"], first_close_tip)
            self.assertEqual(first["base_sha"], previous_round_tip)

            # Model a pre-fix generation whose base already drifted to this round's first tip.
            _run_with_home(home, lambda: overlay.write_round_exposure(
                root, TEST_CLOSE_ROUND_ID, first_close_tip, first_close_tip,
                base_sha=first_close_tip, reviewers=first["reviewers"]))
            _drifted_path, drifted = _run_with_home(
                home, lambda: review.read_round_closeout_exposure(root, TEST_CLOSE_ROUND_ID))
            self.assertEqual(drifted["base_sha"], first_close_tip)

            (root / "after-close.txt").write_text("follow-up\n")
            git(root, "add", ".waystone.yml", "tasks.yaml", "ROADMAP.md", "after-close.txt")
            git(root, "commit", "-qm", "closeout plus follow-up")
            reclose_head = git(root, "rev-parse", "HEAD").stdout.strip()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: round.close(
                    root, TEST_CLOSE_ROUND_ID, done=[], touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            _latest_path, latest = _run_with_home(
                home, lambda: review.read_round_closeout_exposure(root, TEST_CLOSE_ROUND_ID))
            self.assertEqual(latest["head_sha"], reclose_head)
            self.assertEqual(latest["base_sha"], previous_round_tip)
            self.assertNotEqual(latest["base_sha"], first_close_tip)

            narrative = Path(d) / "narrative.md"
            narrative.write_text(PacketPublicationTests.NARRATIVE)
            self.assertEqual(_run_with_home(
                home, lambda: review.prepare_packet_request(
                    root, TEST_CLOSE_ROUND_ID, narrative)), 0)
            request = root / "docs" / "reviews" / f"{TEST_CLOSE_ROUND_ID}-request.md"
            self.assertIn(
                f"- Reviewing: {reclose_head}   (diff against {previous_round_tip})",
                request.read_text())
            binding_path = next((root / "docs" / "reviews").glob(
                f"{TEST_CLOSE_ROUND_ID}-request.binding*.json"))
            binding = review.read_round_request_binding(binding_path)
            self.assertEqual(binding["target_sha"], reclose_head)
            self.assertEqual(binding["base_sha"], previous_round_tip)

    def test_exposure_open_x_collision_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            target = self._exposure_dir(root, home) / "round-race.json"
            original_open = Path.open
            injected = {"done": False}

            def raced_open(path, mode="r", *args, **kwargs):
                if path == target and mode == "x" and not injected["done"]:
                    injected["done"] = True
                    with original_open(path, "w", encoding="utf-8") as stream:
                        stream.write("sentinel\n")
                return original_open(path, mode, *args, **kwargs)

            Path.open = raced_open
            try:
                path, _exposure = _run_with_home(
                    home, lambda: overlay.write_round_exposure(root, "race", None, None))
            finally:
                Path.open = original_open
            self.assertTrue(injected["done"])
            self.assertEqual(target.read_text(), "sentinel\n")
            self.assertEqual(path.name, "round-race-2.json")

    def test_exposure_failure_fails_close_and_rolls_back_registry(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            orig = overlay.write_round_exposure
            overlay.write_round_exposure = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
            import contextlib
            import io
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: round.close(
                        root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"], touched=[], commit="HEAD"))
            finally:
                overlay.write_round_exposure = orig
            self.assertEqual(rc, 1)
            self.assertIn("exposure", err.getvalue().lower())
            task = common.load_tasks(root)["tasks"][0]
            self.assertEqual(task["status"], "active")
            self.assertNotIn("round", task)

    def test_session_id_is_recorded_in_registry_and_exposure(self):
        import contextlib
        import io
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            session_id = "11111111-2222-3333-4444-555555555555"
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session_id}), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: round.close(
                    root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"],
                    touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            task = common.load_tasks(root)["tasks"][0]
            self.assertEqual(task["session_id"], session_id)
            exposure = _json.loads(
                (self._exposure_dir(root, home) / f"round-{TEST_CLOSE_ROUND_ID}.json").read_text())
            self.assertEqual(exposure["session_id"], session_id)

    def test_absent_session_id_is_recorded_as_null(self):
        import contextlib
        import io
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            with mock.patch.dict(os.environ, {}, clear=False), \
                    contextlib.redirect_stdout(io.StringIO()):
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
                os.environ.pop("CODEX_THREAD_ID", None)
                rc = _run_with_home(home, lambda: round.close(
                    root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"],
                    touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            task = common.load_tasks(root)["tasks"][0]
            self.assertIsNone(task["session_id"])
            exposure = _json.loads(
                (self._exposure_dir(root, home) / f"round-{TEST_CLOSE_ROUND_ID}.json").read_text())
            self.assertIsNone(exposure["session_id"])
