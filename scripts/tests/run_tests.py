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

import jw_bundle  # noqa: E402
import jw_common  # noqa: E402
import jw_lanes  # noqa: E402
import jw_merge  # noqa: E402
import jw_resume  # noqa: E402
import jw_review  # noqa: E402
import jw_round  # noqa: E402
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
            # verbatim body sits between the header separator and the appended structured section
            self.assertIn(body + b"\n\n---\n\n## Bundle identity check", content)
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


class BundleTests(unittest.TestCase):
    """v0.3.0 review-bundle kernel: deterministic git-archive packaging, manifest schema validation,
    base/head resolution from the round sidecar, push precondition, and reviewer-kit rendering."""

    def _quiet(self):
        import contextlib
        import io
        return contextlib.redirect_stdout(io.StringIO())

    def test_manifest_validation(self):
        cfg = {"ssot": None, "progress": "PROGRESS.md", "adr_dir": "docs/adr",
               "reviews_dir": "docs/reviews", "generated_dir": "docs/ssot"}
        ident = {"project": "demo", "round_id": "2026-06-23-r", "review_mode": "packet",
                 "review_cycle": None, "branch": "main", "base_sha": "b" * 40,
                 "head_sha": "a" * 40, "repo": None}
        good = jw_bundle.build_manifest(cfg, ident, has_checks=False, worktree_dirty=False, omissions=[])
        good["scope"] = {"tasks": [], "ssot_anchors": [], "changed_file_count": 2}
        self.assertEqual(jw_bundle.validate_manifest(good), [])
        bad_sha = {**good, "head_sha": "nothex"}
        self.assertTrue(any("head_sha" in e for e in jw_bundle.validate_manifest(bad_sha)))
        pr_no_cycle = {**good, "review_mode": "pr", "review_cycle": None}
        self.assertTrue(any("review_cycle" in e for e in jw_bundle.validate_manifest(pr_no_cycle)))
        no_scope = {**good, "scope": {"tasks": []}}  # missing changed_file_count
        self.assertTrue(any("changed_file_count" in e for e in jw_bundle.validate_manifest(no_scope)))

    def _packet_repo(self, d: Path, round_id="2026-06-23-r"):
        """A pushed two-commit repo with workflow files COMMITTED into the reviewed head (so repo/
        and the manifest scope come from one tree) + an on-disk request + a packet sidecar (head is
        stamped by bundle, not here). Returns (work, base, head)."""
        bare = d / "r.git"
        work = d / "w"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)])
        work.mkdir()
        init_repo(work)  # commit c0 = base
        base = git(work, "rev-parse", "HEAD").stdout.strip()
        (work / ".jahns-workflow.yml").write_text(
            "version: 1\nproject: demo\nreviews_dir: docs/reviews\nreview:\n  mode: packet\n")
        (work / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks:\n"
            f"- id: feat/x-thing\n  title: the x thing that does y\n  status: done\n"
            f"  round: {round_id}\n  anchor: '§1'\n")
        (work / "g.txt").write_text("new feature line\n")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "c1")  # the reviewed head: workflow files committed
        head = git(work, "rev-parse", "HEAD").stdout.strip()
        git(work, "remote", "add", "origin", str(bare))
        git(work, "push", "-q", "-u", "origin", "main")
        rdir = work / "docs/reviews"
        rdir.mkdir(parents=True)
        (rdir / f"{round_id}-request.md").write_text("# Review request\n\nfalsifiable claims here.\n")
        rec = {"schema": jw_bundle.RECORD_SCHEMA, "project": "demo", "round_id": round_id,
               "review_mode": "packet", "review_cycle": None, "branch": "main",
               "base_sha": base, "round_commit": head, "head_sha": None}  # head stamped by bundle
        (rdir / f"{round_id}-bundle.yaml").write_text(yaml.safe_dump(rec))
        return work, base, head

    def test_packet_bundle_end_to_end(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            work, base, head = self._packet_repo(d)
            with self._quiet():
                rc = jw_bundle.bundle(work, "2026-06-23-r", None, out_dir=d / "out")
            self.assertEqual(rc, 0)
            zips = list((d / "out").glob("*.review.zip"))
            self.assertEqual(len(zips), 1)
            self.assertIn(head[:12], zips[0].name)  # filename short-SHA cross-check
            ex = d / "ex"
            ex.mkdir()
            with zipfile.ZipFile(zips[0]) as zf:
                zf.extractall(ex)
            # repo/ is the git-archive tree of head (tracked files only — no .git)
            self.assertTrue((ex / "repo" / "f.txt").is_file())
            self.assertTrue((ex / "repo" / "g.txt").is_file())
            self.assertFalse((ex / "repo" / ".git").exists())
            man = yaml.safe_load((ex / "__review__" / "MANIFEST.yaml").read_text())
            self.assertEqual(man["schema"], jw_bundle.BUNDLE_SCHEMA)
            self.assertEqual(man["head_sha"], head)
            self.assertEqual(man["base_sha"], base)        # base = previous watermark
            self.assertEqual(man["review_mode"], "packet")
            self.assertEqual(man["comparison"], f"{base}..{head}")
            self.assertIn("feat/x-thing", man["scope"]["tasks"])
            self.assertIn("§1", man["scope"]["ssot_anchors"])
            self.assertEqual(jw_bundle.validate_manifest(man), [])
            self.assertIn("g.txt", (ex / "__review__" / "CHANGED_FILES.txt").read_text())
            self.assertIn("Review request", (ex / "__review__" / "REQUEST.md").read_text())
            # scope and repo/tasks.yaml come from ONE tree (the committed head) — no provenance split
            self.assertIn("feat/x-thing", (ex / "repo" / "tasks.yaml").read_text())
            # bundle stamps the actual reviewed head into the sidecar (for the ingest cross-check)
            rec = yaml.safe_load((work / "docs/reviews/2026-06-23-r-bundle.yaml").read_text())
            self.assertEqual(rec["head_sha"], head)

    def test_symlink_stored_as_target_not_followed(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            secret = d / "secret.txt"
            secret.write_text("SUPER-SECRET-LOCAL-CONTENT\n")
            bare = d / "r.git"
            work = d / "w"
            subprocess.run(["git", "init", "-q", "--bare", str(bare)])
            work.mkdir()
            init_repo(work)
            base = git(work, "rev-parse", "HEAD").stdout.strip()
            (work / ".jahns-workflow.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\nreview:\n  mode: packet\n")
            (work / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            (work / "leak.txt").symlink_to(secret)            # tracked symlink → out-of-tree secret
            (work / ".gitattributes").write_text("ignoreme.txt export-ignore\n")
            (work / "ignoreme.txt").write_text("present in the tracked tree\n")
            git(work, "add", "-A")
            git(work, "commit", "-qm", "c1")
            head = git(work, "rev-parse", "HEAD").stdout.strip()
            git(work, "remote", "add", "origin", str(bare))
            git(work, "push", "-q", "-u", "origin", "main")
            rdir = work / "docs/reviews"
            rdir.mkdir(parents=True)
            (rdir / "2026-06-23-r-request.md").write_text("# req\n")
            (rdir / "2026-06-23-r-bundle.yaml").write_text(yaml.safe_dump(
                {"schema": jw_bundle.RECORD_SCHEMA, "project": "demo", "round_id": "2026-06-23-r",
                 "review_mode": "packet", "review_cycle": None, "branch": "main",
                 "base_sha": base, "round_commit": head, "head_sha": None}))
            with self._quiet():
                rc = jw_bundle.bundle(work, "2026-06-23-r", None, out_dir=d / "out")
            self.assertEqual(rc, 0)
            zp = list((d / "out").glob("*.review.zip"))[0]
            with zipfile.ZipFile(zp) as zf:
                names = zf.namelist()
                self.assertIn("repo/leak.txt", names)
                # the symlink is stored as its TARGET STRING, never followed — no out-of-tree leak
                self.assertEqual(zf.read("repo/leak.txt").decode(), str(secret))
                self.assertNotIn(b"SUPER-SECRET-LOCAL-CONTENT", b"".join(zf.read(n) for n in names))
                # .gitattributes export-ignore is NOT applied — the exact tracked tree is shipped
                self.assertIn("repo/ignoreme.txt", names)
                # GPT 7th review #2: the entry MUST be a regular file (S_IFREG), NOT a S_IFLNK entry
                # that `unzip` rebuilds as a live out-of-tree link. Record it in manifest.symlinks.
                mode = zf.getinfo("repo/leak.txt").external_attr >> 16
                self.assertEqual(mode & 0o170000, 0o100000, oct(mode))  # S_IFREG, never 0o120000
                man = yaml.safe_load(zf.read("__review__/MANIFEST.yaml"))
                self.assertIn({"path": "leak.txt", "target": str(secret)}, man["symlinks"])
            # the Python-zipfile-only assertion above missed the real-extraction leak; prove the real
            # `unzip` does NOT materialize a live link that would resolve to the out-of-tree secret.
            import shutil
            if shutil.which("unzip"):
                ux = d / "unz"
                ux.mkdir()
                subprocess.run(["unzip", "-q", str(zp), "-d", str(ux)], capture_output=True)
                self.assertFalse((ux / "repo" / "leak.txt").is_symlink())
                self.assertEqual((ux / "repo" / "leak.txt").read_text(), str(secret))

    def test_close_rolls_back_sidecar_on_write_failure(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            init_repo(work)
            base = git(work, "rev-parse", "HEAD").stdout.strip()
            (work / ".jahns-workflow.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\nreview:\n  mode: packet\n"
                "state:\n  last_round_commit: " + base + "\n")
            orig_tasks = ("version: 1\nproject: demo\ntasks:\n"
                          "- id: feat/x-thing\n  title: the x thing that does y\n  status: active\n")
            (work / "tasks.yaml").write_text(orig_tasks)
            git(work, "add", "-A")
            git(work, "commit", "-qm", "c1")
            # reviews_dir is a FILE where the dir should be → the sidecar write raises mid-commit
            (work / "docs").mkdir()
            (work / "docs/reviews").write_text("not a dir\n")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jw_round.close(work, "2026-06-23-r", ["feat/x-thing"], [], "HEAD")
            self.assertEqual(rc, 1)
            # the whole closeout (incl. the task status flip) must be rolled back — atomic with the sidecar
            self.assertEqual((work / "tasks.yaml").read_text(), orig_tasks)

    def test_unpushed_head_refused(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            work, base, head = self._packet_repo(d)
            # an unpushed CLOSEOUT commit (bookkeeping only → passes round-binding) that isn't pushed
            (work / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks:\n- id: feat/x-thing\n  title: the x thing that does y\n"
                "  status: done\n  round: 2026-06-23-r\n  anchor: '§1'\n  notes: stamped\n")
            git(work, "add", "-A")
            git(work, "commit", "-qm", "docs(round): close")
            with self._quiet():
                rc = jw_bundle.bundle(work, "2026-06-23-r", None, out_dir=d / "out")
            self.assertEqual(rc, 1)  # head not pushed → refuse (durable-commit precondition)

    def test_missing_sidecar_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            work, base, head = self._packet_repo(d)
            (work / "docs/reviews/2026-06-23-r-bundle.yaml").unlink()
            with self._quiet():
                rc = jw_bundle.bundle(work, "2026-06-23-r", None, out_dir=d / "out")
            self.assertEqual(rc, 1)  # no record → cannot resolve base/head

    def test_round_close_writes_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            init_repo(work)  # c0 — becomes the base watermark
            base = git(work, "rev-parse", "HEAD").stdout.strip()
            (work / ".jahns-workflow.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\nreview:\n  mode: packet\n"
                "state:\n  last_round_commit: " + base + "\n")
            (work / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks:\n"
                "- id: feat/x-thing\n  title: the x thing that does y\n  status: active\n")
            (work / "code.txt").write_text("impl\n")
            git(work, "add", "-A")
            git(work, "commit", "-qm", "c1")
            head = git(work, "rev-parse", "HEAD").stdout.strip()
            with self._quiet():
                rc = jw_round.close(work, "2026-06-23-r", ["feat/x-thing"], [], "HEAD")
            self.assertEqual(rc, 0)
            rec = yaml.safe_load((work / "docs/reviews/2026-06-23-r-bundle.yaml").read_text())
            self.assertEqual(rec["schema"], jw_bundle.RECORD_SCHEMA)
            self.assertEqual(rec["base_sha"], base)   # previous watermark captured as base
            self.assertEqual(rec["round_commit"], head)  # this round's tip, binds the bundle
            self.assertIsNone(rec["head_sha"])        # head is stamped by `jw review bundle`, not at close
            self.assertEqual(rec["review_mode"], "packet")

    def test_reviewer_kit_render(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "kit"
            with self._quiet():
                rc = jw_bundle.kit(out)
            self.assertEqual(rc, 0)
            for name in jw_bundle.KIT_SOURCES:
                self.assertTrue((out / name).is_file(), name)
            man = yaml.safe_load((out / "KIT_MANIFEST.yaml").read_text())
            self.assertEqual(man["protocol"], jw_bundle.REVIEWER_PROTOCOL)
            self.assertEqual(set(man["templates"]), set(jw_bundle.KIT_SOURCES))
            # manifest hash matches the rendered bytes (tamper-evident kit)
            import hashlib
            for name, h in man["templates"].items():
                self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), h)

    def test_first_round_base_none_is_coherent(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            work, base, head = self._packet_repo(d)
            # first-round semantics: no previous watermark → base_sha null
            rec_path = work / "docs/reviews/2026-06-23-r-bundle.yaml"
            rec = yaml.safe_load(rec_path.read_text())
            rec["base_sha"] = None
            rec_path.write_text(yaml.safe_dump(rec))
            with self._quiet():
                rc = jw_bundle.bundle(work, "2026-06-23-r", None, out_dir=d / "out")
            self.assertEqual(rc, 0)
            ex = d / "ex"
            ex.mkdir()
            with zipfile.ZipFile(list((d / "out").glob("*.review.zip"))[0]) as zf:
                zf.extractall(ex)
            man = yaml.safe_load((ex / "__review__" / "MANIFEST.yaml").read_text())
            self.assertIsNone(man["base_sha"])
            self.assertTrue(man["comparison"].startswith("(root).."))
            # empty-tree diff must cover the FULL tree (f.txt from c0 AND g.txt from c1), so
            # CHANGED_FILES / changed_file_count / DIFF agree with COMMITS + the (root).. label
            cf = (ex / "__review__" / "CHANGED_FILES.txt").read_text()
            self.assertIn("f.txt", cf)
            self.assertIn("g.txt", cf)
            self.assertGreaterEqual(man["scope"]["changed_file_count"], 2)
            self.assertTrue((ex / "__review__" / "DIFF.patch").read_text().strip())
            self.assertEqual(jw_bundle.validate_manifest(man), [])

    def test_unreachable_base_refused(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            work, base, head = self._packet_repo(d)
            rec_path = work / "docs/reviews/2026-06-23-r-bundle.yaml"
            rec = yaml.safe_load(rec_path.read_text())
            rec["base_sha"] = "0" * 40  # well-formed hex, but not a reachable commit
            rec_path.write_text(yaml.safe_dump(rec))
            with self._quiet():
                rc = jw_bundle.bundle(work, "2026-06-23-r", None, out_dir=d / "out")
            self.assertEqual(rc, 1)  # fail closed rather than ship an empty diff

    def test_validate_rejects_base_equals_head(self):
        cfg = {"ssot": None, "progress": "P", "adr_dir": "a", "reviews_dir": "r", "generated_dir": "g"}
        ident = {"project": "demo", "round_id": "2026-06-23-r", "review_mode": "packet",
                 "review_cycle": None, "branch": "main", "base_sha": "a" * 40,
                 "head_sha": "a" * 40, "repo": None}
        m = jw_bundle.build_manifest(cfg, ident, has_checks=False, worktree_dirty=False, omissions=[])
        m["scope"] = {"tasks": [], "ssot_anchors": [], "changed_file_count": 0}
        self.assertTrue(any("equals head_sha" in e for e in jw_bundle.validate_manifest(m)))

    def test_pr_mode_resolve_and_owner_provenance(self):
        HEAD, BASE = "a" * 40, "b" * 40
        mk = jw_review.emit_marker

        def ctx_with(author):
            body = mk("review-cycle", {"round_id": "2026-06-23-pr", "cycle": 2,
                                       "target_sha": HEAD, "base_sha": BASE})
            return lambda root, pr: {
                "repo": "owner/repo", "pr": pr,
                "bundle": {"bodies": [{"id": 1, "body": body, "author": author, "at": "2026-06-23T01:00:00Z"}],
                           "head": HEAD, "base_sha": BASE, "head_ref": "feat/x"},
                "head": HEAD, "base_sha": BASE, "base": "main",
                "policy": {"project": "demo", "review": {"operators": [], "reviewers": ["codex", "gpt-5.5-pro"]}},
            }
        saved = jw_review.pr_context
        try:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                jw_review.pr_context = ctx_with("owner")
                ident = jw_bundle._resolve_pr(root, 7, None)
                self.assertEqual(ident["review_mode"], "pr")
                self.assertEqual(ident["review_cycle"], 2)
                self.assertEqual(ident["head_sha"], HEAD)
                self.assertEqual(ident["base_sha"], BASE)
                self.assertEqual(ident["round_id"], "2026-06-23-pr")
                # a freeze marker posted by an UNTRUSTED actor must be ignored (owner provenance),
                # so the attacker cannot redirect the bundled SHA — no frozen cycle remains
                jw_review.pr_context = ctx_with("attacker")
                with self.assertRaises(jw_common.WorkflowError):
                    jw_bundle._resolve_pr(root, 7, None)
        finally:
            jw_review.pr_context = saved

    def test_round_binding_rejects_forward_code(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            work, base, head = self._packet_repo(d)  # sidecar round_commit = head
            # commit NEXT-ROUND code (non-bookkeeping) without re-closing, then push it
            (work / "nextfeature.py").write_text("next round code\n")
            git(work, "add", "-A")
            git(work, "commit", "-qm", "next round work")
            git(work, "push", "-q", "origin", "main")  # pushed, so only the round-binding guard can refuse
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jw_bundle.bundle(work, "2026-06-23-r", None, out_dir=d / "out")
            self.assertEqual(rc, 1)  # HEAD advanced past the round with CODE → wrong-tree guard fires

    def test_read_blobs_missing_object_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            init_repo(work)
            # a nonexistent object → cat-file --batch prints "<sha> missing"; we must raise, not ship empty
            with self.assertRaises(jw_common.WorkflowError):
                jw_bundle._read_blobs(work, ["0" * 40])

    def test_reclose_preserves_stamped_head(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            work, base, head = self._packet_repo(d)
            cfg = jw_common.load_config(work)
            jw_bundle.record_stamp_head(work, cfg, "2026-06-23-r", head)  # a bundle stamped the head
            jw_bundle.write_record(work, cfg, {  # a RE-CLOSE rewrites the record
                "project": "demo", "round_id": "2026-06-23-r", "branch": "main",
                "base_sha": base, "round_commit": head})
            rec = jw_bundle.read_record(work, cfg, "2026-06-23-r")
            self.assertEqual(rec["head_sha"], head)  # the stamp survives re-close (ingest stays bound)

    def test_non_utf8_path_recorded_as_omission(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            init_repo(work)
            try:
                with open(os.path.join(os.fsencode(str(work)), b"bad\xffname.txt"), "wb") as f:
                    f.write(b"x")
            except OSError:
                self.skipTest("filesystem rejects non-utf8 filenames")
            git(work, "add", "-A")
            git(work, "commit", "-qm", "badname")
            head = git(work, "rev-parse", "HEAD").stdout.strip()
            oms: list = []
            jw_bundle._repo_entries(work, head, oms)
            self.assertTrue(any(o.get("kind") == "non-utf8-path" for o in oms))

    def test_round_binding_requires_round_commit(self):
        # GPT 7th review #1: a record WITHOUT round_commit must FAIL CLOSED, not silently bypass the
        # binding guard (which let a later HEAD's tree ship under this round's label — the v0.3.0 hole).
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            work, base, head = self._packet_repo(d)
            rp = work / "docs/reviews/2026-06-23-r-bundle.yaml"
            rec = yaml.safe_load(rp.read_text())
            rec.pop("round_commit")            # pre-binding / tampered sidecar
            rp.write_text(yaml.safe_dump(rec))
            (work / "nextfeature.py").write_text("next round code\n")   # forward CODE past the round
            git(work, "add", "-A")
            git(work, "commit", "-qm", "next round")
            git(work, "push", "-q", "origin", "main")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jw_bundle.bundle(work, "2026-06-23-r", None, out_dir=d / "out")
            self.assertEqual(rc, 1)            # unbound head → refuse, demand re-close
            self.assertEqual(list((d / "out").glob("*.review.zip")) if (d / "out").exists() else [], [])

    def test_closeout_only_fails_closed_on_git_error(self):
        # GPT 7th review #1: a FAILED `git diff` (nonzero rc, empty stdout) must raise, never read as
        # "closeout-only" — else a binding diff that errors would wave forward CODE commits through.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            work, base, head = self._packet_repo(d)
            cfg = jw_common.load_config(work)
            with self.assertRaises(jw_common.WorkflowError):
                jw_bundle._closeout_only(work, cfg, "0" * 40, head)

    def test_request_symlink_refused(self):
        # GPT 7th review #2: a symlinked request must be refused (read_bytes() would follow it and
        # ship out-of-tree content into __review__/REQUEST.md).
        import contextlib
        import io
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            secret = d / "secret.txt"
            secret.write_text("OUT-OF-TREE-SECRET\n")
            work, base, head = self._packet_repo(d)
            rp = work / "docs/reviews/2026-06-23-r-request.md"
            rp.unlink()
            rp.symlink_to(secret)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jw_bundle.bundle(work, "2026-06-23-r", None, out_dir=d / "out")
            self.assertEqual(rc, 1)
            for z in ((d / "out").glob("*.review.zip") if (d / "out").exists() else []):
                with zipfile.ZipFile(z) as zf:
                    self.assertNotIn(b"OUT-OF-TREE-SECRET", zf.read("__review__/REQUEST.md"))

    def test_checks_symlink_refused(self):
        # GPT 7th review #2: same defense for the optional CHECKS.yaml input.
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            secret = d / "secret.txt"
            secret.write_text("OUT-OF-TREE-SECRET\n")
            work, base, head = self._packet_repo(d)
            (work / "docs/reviews/2026-06-23-r-checks.yaml").symlink_to(secret)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jw_bundle.bundle(work, "2026-06-23-r", None, out_dir=d / "out")
            self.assertEqual(rc, 1)


class BundleIngestTests(unittest.TestCase):
    """Structured ingest: verbatim preservation + fail-closed bundle-identity cross-check."""

    def _root(self, d: Path, head: str, round_id="2026-06-23-r"):
        (d / ".jahns-workflow.yml").write_text(
            "version: 1\nproject: demo\nreviews_dir: docs/reviews\nreview:\n  mode: packet\n")
        rdir = d / "docs/reviews"
        rdir.mkdir(parents=True)
        rec = {"schema": jw_bundle.RECORD_SCHEMA, "project": "demo", "round_id": round_id,
               "review_mode": "packet", "review_cycle": None, "branch": "main",
               "base_sha": "b" * 40, "head_sha": head}
        (rdir / f"{round_id}-bundle.yaml").write_text(yaml.safe_dump(rec))
        return rdir

    def _reply(self, reviewed_sha: str) -> bytes:
        return (
            "# Review Result\n\n## Findings\n\n### JW-GPT-001 — concrete bug in foo\n\n"
            "- Severity: major\n- Category: correctness\n\nsome prose.\n\n"
            "<!-- jw-review-summary:v1\nprotocol: jw-chatgpt-reviewer/v1\nreviewer: gpt-5.5-pro\n"
            "project: demo\nround_id: 2026-06-23-r\nreview_mode: packet\nreview_cycle: null\n"
            f"base_sha: {'b' * 40}\nreviewed_sha: {reviewed_sha}\nverdict: not-shipped\n"
            "decision_required: false\nblocker: 0\nmajor: 1\nminor: 0\n-->\n"
        ).encode("utf-8")

    def test_identity_match_and_finding_skeleton(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            head = "a" * 40
            rdir = self._root(d, head)
            src = d / "in.md"
            src.write_bytes(self._reply(head))
            with contextlib.redirect_stdout(io.StringIO()):
                rc = jw_review.ingest(d, "2026-06-23-r", src=src, reviewer="gpt-5.5-pro")
            self.assertEqual(rc, 0)
            fb = (rdir / "2026-06-23-r-feedback.md").read_text()
            self.assertIn("check: **MATCH**", fb)
            self.assertIn("| JW-GPT-001 — concrete bug in foo | major |", fb)  # triage skeleton row
            self.assertIn(self._reply(head).split(b"<!--")[0].decode(), fb)   # verbatim body present

    def test_identity_mismatch_fails_closed(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rdir = self._root(d, "a" * 40)
            src = d / "in.md"
            src.write_bytes(self._reply("c" * 40))  # reviewed a DIFFERENT sha than was shipped
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jw_review.ingest(d, "2026-06-23-r", src=src)
            self.assertEqual(rc, 3)  # mismatch → triage halted
            fb = (rdir / "2026-06-23-r-feedback.md").read_text()
            self.assertIn("MISMATCH", fb)
            self.assertIn("DO NOT triage automatically", fb)

    def test_no_marker_back_compat(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rdir = self._root(d, "a" * 40)
            src = d / "in.md"
            src.write_bytes(b"plain reviewer prose, no marker at all\n")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = jw_review.ingest(d, "2026-06-23-r", src=src)
            self.assertEqual(rc, 0)  # no summary marker → manual triage, not a failure
            self.assertIn("no-marker", (rdir / "2026-06-23-r-feedback.md").read_text())

    def _reply_no_sha(self) -> bytes:
        return (
            "# Review Result\n\n### JW-GPT-001 — a bug\n\n- Severity: minor\n\n"
            "<!-- jw-review-summary:v1\nprotocol: jw-chatgpt-reviewer/v1\nreviewer: gpt-5.5-pro\n"
            "project: demo\nround_id: 2026-06-23-r\nreview_mode: packet\nreview_cycle: null\n"
            f"base_sha: {'b' * 40}\nverdict: shipped\ndecision_required: false\n"
            "blocker: 0\nmajor: 0\nminor: 1\n-->\n"
        ).encode("utf-8")

    def test_missing_reviewed_sha_fails_closed(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rdir = self._root(d, "a" * 40)
            src = d / "in.md"
            src.write_bytes(self._reply_no_sha())  # marker present but the load-bearing field is absent
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jw_review.ingest(d, "2026-06-23-r", src=src)
            self.assertEqual(rc, 3)  # absence of reviewed_sha must NOT be a silent MATCH
            fb = (rdir / "2026-06-23-r-feedback.md").read_text()
            self.assertIn("MISMATCH", fb)
            self.assertIn("(missing)", fb)

    def test_fenced_marker_flagged_unusable(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rdir = self._root(d, "a" * 40)
            src = d / "in.md"
            # the reviewer (or a paste) wrapped the marker in a code fence → parse strips it
            src.write_bytes(b"# Review Result\n\n```\n" + self._reply("a" * 40) + b"```\n")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jw_review.ingest(d, "2026-06-23-r", src=src)
            self.assertEqual(rc, 0)  # cannot SHA-cross-check, but must be flagged, not silently "no-marker"
            self.assertIn("UNUSABLE-MARKER", (rdir / "2026-06-23-r-feedback.md").read_text())

    def test_duplicate_markers_fail_closed(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rdir = self._root(d, "a" * 40)
            src = d / "in.md"
            src.write_bytes(self._reply("a" * 40) + b"\n\n" + self._reply("a" * 40))  # two summary markers
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jw_review.ingest(d, "2026-06-23-r", src=src)
            self.assertEqual(rc, 3)  # ambiguous identity → fail closed
            self.assertIn("ambiguous", (rdir / "2026-06-23-r-feedback.md").read_text())

    def _reply_fields(self, reviewed_sha: str, review_cycle: str = "null", base_sha: str = "b" * 40) -> bytes:
        return (
            "# Review Result\n\n<!-- jw-review-summary:v1\nprotocol: jw-chatgpt-reviewer/v1\n"
            "reviewer: gpt-5.5-pro\nproject: demo\nround_id: 2026-06-23-r\nreview_mode: packet\n"
            f"review_cycle: {review_cycle}\nbase_sha: {base_sha}\nreviewed_sha: {reviewed_sha}\n"
            "verdict: shipped\ndecision_required: false\nblocker: 0\nmajor: 0\nminor: 0\n-->\n"
        ).encode("utf-8")

    def test_review_cycle_mismatch_fails_closed(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rdir = self._root(d, "a" * 40)  # packet bundle → review_cycle is None
            src = d / "in.md"
            src.write_bytes(self._reply_fields("a" * 40, review_cycle="2"))  # same head, WRONG cycle
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jw_review.ingest(d, "2026-06-23-r", src=src)
            self.assertEqual(rc, 3)  # a same-head reply under a different cycle must NOT pass
            self.assertIn("review_cycle", (rdir / "2026-06-23-r-feedback.md").read_text())

    def test_base_sha_mismatch_fails_closed(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rdir = self._root(d, "a" * 40)  # bundle base_sha = b*40
            src = d / "in.md"
            src.write_bytes(self._reply_fields("a" * 40, base_sha="c" * 40))  # WRONG base
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jw_review.ingest(d, "2026-06-23-r", src=src)
            self.assertEqual(rc, 3)
            self.assertIn("base_sha", (rdir / "2026-06-23-r-feedback.md").read_text())

    def test_no_record_crosschecks_live_head(self):
        # no bundle record (bundle not built / reset by re-close), but a marker carrying a WRONG
        # reviewed_sha must still FAIL CLOSED against the live committed HEAD, not pass silently.
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            init_repo(work)
            (work / ".jahns-workflow.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\nreview:\n  mode: packet\n")
            live = git(work, "rev-parse", "HEAD").stdout.strip()
            rdir = work / "docs/reviews"
            rdir.mkdir(parents=True)  # NO sidecar → identity is None

            def reply(sha):
                return (
                    "# R\n\n<!-- jw-review-summary:v1\nprotocol: jw-chatgpt-reviewer/v1\nreviewer: gpt\n"
                    "project: demo\nround_id: 2026-06-23-r\nreview_mode: packet\nreview_cycle: null\n"
                    f"base_sha: none\nreviewed_sha: {sha}\nverdict: shipped\ndecision_required: false\n"
                    "blocker: 0\nmajor: 0\nminor: 0\n-->\n"
                ).encode("utf-8")
            wrong = work / "w.md"
            wrong.write_bytes(reply("c" * 40))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc_wrong = jw_review.ingest(work, "2026-06-23-r", src=wrong)
            self.assertEqual(rc_wrong, 3)  # wrong reviewed_sha vs live HEAD → fail closed
            ok = work / "ok.md"
            ok.write_bytes(reply(live))
            with contextlib.redirect_stdout(io.StringIO()):
                rc_ok = jw_review.ingest(work, "2026-06-23-r", src=ok)
            self.assertEqual(rc_ok, 0)  # reviewed_sha == live HEAD → accepted (degraded, no bundle record)


if __name__ == "__main__":
    unittest.main(verbosity=2)
