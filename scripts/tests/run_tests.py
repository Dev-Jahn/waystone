#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Integration tests for the jahns-workflow v0.2.0 correctness kernel.

Run: uv run scripts/tests/run_tests.py
Covers the deterministic core: merge-gate computation, review-cycle marker emit/parse/classify,
SHA-bound approval logic, tasks gate counts, remote push verification (real temp git repos),
and config review-mode validation. No network / no gh required.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import jw_cclog  # noqa: E402
import jw_common  # noqa: E402
import jw_delegate  # noqa: E402
import jw_improve  # noqa: E402
import jw_lanes  # noqa: E402
import jw_merge  # noqa: E402
import jw_resume  # noqa: E402
import jw_review  # noqa: E402
import jw_round  # noqa: E402
import jw_tasks  # noqa: E402
import jw_validate  # noqa: E402
import yaml  # noqa: E402


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def init_repo(root: Path):
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("0")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "c0")


class MarkerTests(unittest.TestCase):
    def test_emit_parse_roundtrip(self):
        s = jw_review.emit_marker("review-cycle", {"round_id": "2026-06-15-x", "cycle": 1,
                                                   "target_sha": "a" * 40, "reviewers": ["codex", "gpt-5.5-pro"]})
        got = jw_review.parse_markers(s)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["_kind"], "review-cycle")
        self.assertEqual(got[0]["cycle"], 1)
        self.assertEqual(got[0]["target_sha"], "a" * 40)

    def test_latest_and_next_cycle(self):
        text = (jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": "a" * 40})
                + "\n" + jw_review.emit_marker("review-cycle", {"cycle": 2, "target_sha": "b" * 40}))
        ms = jw_review.parse_markers(text)
        self.assertEqual(jw_review.latest_cycle(ms)["cycle"], 2)
        self.assertEqual(jw_review.next_cycle_number(ms), 3)
        self.assertEqual(jw_review.next_cycle_number([]), 1)

    def test_classify_fresh_vs_stale(self):
        head = "b" * 40
        # cycle frozen at a different sha => stale
        ms = jw_review.parse_markers(jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": "a" * 40}))
        self.assertFalse(jw_review.classify(ms, head)["cycle_fresh"])
        # frozen at head => fresh
        ms = jw_review.parse_markers(jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}))
        self.assertTrue(jw_review.classify(ms, head)["cycle_fresh"])

    def _bodies(self, head, *, reviewer="gpt-5.5-pro", cycle=1, verdict="shipped",
                approver="owner", decision=None):
        return [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner", "at": "2026-06-01T00:00:00Z"},
            {"body": jw_review.emit_marker("review-result", {"reviewer": reviewer, "review_cycle": cycle,
                                                             "reviewed_sha": head, "verdict": verdict,
                                                             "decision_required": decision or []}), "author": reviewer, "at": "2026-06-01T01:00:00Z"},
            {"body": jw_review.emit_marker("approval", {"sha": head, "cycle": 1, "by": approver}), "author": approver, "at": "2026-06-01T03:00:00Z"},
            {"body": jw_review.emit_marker("findings", {"cycle": 1, "resolved": True}), "author": "owner", "at": "2026-06-01T02:00:00Z"},
        ]

    def test_classify_valid_binding(self):
        head = "c" * 40
        c = jw_review.classify(jw_review.parse_bodies(self._bodies(head)), head,
                               macro_reviewers=("gpt-5.5-pro",), approvers=("owner",))
        self.assertTrue(c["pro_result_at_head"])
        self.assertTrue(c["approved_at_head"])
        self.assertTrue(c["findings_resolved"])
        # different head invalidates all three (SHA-binding)
        c2 = jw_review.classify(jw_review.parse_bodies(self._bodies(head)), "d" * 40,
                                macro_reviewers=("gpt-5.5-pro",), approvers=("owner",))
        self.assertFalse(c2["pro_result_at_head"])
        self.assertFalse(c2["approved_at_head"])

    def test_classify_rejects_bad_provenance(self):
        head = "c" * 40
        mr, ap = ("gpt-5.5-pro",), ("owner",)
        # wrong reviewer
        c = jw_review.classify(jw_review.parse_bodies(self._bodies(head, reviewer="random-user")), head, macro_reviewers=mr, approvers=ap)
        self.assertFalse(c["pro_result_at_head"])
        # wrong cycle (result for cycle 99, latest is 1)
        c = jw_review.classify(jw_review.parse_bodies(self._bodies(head, cycle=99)), head, macro_reviewers=mr, approvers=ap)
        self.assertFalse(c["pro_result_at_head"])
        # not-shipped verdict
        c = jw_review.classify(jw_review.parse_bodies(self._bodies(head, verdict="not-shipped")), head, macro_reviewers=mr, approvers=ap)
        self.assertFalse(c["pro_result_at_head"])
        # decision required
        c = jw_review.classify(jw_review.parse_bodies(self._bodies(head, decision=["stop"])), head, macro_reviewers=mr, approvers=ap)
        self.assertFalse(c["pro_result_at_head"])
        # approval by untrusted author
        c = jw_review.classify(jw_review.parse_bodies(self._bodies(head, approver="anyone")), head, macro_reviewers=mr, approvers=ap)
        self.assertFalse(c["approved_at_head"])

    def test_fenced_marker_ignored(self):
        head = "c" * 40
        fenced = "```yaml\n" + jw_review.emit_marker("approval", {"sha": head, "by": "owner"}) + "\n```"
        self.assertEqual(jw_review.parse_markers(fenced), [])
        c = jw_review.classify(jw_review.parse_bodies([{"body": fenced, "author": "owner"}]), head, approvers=("owner",))
        self.assertFalse(c["approved_at_head"])

    def test_findings_resolved_strict_bool(self):
        # a non-True 'resolved' (e.g. arbitrary string) must not count as resolved
        m = jw_review.parse_markers(jw_review.emit_marker("findings", {"cycle": 1, "resolved": "maybe"}))
        c = jw_review.classify([{"_kind": "review-cycle", "cycle": 1, "target_sha": "x"}, *m], "x")
        self.assertFalse(c["findings_resolved"])

    def test_ci_strict(self):
        for bad in ("ACTION_REQUIRED", "NEUTRAL", "SKIPPED", "STALE", "WHATEVER"):
            self.assertEqual(jw_review.ci_state({"checks": [{"conclusion": bad}]}), "failing", bad)
        self.assertEqual(jw_review.ci_state({"checks": [{"conclusion": "SUCCESS"}]}), "passing")
        self.assertEqual(jw_review.ci_state({"checks": [{"conclusion": "PENDING"}]}), "pending")

    def _op_bodies(self, head, *, result_author="owner", findings_author="owner",
                   cycle_author="owner", reviewer="gpt-5.5-pro", cycle=1, verdict="shipped",
                   approver="owner", resolved=True):
        """Bodies where the GitHub author (who POSTED) is distinct from the logical reviewer id —
        the realistic PR-mode case (a human operator posts the macro reviewer's reply)."""
        return [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": cycle_author, "at": "2026-06-01T00:00:00Z"},
            {"body": jw_review.emit_marker("review-result", {"reviewer": reviewer, "review_cycle": cycle,
                "reviewed_sha": head, "verdict": verdict, "decision_required": []}), "author": result_author, "at": "2026-06-01T01:00:00Z"},
            {"body": jw_review.emit_marker("approval", {"sha": head, "cycle": 1, "by": approver}), "author": approver, "at": "2026-06-01T03:00:00Z"},
            {"body": jw_review.emit_marker("findings", {"cycle": 1, "resolved": resolved}), "author": findings_author, "at": "2026-06-01T02:00:00Z"},
        ]

    def test_classify_operator_provenance(self):
        head = "e" * 40
        ops, mr, ap = ("owner",), ("gpt-5.5-pro",), ("owner",)
        c = jw_review.classify(jw_review.parse_bodies(self._op_bodies(head)), head,
                               macro_reviewers=mr, approvers=ap, operators=ops)
        self.assertTrue(c["pro_result_at_head"])
        self.assertTrue(c["findings_resolved"])
        self.assertTrue(c["cycle_fresh"])
        # a non-operator forging the macro result (still claiming reviewer gpt-5.5-pro) is ignored
        c = jw_review.classify(jw_review.parse_bodies(self._op_bodies(head, result_author="attacker")),
                               head, macro_reviewers=mr, approvers=ap, operators=ops)
        self.assertFalse(c["pro_result_at_head"])
        # a non-operator forging findings-resolved is ignored
        c = jw_review.classify(jw_review.parse_bodies(self._op_bodies(head, findings_author="attacker")),
                               head, macro_reviewers=mr, approvers=ap, operators=ops)
        self.assertFalse(c["findings_resolved"])
        # a non-operator can't hijack the latest cycle with a higher-numbered freeze
        bodies = self._op_bodies(head)
        bodies.append({"body": jw_review.emit_marker("review-cycle", {"cycle": 9, "target_sha": "f" * 40}),
                       "author": "attacker"})
        c = jw_review.classify(jw_review.parse_bodies(bodies), head, macro_reviewers=mr, approvers=ap, operators=ops)
        self.assertEqual(c["latest_cycle"], 1)
        self.assertTrue(c["cycle_fresh"])

    def test_approval_by_must_match_author(self):
        head = "e" * 40
        # an approval whose claimed `by` differs from who actually posted it is rejected
        bodies = [{"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner"},
                  {"body": jw_review.emit_marker("approval", {"sha": head, "cycle": 1, "by": "owner"}), "author": "impersonator"}]
        c = jw_review.classify(jw_review.parse_bodies(bodies), head, approvers=("owner", "impersonator"))
        self.assertFalse(c["approved_at_head"])

    def test_cycle_conflict_fails_closed(self):
        head = "e" * 40
        # two operator freeze markers for the same latest cycle, different SHA → not fresh
        bodies = [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 2, "target_sha": head}), "author": "owner"},
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 2, "target_sha": "f" * 40}), "author": "owner"},
        ]
        c = jw_review.classify(jw_review.parse_bodies(bodies), head, operators=("owner",))
        self.assertTrue(c["cycle_conflict"])
        self.assertFalse(c["cycle_fresh"])

    def test_findings_latest_trusted_state_reblocks(self):
        head = "e" * 40
        # an earlier resolved:true followed by a later resolved:false must re-block
        bodies = [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner"},
            {"body": jw_review.emit_marker("findings", {"cycle": 1, "resolved": True}), "author": "owner", "at": "2026-06-19T01:00:00Z"},
            {"body": jw_review.emit_marker("findings", {"cycle": 1, "resolved": False}), "author": "owner", "at": "2026-06-19T02:00:00Z"},
        ]
        c = jw_review.classify(jw_review.parse_bodies(bodies), head, operators=("owner",))
        self.assertFalse(c["findings_resolved"])

    def test_codex_fresh_commit_binding(self):
        head = "a" * 40
        # (1) formal review whose commit_id == head
        self.assertTrue(jw_review.codex_fresh(
            [{"author": jw_review.CODEX_BOT, "commit_id": head, "state": "COMMENTED"}], [], head))
        # a review of a DIFFERENT commit does not count for this head
        self.assertFalse(jw_review.codex_fresh(
            [{"author": jw_review.CODEX_BOT, "commit_id": "b" * 40, "state": "COMMENTED"}], [], head))
        # a non-codex author does not count (formal-review path)
        self.assertFalse(jw_review.codex_fresh(
            [{"author": "someone", "commit_id": head, "state": "APPROVED"}], [], head))
        # (2) the connector's no-issue COMMENT naming the head short-SHA counts (real codex path).
        # GraphQL (gh pr view) drops the [bot] suffix — must still match.
        comment = {"author": "chatgpt-codex-connector", "body": f"Codex Review: no issues.\nReviewed commit: `{head[:10]}`"}
        self.assertTrue(jw_review.codex_fresh([], [comment], head))
        # a codex comment naming a DIFFERENT (old) head does not count
        stale = {"author": jw_review.CODEX_BOT, "body": "Reviewed commit: `" + ("b" * 10) + "`"}
        self.assertFalse(jw_review.codex_fresh([], [stale], head))
        # a non-codex commenter naming the head can't forge it (login is GitHub-verified)
        forged = {"author": "attacker", "body": f"Reviewed commit: `{head[:10]}`"}
        self.assertFalse(jw_review.codex_fresh([], [forged], head))
        # nothing at all (bare 👍 reaction) → fail-closed
        self.assertFalse(jw_review.codex_fresh([], [], head))

    def test_file_at_ref_uses_explicit_get(self):
        import base64 as _b64
        captured = {}

        def fake_gh(root, *args):
            captured["args"] = args
            return (0, _b64.b64encode(b"hello: world\n").decode())

        orig = jw_review._gh
        jw_review._gh = fake_gh
        try:
            out = jw_review.file_at_ref(Path("/x"), "o/r", "tasks.yaml", "sha123")
        finally:
            jw_review._gh = orig
        self.assertEqual(out, "hello: world\n")
        self.assertIn("--method", captured["args"])
        self.assertEqual(captured["args"][captured["args"].index("--method") + 1], "GET")

    def test_base_sha_binding(self):
        # B4: a cycle frozen at (head H, base B1) is stale once the base moves to B2
        head = "f" * 40
        cyc = {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head, "base_sha": "b1" + "0" * 38}),
               "author": "owner"}
        ms = jw_review.parse_bodies([cyc])
        self.assertTrue(jw_review.classify(ms, head, operators=("owner",), current_base="b1" + "0" * 38)["cycle_fresh"])
        self.assertFalse(jw_review.classify(ms, head, operators=("owner",), current_base="b2" + "0" * 38)["cycle_fresh"])

    def test_result_uses_latest_not_any(self):
        # B2: a later not-shipped (with a stop decision) cancels an earlier shipped
        head = "c" * 40
        bodies = [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner"},
            {"body": jw_review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": []}),
             "author": "owner", "at": "2026-06-19T01:00:00Z"},
            {"body": jw_review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "not-shipped", "decision_required": ["stop"]}),
             "author": "owner", "at": "2026-06-19T02:00:00Z"},
        ]
        c = jw_review.classify(jw_review.parse_bodies(bodies), head, macro_reviewers=("gpt-5.5-pro",), operators=("owner",))
        self.assertFalse(c["pro_result_at_head"])

    def test_all_macro_reviewers_required(self):
        # B2: with two configured macro reviewers, one passing result is not enough
        head = "c" * 40
        bodies = [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner"},
            {"body": jw_review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": []}), "author": "owner"},
        ]
        c = jw_review.classify(jw_review.parse_bodies(bodies), head,
                               macro_reviewers=("gpt-5.5-pro", "other-reviewer"), operators=("owner",))
        self.assertFalse(c["pro_result_at_head"])  # 'other-reviewer' has no result

    def test_new_codex_signal_reblocks_findings_and_approval(self):
        # B3: a Codex signal newer than the findings resolution / approval re-blocks both
        head = "c" * 40
        bodies = [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner"},
            {"body": jw_review.emit_marker("findings", {"cycle": 1, "resolved": True}), "author": "owner", "at": "2026-06-19T01:00:00Z"},
            {"body": jw_review.emit_marker("approval", {"sha": head, "cycle": 1, "by": "owner"}), "author": "owner", "at": "2026-06-19T01:30:00Z"},
        ]
        ms = jw_review.parse_bodies(bodies)
        # no later codex signal → both hold
        ok = jw_review.classify(ms, head, approvers=("owner",), operators=("owner",), codex_signal_at=None)
        self.assertTrue(ok["findings_resolved"]); self.assertTrue(ok["approved_at_head"])
        # a Codex signal at T03:00 (after resolution & approval) → both go stale
        blocked = jw_review.classify(ms, head, approvers=("owner",), operators=("owner",),
                                     codex_signal_at="2026-06-19T03:00:00Z")
        self.assertFalse(blocked["findings_resolved"]); self.assertFalse(blocked["approved_at_head"])

    def test_codex_comment_negative_context_not_fresh(self):
        # M5: a SHA appearing in prose (not the 'Reviewed commit' field) must not count
        head = "1234567890" + "a" * 30
        neg = {"author": "chatgpt-codex-connector",
               "body": f"Reviewed commit: `deadbeef00`.\nI did NOT review {head[:10]}; rerun required."}
        self.assertFalse(jw_review.codex_fresh([], [neg], head))
        pos = {"author": "chatgpt-codex-connector", "body": f"**Reviewed commit:** `{head[:10]}`"}
        self.assertTrue(jw_review.codex_fresh([], [pos], head))

    def test_ci_completed_not_passing(self):
        # M7: COMPLETED is a run status, not a success conclusion
        self.assertEqual(jw_review.ci_state({"checks": [{"conclusion": "COMPLETED"}]}), "failing")
        self.assertEqual(jw_review.ci_state({"checks": [{"state": "COMPLETED"}]}), "failing")
        self.assertEqual(jw_review.ci_state({"checks": [{"conclusion": "SUCCESS"}, {"conclusion": "COMPLETED"}]}), "failing")

    def test_rest_reviews_flattens_slurped_pages(self):
        # M6: --slurp returns an array of per-page arrays; rest_reviews must flatten them
        import json as _json
        pages = [[{"id": 1, "user": {"login": "a"}, "commit_id": "x", "state": "COMMENTED", "submitted_at": "t1"}],
                 [{"id": 2, "user": {"login": "b"}, "commit_id": "y", "state": "APPROVED", "submitted_at": "t2"}]]
        orig = jw_review._gh
        jw_review._gh = lambda root, *a: (0, _json.dumps(pages))
        try:
            out = jw_review.rest_reviews(Path("/x"), "o/r", 5)
        finally:
            jw_review._gh = orig
        self.assertEqual([r["id"] for r in out], [1, 2])
        self.assertEqual(out[1]["author"], "b")

    # ---- v0.2.5: cycle-bound evidence (no reuse across a re-freeze) ----
    def test_old_codex_signal_stale_after_refreeze(self):
        head = "f" * 40
        reviews = [{"author": jw_review.CODEX_BOT, "commit_id": head, "state": "COMMENTED",
                    "at": "2026-06-19T01:00:00Z", "id": 1}]
        self.assertTrue(jw_review.codex_fresh(reviews, [], head))  # no freeze gate → counts
        # re-freeze at a later time → the pre-freeze Codex review no longer counts
        self.assertEqual(jw_review.codex_signals_at_head(reviews, [], head, since_at="2026-06-20T05:00:00Z"), [])

    def test_old_approval_rejected_for_new_cycle_and_base(self):
        head, B2 = "f" * 40, "b2" + "0" * 38
        cyc2 = {"body": jw_review.emit_marker("review-cycle", {"cycle": 2, "target_sha": head, "base_sha": B2}),
                "author": "owner", "at": "2026-06-20T05:00:00Z"}
        old = {"body": jw_review.emit_marker("approval", {"sha": head, "cycle": 1, "by": "owner"}),
               "author": "owner", "at": "2026-06-19T02:00:00Z"}  # cycle 1, no base
        c = jw_review.classify(jw_review.parse_bodies([cyc2, old]), head,
                               approvers=("owner",), operators=("owner",), current_base=B2)
        self.assertFalse(c["approved_at_head"])
        # a fresh approval bound to (cycle 2, head, base B2) is accepted
        new = {"body": jw_review.emit_marker("approval", {"sha": head, "base_sha": B2, "cycle": 2, "by": "owner"}),
               "author": "owner", "at": "2026-06-20T06:00:00Z"}
        c2 = jw_review.classify(jw_review.parse_bodies([cyc2, new]), head,
                                approvers=("owner",), operators=("owner",), current_base=B2)
        self.assertTrue(c2["approved_at_head"])

    def test_approval_before_evidence_invalid(self):
        head = "c" * 40
        bodies = [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner", "at": "2026-06-19T00:00:00Z"},
            {"body": jw_review.emit_marker("approval", {"sha": head, "cycle": 1, "by": "owner"}),
             "author": "owner", "at": "2026-06-19T01:00:00Z"},  # approved early
            {"body": jw_review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": []}),
             "author": "owner", "at": "2026-06-19T02:00:00Z"},  # evidence arrived later
        ]
        c = jw_review.classify(jw_review.parse_bodies(bodies), head,
                               macro_reviewers=("gpt-5.5-pro",), approvers=("owner",), operators=("owner",))
        self.assertTrue(c["pro_result_at_head"])
        self.assertFalse(c["approved_at_head"])  # approval predates the result it claims to clear

    def test_same_timestamp_conflicting_results_fail_closed(self):
        head = "c" * 40
        T = "2026-06-20T05:00:00Z"
        bodies = [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head}), "author": "owner", "at": "2026-06-19T00:00:00Z"},
            {"body": jw_review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": []}), "author": "owner", "at": T},
            {"body": jw_review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "not-shipped", "decision_required": ["stop"]}), "author": "owner", "at": T},
        ]
        c = jw_review.classify(jw_review.parse_bodies(bodies), head, macro_reviewers=("gpt-5.5-pro",), operators=("owner",))
        self.assertFalse(c["pro_result_at_head"])

    def test_base_conflict_same_cycle_fails_closed(self):
        head, B1, B2 = "f" * 40, "b1" + "0" * 38, "b2" + "0" * 38
        bodies = [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head, "base_sha": B1}), "author": "owner", "at": "2026-06-19T00:00:00Z"},
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head, "base_sha": B2}), "author": "owner", "at": "2026-06-19T00:00:01Z"},
        ]
        c = jw_review.classify(jw_review.parse_bodies(bodies), head, operators=("owner",), current_base=B2)
        self.assertTrue(c["cycle_conflict"])
        self.assertFalse(c["cycle_fresh"])

    # ---- v0.2.6: strict ordering + canonical paginated comment log ----
    def test_strict_ordering_equal_timestamp_fails(self):
        head, B, T = "c" * 40, "b" * 40, "2026-06-22T00:00:00Z"
        # a Codex review AT the freeze time is not strictly after → stale
        revs = [{"author": jw_review.CODEX_BOT, "commit_id": head, "state": "COMMENTED", "at": T, "id": 1}]
        self.assertEqual(jw_review.codex_signals_at_head(revs, [], head, since_at=T), [])
        # an approval at the SAME second as its evidence is order-ambiguous → invalid
        bodies = [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 1, "target_sha": head, "base_sha": B}), "author": "owner", "at": "2026-06-21T00:00:00Z"},
            {"body": jw_review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": head, "verdict": "shipped", "decision_required": []}), "author": "owner", "at": T},
            {"body": jw_review.emit_marker("findings", {"cycle": 1, "resolved": True}), "author": "owner", "at": T},
            {"body": jw_review.emit_marker("approval", {"sha": head, "base_sha": B, "cycle": 1, "by": "owner"}), "author": "owner", "at": T},
        ]
        c = jw_review.classify(jw_review.parse_bodies(bodies), head, macro_reviewers=("gpt-5.5-pro",),
                               approvers=("owner",), operators=("owner",), current_base=B, codex_signal_at=None)
        self.assertTrue(c["pro_result_at_head"])
        self.assertFalse(c["approved_at_head"])

    def test_refreeze_same_cycle_advances_boundary(self):
        head, B = "c" * 40, "b" * 40
        bodies = [
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 2, "target_sha": head, "base_sha": B}), "author": "owner", "at": "2026-06-22T00:00:00Z"},
            {"body": jw_review.emit_marker("review-cycle", {"cycle": 2, "target_sha": head, "base_sha": B}), "author": "owner", "at": "2026-06-22T02:00:00Z"},
        ]
        ms = jw_review.parse_bodies(bodies)
        self.assertEqual(jw_review.latest_cycle(ms, ("owner",))["_at"], "2026-06-22T02:00:00Z")  # later re-freeze wins
        # a Codex review between the two freezes is stale vs the advanced boundary
        revs = [{"author": jw_review.CODEX_BOT, "commit_id": head, "state": "COMMENTED", "at": "2026-06-22T01:00:00Z", "id": 1}]
        self.assertEqual(jw_review.codex_signals_at_head(revs, [], head, since_at="2026-06-22T02:00:00Z"), [])
        c = jw_review.classify(ms, head, operators=("owner",), current_base=B)
        self.assertFalse(c["cycle_conflict"])  # same head/base → re-freeze, not a conflict
        self.assertTrue(c["cycle_fresh"])

    def test_rest_comments_paginates_and_uses_updated_at(self):
        import json as _json
        pages = [[{"id": 1, "user": {"login": "a"}, "body": "first", "created_at": "t0", "updated_at": "t0"}],
                 [{"id": 2, "user": {"login": "b"}, "body": "edited later", "created_at": "t0", "updated_at": "t5"}]]
        orig = jw_review._gh
        jw_review._gh = lambda root, *a: (0, _json.dumps(pages))
        try:
            out = jw_review.rest_comments(Path("/x"), "o/r", 9)
        finally:
            jw_review._gh = orig
        self.assertEqual([c["id"] for c in out], [1, 2])           # both pages flattened
        self.assertEqual(out[1]["at"], "t5")                       # effective time = updated_at

    def test_codex_regex_anchored_rejects_prose(self):
        head = "9b896a84c0" + "0" * 30  # valid 40-hex
        bot = "chatgpt-codex-connector"
        for neg in (f"I did not review this. Previous Reviewed commit: `{head[:10]}`",
                    f"Not reviewed commit: `{head[:10]}`",
                    f"> **Reviewed commit:** `{head[:10]}` stale quote",
                    f"foo reviewed commit:** `{head[:10]}` bar"):
            self.assertFalse(jw_review.codex_fresh([], [{"author": bot, "body": neg}], head), neg)
        self.assertTrue(jw_review.codex_fresh([], [{"author": bot, "body": f"**Reviewed commit:** `{head[:10]}`"}], head))

    def test_freeze_request_lists_custom_macro_reviewer(self):
        captured = {}
        # freeze reads the BASE policy via pr_context; a custom non-codex reviewer must be prompted
        ctx = {"repo": "o/r", "pr": 3, "head": "a" * 40, "base_sha": "b" * 40, "base": "main",
               "bundle": {"head": "a" * 40, "base_sha": "b" * 40, "bodies": []},
               "policy": jw_common.normalize_config(
                   {"version": 1, "project": "x", "review": {"mode": "pr", "reviewers": ["codex", "research-auditor"]}})}

        def fake_gh(root, *args):
            if len(args) >= 2 and args[0] == "pr" and args[1] == "comment":
                captured["body"] = args[args.index("--body") + 1]
            return (0, "")

        saved = (jw_review.pr_context, jw_review._gh)
        jw_review.pr_context = lambda root, pr: ctx
        jw_review._gh = fake_gh
        try:
            jw_review.freeze(Path("/x"), 3, "2026-06-22-r")
        finally:
            jw_review.pr_context, jw_review._gh = saved
        self.assertIn("research-auditor", captured.get("body", ""))  # custom reviewer prompted, not name-guessed
        self.assertIn("@codex review", captured["body"])


PASS = dict(cycle_fresh=True, require_ci=True, ci="passing", want_codex=True, codex_fresh=True,
            findings_resolved=True, want_pro=True, pro_result_at_head=True, open_blockers=[],
            open_decisions=[], approved_at_head=True, remote_contains_head=None)


class MergeGateTests(unittest.TestCase):
    def test_all_pass(self):
        ok, fails = jw_merge.merge_gate(dict(PASS))
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
            ok, fails = jw_merge.merge_gate(g)
            self.assertFalse(ok, key)
            self.assertTrue(any(needle.lower() in f.lower() for f in fails), (key, fails))

    def test_ci_only_blocks_when_required(self):
        g = dict(PASS); g["ci"] = "failing"
        self.assertFalse(jw_merge.merge_gate(g)[0])
        g["require_ci"] = False
        self.assertTrue(jw_merge.merge_gate(g)[0])
        # ci 'none' with require_ci blocks
        g2 = dict(PASS); g2["ci"] = "none"
        self.assertFalse(jw_merge.merge_gate(g2)[0])

    def test_blockers_and_decisions_block(self):
        g = dict(PASS); g["open_blockers"] = ["fix/x"]
        self.assertFalse(jw_merge.merge_gate(g)[0])
        g = dict(PASS); g["open_decisions"] = ["decision/y"]
        self.assertFalse(jw_merge.merge_gate(g)[0])

    def test_unpushed_local_head_blocks(self):
        g = dict(PASS); g["remote_contains_head"] = False
        self.assertFalse(jw_merge.merge_gate(g)[0])

    def test_gate_only_requires_configured_reviewers(self):
        # codex not wanted: a missing/false codex review must not block
        g = dict(PASS); g["want_codex"] = False; g["codex_fresh"] = False; g["findings_resolved"] = False
        self.assertTrue(jw_merge.merge_gate(g)[0], jw_merge.merge_gate(g)[1])
        # pro not wanted: a missing pro result must not block
        g = dict(PASS); g["want_pro"] = False; g["pro_result_at_head"] = False
        self.assertTrue(jw_merge.merge_gate(g)[0], jw_merge.merge_gate(g)[1])
        # but when wanted, they still block
        g = dict(PASS); g["want_codex"] = True; g["codex_fresh"] = False
        self.assertFalse(jw_merge.merge_gate(g)[0])

    def test_pr_state_and_head_read_block(self):
        g = dict(PASS); g["head_read_ok"] = False
        ok, fails = jw_merge.merge_gate(g)
        self.assertFalse(ok); self.assertTrue(any("policy@base" in f or "tasks@head" in f for f in fails))
        for key, val in (("pr_state", "MERGED"), ("is_draft", True)):
            g = dict(PASS); g[key] = val
            self.assertFalse(jw_merge.merge_gate(g)[0], key)
        g = dict(PASS); g["base"] = "feature"; g["expected_base"] = "main"
        self.assertFalse(jw_merge.merge_gate(g)[0])


class TasksGateTests(unittest.TestCase):
    def test_counts(self):
        data = {"tasks": [
            {"id": "fix/a", "severity": "blocker", "status": "pending"},
            {"id": "fix/b", "severity": "blocker", "status": "done"},
            {"id": "decision/c", "status": "pending"},
            {"id": "decision/d", "status": "done"},
            {"id": "feat/e", "status": "active"},
        ]}
        c = jw_merge.tasks_gate_counts(data)
        self.assertEqual(c["open_blockers"], ["fix/a"])
        self.assertEqual(c["open_decisions"], ["decision/c"])

    def test_defensive_on_malformed(self):
        # a non-list `tasks` must not crash and must not silently report zero open items as valid
        for bad in ({"tasks": "not-a-list"}, {"tasks": 5}, "garbage", None):
            self.assertEqual(jw_merge.tasks_gate_counts(bad), {"open_blockers": [], "open_decisions": []}, bad)
        # such a registry also fails schema validation (the gate's head_read_ok hook)
        self.assertTrue(jw_validate.validate({"version": 1, "project": "x", "tasks": "not-a-list"}))

    def test_validator_malformed_deps_no_crash(self):
        # M8: a non-list `deps` must be a clean validation error, never a process crash
        for bad in (5, "feat/x", None, {"a": 1}):
            data = {"version": 1, "project": "proj", "tasks": [
                {"id": "feat/foo", "title": "a properly explained task", "deps": bad}]}
            errs = jw_validate.validate(data)  # must not raise
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
            pushed, info = jw_common.head_pushed(work, fetch=True)
            self.assertTrue(pushed, info)
            # new local commit, not pushed
            (work / "f.txt").write_text("1")
            git(work, "commit", "-aqm", "c1")
            pushed2, info2 = jw_common.head_pushed(work, fetch=True)
            self.assertFalse(pushed2, info2)
            self.assertEqual(info2.get("behind"), 0)

    def test_no_upstream(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            init_repo(work)
            pushed, info = jw_common.head_pushed(work, fetch=False)
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
            pushed, info = jw_common.head_pushed(work, fetch=True)
            self.assertFalse(pushed)  # must NOT trust the stale ref
            self.assertIn("fetch failed", info.get("reason", ""))


class ResumeStartHereTests(unittest.TestCase):
    """Persistent model-authored re-entry pointer (START_HERE) + its SessionStart injection."""

    def test_start_here_path_distinct_and_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(jw_common.start_here_path(root), jw_common.start_here_path(root))  # per-repo stable
            self.assertNotEqual(jw_common.start_here_path(root), jw_common.resume_path(root))   # vs ephemeral
            self.assertIn("start_here", str(jw_common.start_here_path(root)))
            self.assertNotEqual(jw_common.start_here_path(root), jw_common.start_here_path(root / "sub"))

    def _with_home(self, home: Path, fn):
        import os
        env_bak, argv_bak = os.environ.get("HOME"), sys.argv
        os.environ["HOME"] = str(home)
        try:
            return fn()
        finally:
            sys.argv = argv_bak
            if env_bak is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = env_bak

    def test_start_here_path_cli_creates_parent(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "proj"
            root.mkdir()
            init_repo(root)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            home = Path(d) / "home"
            home.mkdir()

            def run():
                sys.argv = ["jw_resume.py", "--start-here-path", str(root)]
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = jw_resume.main()
                return rc, buf.getvalue().strip()

            rc, printed = self._with_home(home, run)
            self.assertEqual(rc, 0)
            self.assertTrue(printed.startswith(str(home)))   # under the (temp) home, not the real ~/.claude
            self.assertIn("start_here", printed)
            self.assertTrue(Path(printed).parent.is_dir())   # parent created so the model can Write to it

    def test_session_context_injects_and_caps_start_here(self):
        import contextlib
        import io
        import json as _json
        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "proj"
            root.mkdir()
            init_repo(root)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            home = Path(d) / "home"
            home.mkdir()

            def ctx_for(start_here_body: str) -> str:
                def run():
                    sh = jw_common.start_here_path(root)
                    sh.parent.mkdir(parents=True, exist_ok=True)
                    sh.write_text(start_here_body, encoding="utf-8")
                    sys.argv = ["session_context.py", str(root)]
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        session_context.main()
                    return _json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]
                return self._with_home(home, run)

            ctx = ctx_for("# re-entry @ 2026-06-24-x / HEAD abc1234\nMARKER-FRONTIER-LINE\n")
            self.assertIn("START HERE", ctx)            # labeled and surfaced
            self.assertIn("MARKER-FRONTIER-LINE", ctx)  # the model's narrative is injected

            # an over-budget file is capped at read-time (never truncates the file itself)
            ctx_big = ctx_for("Z" * (session_context.MAX_START_HERE + 800))
            self.assertIn("truncated", ctx_big)
            self.assertLess(ctx_big.count("Z"), session_context.MAX_START_HERE + 800)


class ConfigTests(unittest.TestCase):
    def _cfg(self, body: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text(body)
            return jw_common.load_config(root)

    def test_default_review_mode_packet(self):
        cfg = self._cfg("version: 1\nproject: x\n")
        self.assertEqual(cfg["review"]["mode"], "packet")
        self.assertFalse(cfg["review"]["require_ci"])

    def test_pr_mode_ok(self):
        cfg = self._cfg("version: 1\nproject: x\nreview:\n  mode: pr\n  require_ci: true\n")
        self.assertEqual(cfg["review"]["mode"], "pr")
        self.assertTrue(cfg["review"]["require_ci"])

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            self._cfg("version: 1\nproject: x\nreview:\n  mode: bogus\n")

    def test_operators_default_and_parse(self):
        self.assertEqual(self._cfg("version: 1\nproject: x\n")["review"]["operators"], [])
        cfg = self._cfg("version: 1\nproject: x\nreview:\n  mode: pr\n  operators: [alice, bob]\n")
        self.assertEqual(cfg["review"]["operators"], ["alice", "bob"])

    def test_operators_must_be_list(self):
        with self.assertRaises(ValueError):
            self._cfg("version: 1\nproject: x\nreview:\n  operators: notalist\n")

    def test_delegation_default_env_prep_none_no_sandbox_knob(self):
        cfg = self._cfg("version: 1\nproject: x\n")
        self.assertIsNone(cfg["delegation"]["env_prep"])
        self.assertNotIn("sandbox", cfg["delegation"])  # R7: no sandbox config knob in M1

    def test_delegation_env_prep_list_ok(self):
        cfg = self._cfg("version: 1\nproject: x\ndelegation:\n  env_prep:\n    - uv sync --frozen\n")
        self.assertEqual(cfg["delegation"]["env_prep"], ["uv sync --frozen"])

    def test_delegation_env_prep_must_be_str_list(self):
        with self.assertRaises(ValueError):
            self._cfg("version: 1\nproject: x\ndelegation:\n  env_prep: notalist\n")
        with self.assertRaises(ValueError):
            self._cfg("version: 1\nproject: x\ndelegation:\n  env_prep:\n    - 42\n")



TASKS_FIXTURE = """# registry — comments must be preserved
version: 1
project: x
tasks:
  - id: feat/alpha
    title: "first task"
    status: active
    deps: []
  - id: gate/beta
    title: "a gate blocked on alpha"
    status: blocked
    deps: [feat/alpha]
"""


class TextSurgeryTests(unittest.TestCase):
    def test_set_existing_field(self):
        out = jw_round.set_task_field(TASKS_FIXTURE, "feat/alpha", "status", "done")
        self.assertIn("status: done", out)
        self.assertIn("# registry — comments must be preserved", out)  # comment preserved
        self.assertIn('title: "first task"', out)  # other fields intact
        self.assertEqual(out.count("status: active"), 0)

    def test_insert_missing_field(self):
        out = jw_round.set_task_field(TASKS_FIXTURE, "feat/alpha", "round", "2026-06-19-z")
        self.assertIn("round: 2026-06-19-z", out)
        # inserted into feat/a block, not gate/b
        a_block = out.split("gate/beta")[0]
        self.assertIn("round: 2026-06-19-z", a_block)

    def test_only_targets_named_task(self):
        out = jw_round.set_task_field(TASKS_FIXTURE, "gate/beta", "status", "done")
        self.assertIn("status: active", out)  # feat/a untouched
        self.assertEqual(out.count("status: done"), 1)

    def test_missing_task_raises(self):
        with self.assertRaises(KeyError):
            jw_round.set_task_field(TASKS_FIXTURE, "feat/nope", "status", "done")

    def test_set_config_scalar_nested(self):
        cfg = "version: 1\nstate:\n  last_push_commit: null\n  last_round_commit: null\n"
        out = jw_round.set_config_scalar(cfg, "last_round_commit", "abc123")
        self.assertIn("  last_round_commit: abc123", out)
        self.assertIn("  last_push_commit: null", out)  # sibling preserved
        with self.assertRaises(KeyError):
            jw_round.set_config_scalar(cfg, "nonexistent_key", "v")

    def test_set_config_scalar_section_exact_child(self):
        # a deeper nested key of the same name must NOT be touched — only the direct child
        cfg = "state:\n  last_round_commit: null\n  nested:\n    last_round_commit: deep\n"
        out = jw_round.set_config_scalar(cfg, "last_round_commit", "X", section="state")
        self.assertIn("  last_round_commit: X", out)
        self.assertIn("    last_round_commit: deep", out)

    def test_set_replaces_block_list_value(self):
        # a field whose existing value is a BLOCK list must be fully replaced — the continuation
        # lines consumed, not left orphaned under a new flow value (which would break the YAML).
        doc = ("version: 1\nproject: x\ntasks:\n"
               '  - id: feat/alpha\n    title: "base task alpha"\n    status: done\n'
               '  - id: feat/gamma\n    title: "gamma depends on alpha"\n    status: active\n    deps:\n      - feat/alpha\n')
        out = jw_round.set_task_field(doc, "feat/gamma", "deps", '["feat/alpha"]')
        data = yaml.safe_load(out)  # parses only if the `- feat/alpha` block line was not orphaned
        byid = {t["id"]: t for t in data["tasks"]}
        self.assertEqual(byid["feat/gamma"]["deps"], ["feat/alpha"])
        self.assertNotIn("      - feat/alpha", out)
        self.assertEqual(jw_validate.validate(data), [])


class NextActionableTests(unittest.TestCase):
    def test_deps_gate(self):
        data = {"tasks": [
            {"id": "feat/a", "title": "A", "status": "done"},
            {"id": "feat/b", "title": "B", "status": "pending", "deps": ["feat/a"]},
            {"id": "feat/c", "title": "C", "status": "pending", "deps": ["feat/b"]},  # dep b not done
            {"id": "feat/d", "title": "D", "status": "active", "deps": []},
            {"id": "gate/e", "title": "E", "status": "blocked", "deps": ["feat/a"]},  # stale-blocked
        ]}
        got = dict(jw_common.next_actionable(data))
        self.assertIn("feat/b", got)   # dep a done
        self.assertIn("feat/d", got)   # no deps
        self.assertIn("gate/e", got)   # stale-blocked: dep a done → actionable now
        self.assertNotIn("feat/c", got)  # dep b not done
        self.assertNotIn("feat/a", got)  # already done


class LaneTests(unittest.TestCase):
    def test_contains_base(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            base = git(root, "rev-parse", "HEAD").stdout.strip()
            git(root, "checkout", "-q", "-b", "feat/foo")
            (root / "g.txt").write_text("1"); git(root, "add", "-A"); git(root, "commit", "-qm", "c1")
            self.assertEqual(jw_lanes.check_lane(root, "feat/foo", {"branch": "feat/foo", "base_sha": base}), [])
            # a base the branch does NOT contain: make an unrelated commit on a sibling branch
            git(root, "checkout", "-q", "main")
            (root / "h.txt").write_text("2"); git(root, "add", "-A"); git(root, "commit", "-qm", "sib")
            sib = git(root, "rev-parse", "HEAD").stdout.strip()
            fails = jw_lanes.check_lane(root, "feat/foo", {"branch": "feat/foo", "base_sha": sib})
            self.assertTrue(fails and "does NOT contain" in fails[0])

    def test_missing_branch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            base = git(root, "rev-parse", "HEAD").stdout.strip()
            fails = jw_lanes.check_lane(root, "t", {"branch": "no/such", "base_sha": base})
            self.assertTrue(fails and "does not exist" in fails[0])

    def test_done_lane_with_deleted_branch_not_verified(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: x\ntasks:\n"
                "  - id: feat/old-lane\n    title: 'a merged & cleaned-up lane'\n    status: done\n"
                "    lane:\n      branch: deleted/gone\n      base_sha: deadbeef\n")
            self.assertEqual(jw_lanes.verify(root), 0)  # done lane skipped, not a permanent failure


class RoundCloseTests(unittest.TestCase):
    def test_close_integration(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            (root / ".jahns-workflow.yml").write_text(
                "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            git(root, "add", "-A"); git(root, "commit", "-qm", "setup")
            rc = jw_round.close(root, "2026-06-19-z", done=["feat/alpha"], touched=["gate/beta"], commit="HEAD")
            self.assertEqual(rc, 0)
            txt = (root / "tasks.yaml").read_text()
            # feat/a flipped to done and stamped
            a = txt.split("gate/beta")[0]
            self.assertIn("status: done", a)
            self.assertIn("round: 2026-06-19-z", a)
            # gate/b stamped with round but NOT flipped to done
            b = "gate/beta" + txt.split("gate/beta")[1]
            self.assertIn("round: 2026-06-19-z", b)
            self.assertIn("status: blocked", b)
            # comment preserved, ROADMAP generated, watermark advanced
            self.assertIn("# registry — comments must be preserved", txt)
            self.assertTrue((root / "ROADMAP.md").is_file())
            head = git(root, "rev-parse", "HEAD").stdout.strip()
            self.assertIn(f"last_round_commit: {head}", (root / ".jahns-workflow.yml").read_text())

    def _setup(self, root, cfg_body):
        init_repo(root)
        (root / ".jahns-workflow.yml").write_text(cfg_body)
        (root / "tasks.yaml").write_text(TASKS_FIXTURE)
        git(root, "add", "-A"); git(root, "commit", "-qm", "setup")

    def test_missing_watermark_fails_closed_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\n")  # no state.last_round_commit
            before = (root / "tasks.yaml").read_text()
            rc = jw_round.close(root, "2026-06-19-z", done=["feat/alpha"], touched=[], commit="HEAD")
            self.assertEqual(rc, 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)  # nothing written
            self.assertFalse((root / "ROADMAP.md").exists())

    def test_unresolvable_commit_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            before = (root / "tasks.yaml").read_text()
            rc = jw_round.close(root, "2026-06-19-z", done=["feat/alpha"], touched=[], commit="nope-not-a-ref")
            self.assertEqual(rc, 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)

    def test_done_task_with_unmet_dep_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            before = (root / "tasks.yaml").read_text()
            # gate/beta depends on feat/alpha (active) — closing gate/beta as done must fail
            rc = jw_round.close(root, "2026-06-19-z", done=["gate/beta"], touched=[], commit="HEAD")
            self.assertEqual(rc, 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)

    def test_close_dependency_and_dependent_together(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            # closing a dependency (feat/alpha) and its dependent (gate/beta) in ONE round is valid:
            # the dep is done in the final state
            rc = jw_round.close(root, "2026-06-19-z", done=["feat/alpha", "gate/beta"], touched=[], commit="HEAD")
            self.assertEqual(rc, 0)
            self.assertEqual((root / "tasks.yaml").read_text().count("status: done"), 2)

    def test_close_rolls_back_on_render_failure(self):
        import jw_roadmap
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            before_tasks = (root / "tasks.yaml").read_text()
            before_cfg = (root / ".jahns-workflow.yml").read_text()

            def boom(_root):
                raise RuntimeError("render exploded mid-commit")

            orig = jw_roadmap.render
            jw_roadmap.render = boom
            try:
                rc = jw_round.close(root, "2026-06-19-z", done=["feat/alpha"], touched=["gate/beta"], commit="HEAD")
            finally:
                jw_roadmap.render = orig
            self.assertEqual(rc, 1)
            # primary files restored; ROADMAP not left behind
            self.assertEqual((root / "tasks.yaml").read_text(), before_tasks)
            self.assertEqual((root / ".jahns-workflow.yml").read_text(), before_cfg)
            self.assertFalse((root / "ROADMAP.md").exists())

    def test_close_restores_generated_ssot_on_digest_failure(self):
        import jw_ssot
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            (root / ".jahns-workflow.yml").write_text(
                "version: 1\nproject: x\nssot: SSOT.md\ngenerated_dir: docs/ssot\n"
                "state:\n  last_round_commit: null\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            (root / "SSOT.md").write_text("# Title\n\n## A\nalpha\n\n## B\nbeta\n")
            git(root, "add", "-A"); git(root, "commit", "-qm", "setup")
            jw_ssot.regenerate(root)
            gen = root / "docs/ssot"
            v1_hash = (gen / ".hash").read_text()
            v1_digest = (gen / "DIGEST.md").read_text()
            # the SSOT changes during the round; close() will regenerate views, which then fails
            (root / "SSOT.md").write_text("# Title\n\n## A\nalpha2\n\n## B\nbeta\n\n## C\ngamma\n")
            git(root, "add", "-A"); git(root, "commit", "-qm", "ssot edit")

            def boom(_root):
                raise RuntimeError("SSOT regen exploded mid-commit")

            orig = jw_ssot.regenerate
            jw_ssot.regenerate = boom
            try:
                rc = jw_round.close(root, "2026-06-19-z", done=["feat/alpha"], touched=[], commit="HEAD")
            finally:
                jw_ssot.regenerate = orig
            self.assertEqual(rc, 1)
            # generated dir fully rolled back: split/.hash/DIGEST all consistent at v1
            self.assertEqual((gen / ".hash").read_text(), v1_hash)
            self.assertEqual((gen / "DIGEST.md").read_text(), v1_digest)
            self.assertEqual((root / "tasks.yaml").read_text(), TASKS_FIXTURE)  # primary restored too


class BasePolicyTests(unittest.TestCase):
    """B1: the merge-gate trust policy must come from the PR BASE SHA, never the candidate head —
    so a branch can't make itself an operator/approver, drop reviewers, or disable CI."""

    def test_policy_read_from_base_not_head(self):
        STRICT_BASE = ("version: 1\nproject: x\nreview:\n  mode: pr\n  reviewers: [codex, gpt-5.5-pro]\n"
                       "  require_ci: true\n  operators: [owner]\n  approvers: [owner]\n")
        RELAXED_HEAD = ("version: 1\nproject: x\nreview:\n  mode: pr\n  reviewers: []\n"
                        "  require_ci: false\n  operators: [attacker]\n  approvers: [attacker]\n")
        TASKS = "version: 1\nproject: x\ntasks: []\n"
        bundle = {"head": "H" * 40, "base_sha": "B" * 40, "bodies": [], "reviews": [], "checks": [],
                  "merge_state": "", "state": "OPEN", "is_draft": False, "base": "main", "head_ref": "feat/x"}
        calls = []

        def fake_file_at_ref(root, repo, path, ref):
            calls.append((path, ref))
            if path == ".jahns-workflow.yml":
                return STRICT_BASE if ref == bundle["base_sha"] else RELAXED_HEAD
            return TASKS  # tasks.yaml @ head

        saved = (jw_review.resolve_repo, jw_review.pr_bundle, jw_review.file_at_ref, jw_review._gh)
        jw_review.resolve_repo = lambda root: "owner/repo"
        jw_review.pr_bundle = lambda root, pr, repo=None: bundle
        jw_review.file_at_ref = fake_file_at_ref
        jw_review._gh = lambda root, *a: (0, "main")
        try:
            with tempfile.TemporaryDirectory() as d:
                # a local config must exist for the load_config fallback; the gate must ignore it
                # in favour of the base-SHA policy
                (Path(d) / ".jahns-workflow.yml").write_text("version: 1\nproject: x\nreview:\n  mode: pr\n")
                g = jw_merge._gather(Path(d), 7)
        finally:
            jw_review.resolve_repo, jw_review.pr_bundle, jw_review.file_at_ref, jw_review._gh = saved
        # policy taken from the STRICT base, not the RELAXED head
        self.assertTrue(g["head_read_ok"])
        self.assertTrue(g["require_ci"])   # base = true (head said false)
        self.assertTrue(g["want_codex"])   # base lists codex (head dropped it)
        self.assertTrue(g["want_pro"])     # base lists gpt-5.5-pro (head dropped it)
        # the config was read at the base SHA; tasks at the head SHA
        self.assertIn((".jahns-workflow.yml", bundle["base_sha"]), calls)
        self.assertIn(("tasks.yaml", bundle["head"]), calls)
        self.assertNotIn((".jahns-workflow.yml", bundle["head"]), calls)

    def test_custom_named_macro_reviewer_is_mandatory(self):
        # a reviewer that isn't 'codex' and isn't named gpt/pro must still gate the merge
        BASE = ("version: 1\nproject: x\nreview:\n  mode: pr\n  reviewers: [codex, research-auditor]\n"
                "  require_ci: false\n  operators: [owner]\n  approvers: [owner]\n")
        bundle = {"head": "H" * 40, "base_sha": "B" * 40, "bodies": [], "reviews": [], "checks": [],
                  "merge_state": "", "state": "OPEN", "is_draft": False, "base": "main", "head_ref": "feat/x"}
        saved = (jw_review.resolve_repo, jw_review.pr_bundle, jw_review.file_at_ref, jw_review._gh)
        jw_review.resolve_repo = lambda root: "owner/repo"
        jw_review.pr_bundle = lambda root, pr, repo=None: bundle
        jw_review.file_at_ref = lambda root, repo, path, ref: (BASE if path == ".jahns-workflow.yml"
                                                               else "version: 1\nproject: x\ntasks: []\n")
        jw_review._gh = lambda root, *a: (0, "main")
        try:
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / ".jahns-workflow.yml").write_text("version: 1\nproject: x\nreview:\n  mode: pr\n")
                g = jw_merge._gather(Path(d), 7)
        finally:
            jw_review.resolve_repo, jw_review.pr_bundle, jw_review.file_at_ref, jw_review._gh = saved
        self.assertTrue(g["want_pro"])  # research-auditor must be required, not name-guessed away


class IngestTests(unittest.TestCase):
    def _root(self, d):
        root = Path(d)
        (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
        return root

    def test_byte_exact_copy_and_consume(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            src = root / "inbox.md"
            # tricky bytes: CRLF, trailing spaces, multibyte utf-8, NO final newline
            body = "## Review\r\n  trailing   \nutf8: é한\nno final newline".encode("utf-8")
            src.write_bytes(body)
            rc = jw_review.ingest(root, "2026-06-22-x", src=src, reviewer="gpt-5.5-pro")
            self.assertEqual(rc, 0)
            dest = root / "docs/reviews/2026-06-22-x-feedback.md"
            content = dest.read_bytes()
            self.assertIn(body, content)                     # body byte-exact, verbatim (within the file)
            # verbatim body sits between the header separator and the appended triage skeleton
            self.assertIn(body + b"\n\n---\n\n## Findings (triage skeleton", content)
            self.assertIn(b"round: 2026-06-22-x", content)
            self.assertIn(b"reviewer: gpt-5.5-pro", content)
            self.assertFalse(src.exists())                   # drop-file consumed

    def test_missing_inbox_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            self.assertEqual(jw_review.ingest(root, "2026-06-22-x", src=root / "nope.md"), 1)

    def test_empty_inbox_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            src = root / "inbox.md"; src.write_bytes(b"   \n\n")
            self.assertEqual(jw_review.ingest(root, "2026-06-22-x", src=src), 1)
            self.assertTrue(src.exists())  # not consumed on failure

    def test_round_inferred_from_request(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            rdir = root / "docs/reviews"; rdir.mkdir(parents=True)
            (rdir / "2026-06-20-a-request.md").write_text("req")
            src = root / "inbox.md"; src.write_bytes(b"review body")
            self.assertEqual(jw_review.ingest(root, None, src=src), 0)
            self.assertTrue((rdir / "2026-06-20-a-feedback.md").is_file())


class FrozenAcceptanceTests(unittest.TestCase):
    """The frozen v0.2 acceptance boundaries (GPT 6th review) — A: PR reducer, B: YAML mutation,
    C: closeout/views. Each test directly reproduces a defect that must stay closed."""
    HEAD, BASE = "a" * 40, "b" * 40

    def _cycle(self, at, base=None):
        f = {"cycle": 1, "target_sha": self.HEAD}
        if base:
            f["base_sha"] = base
        return {"body": jw_review.emit_marker("review-cycle", f), "author": "owner", "at": at}

    # ---- A: PR review protocol reducer ----
    def test_a1_macro_result_before_freeze_rejected(self):
        bodies = [
            {"body": jw_review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": self.HEAD, "verdict": "shipped", "decision_required": []}),
             "author": "owner", "at": "2026-06-20T00:00:00Z"},
            self._cycle("2026-06-20T02:00:00Z", self.BASE),  # freeze AFTER the result
        ]
        c = jw_review.classify(jw_review.parse_bodies(bodies), self.HEAD,
                               macro_reviewers=("gpt-5.5-pro",), operators=("owner",), current_base=self.BASE)
        self.assertFalse(c["pro_result_at_head"])

    def test_a1_approval_before_freeze_rejected(self):
        bodies = [
            {"body": jw_review.emit_marker("approval", {"sha": self.HEAD, "base_sha": self.BASE, "cycle": 1, "by": "owner"}),
             "author": "owner", "at": "2026-06-20T00:00:00Z"},
            self._cycle("2026-06-20T02:00:00Z", self.BASE),  # freeze AFTER the approval
        ]
        c = jw_review.classify(jw_review.parse_bodies(bodies), self.HEAD,
                               approvers=("owner",), operators=("owner",), current_base=self.BASE)
        self.assertFalse(c["approved_at_head"])

    def test_a2_typed_marker_round_trip(self):
        s = jw_review.emit_marker("review-result", {"reviewer": "r", "review_cycle": 2, "reviewed_sha": self.HEAD,
                                                    "verdict": "shipped", "decision_required": ["D-1", "D-2"]})
        m = jw_review.parse_markers(s)[0]
        self.assertEqual(m["review_cycle"], 2)
        self.assertEqual(m["decision_required"], ["D-1", "D-2"])  # a real list, not "D-1, D-2"

    def test_a2_schema_rejects_bool_float_and_bad_types(self):
        bad = [
            {"_kind": "review-cycle", "cycle": True, "target_sha": self.HEAD},          # bool, not int
            {"_kind": "review-cycle", "cycle": 1.0, "target_sha": self.HEAD},           # float
            {"_kind": "review-cycle", "cycle": 1, "target_sha": "xyz"},                 # bad sha
            {"_kind": "review-result", "review_cycle": 1, "reviewed_sha": self.HEAD, "reviewer": "r",
             "verdict": "shipped", "decision_required": {}},                            # dict, not list[str]
            {"_kind": "findings", "cycle": 1, "resolved": "yes"},                       # str, not bool
            {"_kind": "approval", "sha": self.HEAD, "cycle": 1, "by": ""},              # empty by
        ]
        for m in bad:
            self.assertFalse(jw_review.marker_valid(m), m)
        self.assertTrue(jw_review.marker_valid(
            {"_kind": "findings", "cycle": 1, "resolved": True}))  # the well-typed control

    def test_a3_pending_review_body_not_parsed_as_marker(self):
        import json as _json
        marker = jw_review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
            "reviewed_sha": self.HEAD, "verdict": "shipped", "decision_required": []})

        def fake_gh(root, *args):
            joined = " ".join(str(x) for x in args)
            if args[:2] == ("pr", "view"):
                return (0, _json.dumps({"headRefOid": self.HEAD, "baseRefOid": self.BASE,
                    "statusCheckRollup": [], "mergeStateStatus": "", "state": "OPEN",
                    "isDraft": False, "baseRefName": "main", "headRefName": "x"}))
            if "issues" in joined and "comments" in joined:
                return (0, _json.dumps([[]]))  # no issue comments
            if "pulls" in joined and "reviews" in joined:  # a PENDING review carrying the marker
                return (0, _json.dumps([[{"id": 1, "user": {"login": "someone"}, "body": marker,
                    "state": "PENDING", "commit_id": self.HEAD, "submitted_at": ""}]]))
            return (0, "o/r")

        orig = jw_review._gh
        jw_review._gh = fake_gh
        try:
            bundle = jw_review.pr_bundle(Path("/x"), 1, "o/r")
        finally:
            jw_review._gh = orig
        self.assertNotIn(marker, [b["body"] for b in bundle["bodies"]])  # review body is NOT a marker source
        self.assertEqual(jw_review.parse_bodies(bundle["bodies"]), [])

    def test_a4_base_packet_policy_blocks_local_pr(self):
        BASE_PACKET = "version: 1\nproject: x\nreview:\n  mode: packet\n  reviewers: []\n"
        bundle = {"head": self.HEAD, "base_sha": self.BASE, "bodies": [], "reviews": [], "checks": [],
                  "merge_state": "", "state": "OPEN", "is_draft": False, "base": "main", "head_ref": "x"}
        saved = (jw_review.resolve_repo, jw_review.pr_bundle, jw_review.file_at_ref, jw_review._gh)
        jw_review.resolve_repo = lambda root: "owner/repo"
        jw_review.pr_bundle = lambda root, pr, repo=None: bundle
        jw_review.file_at_ref = lambda root, repo, path, ref: (BASE_PACKET if path == ".jahns-workflow.yml"
                                                               else "version: 1\nproject: x\ntasks: []\n")
        jw_review._gh = lambda root, *a: (0, "main")
        try:
            with tempfile.TemporaryDirectory() as d:
                # local config says pr — but the BASE policy (packet) is authoritative
                (Path(d) / ".jahns-workflow.yml").write_text("version: 1\nproject: x\nreview:\n  mode: pr\n")
                g = jw_merge._gather(Path(d), 7)
        finally:
            jw_review.resolve_repo, jw_review.pr_bundle, jw_review.file_at_ref, jw_review._gh = saved
        self.assertEqual(g["policy_mode"], "packet")
        self.assertFalse(g["want_codex"])
        self.assertFalse(g["want_pro"])  # base packet/empty reviewers — local pr can't add reviewers

    # ---- B: structure-bounded YAML mutation ----
    def test_b1_decoy_task_outside_tasks_untouched(self):
        doc = ("metadata:\n  - id: feat/alpha\n    status: active\n"
               "tasks:\n  - id: feat/alpha\n    title: the real alpha task\n    status: active\n")
        out = jw_round.set_task_field(doc, "feat/alpha", "status", "done")
        self.assertIn("metadata:\n  - id: feat/alpha\n    status: active", out)  # decoy untouched
        self.assertIn("    title: the real alpha task\n    status: done", out)   # real one edited

    def test_b1_duplicate_task_id_fails_closed(self):
        doc = "tasks:\n  - id: feat/x\n    status: active\n  - id: feat/x\n    status: active\n"
        with self.assertRaises(jw_common.WorkflowError):
            jw_round.set_task_field(doc, "feat/x", "status", "done")

    def test_b2_nested_state_not_mistaken_for_top_level(self):
        cfg = "foo:\n  state:\n    last_round_commit: decoy\nstate:\n  last_round_commit: real\n"
        out = jw_round.set_config_scalar(cfg, "last_round_commit", "NEW", section="state")
        self.assertIn("    last_round_commit: decoy", out)  # nested decoy untouched
        self.assertIn("\nstate:\n  last_round_commit: NEW", out)  # top-level edited

    # ---- C: closeout transaction / generated-view validation ----
    def test_c1_library_raises_workflowerror_not_systemexit(self):
        import jw_ssot
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\nssot: missing.md\n")
            with self.assertRaises(jw_common.WorkflowError):  # NOT SystemExit (which slips rollbacks)
                jw_ssot.regenerate(root)

    def test_c2_check_detects_missing_and_extra_views(self):
        import jw_ssot
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text(
                "version: 1\nproject: x\nssot: S.md\ngenerated_dir: docs/ssot\n")
            (root / "S.md").write_text("# T\n\n## A\nalpha\n")
            jw_ssot.regenerate(root)
            self.assertEqual(jw_ssot.check(root), 0)
            (root / "docs/ssot/DIGEST.md").unlink()            # missing view
            self.assertEqual(jw_ssot.check(root), 3)
            jw_ssot.regenerate(root)
            (root / "docs/ssot/sections/99-stale.md").write_text("stale")  # extra section
            self.assertEqual(jw_ssot.check(root), 3)

    def test_c3_non_string_and_duplicate_deps_rejected(self):
        base = {"version": 1, "project": "p", "tasks": [
            {"id": "feat/foo", "title": "a properly explained task", "deps": [123]}]}
        self.assertTrue(any("dep" in e for e in jw_validate.validate(base)))
        dup = {"version": 1, "project": "p", "tasks": [
            {"id": "feat/bar", "title": "another explained task", "deps": ["feat/foo", "feat/foo"]},
            {"id": "feat/foo", "title": "a properly explained task"}]}
        self.assertTrue(any("duplicate dep" in e for e in jw_validate.validate(dup)))


class IntegrationSmokeTests(unittest.TestCase):
    """Fake-gh end-to-end smoke through the REAL pipeline (pr_context → file_at_ref → classify →
    merge_gate): a full lifecycle PASSes, and a re-freeze makes the prior cycle's evidence stale."""
    HEAD, BASE = "a" * 40, "b" * 40
    CODEX = "chatgpt-codex-connector[bot]"

    def _gh(self, comments, reviews):
        import base64 as _b64
        import json as _json
        POLICY = ("version: 1\nproject: x\nreview:\n  mode: pr\n  reviewers: [codex, gpt-5.5-pro]\n"
                  "  require_ci: false\n  operators: [owner]\n  approvers: [owner]\n")
        TASKS = "version: 1\nproject: x\ntasks: []\n"

        def gh(root, *args):
            a, j = list(args), " ".join(str(x) for x in args)
            if a[:2] == ["repo", "view"]:
                return (0, "owner/repo" if "nameWithOwner" in j else "main")
            if a[:2] == ["pr", "view"]:
                return (0, _json.dumps({"headRefOid": self.HEAD, "baseRefOid": self.BASE,
                    "statusCheckRollup": [], "mergeStateStatus": "", "state": "OPEN",
                    "isDraft": False, "baseRefName": "main", "headRefName": "x"}))
            if "issues" in j and "comments" in j:
                return (0, _json.dumps([comments]))
            if "pulls" in j and "reviews" in j:
                return (0, _json.dumps([reviews]))
            if "contents/.jahns-workflow.yml" in j:
                return (0, _b64.b64encode(POLICY.encode()).decode())
            if "contents/tasks.yaml" in j:
                return (0, _b64.b64encode(TASKS.encode()).decode())
            return (0, "")
        return gh

    def test_full_lifecycle_pass_then_refreeze_stale(self):
        import contextlib
        import io
        mk = jw_review.emit_marker
        comments = [
            {"id": 1, "user": {"login": "owner"}, "updated_at": "2026-06-22T01:00:00Z",
             "body": mk("review-cycle", {"cycle": 1, "target_sha": self.HEAD, "base_sha": self.BASE})},
            {"id": 2, "user": {"login": "owner"}, "updated_at": "2026-06-22T03:00:00Z",
             "body": mk("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                 "reviewed_sha": self.HEAD, "verdict": "shipped", "decision_required": []})},
            {"id": 3, "user": {"login": "owner"}, "updated_at": "2026-06-22T04:00:00Z",
             "body": mk("findings", {"cycle": 1, "resolved": True})},
            {"id": 4, "user": {"login": "owner"}, "updated_at": "2026-06-22T05:00:00Z",
             "body": mk("approval", {"sha": self.HEAD, "base_sha": self.BASE, "cycle": 1, "by": "owner"})},
        ]
        reviews = [{"id": 9, "user": {"login": self.CODEX}, "body": "", "state": "COMMENTED",
                    "commit_id": self.HEAD, "submitted_at": "2026-06-22T02:00:00Z"}]  # after freeze, at head

        orig = jw_review._gh
        jw_review._gh = self._gh(comments, reviews)
        try:
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
                root = Path(d)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc_pass = jw_merge.merge(root, 7, execute=False, method=None)
                    # re-freeze cycle 2 (same head/base, later) — every cycle-1 evidence must go stale
                    comments.append({"id": 5, "user": {"login": "owner"}, "updated_at": "2026-06-22T06:00:00Z",
                        "body": mk("review-cycle", {"cycle": 2, "target_sha": self.HEAD, "base_sha": self.BASE})})
                    jw_review._gh = self._gh(comments, reviews)
                    rc_stale = jw_merge.merge(root, 7, execute=False, method=None)
        finally:
            jw_review._gh = orig
        self.assertEqual(rc_pass, 0)    # full lifecycle → gate PASS (dry run)
        self.assertEqual(rc_stale, 3)   # after re-freeze, cycle-1 evidence is stale → BLOCKED


class TaskCliTests(unittest.TestCase):
    def test_render_list_filters(self):
        data = {"tasks": [
            {"id": "feat/a", "title": "alpha task here", "status": "active"},
            {"id": "fix/b", "title": "beta fix here", "status": "done"},
            {"id": "feat/c", "title": "gamma task here", "status": "pending"},
        ]}
        self.assertEqual(len(jw_tasks.render_list(data)), 3)
        active = jw_tasks.render_list(data, status="active")
        self.assertEqual(len(active), 1)
        self.assertIn("feat/a", active[0])
        feats = jw_tasks.render_list(data, type_="feat")
        self.assertEqual({ln.split()[0] for ln in feats}, {"feat/a", "feat/c"})

    def test_show_missing_raises(self):
        with self.assertRaises(KeyError):
            jw_tasks.render_show({"tasks": []}, "feat/x")

    def test_show_returns_record(self):
        data = {"tasks": [{"id": "feat/a", "title": "alpha task here", "status": "active"}]}
        out = jw_tasks.render_show(data, "feat/a")
        self.assertIn("feat/a", out)
        self.assertIn("alpha task here", out)

    def test_add_appends_valid_block(self):
        out = jw_tasks.append_task_block(TASKS_FIXTURE, {
            "id": "fix/gamma", "title": "a newly registered fix", "status": "pending",
            "severity": "major", "deps": ["feat/alpha"]})
        data = yaml.safe_load(out)
        self.assertEqual(jw_validate.validate(data), [])
        self.assertIn("# registry — comments must be preserved", out)  # comment preserved
        self.assertEqual([t["id"] for t in data["tasks"]], ["feat/alpha", "gate/beta", "fix/gamma"])
        g = next(t for t in data["tasks"] if t["id"] == "fix/gamma")
        self.assertEqual(g["severity"], "major")
        self.assertEqual(g["deps"], ["feat/alpha"])

    def test_add_into_empty_tasks(self):
        out = jw_tasks.append_task_block(
            "version: 1\nproject: x\ntasks: []\n", {"id": "feat/first", "title": "the very first task"})
        data = yaml.safe_load(out)
        self.assertEqual(jw_validate.validate(data), [])
        self.assertEqual([t["id"] for t in data["tasks"]], ["feat/first"])

    def test_main_add_set_drop_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            self.assertEqual(jw_tasks.main(["add", "fix/new", str(root), "--title", "a brand new fix task"]), 0)
            self.assertEqual(jw_tasks.main(["set", "fix/new", "status", "active", str(root)]), 0)
            self.assertEqual(jw_tasks.main(["drop", "gate/beta", str(root)]), 0)
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            byid = {t["id"]: t for t in data["tasks"]}
            self.assertEqual(byid["fix/new"]["status"], "active")
            self.assertEqual(byid["gate/beta"]["status"], "dropped")
            self.assertEqual(jw_validate.validate(data), [])
            self.assertIn("# registry — comments must be preserved", (root / "tasks.yaml").read_text())

    def test_main_add_rejects_invalid_id(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            before = (root / "tasks.yaml").read_text()
            self.assertEqual(jw_tasks.main(["add", "P0", str(root), "--title", "a banned codename task"]), 2)
            self.assertEqual((root / "tasks.yaml").read_text(), before)  # fail-closed, nothing written

    def test_set_deps_repoints_and_extends(self):
        doc = ("version: 1\nproject: x\ntasks:\n"
               '  - id: feat/alpha\n    title: "base task alpha"\n    status: done\n'
               '  - id: feat/beta\n    title: "base task beta"\n    status: done\n'
               '  - id: feat/gamma\n    title: "gamma depends on alpha"\n    status: active\n    deps: [feat/alpha]\n')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(doc)
            # re-point gamma's dep alpha→beta (the list-field edit that was impossible before)
            self.assertEqual(jw_tasks.main(["set", "feat/gamma", "deps", "feat/beta", str(root)]), 0)
            byid = {t["id"]: t for t in yaml.safe_load((root / "tasks.yaml").read_text())["tasks"]}
            self.assertEqual(byid["feat/gamma"]["deps"], ["feat/beta"])
            # extend to several ids, comma-separated (same convention as `add --deps`)
            self.assertEqual(jw_tasks.main(["set", "feat/gamma", "deps", "feat/alpha,feat/beta", str(root)]), 0)
            byid = {t["id"]: t for t in yaml.safe_load((root / "tasks.yaml").read_text())["tasks"]}
            self.assertEqual(byid["feat/gamma"]["deps"], ["feat/alpha", "feat/beta"])
            # clear with an empty value
            self.assertEqual(jw_tasks.main(["set", "feat/gamma", "deps", "", str(root)]), 0)
            byid = {t["id"]: t for t in yaml.safe_load((root / "tasks.yaml").read_text())["tasks"]}
            self.assertEqual(byid["feat/gamma"]["deps"], [])

    def test_set_deps_over_block_list_repoints(self):
        doc = ("version: 1\nproject: x\ntasks:\n"
               '  - id: feat/alpha\n    title: "base task alpha"\n    status: done\n'
               '  - id: feat/beta\n    title: "base task beta"\n    status: done\n'
               '  - id: feat/gamma\n    title: "gamma depends on alpha in block form"\n    status: active\n    deps:\n      - feat/alpha\n')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(doc)
            self.assertEqual(jw_tasks.main(["set", "feat/gamma", "deps", "feat/beta", str(root)]), 0)
            text = (root / "tasks.yaml").read_text()
            self.assertEqual({t["id"]: t for t in yaml.safe_load(text)["tasks"]}["feat/gamma"]["deps"], ["feat/beta"])
            self.assertNotIn("- feat/alpha", text)  # block dep fully removed, not orphaned


def _registry(n_done, n_active=2):
    rows = []
    for i in range(n_done):
        rows.append(f'  - id: fix/done-{i:03d}\n    title: "done task number {i}"\n'
                    f"    status: done\n    round: 2026-01-01-r\n")
    for i in range(n_active):
        rows.append(f'  - id: feat/active-{i:03d}\n    title: "active task number {i}"\n    status: active\n')
    return "version: 1\nproject: x\ntasks:\n" + "".join(rows)


class TaskArchiveTests(unittest.TestCase):
    def test_under_threshold_noop(self):
        data = yaml.safe_load(_registry(3))
        self.assertEqual(jw_tasks.select_for_archive(data, threshold=100, keep=10), [])

    def test_selects_old_terminal_keeps_recent(self):
        data = yaml.safe_load(_registry(20, 2))  # 22 tasks total
        ids = jw_tasks.select_for_archive(data, threshold=10, keep=5)
        self.assertEqual(len(ids), 15)                       # 20 done − last 5 kept
        self.assertIn("fix/done-000", ids)                   # oldest archived
        self.assertNotIn("fix/done-019", ids)                # among the last 5 kept
        self.assertTrue(all(i.startswith("fix/done") for i in ids))  # never an active task

    def test_never_archives_terminal_depended_on_by_remaining(self):
        text = _registry(20, 0) + ("  - id: feat/live\n    title: \"a live task needing an old dep\"\n"
                                    "    status: active\n    deps: [fix/done-000]\n")
        data = yaml.safe_load(text)
        ids = jw_tasks.select_for_archive(data, threshold=10, keep=5)
        self.assertNotIn("fix/done-000", ids)  # protected: a remaining task still depends on it

    def test_archive_main_moves_accumulates_and_stays_valid(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(_registry(20, 2))
            self.assertEqual(jw_tasks.main(["archive", str(root), "--threshold", "10", "--keep", "5"]), 0)
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            self.assertEqual(jw_validate.validate(data), [])
            self.assertEqual(len(data["tasks"]), 7)          # 5 kept done + 2 active
            arch = yaml.safe_load((root / "tasks.archive.yaml").read_text())
            self.assertEqual(len(arch["tasks"]), 15)
            # registry now has 7 tasks (< threshold 10): a second run is a clean no-op
            self.assertEqual(jw_tasks.main(["archive", str(root), "--threshold", "10", "--keep", "5"]), 0)
            self.assertEqual(len(yaml.safe_load((root / "tasks.archive.yaml").read_text())["tasks"]), 15)


class TaskReadNudgeTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import tasks_read_nudge
        self.nudge = tasks_read_nudge

    def test_denies_read_of_canonical_tasks_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            out = self.nudge.decide({"tool_name": "Read",
                                     "tool_input": {"file_path": str(root / "tasks.yaml")}})
            self.assertIsNotNone(out)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("jw task", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_allows_other_files_and_tools(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            (root / "other.yaml").write_text("x: 1\n")
            # a different file → no decision
            self.assertIsNone(self.nudge.decide(
                {"tool_name": "Read", "tool_input": {"file_path": str(root / "other.yaml")}}))
            # a non-Read tool on tasks.yaml → no decision (only Read is nudged)
            self.assertIsNone(self.nudge.decide(
                {"tool_name": "Edit", "tool_input": {"file_path": str(root / "tasks.yaml")}}))
            # a same-named file outside an initialized project → no decision
            with tempfile.TemporaryDirectory() as d2:
                stray = Path(d2) / "tasks.yaml"
                stray.write_text("x: 1\n")
                self.assertIsNone(self.nudge.decide(
                    {"tool_name": "Read", "tool_input": {"file_path": str(stray)}}))


class TaskRegressionTests(unittest.TestCase):
    """Regressions from the v0.5.0 adversarial review (no-trailing-newline surgery, transitive
    archive protection, round-recency, value quoting, fail-closed archive, symlink nudge)."""

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import tasks_read_nudge
        self.nudge = tasks_read_nudge

    NO_NL = ('version: 1\nproject: x\ntasks:\n'
             '  - id: feat/last\n    title: "the last existing task"\n    status: active')  # no trailing \n

    def test_add_no_trailing_newline_keeps_last_task(self):
        out = jw_tasks.append_task_block(self.NO_NL, {"id": "fix/added", "title": "an added fix task"})
        data = yaml.safe_load(out)
        self.assertEqual(jw_validate.validate(data), [])
        byid = {t["id"]: t for t in data["tasks"]}
        self.assertEqual(byid["feat/last"]["status"], "active")  # not stolen by the inserted block
        self.assertIn("fix/added", byid)

    def test_set_last_field_no_trailing_newline_updates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(self.NO_NL)
            self.assertEqual(jw_tasks.main(["set", "feat/last", "status", "done", str(root)]), 0)
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            self.assertEqual(data["tasks"][0]["status"], "done")  # actually updated, not a silent no-op

    def test_remove_last_task_no_trailing_newline(self):
        out = jw_tasks.remove_task_blocks(
            self.NO_NL + '\n  - id: fix/tail\n    title: "the tail done task"\n    status: done',
            ["fix/tail"])
        data = yaml.safe_load(out)
        self.assertEqual([t["id"] for t in data["tasks"]], ["feat/last"])
        self.assertEqual(data["tasks"][0]["status"], "active")  # tail's status not re-parented onto it

    def test_add_preserves_crlf(self):
        base = ('version: 1\r\nproject: x\r\ntasks:\r\n'
                '  - id: feat/win\r\n    title: "a windows task"\r\n    status: active\r\n')
        out = jw_tasks.append_task_block(base, {"id": "fix/win2", "title": "another windows task"})
        self.assertEqual(jw_validate.validate(yaml.safe_load(out)), [])
        self.assertNotIn("\n", out.replace("\r\n", ""))  # no bare LF introduced

    def test_set_value_with_colon(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            self.assertEqual(jw_tasks.main(["set", "feat/alpha", "notes", "blocked by X: see ticket 5", str(root)]), 0)
            data = {t["id"]: t for t in yaml.safe_load((root / "tasks.yaml").read_text())["tasks"]}
            self.assertEqual(data["feat/alpha"]["notes"], "blocked by X: see ticket 5")

    def test_set_invalid_value_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            before = (root / "tasks.yaml").read_text()
            self.assertEqual(jw_tasks.main(["set", "feat/alpha", "status", "bogus", str(root)]), 2)
            self.assertEqual((root / "tasks.yaml").read_text(), before)

    def test_transitive_deps_protected(self):
        text = ("version: 1\nproject: x\ntasks:\n"
                '  - id: fix/leaf\n    title: "oldest done leaf task"\n    status: done\n'
                '  - id: fix/mid\n    title: "middle done task here"\n    status: done\n    deps: [fix/leaf]\n'
                '  - id: feat/top\n    title: "active task at the top"\n    status: active\n    deps: [fix/mid]\n')
        ids = jw_tasks.select_for_archive(yaml.safe_load(text), threshold=3, keep=0)
        self.assertEqual(ids, [])  # mid pinned by top, leaf pinned transitively by mid → registry stays valid

    def test_recency_by_round_keeps_latest_closed(self):
        text = ("version: 1\nproject: x\ntasks:\n"
                '  - id: fix/early-file\n    title: "closed recently but early in file"\n    status: done\n    round: 2026-06-01-z\n'
                '  - id: fix/late-file\n    title: "closed long ago but late in file"\n    status: done\n    round: 2026-01-01-a\n')
        ids = jw_tasks.select_for_archive(yaml.safe_load(text), threshold=2, keep=1)
        self.assertEqual(ids, ["fix/late-file"])  # earlier round archived despite later file position

    def test_negative_keep_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(_registry(20, 2))
            before = (root / "tasks.yaml").read_text()
            self.assertEqual(jw_tasks.main(["archive", str(root), "--threshold", "10", "--keep", "-1"]), 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)

    def test_malformed_archive_file_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(_registry(20, 2))
            (root / "tasks.archive.yaml").write_text("just a string, not a registry\n")
            before = (root / "tasks.yaml").read_text()
            self.assertEqual(jw_tasks.main(["archive", str(root), "--threshold", "10", "--keep", "5"]), 2)
            self.assertEqual((root / "tasks.yaml").read_text(), before)                       # live registry untouched
            self.assertEqual((root / "tasks.archive.yaml").read_text(), "just a string, not a registry\n")  # history preserved

    def test_symlinked_tasks_yaml_is_denied(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "real.yaml").write_text(TASKS_FIXTURE)
            (root / "tasks.yaml").symlink_to(root / "real.yaml")
            out = self.nudge.decide({"tool_name": "Read",
                                     "tool_input": {"file_path": str(root / "tasks.yaml")}})
            self.assertIsNotNone(out)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


# ============================================================ v0.7.0 M1: jw_cclog / jw_improve
import json as _json  # noqa: E402

_UUID = "0123abcd-1234-1234-1234-0123456789ab"


def _write_jsonl(path: Path, records, trailing_newline: bool = True) -> None:
    """Write records (dicts or raw strings) as JSONL. The final line omits its newline when
    trailing_newline=False (simulating a truncated active-session tail)."""
    parts = [r if isinstance(r, str) else _json.dumps(r) for r in records]
    text = "\n".join(parts)
    if trailing_newline:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _parse(path: Path, **kw):
    defaults = dict(file_id="f1", server=None, project="proj", session_id="sess",
                    agent_id=None, workflow_id=None, is_sidechain_file=False)
    defaults.update(kw)
    return jw_cclog.parse_transcript_file(path, **defaults)


def _run_with_home(home: Path, fn):
    import os
    bak = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        return fn()
    finally:
        if bak is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = bak


class CclogParseTests(unittest.TestCase):
    """Ported parse-core behavior + real-format quirks (synthetic fixtures only)."""

    def test_replay_uuid_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            rec = {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hi"}}
            _write_jsonl(f, [rec, rec])
            out = _parse(f)
            self.assertEqual(out["replayed_skipped"], 1)
            self.assertEqual(sum(1 for e in out["events"] if e["uuid"] == "u1"), 1)

    def test_tool_result_actor_correction(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            _write_jsonl(f, [{"type": "user", "uuid": "t1", "toolUseResult": {"stdout": "x"},
                              "message": {"role": "user", "content": [
                                  {"type": "tool_result", "tool_use_id": "toolu_1",
                                   "content": "x", "is_error": False}]}}])
            out = _parse(f)
            tr = [e for e in out["events"] if e["event_type"] == "tool_result"]
            self.assertEqual(len(tr), 1)
            self.assertEqual(tr[0]["actor"], "tool")

    def test_cli_control_and_injections_not_user_instruction(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            _write_jsonl(f, [
                {"type": "user", "uuid": "a", "message": {"role": "user",
                 "content": "<command-name>/effort</command-name>"}},
                {"type": "user", "uuid": "b", "isCompactSummary": True,
                 "message": {"role": "user", "content": "prior summary"}},
                {"type": "user", "uuid": "c", "message": {"role": "user",
                 "content": "<system-reminder>note</system-reminder>"}},
                {"type": "user", "uuid": "d", "message": {"role": "user", "content": "real request"}},
            ])
            out = _parse(f)
            ui = [e for e in out["events"] if e["event_type"] == "user_instruction"]
            self.assertEqual([e["uuid"] for e in ui], ["d"])
            cc = [e for e in out["events"] if e["event_type"] == "cli_control"]
            self.assertEqual(cc[0]["event_subtype"], "slash_command")
            self.assertTrue(any(e["event_subtype"] == "compact_summary" for e in out["events"]))
            self.assertTrue(any(e["event_subtype"] == "system_reminder" for e in out["events"]))

    def test_thinking_not_extracted_and_usage_group_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            base_u = {"input_tokens": 100, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 0}
            _write_jsonl(f, [
                {"type": "assistant", "uuid": "a1", "requestId": "r",
                 "message": {"id": "mA", "model": "claude-opus-4-8",
                             "content": [{"type": "thinking", "thinking": "secret"}],
                             "usage": {**base_u, "output_tokens": 5}}},
                {"type": "assistant", "uuid": "a2", "requestId": "r",
                 "message": {"id": "mA", "model": "claude-opus-4-8",
                             "content": [{"type": "text", "text": "hello"}],
                             "usage": {**base_u, "output_tokens": 5}}},
                {"type": "assistant", "uuid": "a3", "requestId": "r",
                 "message": {"id": "mA", "model": "claude-opus-4-8", "stop_reason": "tool_use",
                             "content": [{"type": "tool_use", "id": "toolu_x", "name": "Bash",
                                          "input": {"command": "ls"}}],
                             "usage": {**base_u, "output_tokens": 20}}},
            ])
            out = _parse(f)
            tf = [e for e in out["events"] if e["uuid"] == "a1"][0]
            self.assertIsNone(tf["text"])  # thinking is an opaque stub
            self.assertEqual(tf["event_subtype"], "thinking_marker")
            g = [x for x in jw_cclog.coalesce_messages(out["events"], out["tool_calls"])
                 if x["message_id"] == "mA"][0]
            self.assertEqual(g["fragment_count"], 3)
            self.assertEqual(g["output_tokens"], 20)   # last representative, NOT 5+5+20
            self.assertEqual(g["input_tokens"], 100)   # NOT summed to 300
            self.assertTrue(g["has_thinking"])
            self.assertEqual(g["content_sequence"], "thinking_marker+text+tool_use")

    def test_polymorphic_content_and_session_id_casings(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            _write_jsonl(f, [
                {"type": "user", "uuid": "p1", "message": {"role": "user", "content": "stringform"}},
                {"type": "assistant", "uuid": "p2", "sessionId": "S",
                 "message": {"id": "m", "model": "claude-opus-4-8",
                             "content": [{"type": "text", "text": "blockform"}]}},
                {"type": "user", "uuid": "p3", "session_id": "S",
                 "message": {"role": "user", "content": [{"type": "text", "text": "blockuser"}]}},
            ])
            out = _parse(f)  # both id casings must not crash
            self.assertEqual(len(out["events"]), 3)
            self.assertEqual([e for e in out["events"] if e["uuid"] == "p1"][0]["text"], "stringform")
            self.assertEqual([e for e in out["events"] if e["uuid"] == "p3"][0]["text"], "blockuser")

    def test_synthetic_model_without_request_id(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            _write_jsonl(f, [{"type": "assistant", "uuid": "s1",
                              "message": {"id": "m1", "model": "<synthetic>",
                                          "content": [{"type": "text", "text": "x"}]}}])
            e = _parse(f)["events"][0]
            self.assertEqual(e["model_norm"], "synthetic")
            self.assertIsNone(e["request_id"])

    def test_unknown_type_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            _write_jsonl(f, [{"type": "totally-new-thing", "uuid": "z"}])
            e = _parse(f)["events"][0]
            self.assertEqual(e["event_type"], "unknown_raw")
            self.assertEqual(e["event_subtype"], "totally-new-thing")

    def test_lightweight_state_records_classified(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            _write_jsonl(f, [
                {"type": "mode", "mode": "default"},
                {"type": "last-prompt", "lastPrompt": "hey"},
                {"type": "queue-operation"},
                {"type": "ai-title", "title": "t"},
                {"type": "permission-mode", "permissionMode": "plan"},
                {"type": "agent-setting"},
            ])
            out = _parse(f)  # no uuid/parentUuid -> must not crash
            self.assertEqual(len(out["events"]), 6)
            self.assertTrue(all(e["event_type"] == "session_state" for e in out["events"]))

    def test_partial_tail_vs_parse_error(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            lines = [
                _json.dumps({"type": "user", "uuid": "g1", "message": {"role": "user", "content": "ok"}}),
                "{ this is broken json",  # mid-file (gets a trailing newline) -> parse_error
                _json.dumps({"type": "assistant", "uuid": "g2",
                             "message": {"id": "m", "model": "claude-opus-4-8", "content": []}}),
                '{"type":"assistant","uuid":"g3"',  # truncated tail, NO trailing newline
            ]
            f.write_text("\n".join(lines), encoding="utf-8")
            out = _parse(f)
            self.assertEqual(out["partial_tail_lines"], 1)
            pe = [e for e in out["events"] if e["event_subtype"] == "parse_error"]
            self.assertEqual(len(pe), 1)

    def test_agent_name_is_session_state(self):
        # 'agent-name' is a benign session-state record (sibling of ai-title/mode) — it must NOT
        # surface as an unknown_raw parse-degradation signal
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            _write_jsonl(f, [{"type": "agent-name", "agentName": "researcher"}])
            e = _parse(f)["events"][0]
            self.assertEqual(e["event_type"], "session_state")
            self.assertEqual(e["event_subtype"], "agent_name")

    def test_frame_link_is_session_state(self):
        # 'frame-link' (an Artifact publish: local path -> claude.ai URL) is a structural session-state
        # record, NOT a conversational event or an unknown_raw parse-degradation signal
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            _write_jsonl(f, [{"type": "frame-link", "sessionId": "56fe5235-x",
                              "path": "/tmp/claude-1001/s/scratchpad/foo.html",
                              "frameUrl": "https://claude.ai/code/artifact/uuid",
                              "timestamp": "2026-07-10T12:31:59.069Z"}])
            e = _parse(f)["events"][0]
            self.assertEqual(e["event_type"], "session_state")
            self.assertEqual(e["event_subtype"], "frame_link")

    def test_system_api_error_flagged(self):
        # a type=system/subtype=api_error record is an API failure event (any level) — errors.api
        # must see it, not just isApiErrorMessage-tagged records
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            _write_jsonl(f, [
                {"type": "system", "subtype": "api_error", "level": "error", "content": "API Error 529"},
                {"type": "system", "subtype": "info", "content": "benign"},
            ])
            out = _parse(f)
            err = [e for e in out["events"] if e["event_subtype"] == "api_error"][0]
            self.assertTrue(_json.loads(err["extras_json"]).get("is_api_error"))
            benign = [e for e in out["events"] if e["event_subtype"] == "info"][0]
            self.assertIsNone(benign["extras_json"])  # non-api_error system record carries no flag

    def test_tool_result_content_bytes_is_utf8(self):
        # content_bytes measures real UTF-8 bytes (context_heavy threshold), content_len stays chars
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            big = "가" * 60000  # 60k code points ~= 180k UTF-8 bytes
            _write_jsonl(f, [{"type": "user", "uuid": "t1",
                              "message": {"role": "user", "content": [
                                  {"type": "tool_result", "tool_use_id": "toolu_1",
                                   "content": big, "is_error": False}]}}])
            tr = _parse(f)["tool_results"][0]
            self.assertEqual(tr["content_len"], 60000)
            self.assertEqual(tr["content_bytes"], len(big.encode("utf-8")))
            self.assertGreater(tr["content_bytes"], 100 * 1024)  # counts toward context_heavy


class CclogLayoutTests(unittest.TestCase):
    """New real-layout detectors: detect_kind + scope_of."""

    def _k(self, *parts):
        return jw_cclog.detect_kind(parts)

    def _s(self, *parts):
        return jw_cclog.scope_of(parts)

    def test_main_transcript(self):
        parts = ("-Users-jahn-x", f"{_UUID}.jsonl")
        self.assertEqual(self._k(*parts), "main_transcript")
        sc = self._s(*parts)
        self.assertEqual(sc["project"], "-Users-jahn-x")  # leading-dash slug preserved
        self.assertEqual(sc["session_id"], _UUID)

    def test_subagent_and_meta(self):
        t = ("slug", _UUID, "subagents", "agent-a0ebe0ed54597e120.jsonl")
        self.assertEqual(self._k(*t), "subagent_transcript")
        sc = self._s(*t)
        self.assertEqual(sc["agent_id"], "a0ebe0ed54597e120")
        self.assertEqual(sc["session_id"], _UUID)
        m = ("slug", _UUID, "subagents", "agent-a0ebe0ed54597e120.meta.json")
        self.assertEqual(self._k(*m), "subagent_meta")
        self.assertEqual(self._s(*m)["agent_id"], "a0ebe0ed54597e120")

    def test_workflow_subagent(self):
        t = ("slug", _UUID, "subagents", "workflows", "wf_abc123", "agent-a1b2c3.jsonl")
        self.assertEqual(self._k(*t), "workflow_subagent_transcript")
        sc = self._s(*t)
        self.assertEqual(sc["workflow_id"], "wf_abc123")
        self.assertEqual(sc["agent_id"], "a1b2c3")

    def test_workflow_json_and_script(self):
        self.assertEqual(self._k("slug", _UUID, "workflows", "wf_abc123.json"), "workflow_json")
        self.assertEqual(self._s("slug", _UUID, "workflows", "wf_abc123.json")["workflow_id"], "wf_abc123")
        self.assertEqual(self._k("slug", _UUID, "workflows", "scripts", "run.js"), "workflow_script")
        # a workflow journal is a known manifest-only kind, NOT unknown_jsonl
        self.assertEqual(self._k("slug", _UUID, "subagents", "workflows", "wf_x", "journal.jsonl"),
                         "workflow_journal")

    def test_tool_result_and_memory(self):
        self.assertEqual(self._k("slug", _UUID, "tool-results", "toolu_x.txt"), "tool_result")
        self.assertEqual(self._k("slug", "memory", "note.md"), "memory")
        self.assertIsNone(self._s("slug", "memory", "note.md")["session_id"])

    def test_unknown(self):
        self.assertEqual(self._k("slug", "random.jsonl"), "unknown_jsonl")  # non-uuid stem
        self.assertEqual(self._k("slug", _UUID, "weird.bin"), "unknown_other")

    def test_nested_tool_results(self):
        # real logs nest artifacts one level deeper (tool-results/pdf-<uuid>/page-NN.png); tool-results
        # is an ANCESTOR dir, not the immediate parent — must still classify as tool_result, not skip
        self.assertEqual(self._k("slug", _UUID, "tool-results", "pdf-abc", "page1.png"), "tool_result")
        self.assertEqual(self._k("slug", _UUID, "tool-results", "out.txt"), "tool_result")  # flat still


class ImproveDiscoveryTests(unittest.TestCase):
    """Discovery over a fake projects tree: layout mapping + --source/--project filters."""

    def _tree(self, src: Path):
        (src / "slug-a").mkdir(parents=True)
        _write_jsonl(src / "slug-a" / f"{_UUID}.jsonl",
                     [{"type": "user", "uuid": "x", "message": {"role": "user", "content": "hi"}}])
        # a spurious project: a dir with no transcript (only a tool-result artifact)
        (src / "slug-b" / _UUID / "tool-results").mkdir(parents=True)
        (src / "slug-b" / _UUID / "tool-results" / "toolu_1.txt").write_text("data")

    def test_discover_all_and_project_filter(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "projects"
            self._tree(src)
            kinds_all = sorted(k for _, _, k in jw_improve.discover([src], set()))
            self.assertIn("main_transcript", kinds_all)
            self.assertIn("tool_result", kinds_all)
            only_a = jw_improve.discover([src], {"slug-a"})
            self.assertEqual([k for _, _, k in only_a], ["main_transcript"])
            # a spurious project surfaces as zero transcripts, no special-casing
            only_b = jw_improve.discover([src], {"slug-b"})
            self.assertEqual([k for _, _, k in only_b], ["tool_result"])

    def test_cli_default_out_honors_home(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "projects"
            self._tree(src)
            home = Path(d) / "home"
            home.mkdir()

            def run():
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = jw_improve.main(["trace", "--source", str(src)])
                return rc

            rc = _run_with_home(home, run)
            self.assertEqual(rc, 0)
            out_dir = home / ".claude" / "jahns-workflow" / "improve"
            self.assertTrue((out_dir / "sessions.jsonl").is_file())
            self.assertTrue((out_dir / "parse_coverage.json").is_file())


class ImproveTraceTests(unittest.TestCase):
    """End-to-end trace: schema, provenance, verification/retry classification, determinism."""

    def _fixture(self, src: Path):
        slug = src / "-Users-jahn-demo"
        (slug / _UUID / "subagents").mkdir(parents=True)
        aid = "a1b2c3d4e5f6a7b8c"

        def asst(uuid, req, blocks, model="claude-opus-4-8", ts=None):
            msg = {"model": model, "content": blocks, "usage": {"input_tokens": 10, "output_tokens": 5}}
            r = {"type": "assistant", "uuid": uuid, "requestId": req, "message": msg}
            if ts:
                r["timestamp"] = ts
            return r

        def bash(uuid, req, tuid, cmd, ts=None):
            return asst(uuid, req, [{"type": "tool_use", "id": tuid, "name": "Bash",
                                     "input": {"command": cmd}}], ts=ts)

        def result(uuid, tuid, is_error=False, tur=None):
            r = {"type": "user", "uuid": uuid,
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": tuid, "content": "out", "is_error": is_error}]}}
            if tur is not None:
                r["toolUseResult"] = tur
            return r

        main_records = [
            {"type": "user", "uuid": "u1", "cwd": "/repo", "gitBranch": "dev",
             "timestamp": "2026-07-01T00:00:00Z",
             "message": {"role": "user", "content": "please implement"}},               # 1 turn
            bash("a1", "r1", "toolu_pytest", "uv run pytest tests/ -x"),                  # 2 verification
            result("t1", "toolu_pytest", is_error=False),                                # 3
            bash("a2", "r2", "toolu_build", "make build"),                               # 4 build
            result("t2", "toolu_build", is_error=False),                                 # 5
            bash("a3", "r3", "toolu_rt1", "python run_thing.py"),                        # 6 retry chain
            result("t3", "toolu_rt1", is_error=True),                                    # 7
            bash("a4", "r4", "toolu_rt2", "python run_thing.py"),                        # 8
            result("t4", "toolu_rt2", is_error=True),                                    # 9
            bash("a5", "r5", "toolu_rt3", "python run_thing.py"),                        # 10
            result("t5", "toolu_rt3", is_error=True),                                    # 11
            asst("a6", "r6", [{"type": "tool_use", "id": "toolu_agent", "name": "Agent",
                               "input": {"subagent_type": "Explore", "model": "sonnet",
                                         "prompt": "go explore"}}], ts="2026-07-01T00:05:00Z"),  # 12
            result("t6", "toolu_agent", is_error=False,
                   tur={"agentId": aid, "resolvedModel": "claude-sonnet-4-5",
                        "status": "completed", "isAsync": False}),                       # 13
            {"type": "user", "uuid": "usr", "message": {"role": "user",
             "content": "<system-reminder>ignore me</system-reminder>"}},               # 14 not a turn
            {"type": "mode", "mode": "default"},                                          # 15 state record
        ]
        # append a truncated (partial) tail line, no trailing newline
        parts = [_json.dumps(r) for r in main_records] + ['{"type":"assistant","uuid":"trunc"']
        (slug / f"{_UUID}.jsonl").write_text("\n".join(parts), encoding="utf-8")

        # linked subagent transcript + meta (so linked_transcript resolves and agent_meta populates)
        sub = slug / _UUID / "subagents" / f"agent-{aid}.jsonl"
        _write_jsonl(sub, [
            {"type": "user", "uuid": "s1", "isSidechain": True,
             "message": {"role": "user", "content": "do explore"}},
            {"type": "assistant", "uuid": "s2", "isSidechain": True, "requestId": "sr",
             "message": {"id": "sm", "model": "claude-sonnet-4-5",
                         "content": [{"type": "text", "text": "done"}],
                         "usage": {"input_tokens": 5, "output_tokens": 5}}},
        ])
        (slug / _UUID / "subagents" / f"agent-{aid}.meta.json").write_text(
            _json.dumps({"agentType": "Explore", "description": "exploring", "spawnDepth": 1}),
            encoding="utf-8")
        return aid

    def _load(self, out_dir: Path):
        sessions = [_json.loads(ln) for ln in
                    (out_dir / "sessions.jsonl").read_text().splitlines() if ln]
        dels = [_json.loads(ln) for ln in
                (out_dir / "delegations.jsonl").read_text().splitlines() if ln]
        cov = _json.loads((out_dir / "parse_coverage.json").read_text())
        return sessions, dels, cov

    def test_trace_schema_and_provenance(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "projects"
            src.mkdir()
            aid = self._fixture(src)
            out = Path(d) / "out"
            jw_improve.run_trace([src], set(), out)
            sessions, dels, cov = self._load(out)

            self.assertEqual(cov["row_totals"], {"sessions": 2, "delegations": 1})
            main = [s for s in sessions if s["kind"] == "main"][0]
            sub = [s for s in sessions if s["kind"] == "subagent"][0]

            # explicit reads
            self.assertEqual(main["project"], "-Users-jahn-demo")
            self.assertEqual(main["session_id"], _UUID)
            self.assertEqual(main["cwd"], "/repo")
            self.assertEqual(main["git_branch"], "dev")
            self.assertEqual(main["started_at"], "2026-07-01T00:00:00Z")
            self.assertEqual(main["ended_at"], "2026-07-01T00:05:00Z")
            self.assertIn("opus-4-8", main["models"])
            self.assertEqual(main["errors"], {"api": 0, "tool": 3, "parse": 0})
            self.assertEqual(main["delegations"], 1)

            # inferred labels carry rule + provenance
            self.assertEqual(main["turns"], {"value": 1, "provenance": "inferred", "rule": "turn-index-v1"})
            self.assertEqual(main["tools"]["provenance"], "inferred")
            self.assertEqual(main["tools"]["rule"], "tool-category-v1")

            # verification: pytest classified, evidence pointer line accurate
            self.assertEqual(main["verification"]["runs"], 1)
            self.assertEqual(main["verification"]["failed"], 0)
            self.assertEqual(main["verification"]["provenance"], "inferred")
            self.assertEqual(main["verification"]["examples"][0]["line"], 2)
            self.assertEqual(main["verification"]["examples"][0]["head"], "uv run pytest tests/ -x")
            # build tracked separately
            self.assertEqual(main["build"]["runs"], 1)
            # non-matching shell counted, never force-classified
            self.assertEqual(main["unclassified_shell"], 3)

            # retry loop: 3 same-cmd re-runs after is_error
            self.assertEqual(main["retry_loops"]["count"], 1)
            self.assertEqual(main["retry_loops"]["rule"], "same-cmd-refail-v1")
            self.assertEqual(main["retry_loops"]["examples"][0]["line"], 6)

            # subagent agent_meta from meta.json (explicit)
            self.assertEqual(sub["agent_id"], aid)
            self.assertEqual(sub["agent_meta"],
                             {"agentType": "Explore", "description": "exploring", "spawnDepth": 1})
            self.assertIsNone(main["agent_meta"])

            # delegation row
            self.assertEqual(len(dels), 1)
            dl = dels[0]
            self.assertEqual(dl["tool"], "Agent")
            self.assertEqual(dl["subagent_type"], "Explore")
            self.assertEqual(dl["model_requested"], "sonnet")
            self.assertEqual(dl["resolved_model"], "claude-sonnet-4-5")
            self.assertEqual(dl["agent_id"], aid)
            self.assertEqual(dl["status"], "completed")
            self.assertEqual(dl["is_async"], False)
            self.assertEqual(dl["line"], 12)
            self.assertTrue(dl["linked_transcript"].endswith(f"agent-{aid}.jsonl"))

            # coverage: partial tail counted separately from parse errors
            self.assertGreaterEqual(cov["partial_tail_lines"], 1)
            self.assertEqual(cov["record_parse_errors"], 0)
            self.assertEqual(cov["files_by_kind"]["main_transcript"], 1)
            self.assertEqual(cov["files_by_kind"]["subagent_transcript"], 1)
            self.assertEqual(cov["parser_version"], jw_cclog.PARSER_VERSION)

    def test_byte_identical_reruns(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "projects"
            src.mkdir()
            self._fixture(src)
            out1, out2 = Path(d) / "o1", Path(d) / "o2"
            jw_improve.run_trace([src], set(), out1)
            jw_improve.run_trace([src], set(), out2)
            for name in ("sessions.jsonl", "delegations.jsonl", "parse_coverage.json"):
                self.assertEqual((out1 / name).read_bytes(), (out2 / name).read_bytes(),
                                 f"{name} not byte-identical across re-runs")


class ImproveSelfSessionTests(unittest.TestCase):
    """Self-session truncation: a live `improve trace` must not ingest its own mid-write transcript.
    The current session's main transcript is cut at the improve invocation; everything else is intact."""

    @staticmethod
    def _u(uuid, text):
        return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}

    @staticmethod
    def _bash(uuid, tuid, cmd):
        return {"type": "assistant", "uuid": uuid, "requestId": uuid,
                "message": {"id": "m" + uuid, "model": "claude-opus-4-8",
                            "content": [{"type": "tool_use", "id": tuid, "name": "Bash",
                                         "input": {"command": cmd}}]}}

    @staticmethod
    def _result(uuid, tuid):
        return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tuid, "content": "out", "is_error": False}]}}

    @staticmethod
    def _cmd_tag(uuid):
        return {"type": "user", "uuid": uuid, "message": {"role": "user",
                "content": "<command-name>/jahns-workflow:improve</command-name>\n<command-args></command-args>"}}

    @staticmethod
    def _agent(uuid, tuid):
        return {"type": "assistant", "uuid": uuid, "requestId": uuid,
                "message": {"id": "m" + uuid, "model": "claude-opus-4-8",
                            "content": [{"type": "tool_use", "id": tuid, "name": "Agent",
                                         "input": {"subagent_type": "Explore", "prompt": "go"}}]}}

    def _trace(self, sources, out, sid):
        from unittest.mock import patch
        import os
        with patch.dict(os.environ, {}, clear=False):
            if sid is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = sid
            return jw_improve.run_trace(list(sources), set(), out)

    def _sessions(self, out):
        return [_json.loads(ln) for ln in (out / "sessions.jsonl").read_text().splitlines() if ln]

    @staticmethod
    def _shell(session):
        return session["tools"]["by_category"].get("shell", 0)

    def _main_src(self, d, stem, records) -> Path:
        src = Path(d) / "projects"
        slug = src / "-Users-jahn-demo"
        slug.mkdir(parents=True, exist_ok=True)
        _write_jsonl(slug / f"{stem}.jsonl", records)
        return src

    def test_env_unset_or_empty_no_self_session_key(self):
        # env unset AND empty-string both mean "not a live self-run": no truncation, no coverage key,
        # byte-identical outputs (a command-tag in the file must be ignored)
        with tempfile.TemporaryDirectory() as d:
            src = self._main_src(d, _UUID, [
                self._u("u1", "implement"), self._cmd_tag("c1"),
                self._bash("a1", "t1", "echo tail")])
            out_unset, out_empty = Path(d) / "unset", Path(d) / "empty"
            cov = self._trace([src], out_unset, None)
            self.assertNotIn("self_session", cov)
            self._trace([src], out_empty, "")
            for name in ("sessions.jsonl", "delegations.jsonl", "parse_coverage.json"):
                self.assertEqual((out_unset / name).read_bytes(), (out_empty / name).read_bytes())
            main = [s for s in self._sessions(out_unset) if s["kind"] == "main"][0]
            self.assertEqual(self._shell(main), 1)  # tail bash processed (no truncation)

    def test_command_tag_anchor_mid_file(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._main_src(d, _UUID, [
                self._u("u1", "implement"),                          # 1 turn
                self._bash("a2", "t2", "echo hi"),                   # 2 pre-anchor shell
                self._result("r2", "t2"),                            # 3
                self._cmd_tag("c1"),                                 # 4 ANCHOR
                self._bash("a5", "t5", "uv run jw.py improve trace"),  # 5 excluded
                self._result("r5", "t5"),                            # 6 excluded
                self._agent("a7", "t7"),                             # 7 excluded delegation
            ])
            out = Path(d) / "out"
            cov = self._trace([src], out, _UUID)
            self.assertEqual(cov["self_session"], {"session_id": _UUID, "file_found": True,
                                                   "anchor": "command-tag", "lines_excluded": 4})
            main = [s for s in self._sessions(out) if s["kind"] == "main"][0]
            self.assertEqual(main["turns"]["value"], 1)
            self.assertEqual(self._shell(main), 1)      # pre-anchor bash only
            self.assertEqual(main["delegations"], 0)    # post-anchor Agent excluded
            self.assertEqual((out / "delegations.jsonl").read_text().splitlines(), [])

    def test_tool_use_anchor_when_no_command_tag(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._main_src(d, _UUID, [
                self._u("u1", "implement"),                          # 1 turn
                self._bash("a2", "t2", "echo hi"),                   # 2 pre-anchor shell
                self._result("r2", "t2"),                            # 3
                self._bash("a4", "t4", "uv run /x/jw.py improve trace --out /tmp/o"),  # 4 ANCHOR
                self._result("r4", "t4"),                            # 5 excluded
            ])
            out = Path(d) / "out"
            cov = self._trace([src], out, _UUID)
            self.assertEqual(cov["self_session"], {"session_id": _UUID, "file_found": True,
                                                   "anchor": "tool-use", "lines_excluded": 2})
            main = [s for s in self._sessions(out) if s["kind"] == "main"][0]
            self.assertEqual(self._shell(main), 1)      # anchor bash itself excluded

    def test_no_anchor_processes_whole_file(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._main_src(d, _UUID, [
                self._u("u1", "implement"), self._bash("a2", "t2", "echo hi"),
                self._result("r2", "t2")])
            out = Path(d) / "out"
            cov = self._trace([src], out, _UUID)
            self.assertEqual(cov["self_session"], {"session_id": _UUID, "file_found": True,
                                                   "anchor": None, "lines_excluded": 0})
            main = [s for s in self._sessions(out) if s["kind"] == "main"][0]
            self.assertEqual(main["turns"]["value"], 1)
            self.assertEqual(self._shell(main), 1)

    def test_only_matching_main_session_truncated(self):
        sid_b = "0123abcd-1234-1234-1234-0123456789ff"
        aid = "a1b2c3d4e5f6"
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "projects"
            slug = src / "-Users-jahn-demo"
            (slug / _UUID / "subagents").mkdir(parents=True)
            # session A (matches env) — truncated at its command-tag
            _write_jsonl(slug / f"{_UUID}.jsonl", [
                self._u("ua", "implement"),                 # 1
                self._bash("aa", "ta", "echo preA"),        # 2 kept
                self._cmd_tag("ca"),                        # 3 ANCHOR
                self._bash("ab", "tb", "echo postA"),       # 4 excluded
            ])
            # A's subagent — NOT a main transcript, so its command-tag is NOT an anchor
            _write_jsonl(slug / _UUID / "subagents" / f"agent-{aid}.jsonl", [
                {"type": "user", "uuid": "s1", "isSidechain": True,
                 "message": {"role": "user", "content": "do work"}},
                self._cmd_tag("s2"),
                self._bash("s3", "ts3", "echo subpost"),    # must still be counted
            ])
            # session B (different id) — not the self session, so it is left intact
            _write_jsonl(slug / f"{sid_b}.jsonl", [
                self._u("ub", "implement"),
                self._cmd_tag("cb"),
                self._bash("bb", "tbb", "echo postB"),      # must still be counted
            ])
            out = Path(d) / "out"
            cov = self._trace([src], out, _UUID)
            self.assertEqual(cov["self_session"], {"session_id": _UUID, "file_found": True,
                                                   "anchor": "command-tag", "lines_excluded": 2})
            sess = self._sessions(out)
            a_main = [s for s in sess if s["kind"] == "main" and s["session_id"] == _UUID][0]
            b_main = [s for s in sess if s["kind"] == "main" and s["session_id"] == sid_b][0]
            subagent = [s for s in sess if s["kind"] == "subagent"][0]
            self.assertEqual(self._shell(a_main), 1)    # preA only (truncated)
            self.assertEqual(self._shell(b_main), 1)    # postB kept (untruncated)
            self.assertEqual(self._shell(subagent), 1)  # subpost kept (untruncated)

    def test_last_command_tag_is_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._main_src(d, _UUID, [
                self._u("u1", "implement"),                 # 1 turn
                self._cmd_tag("c1"),                        # 2 first invocation
                self._bash("a3", "t3", "echo between"),     # 3 BETWEEN the two -> included
                self._cmd_tag("c2"),                        # 4 LAST -> ANCHOR
                self._bash("a5", "t5", "echo after"),       # 5 excluded
            ])
            out = Path(d) / "out"
            cov = self._trace([src], out, _UUID)
            self.assertEqual(cov["self_session"], {"session_id": _UUID, "file_found": True,
                                                   "anchor": "command-tag", "lines_excluded": 2})
            main = [s for s in self._sessions(out) if s["kind"] == "main"][0]
            self.assertEqual(self._shell(main), 1)      # 'between' kept, 'after' cut

    def test_quoted_command_tag_is_not_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._main_src(d, _UUID, [
                self._u("u1", "implement"),                                                # 1 turn
                self._u("u2", "I ran <command-name>/jahns-workflow:improve</command-name> earlier"),  # 2
                self._bash("a3", "t3", "echo tail"),                                       # 3 included
            ])
            out = Path(d) / "out"
            cov = self._trace([src], out, _UUID)
            self.assertEqual(cov["self_session"], {"session_id": _UUID, "file_found": True,
                                                   "anchor": None, "lines_excluded": 0})
            main = [s for s in self._sessions(out) if s["kind"] == "main"][0]
            self.assertEqual(self._shell(main), 1)      # tail processed (a quoted tag is not a cut)

    def test_audit_coverage_caveats_carries_self_session(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cov = {"parser_version": jw_cclog.PARSER_VERSION, "files_skipped": 0,
                   "record_parse_errors": 0, "replayed_records_skipped": 0, "partial_tail_lines": 0,
                   "unknown_raw_types": {}, "row_totals": {"sessions": 0, "delegations": 0},
                   "self_session": {"session_id": _UUID, "file_found": True,
                                    "anchor": "command-tag", "lines_excluded": 3}}
            (d / "parse_coverage.json").write_text(_json.dumps(cov))
            facts = jw_improve.run_audit(d)
            cc = [l for l in facts["lenses"] if l["lens"] == "coverage_caveats"][0]
            self.assertEqual(cc["summary"]["self_session"],
                             {"session_id": _UUID, "file_found": True,
                              "anchor": "command-tag", "lines_excluded": 3})


# feedback file exactly as jw_review.ingest writes it: metadata header, byte-exact reviewer body
# (which itself contains `### JW-GPT-NNN` blocks + `- Severity:` lines we must NOT parse), then an
# APPENDED triage table under `## Findings (triage skeleton …)` — the only thing improve reviews reads.
_TRIAGE_FEEDBACK = """<!-- jahns-workflow feedback: verbatim body below; triage skeleton appended. -->
round: 2026-07-01-alpha
reviewer: gpt-5.5-pro
ingested: 2026-07-01
source: /tmp/review.md

---

### JW-GPT-001 — some finding
- Severity: blocker

### JW-GPT-002 — another finding
- Severity: minor


---

## Findings (triage skeleton — verify each before registering)

| finding | severity | verdict (REAL/REJECTED/NEEDS-RULING) | evidence | task id |
|---|---|---|---|---|
| JW-GPT-001 — some finding | blocker | REAL | confirmed in code | fix/thing |
| JW-GPT-002 — another finding | minor | REJECTED | wrong, see SSOT | |
| JW-GPT-003 — unscored finding | ? |  |  |  |
"""


class ImproveReviewsTests(unittest.TestCase):
    """Registry-driven review projection: triage-table + finding-task join, provenance, skips."""

    def _fixture(self, d: Path) -> Path:
        proj = d / "projA"
        proj.mkdir()
        (proj / ".jahns-workflow.yml").write_text("version: 1\nproject: a\n")  # reviews_dir=docs/reviews
        rdir = proj / "docs" / "reviews"
        rdir.mkdir(parents=True)
        (rdir / "2026-07-01-alpha-request.md").write_text("# Review Request — alpha\n")
        (rdir / "2026-07-01-alpha-feedback.md").write_text(_TRIAGE_FEEDBACK)
        (rdir / "2026-07-02-beta-request.md").write_text("# Review Request — beta\n")  # no feedback yet
        # finding-derived tasks: linked to the REVIEW round via `origin: review-<round-id>`
        (proj / "tasks.yaml").write_text(
            "version: 1\nproject: a\ntasks:\n"
            "  - id: fix/thing\n    title: 'fix the thing'\n    status: pending\n"
            "    severity: major\n    origin: review-2026-07-01-alpha\n"
            "  - id: feat/unrelated\n    title: 'not a finding'\n    status: active\n")
        (proj / "tasks.archive.yaml").write_text(
            "version: 1\nproject: a\ntasks:\n"
            "  - id: fix/old\n    title: 'archived finding'\n    status: done\n"
            "    severity: blocker\n    origin: review-2026-07-01-alpha\n")
        registry = d / "projects.json"
        registry.write_text(_json.dumps({"projects": [
            {"name": "proj-a", "path": str(proj)},
            {"name": "remote-only", "repo": "owner/x"},
            {"name": "gone", "path": str(d / "missing")},
        ]}))
        return registry

    def _load(self, out: Path):
        rows = [_json.loads(ln) for ln in (out / "reviews.jsonl").read_text().splitlines() if ln]
        cov = _json.loads((out / "reviews_coverage.json").read_text())
        return rows, cov

    def test_reviews_projection(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            registry = self._fixture(d)
            out = d / "out"
            jw_improve.run_reviews(registry, out)
            rows, cov = self._load(out)

            # coverage: one scanned, remote-only + missing-path skipped (fail-loud, not fatal)
            self.assertEqual(cov["projects_scanned"], ["proj-a"])
            self.assertEqual(cov["projects_total"], 3)
            self.assertEqual([s["project"] for s in cov["projects_skipped"]], ["gone", "remote-only"])
            # a triage row that names its fix-task (task-id cell) is ONE finding, not two (dedup):
            # JW-GPT-001 + JW-GPT-002 + JW-GPT-003 (triage) + fix/old (unreferenced task) = 4
            self.assertEqual(cov["row_totals"], {"reviews": 2, "findings": 4})

            # rows sorted by (project, round_id)
            self.assertEqual([r["round_id"] for r in rows], ["2026-07-01-alpha", "2026-07-02-beta"])
            alpha = rows[0]
            self.assertEqual(alpha["project"], "proj-a")
            self.assertTrue(alpha["request_file"].endswith("2026-07-01-alpha-request.md"))
            self.assertTrue(alpha["feedback_file"].endswith("2026-07-01-alpha-feedback.md"))

            byid = {f["id"]: f for f in alpha["findings"]}
            # triage findings: severity read structurally from the table cell (explicit). JW-GPT-001's
            # task-id cell names fix/thing (a joined task) → merged into ONE triage finding carrying
            # task_id; the separate fix/thing task finding is NOT emitted (dedup)
            self.assertEqual(byid["JW-GPT-001"],
                             {"id": "JW-GPT-001", "severity": "blocker", "status": "REAL",
                              "source": "triage", "provenance": "explicit", "task_id": "fix/thing"})
            self.assertNotIn("fix/thing", byid)  # deduped into JW-GPT-001, not a second finding
            self.assertEqual(byid["JW-GPT-002"]["status"], "REJECTED")
            self.assertEqual(byid["JW-GPT-002"]["severity"], "minor")
            # `?` severity is unparseable → provenance unknown, NOT keyword-guessed from prose
            self.assertEqual(byid["JW-GPT-003"]["severity"], None)
            self.assertEqual(byid["JW-GPT-003"]["provenance"], "unknown")
            # a finding-derived task NOT referenced by any triage row remains source "task"
            self.assertEqual(byid["fix/old"]["source"], "task")
            self.assertNotIn("feat/unrelated", byid)
            # counts: blocker JW-GPT-001 + fix/old = 2; minor JW-GPT-002 = 1; unknown JW-GPT-003 = 1;
            # the merged fix/thing is counted once (as its triage blocker), not doubled as a major
            self.assertEqual(alpha["counts"], {"blocker": 2, "major": 0, "minor": 1, "unknown": 1})

            # beta: request only, no findings
            beta = rows[1]
            self.assertIsNone(beta["feedback_file"])
            self.assertEqual(beta["findings"], [])
            self.assertEqual(beta["counts"], {"blocker": 0, "major": 0, "minor": 0, "unknown": 0})

    def test_triage_ignores_verbatim_body(self):
        # the verbatim body's `### JW-GPT-*` blocks must not be parsed — only the appended table
        findings = jw_improve._parse_triage(_TRIAGE_FEEDBACK)
        self.assertEqual([f["id"] for f in findings], ["JW-GPT-001", "JW-GPT-002", "JW-GPT-003"])
        # a feedback body with NO appended skeleton yields nothing
        self.assertEqual(jw_improve._parse_triage("just prose, no table\n### JW-GPT-9 — x"), [])

    def test_byte_identical_reruns(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            registry = self._fixture(d)
            o1, o2 = d / "o1", d / "o2"
            jw_improve.run_reviews(registry, o1)
            jw_improve.run_reviews(registry, o2)
            for name in ("reviews.jsonl", "reviews_coverage.json"):
                self.assertEqual((o1 / name).read_bytes(), (o2 / name).read_bytes(),
                                 f"{name} not byte-identical across re-runs")

    def test_cli_default_out_honors_home(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            (home / ".claude" / "jahns-workflow").mkdir(parents=True)
            # place the registry where the runtime path resolves it under the fake HOME
            reg = self._fixture(d)
            (home / ".claude" / "jahns-workflow" / "projects.json").write_text(reg.read_text())

            def run():
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = jw_improve.main(["reviews"])
                return rc

            rc = _run_with_home(home, run)
            self.assertEqual(rc, 0)
            out_dir = home / ".claude" / "jahns-workflow" / "improve"
            self.assertTrue((out_dir / "reviews.jsonl").is_file())
            self.assertTrue((out_dir / "reviews_coverage.json").is_file())


class ImproveAuditTests(unittest.TestCase):
    """Deterministic audit facts over the four projection artifacts (synthetic fixtures)."""

    def _sessions(self):
        return [
            {"project": "-p", "kind": "main", "session_id": "s1", "file": "/x/s1.jsonl",
             "tools": {"by_category": {"file_write": 5, "shell": 3, "agent_spawn": 1}},
             "delegations": 1, "verification": {"runs": 0}, "unclassified_shell": 2,
             "retry_loops": {"count": 2, "examples": [{"line": 10, "head": "cmd"}]},
             "context_heavy": {"tool_results_over_100kb": 1, "max_result_bytes": 200000},
             "errors": {"api": 0, "tool": 2, "parse": 0}},
            {"project": "-p", "kind": "main", "session_id": "s2", "file": "/x/s2.jsonl",
             "tools": {"by_category": {"file_write": 0, "shell": 1}},
             "delegations": 0, "verification": {"runs": 1}, "unclassified_shell": 0,
             "retry_loops": {"count": 0, "examples": []},
             "context_heavy": {"tool_results_over_100kb": 0, "max_result_bytes": 50},
             "errors": {"api": 1, "tool": 0, "parse": 0}},
        ]

    def _delegations(self):
        return [
            {"project": "-p", "session_id": "s1", "file": "/x/s1.jsonl", "line": 12, "tool": "Agent",
             "subagent_type": "Explore", "model_requested": "sonnet",
             "resolved_model": "claude-sonnet-4-5", "agent_id": "a", "status": "completed",
             "is_async": False, "linked_transcript": None},
            {"project": "-p", "session_id": "s1", "file": "/x/s1.jsonl", "line": 20, "tool": "Workflow",
             "subagent_type": None, "model_requested": None,
             "resolved_model": {"provenance": "unknown"}, "agent_id": {"provenance": "unknown"},
             "status": {"provenance": "unknown"}, "is_async": {"provenance": "unknown"},
             "linked_transcript": None},
        ]

    def _reviews(self):
        return [
            {"project": "proj-a", "root": "/r", "round_id": "2026-07-01-alpha",
             "request_file": "/r/req.md", "feedback_file": "/r/fb.md",
             "findings": [
                 {"id": "JW-GPT-001", "severity": "blocker", "status": "REAL",
                  "source": "triage", "provenance": "explicit"},
                 {"id": "fix/x", "severity": "major", "status": "done",
                  "source": "task", "provenance": "explicit"}],
             "counts": {"blocker": 1, "major": 1, "minor": 0, "unknown": 0}},
        ]

    def _coverage(self):
        return {"parser_version": "jw-trace-2", "generated_from": ["/x"],
                "files_by_kind": {"main_transcript": 2}, "files_skipped": 0,
                "event_type_counts": {}, "unknown_raw_types": {"weird": 1},
                "record_parse_errors": 0, "replayed_records_skipped": 1,
                "partial_tail_lines": 1, "row_totals": {"sessions": 2, "delegations": 2}}

    def _write_inputs(self, d: Path, *, sessions=True, delegations=True, reviews=True, coverage=True):
        if sessions:
            _write_jsonl(d / "sessions.jsonl", self._sessions())
        if delegations:
            _write_jsonl(d / "delegations.jsonl", self._delegations())
        if reviews:
            _write_jsonl(d / "reviews.jsonl", self._reviews())
        if coverage:
            (d / "parse_coverage.json").write_text(_json.dumps(self._coverage()))

    def test_all_lenses(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self._write_inputs(d)
            facts = jw_improve.run_audit(d)
            self.assertEqual(facts["skipped_lenses"], [])
            lenses = {l["lens"]: l for l in facts["lenses"]}
            self.assertEqual(sorted(lenses), [
                "context_heavy", "coverage_caveats", "delegation_pattern", "error_landscape",
                "main_direct_work", "retry_loops", "review_association", "verification_debt"])
            # every fact carries a versioned rule + provenance
            for l in facts["lenses"]:
                self.assertRegex(l["rule"], r"-v\d+$")
                self.assertIn(l["provenance"], ("inferred", "explicit"))
                self.assertLessEqual(len(l["examples"]), 5)

            mdw = lenses["main_direct_work"]["per_project"]["-p"]
            self.assertEqual((mdw["main_sessions"], mdw["file_write"], mdw["shell"],
                              mdw["direct_work"]), (2, 5, 4, 9))
            self.assertEqual(mdw["sessions_delegation_zero_direct"], 1)  # s2: deleg 0, direct 1
            self.assertEqual(lenses["main_direct_work"]["provenance"], "inferred")

            vd = lenses["verification_debt"]["per_project"]["-p"]
            self.assertEqual((vd["file_write_sessions"], vd["debt_sessions"],
                              vd["unclassified_shell_total"]), (1, 1, 2))

            rl = lenses["retry_loops"]["per_project"]["-p"]
            self.assertEqual((rl["sessions_with_retry"], rl["retry_loops_total"]), (1, 2))
            self.assertEqual(lenses["retry_loops"]["examples"][0]["line"], 10)

            ch = lenses["context_heavy"]["per_project"]["-p"]
            self.assertEqual((ch["sessions_over_100kb"], ch["max_result_bytes"]), (1, 200000))

            dp = lenses["delegation_pattern"]["per_project"]["-p"]
            self.assertEqual(dp["delegations"], 2)
            self.assertEqual(dp["by_tool"], {"Agent": 1, "Workflow": 1})
            self.assertEqual(dp["workflow_delegations"], 1)
            self.assertEqual(dp["by_resolved_model"], {"claude-sonnet-4-5": 1, "unknown": 1})
            self.assertEqual(dp["async_count"], 0)

            el = lenses["error_landscape"]["per_project"]["-p"]
            self.assertEqual((el["api"], el["tool"], el["parse"], el["sessions_with_errors"]),
                             (1, 2, 0, 2))

            ra = lenses["review_association"]["per_project"]["proj-a"]
            self.assertEqual(ra["rounds"], 1)
            self.assertEqual(ra["findings_total"], 2)
            self.assertEqual(ra["severity_counts"], {"blocker": 1, "major": 1, "minor": 0, "unknown": 0})
            self.assertEqual(ra["by_source"], {"task": 1, "triage": 1})
            self.assertEqual(ra["round_session_mapping"], {"provenance": "unknown"})

            cc = lenses["coverage_caveats"]["summary"]
            self.assertEqual(cc["partial_tail_lines"], 1)
            self.assertEqual(cc["unknown_raw_types"], {"weird": 1})

    def test_missing_inputs_skip_lenses(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self._write_inputs(d, delegations=False, reviews=False, coverage=False)  # sessions only
            facts = jw_improve.run_audit(d)
            self.assertEqual({s["lens"] for s in facts["skipped_lenses"]},
                             {"delegation_pattern", "review_association", "coverage_caveats"})
            self.assertEqual({l["lens"] for l in facts["lenses"]},
                             {"main_direct_work", "verification_debt", "retry_loops",
                              "context_heavy", "error_landscape"})
            self.assertEqual(facts["inputs"],
                             {"sessions": True, "delegations": False, "reviews": False,
                              "parse_coverage": False})

    def test_byte_identical_reruns(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self._write_inputs(d)
            jw_improve.run_audit(d)
            first = (d / "facts.json").read_bytes()
            jw_improve.run_audit(d)
            self.assertEqual(first, (d / "facts.json").read_bytes())


class ImproveDecideTests(unittest.TestCase):
    """Append-only user-decision log for improve recommendations (synthetic fixtures)."""

    def _lines(self, path: Path):
        return [_json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def test_append_shape(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "improve"
            rc = jw_improve.main(
                ["decide", "main_direct_work/heavy-mains", "accept",
                 "--title", "delegate heavy mains", "--note", "seen in 3 sessions", "--out", str(out)])
            self.assertEqual(rc, 0)
            lines = self._lines(out / "decisions.jsonl")
            self.assertEqual(len(lines), 1)
            rec = lines[0]
            self.assertEqual(rec["rec_id"], "main_direct_work/heavy-mains")
            self.assertEqual(rec["decision"], "accept")
            self.assertEqual(rec["title"], "delegate heavy mains")
            self.assertEqual(rec["note"], "seen in 3 sessions")
            # `at` is an ISO-8601 timestamp (allowed here — user-action log, not a derived artifact)
            from datetime import datetime
            datetime.fromisoformat(rec["at"])

    def test_optional_fields_omitted(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "improve"
            rc = jw_improve.main(["decide", "retry_loops/same-cmd", "reject", "--out", str(out)])
            self.assertEqual(rc, 0)
            rec = self._lines(out / "decisions.jsonl")[0]
            self.assertNotIn("title", rec)
            self.assertNotIn("note", rec)
            self.assertEqual(rec["decision"], "reject")

    def test_redecision_appends_history_latest_wins(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "improve"
            rid = "verification_debt/add-verify"
            self.assertEqual(jw_improve.main(["decide", rid, "reject", "--out", str(out)]), 0)
            self.assertEqual(jw_improve.main(["decide", rid, "accept", "--out", str(out)]), 0)
            lines = self._lines(out / "decisions.jsonl")
            self.assertEqual(len(lines), 2)  # both preserved (append-only history)
            self.assertEqual([l["decision"] for l in lines], ["reject", "accept"])
            self.assertTrue(all(l["rec_id"] == rid for l in lines))
            latest = [l for l in lines if l["rec_id"] == rid][-1]
            self.assertEqual(latest["decision"], "accept")  # latest row is the effective decision

    def test_missing_and_invalid_args(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "improve"
            # missing decision verb
            self.assertEqual(jw_improve.main(["decide", "main_direct_work/x", "--out", str(out)]), 1)
            # decision must be accept|reject
            self.assertEqual(jw_improve.main(["decide", "main_direct_work/x", "maybe", "--out", str(out)]), 1)
            # rec-id must be <lens>/<kebab-gist> (single slash)
            self.assertEqual(jw_improve.main(["decide", "noslash", "accept", "--out", str(out)]), 1)
            # no uppercase / non-kebab gist
            self.assertEqual(jw_improve.main(["decide", "Lens/Bad_Gist", "accept", "--out", str(out)]), 1)
            # gist may not end in a hyphen
            self.assertEqual(jw_improve.main(["decide", "lens/bad-", "accept", "--out", str(out)]), 1)
            # unknown flag rejected
            self.assertEqual(jw_improve.main(["decide", "lens/ok", "accept", "--bogus", "x", "--out", str(out)]), 1)
            # a precondition failure never creates the log
            self.assertFalse((out / "decisions.jsonl").exists())

    def test_out_override_and_home_default(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            (home / ".claude" / "jahns-workflow").mkdir(parents=True)

            def run():
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    return jw_improve.main(["decide", "context_heavy/trim", "accept"])
            rc = _run_with_home(home, run)
            self.assertEqual(rc, 0)
            default_log = home / ".claude" / "jahns-workflow" / "improve" / "decisions.jsonl"
            self.assertTrue(default_log.is_file())  # default --out honors HOME

            explicit = d / "elsewhere"
            rc2 = _run_with_home(home, lambda: jw_improve.main(
                ["decide", "context_heavy/trim", "reject", "--out", str(explicit)]))
            self.assertEqual(rc2, 0)
            self.assertTrue((explicit / "decisions.jsonl").is_file())  # override lands elsewhere
            self.assertEqual(len(self._lines(default_log)), 1)  # default log untouched by the override


class ImproveM1DefectTests(unittest.TestCase):
    """Regression tests for the 0.7.0 M1 adversarial-review defects (RED-turned-GREEN)."""

    def _quiet(self, fn):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn()

    # ---- finding 3: verify-cmd regex must require a runner/verb, not a bare tests/ path ----
    def test_verify_cmd_requires_runner(self):
        c = jw_improve.classify_verification
        self.assertIsNone(c("cat tests/x.py"))
        self.assertIsNone(c("git diff tests/"))
        self.assertIsNone(c("ls tests/"))
        self.assertIsNone(c("rm -rf tests/__pycache__"))
        # real runners / a runner-led tests/ path still classify
        self.assertEqual(c("uv run pytest tests/x.py"), "test")
        self.assertEqual(c("pytest tests/"), "test")
        self.assertEqual(c("python tests/run.py"), "test")

    # ---- finding 2: a passing build is verification; build-only session is NOT debt ----
    def test_build_only_session_not_debt(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _write_jsonl(d / "sessions.jsonl", [
                {"project": "-p", "kind": "main", "session_id": "s1", "file": "/x/s1.jsonl",
                 "tools": {"by_category": {"file_write": 3, "shell": 1}},
                 "verification": {"runs": 0}, "build": {"runs": 2}, "unclassified_shell": 0},
                {"project": "-p", "kind": "main", "session_id": "s2", "file": "/x/s2.jsonl",
                 "tools": {"by_category": {"file_write": 2}},
                 "verification": {"runs": 0}, "build": {"runs": 0}, "unclassified_shell": 0},
            ])
            facts = jw_improve.run_audit(d)
            vd = {l["lens"]: l for l in facts["lenses"]}["verification_debt"]
            self.assertEqual(vd["rule"], "verification-debt-v2")
            pp = vd["per_project"]["-p"]
            self.assertEqual(pp["file_write_sessions"], 2)
            self.assertEqual(pp["debt_sessions"], 1)        # only s2 (no build, no verify)
            self.assertEqual(pp["build_only_sessions"], 1)  # s1 rescued from false debt
            self.assertEqual(pp["debt_ratio"], 0.5)

    # ---- finding 5: all-unknown is_async must yield null ratio, not a definite 0.0 ----
    def test_async_unknown_ratio_is_null(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _write_jsonl(d / "delegations.jsonl", [
                {"project": "-p", "session_id": "s", "file": "/x/s.jsonl", "line": i, "tool": "Agent",
                 "subagent_type": None, "model_requested": None,
                 "resolved_model": {"provenance": "unknown"}, "status": {"provenance": "unknown"},
                 "is_async": {"provenance": "unknown"}} for i in (1, 2, 3)])
            facts = jw_improve.run_audit(d)
            dp = {l["lens"]: l for l in facts["lenses"]}["delegation_pattern"]
            self.assertEqual(dp["rule"], "delegation-pattern-v2")
            pp = dp["per_project"]["-p"]
            self.assertEqual(pp["async_count"], 0)
            self.assertEqual(pp["async_unknown"], 3)
            self.assertIsNone(pp["async_ratio"])  # NOT a fabricated 0.0

    # ---- finding 6: a triage row + its registered task are ONE finding, not two ----
    def test_review_dedup_single_finding(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            proj = d / "projA"
            proj.mkdir()
            (proj / ".jahns-workflow.yml").write_text("version: 1\nproject: a\n")
            rdir = proj / "docs" / "reviews"
            rdir.mkdir(parents=True)
            (rdir / "2026-07-01-x-feedback.md").write_text(
                "## Findings (triage skeleton — verify each)\n\n"
                "| finding | severity | verdict | evidence | task id |\n"
                "|---|---|---|---|---|\n"
                "| JW-GPT-001 — the one bug | blocker | REAL | confirmed | fix/the-bug |\n")
            (proj / "tasks.yaml").write_text(
                "version: 1\nproject: a\ntasks:\n"
                "  - id: fix/the-bug\n    title: 'fix'\n    status: pending\n"
                "    severity: major\n    origin: review-2026-07-01-x\n")
            registry = d / "projects.json"
            registry.write_text(_json.dumps({"projects": [{"name": "proj-a", "path": str(proj)}]}))
            out = d / "out"
            jw_improve.run_reviews(registry, out)
            rows = [_json.loads(ln) for ln in (out / "reviews.jsonl").read_text().splitlines() if ln]
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(len(r["findings"]), 1)                     # ONE finding, not two
            f = r["findings"][0]
            self.assertEqual(f["source"], "triage")
            self.assertEqual(f["severity"], "blocker")                  # triage severity kept
            self.assertEqual(f["task_id"], "fix/the-bug")
            self.assertEqual(r["counts"], {"blocker": 1, "major": 0, "minor": 0, "unknown": 0})

    # ---- finding 7: a relative --out/--in is refused (exit 1) for every subcommand ----
    def test_relative_out_in_refused(self):
        self.assertEqual(self._quiet(lambda: jw_improve.main(
            ["trace", "--source", "/tmp", "--out", "rel/out"])), 1)
        self.assertEqual(self._quiet(lambda: jw_improve.main(["reviews", "--out", "rel/out"])), 1)
        self.assertEqual(self._quiet(lambda: jw_improve.main(["audit", "--in", "rel/in"])), 1)
        self.assertEqual(self._quiet(lambda: jw_improve.main(
            ["decide", "lens/x", "accept", "--out", "rel/out"])), 1)

    # ---- finding 8: registry MISSING is soft (exit 0); EXISTING but corrupt fails loud ----
    def test_registry_fail_loud(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "out"
            cov = jw_improve.run_reviews(d / "nope.json", out)  # MISSING -> 0 projects, no raise
            self.assertEqual(cov["projects_total"], 0)
            bad = d / "bad.json"
            bad.write_text("{ not json ")
            with self.assertRaises(jw_common.WorkflowError):
                jw_improve.run_reviews(bad, out)                # unparseable -> fail loud
            wrong = d / "wrong.json"
            wrong.write_text("[1, 2, 3]")
            with self.assertRaises(jw_common.WorkflowError):
                jw_improve.run_reviews(wrong, out)              # wrong shape -> fail loud
            # exit-code contract via the CLI (corrupt registry under a fake HOME) -> rc 1, not 0
            home = d / "home"
            (home / ".claude" / "jahns-workflow").mkdir(parents=True)
            (home / ".claude" / "jahns-workflow" / "projects.json").write_text("{ nope ")
            rc = _run_with_home(home, lambda: self._quiet(
                lambda: jw_improve.main(["reviews", "--out", str(d / "o2")])))
            self.assertEqual(rc, 1)

    # ---- finding 1: an unreadable INPUT transcript is recorded, not fatal, exit 0 ----
    def test_unreadable_input_recorded_not_fatal(self):
        import os
        import stat
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "projects"
            good = src / "good-slug"
            good.mkdir(parents=True)
            _write_jsonl(good / f"{_UUID}.jsonl",
                         [{"type": "user", "uuid": "u", "message": {"role": "user", "content": "hi"}}])
            bad = src / "bad-slug"
            bad.mkdir(parents=True)
            badf = bad / f"{_UUID}.jsonl"
            _write_jsonl(badf, [{"type": "user", "uuid": "v",
                                 "message": {"role": "user", "content": "x"}}])
            os.chmod(badf, 0)
            out = d / "out"
            try:
                cov = jw_improve.run_trace([src], set(), out)
            finally:
                os.chmod(badf, stat.S_IRUSR | stat.S_IWUSR)
            self.assertEqual(cov["row_totals"]["sessions"], 1)      # good session still projected
            self.assertEqual(cov["files_unreadable_total"], 1)
            self.assertEqual(list(cov["files_unreadable"]), [f"bad-slug/{_UUID}.jsonl"])
            self.assertEqual((out / "sessions.jsonl").read_text().count("\n"), 1)

    # ---- finding 1 (other half): a real OUTPUT write failure stays exit 2 ----
    def test_unwritable_out_is_exit_2(self):
        import os
        import stat
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "projects"
            slug = src / "s"
            slug.mkdir(parents=True)
            _write_jsonl(slug / f"{_UUID}.jsonl",
                         [{"type": "user", "uuid": "u", "message": {"role": "user", "content": "hi"}}])
            locked = d / "locked"
            locked.mkdir()
            os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)  # no write bit
            try:
                rc = self._quiet(lambda: jw_improve.main(
                    ["trace", "--source", str(src), "--out", str(locked / "sub")]))
            finally:
                os.chmod(locked, stat.S_IRWXU)
            self.assertEqual(rc, 2)

    # ---- finding 12: explicit non-dir --source exits 1; a missing source is soft in run_trace ----
    def test_explicit_missing_source_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rc = self._quiet(lambda: jw_improve.main(
                ["trace", "--source", str(d / "does-not-exist"), "--out", str(d / "out")]))
            self.assertEqual(rc, 1)

    def test_missing_source_recorded_soft(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            missing = d / "gone"
            cov = jw_improve.run_trace([missing], set(), d / "out")
            self.assertEqual(cov["sources_missing"], [str(missing)])
            self.assertEqual(cov["row_totals"]["sessions"], 0)

    # ---- finding 4 (end-to-end): a >100KiB-UTF-8 CJK tool_result counts as context_heavy ----
    def test_context_heavy_counts_utf8_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "projects"
            slug = src / "-p"
            slug.mkdir(parents=True)
            big = "가" * 60000  # ~180KB UTF-8, only 60k code points
            _write_jsonl(slug / f"{_UUID}.jsonl", [
                {"type": "user", "uuid": "u", "message": {"role": "user", "content": "hi"}},
                {"type": "assistant", "uuid": "a", "requestId": "r",
                 "message": {"id": "m", "model": "claude-opus-4-8",
                             "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                          "input": {"command": "echo hi"}}]}},
                {"type": "user", "uuid": "t", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": big,
                     "is_error": False}]}},
            ])
            out = d / "out"
            jw_improve.run_trace([src], set(), out)
            sess = [_json.loads(ln) for ln in
                    (out / "sessions.jsonl").read_text().splitlines() if ln][0]
            ch = sess["context_heavy"]
            self.assertEqual(ch["tool_results_over_100kb"], 1)  # 180KB bytes > 100KiB
            self.assertEqual(ch["max_result_bytes"], len(big.encode("utf-8")))


class AcceptFieldTests(unittest.TestCase):
    """0.8.0 M1: the optional task `accept` field (YAML list of acceptance criteria) — validated by
    jw_validate, but NOT settable via `jw task add/set` (comma-split would distort free text)."""

    def test_validate_accepts_string_list(self):
        data = {"version": 1, "project": "x", "tasks": [
            {"id": "feat/alpha", "title": "a valid task here", "status": "active",
             "accept": ["uv run pytest passes", "no new ruff findings"]}]}
        self.assertEqual(jw_validate.validate(data), [])

    def test_validate_rejects_non_list_accept(self):
        data = {"version": 1, "project": "x", "tasks": [
            {"id": "feat/alpha", "title": "a valid task here", "status": "active",
             "accept": "just a string"}]}
        errs = jw_validate.validate(data)
        self.assertTrue(any("accept" in e for e in errs))

    def test_validate_rejects_non_str_element(self):
        data = {"version": 1, "project": "x", "tasks": [
            {"id": "feat/alpha", "title": "a valid task here", "status": "active",
             "accept": ["ok", 42]}]}
        errs = jw_validate.validate(data)
        self.assertTrue(any("accept" in e for e in errs))

    def test_task_add_rejects_accept_flag(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            before = (root / "tasks.yaml").read_text()
            rc = jw_tasks.main(["add", "feat/new", str(root), "--title",
                                "a fresh task here", "--accept", "some criterion"])
            self.assertEqual(rc, 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)  # nothing written

    def test_task_set_rejects_accept_field(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            before = (root / "tasks.yaml").read_text()
            rc = jw_tasks.main(["set", "feat/alpha", "accept", "some criterion", str(root)])
            self.assertEqual(rc, 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)


class DelegateSnapshotTests(unittest.TestCase):
    """0.8.0 M1 §3 — snapshot primitive (temp-index read-tree-HEAD seeding). Real temp git repos."""

    def _repo(self, d) -> Path:
        root = Path(d)
        init_repo(root)
        return root

    def test_clean_tree_shortcut(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            head = git(root, "rev-parse", "HEAD").stdout.strip()
            before = git(root, "rev-list", "--count", "HEAD").stdout.strip()
            sha, dirty = jw_delegate._snapshot(root, "snap")
            self.assertFalse(dirty)
            self.assertEqual(sha, head)  # no snapshot commit created
            self.assertEqual(git(root, "rev-list", "--count", "HEAD").stdout.strip(), before)

    def test_dirty_includes_untracked_and_staged_excludes_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / "f.txt").write_text("MODIFIED")           # tracked modification
            (root / "new_untracked.txt").write_text("wip")    # untracked, non-ignored
            (root / ".gitignore").write_text("secret.txt\n")
            (root / "secret.txt").write_text("nope")          # ignored
            (root / "staged.txt").write_text("stg")
            git(root, "add", "staged.txt")                    # staged addition
            sha, dirty = jw_delegate._snapshot(root, "snap")
            self.assertTrue(dirty)
            tree = git(root, "ls-tree", "-r", "--name-only", sha).stdout.split()
            self.assertIn("new_untracked.txt", tree)
            self.assertIn("staged.txt", tree)
            self.assertNotIn("secret.txt", tree)
            self.assertEqual(git(root, "show", f"{sha}:f.txt").stdout, "MODIFIED")

    def test_live_tree_and_index_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / "f.txt").write_text("MODIFIED")
            (root / "new.txt").write_text("wip")
            git(root, "add", "new.txt")
            status_before = git(root, "status", "--porcelain").stdout
            head_before = git(root, "rev-parse", "HEAD").stdout
            jw_delegate._snapshot(root, "snap")
            self.assertEqual(git(root, "status", "--porcelain").stdout, status_before)
            self.assertEqual(git(root, "rev-parse", "HEAD").stdout, head_before)

    def test_precondition_unborn_head(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            git(root, "init", "-q", "-b", "main")
            with self.assertRaises(jw_delegate.WorkflowError):
                jw_delegate._check_snapshot_preconditions(root)

    def test_precondition_submodule(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / ".gitmodules").write_text("[submodule \"x\"]\n")
            with self.assertRaises(jw_delegate.WorkflowError):
                jw_delegate._check_snapshot_preconditions(root)

    def test_precondition_unmerged_index(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            git(root, "checkout", "-q", "-b", "other")
            (root / "f.txt").write_text("other-side")
            git(root, "commit", "-qam", "other")
            git(root, "checkout", "-q", "main")
            (root / "f.txt").write_text("main-side")
            git(root, "commit", "-qam", "main")
            git(root, "merge", "other")  # conflicts -> unmerged entries
            self.assertTrue(git(root, "ls-files", "-u").stdout.strip())
            with self.assertRaises(jw_delegate.WorkflowError):
                jw_delegate._check_snapshot_preconditions(root)

    def test_precondition_rebase_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / ".git" / "rebase-merge").mkdir()
            with self.assertRaises(jw_delegate.WorkflowError):
                jw_delegate._check_snapshot_preconditions(root)

    def test_precondition_cherry_pick_head(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / ".git" / "CHERRY_PICK_HEAD").write_text("deadbeef\n")
            with self.assertRaises(jw_delegate.WorkflowError):
                jw_delegate._check_snapshot_preconditions(root)

    def test_preconditions_pass_on_clean_repo(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            jw_delegate._check_snapshot_preconditions(root)  # no raise

    def test_precondition_reserved_report_filename(self):
        # H2: a pre-existing JW_REPORT.yaml would be baked into the base, then consumed as the
        # delegate's report and phantom-deleted by the patch — refuse up front.
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / "JW_REPORT.yaml").write_text("stale: report\n")
            with self.assertRaises(jw_delegate.WorkflowError) as cm:
                jw_delegate._check_snapshot_preconditions(root)
            self.assertIn("reserved", str(cm.exception))

    def test_make_did_shape(self):
        did = jw_delegate._make_did("feat/xyz")
        self.assertRegex(did, r"^\d{8}T\d{6}Z-feat-xyz$")


_PROFILE_BODY = ('schema: jw-profile-1\nbindings:\n'
                 '  implementer: {execution: external-runner, backend: "codex:gpt-5.4-codex"}\n')


def _write_profile(home: Path, body: str = _PROFILE_BODY):
    pdir = home / ".claude" / "jahns-workflow"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "profile.yml").write_text(body, encoding="utf-8")


class DelegateProfileTests(unittest.TestCase):
    """0.8.0 M1 §11 — profile binding resolution (fail-loud, no default-model guessing)."""

    def test_missing_profile_raises(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            with self.assertRaises(jw_delegate.WorkflowError):
                _run_with_home(home, jw_delegate._load_profile)

    def test_resolve_binding_ok_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            _write_profile(home)
            profile, fp = _run_with_home(home, jw_delegate._load_profile)
            self.assertTrue(fp.startswith("sha256:"))
            b = jw_delegate._resolve_binding(profile, "implementer")
            self.assertEqual(b["backend"], "codex:gpt-5.4-codex")
            self.assertEqual(b["execution"], "external-runner")
            self.assertEqual(b["source"], "profile")

    def test_missing_role_binding_raises(self):
        profile = yaml.safe_load(_PROFILE_BODY)
        with self.assertRaises(jw_delegate.WorkflowError):
            jw_delegate._resolve_binding(profile, "verifier")

    def test_unsupported_execution_raises(self):
        profile = {"bindings": {"implementer": {"execution": "in-process", "backend": "codex:x"}}}
        with self.assertRaises(jw_delegate.WorkflowError):
            jw_delegate._resolve_binding(profile, "implementer")

    def test_bad_backend_format_raises(self):
        profile = {"bindings": {"implementer": {"execution": "external-runner", "backend": "codexonly"}}}
        with self.assertRaises(jw_delegate.WorkflowError):
            jw_delegate._resolve_binding(profile, "implementer")

    def test_non_codex_backend_not_implemented(self):
        with self.assertRaises(jw_delegate.WorkflowError):
            jw_delegate._runner_model("claude:sonnet")
        self.assertEqual(jw_delegate._runner_model("codex:gpt-5.4-codex"), "gpt-5.4-codex")


def _packet_registry():
    return {"project": "demo", "tasks": [
        {"id": "feat/xyz", "title": "implement the xyz feature", "status": "active",
         "milestone": None, "deps": ["feat/dep"], "anchor": "SSOT §2", "notes": "do the thing",
         "accept": ["registry criterion one"]},
        {"id": "feat/dep", "title": "a dependency task", "status": "done"},
        {"id": "feat/blk", "title": "a blocked task here", "status": "blocked"},
        {"id": "feat/dn", "title": "an already done task", "status": "done"},
    ]}


class DelegatePacketTests(unittest.TestCase):
    """0.8.0 M1 §7 — task packet assembly + acceptance merge (fail-loud on empty)."""

    def test_packet_merges_accept_and_flags_dedup_order(self):
        data = _packet_registry()
        packet, acceptance = jw_delegate._build_packet(data, "feat/xyz",
                                                       ["flag criterion", "registry criterion one"], Path("/x"))
        self.assertEqual(packet["schema"], "jw-packet-1")
        self.assertEqual(acceptance, ["registry criterion one", "flag criterion"])  # order + dedup
        self.assertEqual(packet["task"]["deps"], [{"id": "feat/dep", "status": "done"}])
        self.assertEqual(packet["project"]["name"], "demo")

    def test_empty_acceptance_raises(self):
        data = {"project": "d", "tasks": [{"id": "feat/na", "title": "no acceptance here", "status": "active"}]}
        with self.assertRaises(jw_delegate.WorkflowError) as cm:
            jw_delegate._build_packet(data, "feat/na", [], Path("/x"))
        self.assertIn("no acceptance criteria", str(cm.exception))

    def test_blocked_task_message(self):
        with self.assertRaises(jw_delegate.WorkflowError) as cm:
            jw_delegate._build_packet(_packet_registry(), "feat/blk", ["c"], Path("/x"))
        msg = str(cm.exception)
        self.assertIn("blocked", msg)
        # R10: must NOT assert deps are unmet (stale-blocked exists) — offer the conditional path
        self.assertIn("if its deps are now satisfied", msg)
        self.assertNotIn("unmet", msg.lower())

    def test_done_task_rejected(self):
        with self.assertRaises(jw_delegate.WorkflowError):
            jw_delegate._build_packet(_packet_registry(), "feat/dn", ["c"], Path("/x"))

    def test_unknown_task_rejected(self):
        with self.assertRaises(jw_delegate.WorkflowError):
            jw_delegate._build_packet(_packet_registry(), "feat/nope", ["c"], Path("/x"))


class DelegateRunTests(unittest.TestCase):
    """0.8.0 M1 §§4-10 — full run flow with a fake (monkeypatched) codex runner. Never invokes codex."""

    def _project(self, d) -> tuple[Path, Path]:
        root = Path(d) / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".jahns-workflow.yml").write_text("version: 1\nproject: demo\n")
        (root / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks:\n"
            '  - id: feat/xyz\n    title: "implement xyz feature"\n    status: active\n'
            '    accept:\n      - "criterion alpha here"\n')
        git(root, "add", "-A")
        git(root, "commit", "-qm", "setup")
        home = Path(d) / "home"
        _write_profile(home)
        return root, home

    def _fake_runner(self, changes, report=None, rc=0):
        def fake(worktree, model, prompt_path, record_dir):
            for name, content in changes.items():
                (worktree / name).write_text(content)
            (record_dir / "last_message.md").write_text("delegate summary", encoding="utf-8")
            (record_dir / "runner.jsonl").write_text("{}\n", encoding="utf-8")
            if report is not None:
                (worktree / "JW_REPORT.yaml").write_text(report, encoding="utf-8")
            return (rc, 0.42)
        return fake

    def _run(self, root, home, fake, task="feat/xyz", accept=None):
        orig = jw_delegate._run_codex
        jw_delegate._run_codex = fake
        try:
            return _run_with_home(home, lambda: jw_delegate.run_delegation(root, task, "implementer", accept or []))
        finally:
            jw_delegate._run_codex = orig

    def _record_dir(self, root, home):
        return _run_with_home(home, lambda: sorted(jw_delegate._delegations_dir(root).iterdir())[-1])

    def test_success_path_contract_and_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            report = ("verification:\n  - {cmd: \"pytest\", rc: 0, summary: \"passed\"}\n"
                      "limitations: [\"none\"]\nrisks: []\nescalations: []\n")
            fake = self._fake_runner({"impl.py": "print('hi')\n"}, report=report)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self._run(root, home, fake)
            self.assertEqual(rc, 0)
            rec = self._record_dir(root, home)
            self.assertEqual(jw_delegate._read_status(rec)["state"], "needs-review")
            # prompt carries acceptance criterion text
            self.assertIn("criterion alpha here", (rec / "prompt.txt").read_text())
            # JW_REPORT consumed from the worktree (not left to pollute the patch)
            wt = _run_with_home(home, lambda: jw_delegate._worktree_path(root, rec.name))
            self.assertFalse((wt / "JW_REPORT.yaml").exists())
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["schema"], "jw-artifact-1")
            self.assertFalse(contract["empty"])
            self.assertEqual([c["path"] for c in contract["changed_files"]], ["impl.py"])
            self.assertEqual(contract["changed_files"][0]["status"], "A")
            self.assertEqual(contract["delegate_report"]["present"], True)
            self.assertEqual(contract["delegate_report"]["verification"][0]["rc"], 0)
            self.assertEqual(contract["runner"]["backend"], "codex:gpt-5.4-codex")
            self.assertTrue((rec / "artifact" / "changes.patch").exists())
            # exposure immutable fields
            import json as _json
            exp = _json.loads((rec / "exposure.json").read_text())
            self.assertEqual(exp["schema"], "jw-exposure-1")
            self.assertEqual(exp["sandbox"], "workspace-write")
            self.assertEqual(exp["binding"]["backend"], "codex:gpt-5.4-codex")
            self.assertEqual(exp["overlays"], [])
            # result ref exists
            self.assertTrue(git(root, "rev-parse", "--verify",
                                f"refs/jw/delegations/{rec.name}-result").returncode == 0)

    def test_missing_report_marked_absent(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({"impl.py": "x\n"}, report=None)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self._run(root, home, fake)
            rec = self._record_dir(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["delegate_report"]["present"], False)

    def test_empty_diff_marks_empty(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({}, report=None)  # no changes
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self._run(root, home, fake)
            rec = self._record_dir(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertTrue(contract["empty"])
            self.assertFalse((rec / "artifact" / "changes.patch").exists())
            self.assertEqual(jw_delegate._read_status(rec)["state"], "needs-review")

    def test_binary_change_preserved_in_patch(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def fake(worktree, model, prompt_path, record_dir):
                (worktree / "blob.bin").write_bytes(bytes(range(256)))
                (record_dir / "last_message.md").write_text("x")
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self._run(root, home, fake)
            rec = self._record_dir(root, home)
            patch = (rec / "artifact" / "changes.patch").read_text()
            self.assertIn("GIT binary patch", patch)

    def test_env_prep_failure_is_failed_env_no_runner(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            (root / ".jahns-workflow.yml").write_text(
                "version: 1\nproject: demo\ndelegation:\n  env_prep:\n    - \"false\"\n")
            called = {"n": 0}

            def fake(*a, **k):
                called["n"] += 1
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(jw_delegate.WorkflowError):
                    self._run(root, home, fake)
            self.assertEqual(called["n"], 0)  # runner never invoked
            rec = self._record_dir(root, home)
            self.assertEqual(jw_delegate._read_status(rec)["state"], "failed-env")

    def test_runner_failure_is_failed_runner_with_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({"impl.py": "x\n"}, rc=3)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(jw_delegate.WorkflowError):
                    self._run(root, home, fake)
            rec = self._record_dir(root, home)
            self.assertEqual(jw_delegate._read_status(rec)["state"], "failed-runner")
            self.assertTrue((rec / "exposure.json").exists())  # exposure recorded before runner

    def test_run_refuses_preexisting_jw_report(self):
        # H2 repro: an untracked JW_REPORT.yaml in the user's tree must refuse the run entirely —
        # before any record is created (no phantom deletion via the patch).
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            (root / "JW_REPORT.yaml").write_text("stale: report\n")  # untracked user file
            with self.assertRaises(jw_delegate.WorkflowError) as cm:
                self._run(root, home, self._fake_runner({"impl.py": "x\n"}))
            self.assertIn("JW_REPORT.yaml", str(cm.exception))
            self.assertFalse(_run_with_home(home, lambda: jw_delegate._delegations_dir(root)).exists())

    def test_non_utf8_text_change_roundtrip(self):
        # H1 repro: latin-1 content (0xE9, no NUL -> git classifies it as text) must not crash the
        # harness — the patch is bytes and must never round-trip through strict UTF-8.
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def fake(worktree, model, prompt_path, record_dir):
                (worktree / "cafe.txt").write_bytes(b"caf\xe9 au lait\n")
                (record_dir / "last_message.md").write_text("x")
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self._run(root, home, fake)
            self.assertEqual(rc, 0)
            rec = self._record_dir(root, home)
            self.assertEqual(jw_delegate._read_status(rec)["state"], "needs-review")
            self.assertTrue((rec / "artifact" / "contract.yaml").exists())
            patch = (rec / "artifact" / "changes.patch").read_bytes()
            self.assertIn(b"caf\xe9 au lait", patch)  # original bytes preserved, not mangled

    def test_non_utf8_report_marked_invalid(self):
        # H1: a non-UTF-8 JW_REPORT.yaml must surface as delegate_report invalid, not crash the run
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def fake(worktree, model, prompt_path, record_dir):
                (worktree / "impl.py").write_text("x\n")
                (worktree / "JW_REPORT.yaml").write_bytes(b"verification: caf\xe9\n")
                (record_dir / "last_message.md").write_text("x")
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self._run(root, home, fake)
            self.assertEqual(rc, 0)
            rec = self._record_dir(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["delegate_report"]["present"], "invalid")

    def test_artifact_failure_is_failed_artifact_lock_held(self):
        # H1: a post-runner artifact-computation failure must not strand the record as `running`
        # (permanent owner-lock) — it transitions to failed-artifact, preserves the worktree as
        # evidence, keeps the lock, and discard clears it.
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def fake(worktree, model, prompt_path, record_dir):
                (record_dir / "last_message.md").write_text("x")
                (worktree / ".git").unlink()  # breaks the result snapshot
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(jw_delegate.WorkflowError):
                    self._run(root, home, fake)
            rec = self._record_dir(root, home)
            self.assertEqual(jw_delegate._read_status(rec)["state"], "failed-artifact")
            wt = _run_with_home(home, lambda: jw_delegate._worktree_path(root, rec.name))
            self.assertTrue(wt.exists())  # evidence preserved
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(jw_delegate.WorkflowError) as cm:
                    self._run(root, home, self._fake_runner({"impl.py": "y\n"}))
                self.assertIn("already has active delegation", str(cm.exception))
                rc = _run_with_home(home, lambda: jw_delegate.discard_delegation(root, rec.name))
            self.assertEqual(rc, 0)

    def test_same_second_did_gets_suffix(self):
        # H4: two delegations minted in the same second must land in two independent records —
        # deterministic -2/-3... suffix, no state transitions appended to the first record.
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks:\n"
                '  - id: feat/xyz\n    title: "implement xyz feature"\n    status: active\n'
                '    accept:\n      - "criterion alpha here"\n'
                '  - id: feat/two\n    title: "the second task here"\n    status: active\n'
                '    accept:\n      - "criterion beta here"\n')
            orig = jw_delegate._make_did
            jw_delegate._make_did = lambda tid: "20260713T120000Z-fixed"
            import contextlib
            import io
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc1 = self._run(root, home, self._fake_runner({"a.py": "x\n"}), task="feat/xyz")
                    rc2 = self._run(root, home, self._fake_runner({"b.py": "y\n"}), task="feat/two")
            finally:
                jw_delegate._make_did = orig
            self.assertEqual((rc1, rc2), (0, 0))
            ddir = _run_with_home(home, lambda: jw_delegate._delegations_dir(root))
            names = sorted(p.name for p in ddir.iterdir())
            self.assertEqual(names, ["20260713T120000Z-fixed", "20260713T120000Z-fixed-2"])
            st1 = jw_delegate._read_status(ddir / names[0])
            self.assertEqual(st1["state"], "needs-review")
            self.assertEqual(len(st1["at_transitions"]), 2)  # running -> needs-review only, untouched
            import json as _json
            exp2 = _json.loads((ddir / names[1] / "exposure.json").read_text())
            self.assertEqual(exp2["task_id"], "feat/two")

    def test_owner_lock_refuses_second_delegation_from_failed_state(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake_fail = self._fake_runner({"impl.py": "x\n"}, rc=3)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(jw_delegate.WorkflowError):
                    self._run(root, home, fake_fail)  # -> failed-runner (non-terminal, holds lock)
                with self.assertRaises(jw_delegate.WorkflowError) as cm:
                    self._run(root, home, self._fake_runner({"impl.py": "y\n"}))
            self.assertIn("already has active delegation", str(cm.exception))


def _deleg_project(d) -> tuple[Path, Path]:
    root = Path(d) / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".jahns-workflow.yml").write_text("version: 1\nproject: demo\n")
    (root / "tasks.yaml").write_text(
        "version: 1\nproject: demo\ntasks:\n"
        '  - id: feat/xyz\n    title: "implement xyz feature"\n    status: active\n'
        '    accept:\n      - "criterion alpha here"\n')
    git(root, "add", "-A")
    git(root, "commit", "-qm", "setup")
    home = Path(d) / "home"
    _write_profile(home)
    return root, home


def _deleg_fake(changes, report=None, rc=0):
    def fake(worktree, model, prompt_path, record_dir):
        for name, content in changes.items():
            (worktree / name).write_text(content)
        (record_dir / "last_message.md").write_text("s", encoding="utf-8")
        if report is not None:
            (worktree / "JW_REPORT.yaml").write_text(report, encoding="utf-8")
        return (rc, 0.1)
    return fake


def _deleg_run(root, home, fake, task="feat/xyz", accept=None):
    import contextlib
    import io
    orig = jw_delegate._run_codex
    jw_delegate._run_codex = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return _run_with_home(
                home, lambda: jw_delegate.run_delegation(root, task, "implementer", accept or []))
    finally:
        jw_delegate._run_codex = orig


def _latest_rec(root, home):
    return _run_with_home(home, lambda: sorted(jw_delegate._delegations_dir(root).iterdir())[-1])


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
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: jw_delegate.apply_delegation(root, rec.name))
            self.assertEqual(rc, 0)
            self.assertEqual((root / "cafe.txt").read_bytes(), b"caf\xe9 au lait\n")

    def test_apply_success_and_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "print('x')\n"}))
            rec = _latest_rec(root, home)
            wt = _run_with_home(home, lambda: jw_delegate._worktree_path(root, rec.name))
            rc = _run_with_home(home, lambda: jw_delegate.apply_delegation(root, rec.name))
            self.assertEqual(rc, 0)
            self.assertTrue((root / "impl.py").exists())                     # patch landed on live tree
            self.assertEqual(jw_delegate._read_status(rec)["state"], "applied")
            self.assertFalse(wt.exists())                                    # worktree removed
            self.assertTrue((rec / "artifact" / "contract.yaml").exists())   # record preserved
            self.assertNotEqual(git(root, "rev-parse", "--verify",
                                    f"refs/jw/delegations/{rec.name}").returncode, 0)  # ref gone

    def test_apply_drift_is_atomic_exit1(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"a.py": "AAA\n", "b.py": "BBB\n"}))
            rec = _latest_rec(root, home)
            (root / "a.py").write_text("conflicting live content\n")  # drift on a patch target
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: jw_delegate.main(["apply", rec.name, "--root", str(root)]))
            self.assertEqual(rc, 1)                                   # not a raw git rc
            self.assertIn("drifted", err.getvalue())
            self.assertFalse((root / "b.py").exists())               # atomic: other target untouched
            self.assertEqual(jw_delegate._read_status(rec)["state"], "needs-review")  # unchanged

    def test_apply_unrelated_dirty_ok(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            (root / "f.txt").write_text("locally dirtied but unrelated")
            rc = _run_with_home(home, lambda: jw_delegate.apply_delegation(root, rec.name))
            self.assertEqual(rc, 0)
            self.assertTrue((root / "impl.py").exists())

    def test_apply_empty_patch_noop(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({}))  # no changes
            rec = _latest_rec(root, home)
            rc = _run_with_home(home, lambda: jw_delegate.apply_delegation(root, rec.name))
            self.assertEqual(rc, 0)
            self.assertEqual(jw_delegate._read_status(rec)["state"], "applied")

    def test_reapply_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            _run_with_home(home, lambda: jw_delegate.apply_delegation(root, rec.name))
            with self.assertRaises(jw_delegate.WorkflowError):
                _run_with_home(home, lambda: jw_delegate.apply_delegation(root, rec.name))

    def test_discard_cleanup_and_accepts_running(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            wt = _run_with_home(home, lambda: jw_delegate._worktree_path(root, rec.name))
            jw_delegate._set_state(rec, "running")  # simulate a crash remnant (R1)
            rc = _run_with_home(home, lambda: jw_delegate.discard_delegation(root, rec.name))
            self.assertEqual(rc, 0)
            self.assertEqual(jw_delegate._read_status(rec)["state"], "discarded")
            self.assertFalse(wt.exists())
            self.assertTrue((rec / "exposure.json").exists())  # record preserved


class DelegateCorruptRecordTests(unittest.TestCase):
    """H3 — corrupt record JSON fails safe (named file, lock held, list survives), never a traceback."""

    def test_owner_lock_scan_fail_safe_on_corrupt_status(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            (rec / "status.json").write_text("{ corrupt", encoding="utf-8")
            with self.assertRaises(jw_delegate.WorkflowError) as cm:
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
            orig = jw_delegate._make_did
            try:
                jw_delegate._make_did = lambda tid: "20260713T000001Z-" + tid.replace("/", "-")
                _deleg_run(root, home, _deleg_fake({"a.py": "x\n"}), task="feat/xyz")
                jw_delegate._make_did = lambda tid: "20260713T000002Z-" + tid.replace("/", "-")
                _deleg_run(root, home, _deleg_fake({"b.py": "y\n"}), task="feat/two")
            finally:
                jw_delegate._make_did = orig
            recs = _run_with_home(home, lambda: sorted(jw_delegate._delegations_dir(root).iterdir()))
            (recs[0] / "status.json").write_text("{ corrupt", encoding="utf-8")
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = _run_with_home(home, lambda: jw_delegate.main(["status", "--root", str(root)]))
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
                rc = _run_with_home(home, lambda: jw_delegate.main(["show", rec.name, "--root", str(root)]))
            self.assertEqual(rc, 1)                  # WorkflowError, never a traceback
            self.assertIn("status.json", err.getvalue())

    def test_apply_corrupt_contract_exit1(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            (rec / "artifact" / "contract.yaml").write_text("{invalid: [unclosed", encoding="utf-8")
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: jw_delegate.main(["apply", rec.name, "--root", str(root)]))
            self.assertEqual(rc, 1)
            self.assertIn("contract.yaml", err.getvalue())
            self.assertEqual(jw_delegate._read_status(rec)["state"], "needs-review")  # unchanged

    def test_discard_accepts_corrupt_record(self):
        # the cleanup path must not block itself on the very corruption it is meant to clear
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))
            rec = _latest_rec(root, home)
            wt = _run_with_home(home, lambda: jw_delegate._worktree_path(root, rec.name))
            (rec / "status.json").write_text("{ corrupt", encoding="utf-8")
            (rec / "exposure.json").write_text("{ corrupt", encoding="utf-8")
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: jw_delegate.discard_delegation(root, rec.name))
            self.assertEqual(rc, 0)
            self.assertEqual(jw_delegate._read_status(rec)["state"], "discarded")
            self.assertFalse(wt.exists())


class DelegateCliTests(unittest.TestCase):
    """0.8.0 M1 §2 — arg parsing, exit codes, status/show surfaces (incl. R11 no-artifact refusal)."""

    def test_run_via_main_and_status_list(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            import contextlib
            import io
            orig = jw_delegate._run_codex
            jw_delegate._run_codex = _deleg_fake({"impl.py": "x\n"})
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = _run_with_home(home, lambda: jw_delegate.main(
                        ["run", "feat/xyz", "--root", str(root), "--accept", "extra criterion"]))
                self.assertEqual(rc, 0)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    _run_with_home(home, lambda: jw_delegate.main(["status", "--root", str(root)]))
            finally:
                jw_delegate._run_codex = orig
            self.assertIn("feat/xyz", out.getvalue())
            self.assertIn("needs-review", out.getvalue())

    def test_unknown_subcommand(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(jw_delegate.main(["frobnicate"]), 1)

    def test_jw_dispatcher_routes_delegate(self):
        import jw
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: jw.main(["delegate", "status", "--root", str(root)]))
            self.assertEqual(rc, 0)

    def test_unknown_delegation_id(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: jw_delegate.main(
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
            for opt, needle in (("--patch", "hello"), ("--report", "jw-artifact-1"),
                                ("--exposure", "jw-exposure-1")):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = _run_with_home(home, lambda o=opt: jw_delegate.main(
                        ["show", rec.name, o, "--root", str(root)]))
                self.assertEqual(rc, 0)
                self.assertIn(needle, buf.getvalue())

    def test_show_patch_report_refused_when_no_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            (root / ".jahns-workflow.yml").write_text(
                "version: 1\nproject: demo\ndelegation:\n  env_prep:\n    - \"false\"\n")
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(jw_delegate.WorkflowError):
                    _deleg_run(root, home, _deleg_fake({"impl.py": "x\n"}))  # -> failed-env
            rec = _latest_rec(root, home)
            self.assertEqual(jw_delegate._read_status(rec)["state"], "failed-env")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_run_with_home(home, lambda: jw_delegate.main(
                    ["show", rec.name, "--patch", "--root", str(root)])), 1)
                self.assertEqual(_run_with_home(home, lambda: jw_delegate.main(
                    ["show", rec.name, "--report", "--root", str(root)])), 1)
            # exposure + summary always available (recorded at start)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(_run_with_home(home, lambda: jw_delegate.main(
                    ["show", rec.name, "--exposure", "--root", str(root)])), 0)
                self.assertEqual(_run_with_home(home, lambda: jw_delegate.main(
                    ["show", rec.name, "--root", str(root)])), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
