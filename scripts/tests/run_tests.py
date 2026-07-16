#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Integration tests for the waystone v0.2.0 correctness kernel.

Run: uv run scripts/tests/run_tests.py
Covers the deterministic core: merge-gate computation, review-cycle marker emit/parse/classify,
SHA-bound approval logic, tasks gate counts, remote push verification (real temp git repos),
and config review-mode validation. No network / no gh required.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import cclog  # noqa: E402
import codexlog  # noqa: E402
import common  # noqa: E402
import dashboard  # noqa: E402
import delegate  # noqa: E402
import improve  # noqa: E402
import lanes  # noqa: E402
import overlay  # noqa: E402
import merge  # noqa: E402
import resume  # noqa: E402
import review  # noqa: E402
import roadmap  # noqa: E402
import round  # noqa: E402
import tasks  # noqa: E402
import validate  # noqa: E402
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


class ReleaseToMainTests(unittest.TestCase):
    def _repo(self, d: str, *, shipped_change: bool) -> tuple[Path, dict[str, str]]:
        import os

        root = Path(d) / "repo"
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "t@t")
        git(root, "config", "user.name", "t")
        (root / ".gitignore").write_text(".claude/settings.local.json\n")
        (root / "README.md").write_text("main\n")
        (root / "bin").mkdir()
        (root / "bin" / "waystone").write_text("runtime\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "main base")
        git(root, "branch", "dev")
        git(root, "checkout", "-q", "dev")

        release = root / "release-to-main.sh"
        release.write_bytes((SCRIPTS.parent / "release-to-main.sh").read_bytes())
        release.chmod(0o755)
        (root / ".claude" / "agents").mkdir(parents=True)
        (root / ".claude" / "agents" / "waystone.md").write_text("agent\n")
        (root / "future-dogfood.md").write_text("must not ship\n")
        if shipped_change:
            (root / "README.md").write_text("release\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "dev changes")
        settings = root / ".claude" / "settings.local.json"
        settings.write_bytes(b'{"permission":"local"}\n')

        fake_bin = Path(d) / "fake-bin"
        fake_bin.mkdir()
        uv = fake_bin / "uv"
        uv.write_text("#!/bin/sh\nexit 0\n")
        uv.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
        return root, env

    def _run(self, root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(root / "release-to-main.sh")], cwd=root,
            env=env, capture_output=True, text=True, timeout=20)

    def _worktree_files(self, root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(root).parts
        }

    def test_release_preserves_ignored_local_file_and_current_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            branch_before = git(root, "symbolic-ref", "--short", "HEAD").stdout
            head_before = git(root, "rev-parse", "HEAD").stdout
            status_before = git(root, "status", "--porcelain").stdout
            files_before = self._worktree_files(root)

            result = self._run(root, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(git(root, "symbolic-ref", "--short", "HEAD").stdout, branch_before)
            self.assertEqual(git(root, "rev-parse", "HEAD").stdout, head_before)
            self.assertEqual(git(root, "status", "--porcelain").stdout, status_before)
            self.assertEqual(self._worktree_files(root), files_before)
            self.assertEqual(git(root, "show", "main:README.md").stdout, "release\n")
            released = git(root, "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
            self.assertIn("bin/waystone", released)
            self.assertNotIn("future-dogfood.md", released)
            self.assertFalse(any(path.startswith(".claude/") for path in released))

    def test_commit_failure_preserves_ref_branch_and_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            signer = Path(d) / "fake-bin" / "gpg-fail"
            signer.write_text("#!/bin/sh\nexit 1\n")
            signer.chmod(0o755)
            git(root, "config", "commit.gpgsign", "true")
            git(root, "config", "gpg.program", str(signer))
            main_before = git(root, "rev-parse", "main").stdout
            branch_before = git(root, "symbolic-ref", "--short", "HEAD").stdout
            head_before = git(root, "rev-parse", "HEAD").stdout
            status_before = git(root, "status", "--porcelain").stdout
            files_before = self._worktree_files(root)

            result = self._run(root, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(git(root, "rev-parse", "main").stdout, main_before)
            self.assertEqual(git(root, "symbolic-ref", "--short", "HEAD").stdout, branch_before)
            self.assertEqual(git(root, "rev-parse", "HEAD").stdout, head_before)
            self.assertEqual(git(root, "status", "--porcelain").stdout, status_before)
            self.assertEqual(self._worktree_files(root), files_before)
            worktrees = git(root, "worktree", "list", "--porcelain").stdout
            self.assertEqual(worktrees.count("worktree "), 1)

    def test_unlisted_dev_paths_are_noop_when_shipped_tree_matches_main(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=False)
            main_before = git(root, "rev-parse", "main").stdout

            result = self._run(root, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("nothing to release", result.stdout)
            self.assertEqual(git(root, "rev-parse", "main").stdout, main_before)

    def test_release_script_has_isolated_staging_contract(self):
        script = (SCRIPTS.parent / "release-to-main.sh").read_text()
        self.assertIn("SHIP_PATHS=(", script)
        self.assertNotIn("git checkout", script)
        self.assertNotIn("git read-tree -u", script)
        self.assertNotIn('rm -rf -- "$p"', script)


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
        # freeze reads the BASE policy via pr_context; a custom non-codex reviewer must be prompted
        ctx = {"repo": "o/r", "pr": 3, "head": "a" * 40, "base_sha": "b" * 40, "base": "main",
               "bundle": {"head": "a" * 40, "base_sha": "b" * 40, "bodies": []},
               "policy": common.normalize_config(
                   {"version": 1, "project": "x", "review": {"mode": "pr", "reviewers": ["codex", "research-auditor"]}})}

        def fake_gh(root, *args):
            if len(args) >= 2 and args[0] == "pr" and args[1] == "comment":
                captured["body"] = args[args.index("--body") + 1]
            return (0, "")

        saved = (review.pr_context, review._gh)
        review.pr_context = lambda root, pr: ctx
        review._gh = fake_gh
        try:
            review.freeze(Path("/x"), 3, "2026-06-22-r")
        finally:
            review.pr_context, review._gh = saved
        self.assertIn("research-auditor", captured.get("body", ""))  # custom reviewer prompted, not name-guessed
        self.assertIn("@codex review", captured["body"])

    def test_reviewer_role_freezes_full_backend_identity_and_profile_fingerprint(self):
        captured = {}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = common.ensure_project_state_dir(root)
            (state / "profile.yml").write_text(
                "schema: waystone-profile-1\nbindings:\n"
                "  reviewer: {execution: forked-subagent, backend: 'claude:opus-4.1'}\n")
            ctx = {
                "repo": "o/r", "pr": 3, "head": "a" * 40, "base_sha": "b" * 40,
                "base": "main", "bundle": {
                    "head": "a" * 40, "base_sha": "b" * 40, "bodies": []},
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
                self.assertEqual(review.freeze(root, 3, "2026-07-15-l2-a"), 0)
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
                    rc = review.main(["freeze", "--pr", "3", str(root)])
            finally:
                review.pr_context = original
            self.assertEqual(rc, 1)
            self.assertIn("profile has no binding for role 'reviewer'", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())


PASS = dict(cycle_fresh=True, require_ci=True, ci="passing", want_codex=True, codex_fresh=True,
            findings_resolved=True, want_pro=True, pro_result_at_head=True, open_blockers=[],
            open_decisions=[], approved_at_head=True, remote_contains_head=None)


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


class PacketPublicationTests(unittest.TestCase):
    ROUND_ID = "2026-07-16-packet"

    def _request(self, root: Path, target: str, base: str = "(root)") -> Path:
        request = root / "docs" / "reviews" / f"{self.ROUND_ID}-request.md"
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(
            f"# Review Request — {self.ROUND_ID}\n\n"
            f"- Reviewing: {target}   (diff against {base})\n")
        return request

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
            root = Path(d)
            init_repo(root)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
            head = git(root, "rev-parse", "HEAD").stdout.strip()
            self._request(root, head)

            self.assertEqual(review.prepare_packet_request(root, self.ROUND_ID), 0)
            bindings = list((root / "docs" / "reviews").glob(
                f"{self.ROUND_ID}-request.binding*.json"))
            self.assertEqual(len(bindings), 1)
            self.assertEqual(review.prepare_packet_request(root, self.ROUND_ID), 0)
            self.assertEqual(list((root / "docs" / "reviews").glob(
                f"{self.ROUND_ID}-request.binding*.json")), bindings)

            other = "a" * 40 if head != "a" * 40 else "b" * 40
            self._request(root, other)
            self.assertEqual(review.prepare_packet_request(root, self.ROUND_ID), 1)

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
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "closeout")
            git(root, "remote", "add", "origin", str(bare))
            git(root, "push", "-q", "-u", "origin", "main")
            closeout = git(root, "rev-parse", "HEAD").stdout.strip()
            request = self._request(root, closeout)
            env = os.environ.copy()
            env.update({"HOME": str(home), "WAYSTONE_HOME": str(home / ".waystone")})

            def cli(*args: str):
                return subprocess.run(
                    [sys.executable, str(SCRIPTS / "waystone.py"), *args],
                    cwd=root, env=env, capture_output=True, text=True, timeout=15)

            prepared = cli("review", "prepare", "--round", self.ROUND_ID, str(root))
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            binding = next((root / "docs" / "reviews").glob(
                f"{self.ROUND_ID}-request.binding*.json"))

            untracked = cli("remote", "verify", str(root), "--round", self.ROUND_ID)
            self.assertNotEqual(untracked.returncode, 0)
            self.assertIn("not committed", untracked.stderr)

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

    def test_improve_warns_when_corrupt_request_binding_is_skipped(self):
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
                self.assertEqual(improve._round_review_sidecars(rdir), {})
            self.assertIn("corrupt review binding", err.getvalue())
            self.assertIn(str(corrupt), err.getvalue())
            self.assertIn(str(corrupt_freeze), err.getvalue())
            with self.assertRaisesRegex(common.WorkflowError, "corrupt review binding"):
                review.write_pr_freeze_binding(
                    Path(d), self.ROUND_ID, 1, 1, "a" * 40, "b" * 40,
                    ["codex:test"], None, "reviews")


class ResumeStartHereTests(unittest.TestCase):
    """Persistent model-authored re-entry pointer (START_HERE) + its SessionStart injection."""

    def test_start_here_path_distinct_and_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            self.assertEqual(common.start_here_path(root), common.start_here_path(root))  # per-repo stable
            self.assertNotEqual(common.start_here_path(root), common.resume_path(root))   # vs ephemeral
            self.assertEqual(common.start_here_path(root), root / ".waystone" / "start-here.md")
            self.assertEqual(common.resume_path(root), root / ".waystone" / "resume.md")

    def _with_home(self, home: Path, fn):
        argv_bak = sys.argv
        try:
            return _run_with_home(home, fn)
        finally:
            sys.argv = argv_bak

    def test_start_here_path_cli_creates_parent(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "proj"
            root.mkdir()
            init_repo(root)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            home = Path(d) / "home"
            home.mkdir()

            def run():
                sys.argv = ["resume.py", "--start-here-path", str(root)]
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = resume.main()
                return rc, buf.getvalue().strip()

            rc, printed = self._with_home(home, run)
            self.assertEqual(rc, 0)
            self.assertEqual(Path(printed), root.resolve() / ".waystone" / "start-here.md")
            self.assertTrue(Path(printed).parent.is_dir())   # parent created so the model can Write to it

    def test_resume_write_is_atomic_and_cleans_temp_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "proj"
            root.mkdir()
            target = common.resume_path(root)
            original_snapshot = resume.snapshot
            original_replace = common.os.replace

            def fail_replace(_source, _target):
                raise OSError("injected replace failure")

            resume.snapshot = lambda _root: "snapshot\n"
            common.os.replace = fail_replace
            try:
                with self.assertRaises(OSError):
                    resume.write(root)
            finally:
                common.os.replace = original_replace
                resume.snapshot = original_snapshot
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_resume_consume_claim_preserves_concurrent_replacement(self):
        import contextlib
        import io
        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "proj"
            root.mkdir()
            init_repo(root)
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            home = Path(d) / "home"
            home.mkdir()
            rp = common.resume_path(root)
            rp.parent.mkdir(parents=True)
            old = "captured_head: old\ncaptured_at: old-at\n"
            new = "captured_head: new\ncaptured_at: new-at\n"
            rp.write_text(old)
            original_rename = session_context.os.rename
            renamed = []

            def replace_after_claim(source, claim):
                original_rename(source, claim)
                renamed.append(Path(claim))
                common.write_text_atomic(Path(source), new)

            old_argv = sys.argv
            session_context.os.rename = replace_after_claim
            sys.argv = ["session_context.py", str(root)]
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    rc = _run_with_home(home, session_context.main)
            finally:
                session_context.os.rename = original_rename
                sys.argv = old_argv
            ctx = _json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(rc, 0)
            self.assertEqual(len(renamed), 1)
            self.assertIn("last checkpoint: old-at @ old", ctx)
            self.assertEqual(rp.read_text(), new)
            self.assertFalse(renamed[0].exists())

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
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            home = Path(d) / "home"
            home.mkdir()

            def ctx_for(start_here_body: str) -> str:
                def run():
                    sh = common.start_here_path(root)
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


class StoragePathTests(unittest.TestCase):
    def test_machine_dir_is_host_neutral_and_honors_override(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            old_host = os.environ.get("WAYSTONE_HOST")
            old_override = os.environ.get("WAYSTONE_HOME")
            try:
                os.environ["WAYSTONE_HOST"] = "claude"
                self.assertEqual(common.machine_dir(home), home / ".waystone")
                os.environ["WAYSTONE_HOST"] = "codex"
                self.assertEqual(common.machine_dir(home), home / ".waystone")
                os.environ["WAYSTONE_HOME"] = "~/custom-waystone"
                self.assertEqual(_run_with_home(
                    home, common.machine_dir, isolate_storage=False),
                                 home / "custom-waystone")
            finally:
                if old_host is None:
                    os.environ.pop("WAYSTONE_HOST", None)
                else:
                    os.environ["WAYSTONE_HOST"] = old_host
                if old_override is None:
                    os.environ.pop("WAYSTONE_HOME", None)
                else:
                    os.environ["WAYSTONE_HOME"] = old_override

    def test_machine_dir_rejects_relative_override(self):
        import os

        old_override = os.environ.get("WAYSTONE_HOME")
        try:
            os.environ["WAYSTONE_HOME"] = "relative-waystone"
            with self.assertRaises(common.WorkflowError) as cm:
                common.machine_dir()
            self.assertIn("absolute path", str(cm.exception))
        finally:
            if old_override is None:
                os.environ.pop("WAYSTONE_HOME", None)
            else:
                os.environ["WAYSTONE_HOME"] = old_override

    def test_project_state_path_is_pure_and_ensure_restores_self_gitignore(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            state = common.project_state_path(root)
            self.assertEqual(state, root / ".waystone")
            self.assertFalse(state.exists())
            self.assertEqual(common.project_lock_path(root), state / "lock")
            self.assertFalse(state.exists())
            self.assertEqual(common.ensure_project_state_dir(root), state)
            self.assertEqual((state / ".gitignore").read_text(), "*\n")
            (state / ".gitignore").unlink()
            self.assertEqual(common.ensure_project_state_dir(root), state)
            self.assertEqual((state / ".gitignore").read_text(), "*\n")

    def test_project_lock_bootstrap_creates_verified_state_and_self_ignore(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            lock = common.project_lock_path(root)
            self.assertFalse(lock.parent.exists())
            with common.hold_lock(lock, timeout=0.2):
                self.assertTrue(lock.is_file())
                self.assertEqual((lock.parent / ".gitignore").read_text(), "*\n")

    def test_consumers_use_project_state_and_machine_worktree_cache(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            home = Path(d) / "home"
            home.mkdir()
            self.assertEqual(delegate._delegations_dir(root),
                             root / ".waystone" / "delegations")
            self.assertEqual(delegate._profile_path(root), root / ".waystone" / "profile.yml")
            self.assertEqual(overlay._overlay_dir(root), root / ".waystone" / "overlay")
            self.assertEqual(overlay._exposure_dir(root), root / ".waystone" / "exposure")
            self.assertEqual(
                _run_with_home(home, lambda: delegate._worktrees_dir(root)),
                home / ".waystone" / "cache" / "worktrees" / common._project_slug(root),
            )


class DashboardLockingTests(unittest.TestCase):
    def test_local_migration_waits_for_project_lock_then_runs_exactly_once(self):
        import contextlib
        import threading

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            entered = threading.Event()
            released = threading.Event()
            migrated = threading.Event()
            calls = []
            failures = []
            originals = (getattr(dashboard, "hold_lock", None), dashboard.migrate_project_state,
                         dashboard.git_branch_info, dashboard.load_tasks)

            @contextlib.contextmanager
            def observed_lock(path, timeout=None):
                self.assertEqual(Path(path), common.project_lock_path(root))
                entered.set()
                with originals[0](path, timeout=timeout):
                    yield

            def migrate(path):
                self.assertTrue(released.is_set())
                calls.append(Path(path))
                migrated.set()

            def run():
                try:
                    dashboard.show_local("demo", root)
                except BaseException as e:  # capture thread assertion for the main test thread
                    failures.append(e)

            dashboard.hold_lock = observed_lock
            dashboard.migrate_project_state = migrate
            dashboard.git_branch_info = lambda _root: {
                "branch": "dev", "dirty": 0, "ahead": 0, "behind": 0,
            }
            dashboard.load_tasks = lambda _root: {"tasks": []}
            worker = threading.Thread(target=run)
            try:
                with common.hold_lock(common.project_lock_path(root), timeout=0.2):
                    worker.start()
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(migrated.is_set())
                    released.set()
                worker.join(1)
            finally:
                released.set()
                worker.join(1)
                if originals[0] is None:
                    del dashboard.hold_lock
                else:
                    dashboard.hold_lock = originals[0]
                dashboard.migrate_project_state = originals[1]
                dashboard.git_branch_info = originals[2]
                dashboard.load_tasks = originals[3]
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(calls, [root])

    def test_one_project_migration_failure_does_not_skip_later_projects(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            registry = home / ".waystone" / "projects.json"
            registry.parent.mkdir(parents=True)
            registry.write_text(_json.dumps({"projects": [
                {"name": "broken", "path": str(Path(d) / "broken")},
                {"name": "healthy", "path": str(Path(d) / "healthy")},
            ]}))
            (Path(d) / "broken").mkdir()
            (Path(d) / "healthy").mkdir()
            seen = []
            original = dashboard.show_entry
            old_argv = sys.argv

            def show(entry):
                seen.append(entry["name"])
                if entry["name"] == "broken":
                    raise common.WorkflowError("synthetic migration failure")

            dashboard.show_entry = show
            sys.argv = ["dashboard.py"]
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, dashboard.main)
            finally:
                dashboard.show_entry = original
                sys.argv = old_argv
            self.assertEqual(rc, 1)
            self.assertEqual(seen, ["broken", "healthy"])
            self.assertIn("synthetic migration failure", err.getvalue())


class WaystoneStorageCliTests(unittest.TestCase):
    def _capture(self, home: Path, cwd: Path, argv: list[str]):
        import contextlib
        import io
        import os
        import waystone

        old_cwd = Path.cwd()
        out, err = io.StringIO(), io.StringIO()
        try:
            os.chdir(cwd)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: waystone.main(argv))
        finally:
            os.chdir(old_cwd)
        return rc, out.getvalue(), err.getvalue()

    def test_paths_outside_project_lists_only_machine_paths(self):
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            outside = Path(d) / "outside"
            outside.mkdir()
            home = Path(d) / "home"
            home.mkdir()
            rc, out, err = self._capture(home, outside, ["paths", "--json"])
            self.assertEqual((rc, err), (0, ""))
            paths = _json.loads(out)
            self.assertEqual(set(paths), {"machine_root", "worktrees_cache", "registry"})
            self.assertEqual(paths["machine_root"], str((home / ".waystone").resolve()))
            self.assertEqual(paths["registry"], str((home / ".waystone" / "projects.json").resolve()))

    def test_paths_inside_project_lists_resolved_project_paths(self):
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            home = Path(d) / "home"
            home.mkdir()
            rc, out, err = self._capture(home, nested, ["paths", "--json"])
            self.assertEqual((rc, err), (0, ""))
            paths = _json.loads(out)
            state = root.resolve() / ".waystone"
            self.assertEqual(paths["project_root"], str(root.resolve()))
            self.assertEqual(paths["project_state"], str(state))
            self.assertEqual(paths["resume"], str(state / "resume.md"))
            self.assertEqual(paths["start_here"], str(state / "start-here.md"))
            self.assertEqual(paths["delegations"], str(state / "delegations"))
            self.assertEqual(paths["overlay"], str(state / "overlay"))
            self.assertEqual(paths["exposure"], str(state / "exposure"))
            self.assertEqual(paths["profile"], str(state / "profile.yml"))
            self.assertEqual(paths["project_improve"], str(state / "improve"))
            self.assertEqual({p.name for p in state.iterdir()}, {".gitignore", "lock"})
            rc, human, err = self._capture(
                home, Path(d), ["paths", "--root", str(root)])
            self.assertEqual((rc, err), (0, ""))
            self.assertIn(f"project_state: {state}", human)
            self.assertEqual({p.name for p in state.iterdir()}, {".gitignore", "lock"})

    def test_dispatcher_runs_lazy_migration_for_explicit_project_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            outside = Path(d) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            home = Path(d) / "home"
            home.mkdir()
            slug = common._project_slug(root)
            source = home / ".claude" / "waystone.pre-0.9" / "start_here" / f"{slug}.md"
            source.parent.mkdir(parents=True)
            source.write_text("CLI-EXPLICIT-FRONTIER")
            rc, _out, err = self._capture(
                home, outside, ["paths", "--root", str(root)])
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(
                (root / ".waystone" / "start-here.md").read_text(), "CLI-EXPLICIT-FRONTIER")
            self.assertFalse(source.exists())

    def test_empty_state_readers_create_only_persistent_lock_state(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            home = Path(d) / "home"
            home.mkdir()
            state = root / ".waystone"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_run_with_home(
                    home, lambda: delegate.main(["status", "--root", str(root)])), 0)
                self.assertEqual(_run_with_home(
                    home, lambda: overlay.main(["list", "--root", str(root)])), 0)
            self.assertEqual({p.name for p in state.iterdir()}, {".gitignore", "lock"})

    def test_project_register_list_unregister_roundtrip_is_atomic(self):
        import json as _json
        import waystone

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            home = Path(d) / "home"
            home.mkdir()
            calls = []
            original_replace = waystone.os.replace

            def tracked_replace(src, dst):
                calls.append((Path(src), Path(dst)))
                return original_replace(src, dst)

            waystone.os.replace = tracked_replace
            try:
                rc, out, err = self._capture(
                    home, root, ["project", "register", str(root)])
                self.assertEqual((rc, err), (0, ""))
                self.assertIn("registered: demo", out)
                rc, out, err = self._capture(home, root, ["project", "list"])
                self.assertEqual((rc, err), (0, ""))
                self.assertIn(f"demo\t{root.resolve()}", out)
                rc, out, err = self._capture(
                    home, root, ["project", "unregister", str(root)])
                self.assertEqual((rc, err), (0, ""))
                self.assertIn("unregistered:", out)
            finally:
                waystone.os.replace = original_replace

            registry = home / ".waystone" / "projects.json"
            self.assertEqual(_json.loads(registry.read_text()), {"projects": []})
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(dst == registry for _src, dst in calls))
            self.assertTrue(all(src.parent == registry.parent for src, _dst in calls))
            self.assertEqual(len({src for src, _dst in calls}), 2)
            self.assertTrue(all(src.name.startswith(".projects.json.") and src.suffix == ".tmp"
                                for src, _dst in calls))
            self.assertTrue(all(not src.exists() for src, _dst in calls))

    def test_project_register_replace_failure_preserves_registry_and_cleans_temp(self):
        import json as _json
        import waystone

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            home = Path(d) / "home"
            registry = home / ".waystone" / "projects.json"
            registry.parent.mkdir(parents=True)
            original = _json.dumps({"projects": [{"name": "existing", "repo": "org/repo"}]})
            registry.write_text(original)
            original_replace = waystone.os.replace
            waystone.os.replace = lambda *_args: (_ for _ in ()).throw(OSError("replace failed"))
            try:
                rc, _out, err = self._capture(home, root, ["project", "register", str(root)])
            finally:
                waystone.os.replace = original_replace
            self.assertEqual(rc, 2)
            self.assertIn("replace failed", err)
            self.assertEqual(registry.read_bytes(), original.encode())
            self.assertEqual(
                sorted(p.name for p in registry.parent.iterdir()),
                ["projects.json", "registry.lock"],
            )

    def test_project_register_preserves_existing_union(self):
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "new"
            root.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: new\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: new\ntasks: []\n")
            home = Path(d) / "home"
            registry = home / ".waystone" / "projects.json"
            registry.parent.mkdir(parents=True)
            existing = [
                {"name": "local", "path": str(Path(d) / "local")},
                {"name": "remote", "repo": "org/remote"},
            ]
            registry.write_text(_json.dumps({"projects": existing}))
            rc, _out, err = self._capture(home, root, ["project", "register", str(root)])
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(_json.loads(registry.read_text())["projects"], [
                *existing, {"name": "new", "path": str(root.resolve()), "aliases": []},
            ])

    def test_project_register_duplicate_is_idempotent(self):
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            home = Path(d) / "home"
            first_rc, _out, first_err = self._capture(
                home, root, ["project", "register", str(root)])
            registry = home / ".waystone" / "projects.json"
            first_bytes = registry.read_bytes()
            second_rc, second_out, second_err = self._capture(
                home, root, ["project", "register", str(root)])
            self.assertEqual((first_rc, first_err, second_rc, second_err), (0, "", 0, ""))
            self.assertIn("already registered", second_out)
            self.assertEqual(registry.read_bytes(), first_bytes)
            self.assertEqual(_json.loads(first_bytes)["projects"], [
                {"name": "demo", "path": str(root.resolve()), "aliases": []},
            ])

    def test_project_alias_roundtrip_is_idempotent(self):
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            alias = Path(d) / "old-checkout"
            root.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            home = Path(d) / "home"
            home.mkdir()

            rc, _out, err = self._capture(
                home, root, ["project", "register", str(root)])
            self.assertEqual((rc, err), (0, ""))
            rc, out, err = self._capture(home, root, [
                "project", "alias", str(alias), "--root", str(root),
            ])
            self.assertEqual((rc, err), (0, ""))
            self.assertIn("alias added", out)
            registry = home / ".waystone" / "projects.json"
            first = registry.read_bytes()
            self.assertEqual(_json.loads(first)["projects"], [{
                "name": "demo", "path": str(root.resolve()),
                "aliases": [str(alias.resolve())],
            }])

            rc, out, err = self._capture(home, root, [
                "project", "alias", str(alias), "--root", str(root),
            ])
            self.assertEqual((rc, err), (0, ""))
            self.assertIn("already aliases", out)
            self.assertEqual(registry.read_bytes(), first)

    def test_project_register_rejects_path_already_owned_as_alias(self):
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            existing = Path(d) / "existing"
            requested = Path(d) / "requested"
            requested.mkdir()
            (requested / ".waystone.yml").write_text("version: 1\nproject: requested\n")
            (requested / "tasks.yaml").write_text(
                "version: 1\nproject: requested\ntasks: []\n")
            home = Path(d) / "home"
            registry = home / ".waystone" / "projects.json"
            registry.parent.mkdir(parents=True)
            registry.write_text(_json.dumps({"projects": [{
                "name": "existing", "path": str(existing.resolve()),
                "aliases": [str(requested.resolve())],
            }]}))
            before = registry.read_bytes()

            rc, _out, err = self._capture(
                home, requested, ["project", "register", str(requested)])

            self.assertEqual(rc, 1)
            self.assertIn("already belongs to", err)
            self.assertEqual(registry.read_bytes(), before)


class ConfigTests(unittest.TestCase):
    def _cfg(self, body: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text(body)
            return common.load_config(root)

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

    def test_reviewers_accept_literal_models_and_reviewer_role_reference(self):
        cfg = self._cfg(
            "version: 1\nproject: x\nreview:\n"
            "  reviewers: [codex, gpt-5.5-pro, 'role:reviewer']\n")
        self.assertEqual(
            cfg["review"]["reviewers"], ["codex", "gpt-5.5-pro", "role:reviewer"])
        with self.assertRaisesRegex(ValueError, "role:reviewer"):
            self._cfg(
                "version: 1\nproject: x\nreview:\n  reviewers: ['role:implementer']\n")

    def test_reviewer_role_reference_fails_loud_at_classifier_consumption(self):
        current_head = "a" * 40
        literal = review.classify([], current_head, macro_reviewers=("gpt-5.5-pro",))
        self.assertFalse(literal["pro_result_at_head"])
        with self.assertRaisesRegex(
                common.WorkflowError,
                "role:reviewer must be resolved from the profile before classification"):
            review.classify([], current_head, macro_reviewers=("role:reviewer",))



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
        out = round.set_task_field(TASKS_FIXTURE, "feat/alpha", "status", "done")
        self.assertIn("status: done", out)
        self.assertIn("# registry — comments must be preserved", out)  # comment preserved
        self.assertIn('title: "first task"', out)  # other fields intact
        self.assertEqual(out.count("status: active"), 0)

    def test_insert_missing_field(self):
        out = round.set_task_field(TASKS_FIXTURE, "feat/alpha", "round", "2026-06-19-z")
        self.assertIn("round: 2026-06-19-z", out)
        # inserted into feat/a block, not gate/b
        a_block = out.split("gate/beta")[0]
        self.assertIn("round: 2026-06-19-z", a_block)

    def test_only_targets_named_task(self):
        out = round.set_task_field(TASKS_FIXTURE, "gate/beta", "status", "done")
        self.assertIn("status: active", out)  # feat/a untouched
        self.assertEqual(out.count("status: done"), 1)

    def test_missing_task_raises(self):
        with self.assertRaises(KeyError):
            round.set_task_field(TASKS_FIXTURE, "feat/nope", "status", "done")

    def test_set_config_scalar_nested(self):
        cfg = "version: 1\nstate:\n  last_push_commit: null\n  last_round_commit: null\n"
        out = round.set_config_scalar(cfg, "last_round_commit", "abc123")
        self.assertIn("  last_round_commit: abc123", out)
        self.assertIn("  last_push_commit: null", out)  # sibling preserved
        with self.assertRaises(KeyError):
            round.set_config_scalar(cfg, "nonexistent_key", "v")

    def test_set_config_scalar_section_exact_child(self):
        # a deeper nested key of the same name must NOT be touched — only the direct child
        cfg = "state:\n  last_round_commit: null\n  nested:\n    last_round_commit: deep\n"
        out = round.set_config_scalar(cfg, "last_round_commit", "X", section="state")
        self.assertIn("  last_round_commit: X", out)
        self.assertIn("    last_round_commit: deep", out)

    def test_set_replaces_block_list_value(self):
        # a field whose existing value is a BLOCK list must be fully replaced — the continuation
        # lines consumed, not left orphaned under a new flow value (which would break the YAML).
        doc = ("version: 1\nproject: x\ntasks:\n"
               '  - id: feat/alpha\n    title: "base task alpha"\n    status: done\n'
               '  - id: feat/gamma\n    title: "gamma depends on alpha"\n    status: active\n    deps:\n      - feat/alpha\n')
        out = round.set_task_field(doc, "feat/gamma", "deps", '["feat/alpha"]')
        data = yaml.safe_load(out)  # parses only if the `- feat/alpha` block line was not orphaned
        byid = {t["id"]: t for t in data["tasks"]}
        self.assertEqual(byid["feat/gamma"]["deps"], ["feat/alpha"])
        self.assertNotIn("      - feat/alpha", out)
        self.assertEqual(validate.validate(data), [])


class NextActionableTests(unittest.TestCase):
    def test_deps_gate(self):
        data = {"tasks": [
            {"id": "feat/a", "title": "A", "status": "done"},
            {"id": "feat/b", "title": "B", "status": "pending", "deps": ["feat/a"]},
            {"id": "feat/c", "title": "C", "status": "pending", "deps": ["feat/b"]},  # dep b not done
            {"id": "feat/d", "title": "D", "status": "active", "deps": []},
            {"id": "gate/e", "title": "E", "status": "blocked", "deps": ["feat/a"]},  # stale-blocked
        ]}
        got = dict(common.next_actionable(data))
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
            self.assertEqual(lanes.check_lane(root, "feat/foo", {"branch": "feat/foo", "base_sha": base}), [])
            # a base the branch does NOT contain: make an unrelated commit on a sibling branch
            git(root, "checkout", "-q", "main")
            (root / "h.txt").write_text("2"); git(root, "add", "-A"); git(root, "commit", "-qm", "sib")
            sib = git(root, "rev-parse", "HEAD").stdout.strip()
            fails = lanes.check_lane(root, "feat/foo", {"branch": "feat/foo", "base_sha": sib})
            self.assertTrue(fails and "does NOT contain" in fails[0])

    def test_missing_branch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            base = git(root, "rev-parse", "HEAD").stdout.strip()
            fails = lanes.check_lane(root, "t", {"branch": "no/such", "base_sha": base})
            self.assertTrue(fails and "does not exist" in fails[0])

    def test_done_lane_with_deleted_branch_not_verified(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: x\ntasks:\n"
                "  - id: feat/old-lane\n    title: 'a merged & cleaned-up lane'\n    status: done\n"
                "    lane:\n      branch: deleted/gone\n      base_sha: deadbeef\n")
            self.assertEqual(lanes.verify(root), 0)  # done lane skipped, not a permanent failure


class RoundCloseTests(unittest.TestCase):
    def test_close_integration(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            git(root, "add", "-A"); git(root, "commit", "-qm", "setup")
            rc = round.close(root, "2026-06-19-z", done=["feat/alpha"], touched=["gate/beta"], commit="HEAD")
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
            self.assertIn(f"last_round_commit: {head}", (root / ".waystone.yml").read_text())

    def test_close_exposes_resolved_reviewer_identity_for_request_render(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, (
                "version: 1\nproject: x\nreview:\n  reviewers: ['role:reviewer']\n"
                "state:\n  last_round_commit: null\n"))
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  reviewer: {execution: forked-subagent, backend: 'claude:opus-4.1'}\n"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = round.close(
                    root, "2026-07-15-l2-a", done=["feat/alpha"], touched=[], commit="HEAD")
            self.assertEqual(rc, 0)
            self.assertIn("review reviewers = claude:opus-4.1", out.getvalue())
            self.assertNotIn("role:reviewer", out.getvalue())

    def test_close_role_reviewer_without_binding_fails_before_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, (
                "version: 1\nproject: x\nreview:\n  reviewers: ['role:reviewer']\n"
                "state:\n  last_round_commit: null\n"))
            _write_profile(root)
            before = (root / "tasks.yaml").read_bytes()
            self.assertEqual(round.close(
                root, "2026-07-15-l2-a", done=["feat/alpha"], touched=[], commit="HEAD"), 1)
            self.assertEqual((root / "tasks.yaml").read_bytes(), before)

    def test_close_replaces_all_primary_files_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            targets = []
            original_replace = common.os.replace

            def tracked_replace(src, dst):
                targets.append(Path(dst).resolve())
                return original_replace(src, dst)

            common.os.replace = tracked_replace
            try:
                rc = round.close(
                    root, "2026-06-19-atomic", done=["feat/alpha"], touched=[], commit="HEAD")
            finally:
                common.os.replace = original_replace
            self.assertEqual(rc, 0)
            self.assertTrue({
                (root / "tasks.yaml").resolve(),
                (root / ".waystone.yml").resolve(),
                (root / "ROADMAP.md").resolve(),
            }.issubset(set(targets)))

    def _setup(self, root, cfg_body):
        init_repo(root)
        (root / ".waystone.yml").write_text(cfg_body)
        (root / "tasks.yaml").write_text(TASKS_FIXTURE)
        git(root, "add", "-A"); git(root, "commit", "-qm", "setup")

    def test_missing_watermark_fails_closed_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\n")  # no state.last_round_commit
            before = (root / "tasks.yaml").read_text()
            rc = round.close(root, "2026-06-19-z", done=["feat/alpha"], touched=[], commit="HEAD")
            self.assertEqual(rc, 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)  # nothing written
            self.assertFalse((root / "ROADMAP.md").exists())

    def test_unresolvable_commit_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            before = (root / "tasks.yaml").read_text()
            rc = round.close(root, "2026-06-19-z", done=["feat/alpha"], touched=[], commit="nope-not-a-ref")
            self.assertEqual(rc, 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)

    def test_done_task_with_unmet_dep_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            before = (root / "tasks.yaml").read_text()
            # gate/beta depends on feat/alpha (active) — closing gate/beta as done must fail
            rc = round.close(root, "2026-06-19-z", done=["gate/beta"], touched=[], commit="HEAD")
            self.assertEqual(rc, 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)

    def test_close_dependency_and_dependent_together(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            # closing a dependency (feat/alpha) and its dependent (gate/beta) in ONE round is valid:
            # the dep is done in the final state
            rc = round.close(root, "2026-06-19-z", done=["feat/alpha", "gate/beta"], touched=[], commit="HEAD")
            self.assertEqual(rc, 0)
            self.assertEqual((root / "tasks.yaml").read_text().count("status: done"), 2)

    def test_close_rolls_back_on_render_failure(self):
        import roadmap
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            before_tasks = (root / "tasks.yaml").read_text()
            before_cfg = (root / ".waystone.yml").read_text()

            def boom(_root):
                raise RuntimeError("render exploded mid-commit")

            orig = roadmap.render
            roadmap.render = boom
            try:
                rc = round.close(root, "2026-06-19-z", done=["feat/alpha"], touched=["gate/beta"], commit="HEAD")
            finally:
                roadmap.render = orig
            self.assertEqual(rc, 1)
            # primary files restored; ROADMAP not left behind
            self.assertEqual((root / "tasks.yaml").read_text(), before_tasks)
            self.assertEqual((root / ".waystone.yml").read_text(), before_cfg)
            self.assertFalse((root / "ROADMAP.md").exists())

    def test_close_restores_generated_ssot_on_digest_failure(self):
        import ssot
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: x\nssot: SSOT.md\ngenerated_dir: docs/ssot\n"
                "state:\n  last_round_commit: null\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            (root / "SSOT.md").write_text("# Title\n\n## A\nalpha\n\n## B\nbeta\n")
            git(root, "add", "-A"); git(root, "commit", "-qm", "setup")
            ssot.regenerate(root)
            gen = root / "docs/ssot"
            v1_hash = (gen / ".hash").read_text()
            v1_digest = (gen / "DIGEST.md").read_text()
            # the SSOT changes during the round; close() will regenerate views, which then fails
            (root / "SSOT.md").write_text("# Title\n\n## A\nalpha2\n\n## B\nbeta\n\n## C\ngamma\n")
            git(root, "add", "-A"); git(root, "commit", "-qm", "ssot edit")

            def boom(_root):
                raise RuntimeError("SSOT regen exploded mid-commit")

            orig = ssot.regenerate
            ssot.regenerate = boom
            try:
                rc = round.close(root, "2026-06-19-z", done=["feat/alpha"], touched=[], commit="HEAD")
            finally:
                ssot.regenerate = orig
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
        bundle = {"head": "a" * 40, "base_sha": "b" * 40, "bodies": [], "reviews": [], "checks": [],
                  "merge_state": "", "state": "OPEN", "is_draft": False, "base": "main", "head_ref": "feat/x"}
        bundle["bodies"] = [{
            "body": review.emit_marker("review-cycle", {
                "cycle": 1, "target_sha": bundle["head"], "base_sha": bundle["base_sha"],
                "reviewers": ["codex", "gpt-5.5-pro"], "profile_fingerprint": None,
            }), "author": "owner", "at": "2026-07-15T00:00:00Z",
        }]
        calls = []

        def fake_file_at_ref(root, repo, path, ref):
            calls.append((path, ref))
            if path == ".waystone.yml":
                return STRICT_BASE if ref == bundle["base_sha"] else RELAXED_HEAD
            return TASKS  # tasks.yaml @ head

        saved = (review.resolve_repo, review.pr_bundle, review.file_at_ref, review._gh)
        review.resolve_repo = lambda root: "owner/repo"
        review.pr_bundle = lambda root, pr, repo=None: bundle
        review.file_at_ref = fake_file_at_ref
        review._gh = lambda root, *a: (0, "main")
        try:
            with tempfile.TemporaryDirectory() as d:
                # a local config must exist for the load_config fallback; the gate must ignore it
                # in favour of the base-SHA policy
                (Path(d) / ".waystone.yml").write_text("version: 1\nproject: x\nreview:\n  mode: pr\n")
                g = merge._gather(Path(d), 7)
        finally:
            review.resolve_repo, review.pr_bundle, review.file_at_ref, review._gh = saved
        # policy taken from the STRICT base, not the RELAXED head
        self.assertTrue(g["head_read_ok"])
        self.assertTrue(g["require_ci"])   # base = true (head said false)
        self.assertTrue(g["want_codex"])   # base lists codex (head dropped it)
        self.assertTrue(g["want_pro"])     # base lists gpt-5.5-pro (head dropped it)
        # the config was read at the base SHA; tasks at the head SHA
        self.assertIn((".waystone.yml", bundle["base_sha"]), calls)
        self.assertIn(("tasks.yaml", bundle["head"]), calls)
        self.assertNotIn((".waystone.yml", bundle["head"]), calls)

    def test_custom_named_macro_reviewer_is_mandatory(self):
        # a reviewer that isn't 'codex' and isn't named gpt/pro must still gate the merge
        BASE = ("version: 1\nproject: x\nreview:\n  mode: pr\n  reviewers: [codex, research-auditor]\n"
                "  require_ci: false\n  operators: [owner]\n  approvers: [owner]\n")
        bundle = {"head": "a" * 40, "base_sha": "b" * 40, "bodies": [], "reviews": [], "checks": [],
                  "merge_state": "", "state": "OPEN", "is_draft": False, "base": "main", "head_ref": "feat/x"}
        bundle["bodies"] = [{
            "body": review.emit_marker("review-cycle", {
                "cycle": 1, "target_sha": bundle["head"], "base_sha": bundle["base_sha"],
                "reviewers": ["codex", "research-auditor"], "profile_fingerprint": None,
            }), "author": "owner", "at": "2026-07-15T00:00:00Z",
        }]
        saved = (review.resolve_repo, review.pr_bundle, review.file_at_ref, review._gh)
        review.resolve_repo = lambda root: "owner/repo"
        review.pr_bundle = lambda root, pr, repo=None: bundle
        review.file_at_ref = lambda root, repo, path, ref: (BASE if path == ".waystone.yml"
                                                               else "version: 1\nproject: x\ntasks: []\n")
        review._gh = lambda root, *a: (0, "main")
        try:
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / ".waystone.yml").write_text("version: 1\nproject: x\nreview:\n  mode: pr\n")
                g = merge._gather(Path(d), 7)
        finally:
            review.resolve_repo, review.pr_bundle, review.file_at_ref, review._gh = saved
        self.assertTrue(g["want_pro"])  # research-auditor must be required, not name-guessed away

    def test_merge_gather_role_reviewer_uses_one_frozen_backend_list(self):
        head, base = "a" * 40, "b" * 40
        policy = common.normalize_config({
            "version": 1, "project": "x", "review": {
                "mode": "pr", "reviewers": ["role:reviewer"],
                "require_ci": False, "operators": ["owner"], "approvers": ["owner"],
            },
        })
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            state = common.ensure_project_state_dir(root)
            (state / "profile.yml").write_text(
                "schema: waystone-profile-1\nbindings:\n"
                "  reviewer: {execution: external-runner, backend: 'claude:opus'}\n")
            _profile, fingerprint = delegate._load_profile(root)
            bodies = [
                {"body": review.emit_marker("review-cycle", {
                    "cycle": 1, "target_sha": head, "base_sha": base,
                    "reviewers": ["claude:opus"], "profile_fingerprint": fingerprint,
                }), "author": "owner", "at": "2026-07-15T00:00:00Z"},
                {"body": review.emit_marker("review-result", {
                    "reviewer": "claude:opus", "review_cycle": 1,
                    "reviewed_sha": head, "verdict": "shipped", "decision_required": [],
                }), "author": "owner", "at": "2026-07-15T01:00:00Z"},
                {"body": review.emit_marker("findings", {"cycle": 1, "resolved": True}),
                 "author": "owner", "at": "2026-07-15T02:00:00Z"},
                {"body": review.emit_marker("approval", {
                    "sha": head, "cycle": 1, "base_sha": base, "by": "owner"}),
                 "author": "owner", "at": "2026-07-15T03:00:00Z"},
            ]
            bundle = {
                "head": head, "base_sha": base, "bodies": bodies, "reviews": [],
                "checks": [], "merge_state": "CLEAN", "state": "OPEN",
                "is_draft": False, "base": "main", "head_ref": "feat/x",
            }
            ctx = {"repo": "owner/repo", "bundle": bundle, "head": head,
                   "base_sha": base, "base": "main", "policy": policy}
            saved = review.pr_context, review.file_at_ref, review._gh
            review.pr_context = lambda _root, _pr: ctx
            review.file_at_ref = lambda *_args: "version: 1\nproject: x\ntasks: []\n"
            review._gh = lambda _root, *_args: (0, "main")
            try:
                facts = merge._gather(root, 7)
            finally:
                review.pr_context, review.file_at_ref, review._gh = saved
        self.assertEqual(facts["reviewers"], ["claude:opus"])
        self.assertFalse(facts["want_codex"])
        self.assertTrue(facts["want_pro"])
        self.assertTrue(merge.merge_gate(facts)[0], merge.merge_gate(facts)[1])


class IngestTests(unittest.TestCase):
    def _root(self, d):
        root = Path(d)
        (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
        return root

    def test_byte_exact_copy_and_consume(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            src = root / "inbox.md"
            # tricky bytes: CRLF, trailing spaces, multibyte utf-8, NO final newline
            body = "## Review\r\n  trailing   \nutf8: é한\nno final newline".encode("utf-8")
            src.write_bytes(body)
            rc = review.ingest(root, "2026-06-22-x", src=src, reviewer="gpt-5.5-pro")
            self.assertEqual(rc, 0)
            dest = root / "docs/reviews/2026-06-22-x-feedback.md"
            content = dest.read_bytes()
            self.assertIn(body, content)                     # body byte-exact, verbatim (within the file)
            # verbatim body sits between the header separator and the appended triage skeleton
            self.assertIn(body + b"\n\n---\n\n## Findings (triage skeleton", content)
            self.assertIn(b"round: 2026-06-22-x", content)
            self.assertIn(b"reviewer: gpt-5.5-pro", content)
            self.assertFalse(src.exists())                   # drop-file consumed

    def test_packet_feedback_identity_uses_resolved_reviewer_membership(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: x\nreview:\n  reviewers: [role:reviewer]\n")
            (common.ensure_project_state_dir(root) / "profile.yml").write_text(
                "schema: waystone-profile-1\nbindings:\n"
                "  reviewer: {execution: external-runner, backend: 'claude:review-model'}\n")
            configured = root / "configured.md"
            configured.write_bytes(b"configured review")
            self.assertEqual(review.ingest(
                root, "r1", src=configured, reviewer="claude:review-model"), 0)
            ad_hoc = root / "ad-hoc.md"
            ad_hoc.write_bytes(b"ad-hoc review")
            self.assertEqual(review.ingest(
                root, "r2", src=ad_hoc, reviewer="unconfigured:model"), 0)

            events, skipped = overlay.load_review_ingests(root)
            self.assertEqual(skipped, 0)
            by_round = {event["round_id"]: event for event in events}
            self.assertEqual(by_round["r1"]["reviewer"], "claude:review-model")
            self.assertIs(by_round["r1"]["reviewer_configured"], True)
            self.assertEqual(by_round["r2"]["reviewer"], "unconfigured:model")
            self.assertIsNone(by_round["r2"]["reviewer_configured"])

            fixed_events = [
                {**by_round["r1"], "at": "2026-07-15T01:00:00+00:00"},
                {**by_round["r2"], "at": "2026-07-15T03:00:00+00:00"},
            ]
            rounds = [
                {"round_id": "r1", "at": "2026-07-15T00:00:00+00:00",
                 "review_mode": "packet"},
                {"round_id": "r2", "at": "2026-07-15T02:00:00+00:00",
                 "review_mode": "packet"},
                {"round_id": "r3", "at": "2026-07-15T04:00:00+00:00",
                 "review_mode": "packet"},
            ]
            result = overlay.evaluate_review_skipped_closes(
                rounds, fixed_events, consecutive=2)
            self.assertEqual(result["fires"], ["r3"])
            self.assertIsNone(result["by_round"][-1]["feedback_observed"])
            self.assertEqual(result["unknown_reviewer_feedback"], 1)

    def test_missing_inbox_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            self.assertEqual(review.ingest(root, "2026-06-22-x", src=root / "nope.md"), 1)

    def test_empty_inbox_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            src = root / "inbox.md"; src.write_bytes(b"   \n\n")
            self.assertEqual(review.ingest(root, "2026-06-22-x", src=src), 1)
            self.assertTrue(src.exists())  # not consumed on failure

    def test_round_inferred_from_request(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            rdir = root / "docs/reviews"; rdir.mkdir(parents=True)
            (rdir / "2026-06-20-a-request.md").write_text("req")
            src = root / "inbox.md"; src.write_bytes(b"review body")
            self.assertEqual(review.ingest(root, None, src=src), 0)
            self.assertTrue((rdir / "2026-06-20-a-feedback.md").is_file())

    def test_packet_ingest_records_round_bound_request_sha_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            rdir = root / "docs" / "reviews"
            rdir.mkdir(parents=True)
            rid = "2026-06-20-a"
            (rdir / f"{rid}-request.md").write_text(
                f"# Review Request — {rid}\n\n"
                f"- Reviewing: {'a' * 40}   (diff against (root))\n")
            src = root / "inbox.md"
            src.write_bytes(b"review body")
            self.assertEqual(review.ingest(root, rid, src=src), 0)
            sidecars = list(rdir.glob(f"{rid}-request.binding*.json"))
            self.assertEqual(len(sidecars), 1)
            binding = _json.loads(sidecars[0].read_text())
            self.assertEqual(binding["round_id"], rid)
            self.assertEqual(binding["target_sha"], "a" * 40)
            self.assertIsNone(binding["base_sha"])

    def test_reingest_requires_force_and_preserves_source_on_refusal(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            first = root / "first.md"
            first.write_bytes(b"first review")
            self.assertEqual(review.ingest(root, "2026-06-22-x", src=first), 0)
            dest = root / "docs/reviews/2026-06-22-x-feedback.md"
            original = dest.read_bytes()

            second = root / "second.md"
            second.write_bytes(b"second review")
            self.assertEqual(review.ingest(root, "2026-06-22-x", src=second), 1)
            self.assertEqual(dest.read_bytes(), original)
            self.assertTrue(second.exists())
            self.assertEqual(
                review.ingest(root, "2026-06-22-x", src=second, force=True), 0)
            self.assertIn(b"second review", dest.read_bytes())
            self.assertNotIn(b"first review", dest.read_bytes())
            self.assertFalse(second.exists())

    def test_ingest_cli_forwards_force_flag(self):
        seen = {}
        original = review.ingest

        def fake(root, round_id, src=review.INBOX, reviewer=None, force=False):
            seen["force"] = force
            return 0

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            review.ingest = fake
            try:
                self.assertEqual(review.main(["ingest", "--force", str(root)]), 0)
            finally:
                review.ingest = original
        self.assertIs(seen["force"], True)

    def test_warn_failure_is_noticed_without_changing_ingest_exit(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            src = root / "inbox.md"
            src.write_bytes(b"review body")
            orig = overlay.evaluate_boundary
            overlay.evaluate_boundary = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("synthetic warn crash"))
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = review.ingest(root, "2026-06-22-x", src=src)
            finally:
                overlay.evaluate_boundary = orig
            self.assertEqual(rc, 0)
            self.assertIn("overlay warning", err.getvalue())
            self.assertIn("synthetic warn crash", err.getvalue())


class FrozenAcceptanceTests(unittest.TestCase):
    """The frozen v0.2 acceptance boundaries (GPT 6th review) — A: PR reducer, B: YAML mutation,
    C: closeout/views. Each test directly reproduces a defect that must stay closed."""
    HEAD, BASE = "a" * 40, "b" * 40

    def _cycle(self, at, base=None):
        f = {"cycle": 1, "target_sha": self.HEAD}
        if base:
            f["base_sha"] = base
        return {"body": review.emit_marker("review-cycle", f), "author": "owner", "at": at}

    # ---- A: PR review protocol reducer ----
    def test_a1_macro_result_before_freeze_rejected(self):
        bodies = [
            {"body": review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
                "reviewed_sha": self.HEAD, "verdict": "shipped", "decision_required": []}),
             "author": "owner", "at": "2026-06-20T00:00:00Z"},
            self._cycle("2026-06-20T02:00:00Z", self.BASE),  # freeze AFTER the result
        ]
        c = review.classify(review.parse_bodies(bodies), self.HEAD,
                               macro_reviewers=("gpt-5.5-pro",), operators=("owner",), current_base=self.BASE)
        self.assertFalse(c["pro_result_at_head"])

    def test_a1_approval_before_freeze_rejected(self):
        bodies = [
            {"body": review.emit_marker("approval", {"sha": self.HEAD, "base_sha": self.BASE, "cycle": 1, "by": "owner"}),
             "author": "owner", "at": "2026-06-20T00:00:00Z"},
            self._cycle("2026-06-20T02:00:00Z", self.BASE),  # freeze AFTER the approval
        ]
        c = review.classify(review.parse_bodies(bodies), self.HEAD,
                               approvers=("owner",), operators=("owner",), current_base=self.BASE)
        self.assertFalse(c["approved_at_head"])

    def test_a2_typed_marker_round_trip(self):
        s = review.emit_marker("review-result", {"reviewer": "r", "review_cycle": 2, "reviewed_sha": self.HEAD,
                                                    "verdict": "shipped", "decision_required": ["D-1", "D-2"]})
        m = review.parse_markers(s)[0]
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
            self.assertFalse(review.marker_valid(m), m)
        self.assertTrue(review.marker_valid(
            {"_kind": "findings", "cycle": 1, "resolved": True}))  # the well-typed control

    def test_a3_pending_review_body_not_parsed_as_marker(self):
        import json as _json
        marker = review.emit_marker("review-result", {"reviewer": "gpt-5.5-pro", "review_cycle": 1,
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

        orig = review._gh
        review._gh = fake_gh
        try:
            bundle = review.pr_bundle(Path("/x"), 1, "o/r")
        finally:
            review._gh = orig
        self.assertNotIn(marker, [b["body"] for b in bundle["bodies"]])  # review body is NOT a marker source
        self.assertEqual(review.parse_bodies(bundle["bodies"]), [])

    def test_a4_base_packet_policy_blocks_local_pr(self):
        BASE_PACKET = "version: 1\nproject: x\nreview:\n  mode: packet\n  reviewers: []\n"
        bundle = {"head": self.HEAD, "base_sha": self.BASE, "bodies": [], "reviews": [], "checks": [],
                  "merge_state": "", "state": "OPEN", "is_draft": False, "base": "main", "head_ref": "x"}
        saved = (review.resolve_repo, review.pr_bundle, review.file_at_ref, review._gh)
        review.resolve_repo = lambda root: "owner/repo"
        review.pr_bundle = lambda root, pr, repo=None: bundle
        review.file_at_ref = lambda root, repo, path, ref: (BASE_PACKET if path == ".waystone.yml"
                                                               else "version: 1\nproject: x\ntasks: []\n")
        review._gh = lambda root, *a: (0, "main")
        try:
            with tempfile.TemporaryDirectory() as d:
                # local config says pr — but the BASE policy (packet) is authoritative
                (Path(d) / ".waystone.yml").write_text("version: 1\nproject: x\nreview:\n  mode: pr\n")
                g = merge._gather(Path(d), 7)
        finally:
            review.resolve_repo, review.pr_bundle, review.file_at_ref, review._gh = saved
        self.assertEqual(g["policy_mode"], "packet")
        self.assertFalse(g["want_codex"])
        self.assertFalse(g["want_pro"])  # base packet/empty reviewers — local pr can't add reviewers

    # ---- B: structure-bounded YAML mutation ----
    def test_b1_decoy_task_outside_tasks_untouched(self):
        doc = ("metadata:\n  - id: feat/alpha\n    status: active\n"
               "tasks:\n  - id: feat/alpha\n    title: the real alpha task\n    status: active\n")
        out = round.set_task_field(doc, "feat/alpha", "status", "done")
        self.assertIn("metadata:\n  - id: feat/alpha\n    status: active", out)  # decoy untouched
        self.assertIn("    title: the real alpha task\n    status: done", out)   # real one edited

    def test_b1_duplicate_task_id_fails_closed(self):
        doc = "tasks:\n  - id: feat/x\n    status: active\n  - id: feat/x\n    status: active\n"
        with self.assertRaises(common.WorkflowError):
            round.set_task_field(doc, "feat/x", "status", "done")

    def test_b2_nested_state_not_mistaken_for_top_level(self):
        cfg = "foo:\n  state:\n    last_round_commit: decoy\nstate:\n  last_round_commit: real\n"
        out = round.set_config_scalar(cfg, "last_round_commit", "NEW", section="state")
        self.assertIn("    last_round_commit: decoy", out)  # nested decoy untouched
        self.assertIn("\nstate:\n  last_round_commit: NEW", out)  # top-level edited

    # ---- C: closeout transaction / generated-view validation ----
    def test_c1_library_raises_workflowerror_not_systemexit(self):
        import ssot
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\nssot: missing.md\n")
            with self.assertRaises(common.WorkflowError):  # NOT SystemExit (which slips rollbacks)
                ssot.regenerate(root)

    def test_c2_check_detects_missing_and_extra_views(self):
        import ssot
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: x\nssot: S.md\ngenerated_dir: docs/ssot\n")
            (root / "S.md").write_text("# T\n\n## A\nalpha\n")
            ssot.regenerate(root)
            self.assertEqual(ssot.check(root), 0)
            (root / "docs/ssot/DIGEST.md").unlink()            # missing view
            self.assertEqual(ssot.check(root), 3)
            ssot.regenerate(root)
            (root / "docs/ssot/sections/99-stale.md").write_text("stale")  # extra section
            self.assertEqual(ssot.check(root), 3)

    def test_c3_non_string_and_duplicate_deps_rejected(self):
        base = {"version": 1, "project": "p", "tasks": [
            {"id": "feat/foo", "title": "a properly explained task", "deps": [123]}]}
        self.assertTrue(any("dep" in e for e in validate.validate(base)))
        dup = {"version": 1, "project": "p", "tasks": [
            {"id": "feat/bar", "title": "another explained task", "deps": ["feat/foo", "feat/foo"]},
            {"id": "feat/foo", "title": "a properly explained task"}]}
        self.assertTrue(any("duplicate dep" in e for e in validate.validate(dup)))


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
            if "contents/.waystone.yml" in j:
                return (0, _b64.b64encode(POLICY.encode()).decode())
            if "contents/tasks.yaml" in j:
                return (0, _b64.b64encode(TASKS.encode()).decode())
            return (0, "")
        return gh

    def test_full_lifecycle_pass_then_refreeze_stale(self):
        import contextlib
        import io
        mk = review.emit_marker
        comments = [
            {"id": 1, "user": {"login": "owner"}, "updated_at": "2026-06-22T01:00:00Z",
             "body": mk("review-cycle", {
                 "cycle": 1, "target_sha": self.HEAD, "base_sha": self.BASE,
                 "reviewers": ["codex", "gpt-5.5-pro"], "profile_fingerprint": None})},
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

        orig = review._gh
        review._gh = self._gh(comments, reviews)
        try:
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / ".waystone.yml").write_text("version: 1\nproject: x\n")
                root = Path(d)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc_pass = merge.merge(root, 7, execute=False, method=None)
                    # re-freeze cycle 2 (same head/base, later) — every cycle-1 evidence must go stale
                    comments.append({"id": 5, "user": {"login": "owner"}, "updated_at": "2026-06-22T06:00:00Z",
                        "body": mk("review-cycle", {
                            "cycle": 2, "target_sha": self.HEAD, "base_sha": self.BASE,
                            "reviewers": ["codex", "gpt-5.5-pro"],
                            "profile_fingerprint": None})})
                    review._gh = self._gh(comments, reviews)
                    rc_stale = merge.merge(root, 7, execute=False, method=None)
        finally:
            review._gh = orig
        self.assertEqual(rc_pass, 0)    # full lifecycle → gate PASS (dry run)
        self.assertEqual(rc_stale, 3)   # after re-freeze, cycle-1 evidence is stale → BLOCKED


class TaskCliTests(unittest.TestCase):
    def test_render_list_filters(self):
        data = {"tasks": [
            {"id": "feat/a", "title": "alpha task here", "status": "active"},
            {"id": "fix/b", "title": "beta fix here", "status": "done"},
            {"id": "feat/c", "title": "gamma task here", "status": "pending"},
        ]}
        self.assertEqual(len(tasks.render_list(data)), 3)
        active = tasks.render_list(data, status="active")
        self.assertEqual(len(active), 1)
        self.assertIn("feat/a", active[0])
        feats = tasks.render_list(data, type_="feat")
        self.assertEqual({ln.split()[0] for ln in feats}, {"feat/a", "feat/c"})

    def test_show_missing_raises(self):
        with self.assertRaises(KeyError):
            tasks.render_show({"tasks": []}, "feat/x")

    def test_show_returns_record(self):
        data = {"tasks": [{"id": "feat/a", "title": "alpha task here", "status": "active"}]}
        out = tasks.render_show(data, "feat/a")
        self.assertIn("feat/a", out)
        self.assertIn("alpha task here", out)

    def test_add_appends_valid_block(self):
        out = tasks.append_task_block(TASKS_FIXTURE, {
            "id": "fix/gamma", "title": "a newly registered fix", "status": "pending",
            "severity": "major", "deps": ["feat/alpha"]})
        data = yaml.safe_load(out)
        self.assertEqual(validate.validate(data), [])
        self.assertIn("# registry — comments must be preserved", out)  # comment preserved
        self.assertEqual([t["id"] for t in data["tasks"]], ["feat/alpha", "gate/beta", "fix/gamma"])
        g = next(t for t in data["tasks"] if t["id"] == "fix/gamma")
        self.assertEqual(g["severity"], "major")
        self.assertEqual(g["deps"], ["feat/alpha"])

    def test_add_into_empty_tasks(self):
        out = tasks.append_task_block(
            "version: 1\nproject: x\ntasks: []\n", {"id": "feat/first", "title": "the very first task"})
        data = yaml.safe_load(out)
        self.assertEqual(validate.validate(data), [])
        self.assertEqual([t["id"] for t in data["tasks"]], ["feat/first"])

    def test_main_add_set_drop_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            self.assertEqual(tasks.main(["add", "fix/new", str(root), "--title", "a brand new fix task"]), 0)
            self.assertEqual(tasks.main(["set", "fix/new", "status", "active", str(root)]), 0)
            self.assertEqual(tasks.main(["drop", "gate/beta", str(root)]), 0)
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            byid = {t["id"]: t for t in data["tasks"]}
            self.assertEqual(byid["fix/new"]["status"], "active")
            self.assertEqual(byid["gate/beta"]["status"], "dropped")
            self.assertEqual(validate.validate(data), [])
            self.assertIn("# registry — comments must be preserved", (root / "tasks.yaml").read_text())

    def test_task_set_replaces_tasks_file_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "home"
            home.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            targets = []
            original_replace = common.os.replace

            def tracked_replace(src, dst):
                targets.append(Path(dst))
                return original_replace(src, dst)

            common.os.replace = tracked_replace
            try:
                rc = _run_with_home(home, lambda: tasks.main(
                    ["set", "feat/alpha", "status", "done", str(root)]))
            finally:
                common.os.replace = original_replace
            self.assertEqual(rc, 0)
            self.assertIn((root / "tasks.yaml").resolve(), [target.resolve() for target in targets])

    def test_main_add_rejects_invalid_id(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            before = (root / "tasks.yaml").read_text()
            self.assertEqual(tasks.main(["add", "P0", str(root), "--title", "a banned codename task"]), 2)
            self.assertEqual((root / "tasks.yaml").read_text(), before)  # fail-closed, nothing written

    def test_set_deps_repoints_and_extends(self):
        doc = ("version: 1\nproject: x\ntasks:\n"
               '  - id: feat/alpha\n    title: "base task alpha"\n    status: done\n'
               '  - id: feat/beta\n    title: "base task beta"\n    status: done\n'
               '  - id: feat/gamma\n    title: "gamma depends on alpha"\n    status: active\n    deps: [feat/alpha]\n')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(doc)
            # re-point gamma's dep alpha→beta (the list-field edit that was impossible before)
            self.assertEqual(tasks.main(["set", "feat/gamma", "deps", "feat/beta", str(root)]), 0)
            byid = {t["id"]: t for t in yaml.safe_load((root / "tasks.yaml").read_text())["tasks"]}
            self.assertEqual(byid["feat/gamma"]["deps"], ["feat/beta"])
            # extend to several ids, comma-separated (same convention as `add --deps`)
            self.assertEqual(tasks.main(["set", "feat/gamma", "deps", "feat/alpha,feat/beta", str(root)]), 0)
            byid = {t["id"]: t for t in yaml.safe_load((root / "tasks.yaml").read_text())["tasks"]}
            self.assertEqual(byid["feat/gamma"]["deps"], ["feat/alpha", "feat/beta"])
            # clear with an empty value
            self.assertEqual(tasks.main(["set", "feat/gamma", "deps", "", str(root)]), 0)
            byid = {t["id"]: t for t in yaml.safe_load((root / "tasks.yaml").read_text())["tasks"]}
            self.assertEqual(byid["feat/gamma"]["deps"], [])

    def test_set_deps_over_block_list_repoints(self):
        doc = ("version: 1\nproject: x\ntasks:\n"
               '  - id: feat/alpha\n    title: "base task alpha"\n    status: done\n'
               '  - id: feat/beta\n    title: "base task beta"\n    status: done\n'
               '  - id: feat/gamma\n    title: "gamma depends on alpha in block form"\n    status: active\n    deps:\n      - feat/alpha\n')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(doc)
            self.assertEqual(tasks.main(["set", "feat/gamma", "deps", "feat/beta", str(root)]), 0)
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
        self.assertEqual(tasks.select_for_archive(data, threshold=100, keep=10), [])

    def test_selects_old_terminal_keeps_recent(self):
        data = yaml.safe_load(_registry(20, 2))  # 22 tasks total
        ids = tasks.select_for_archive(data, threshold=10, keep=5)
        self.assertEqual(len(ids), 15)                       # 20 done − last 5 kept
        self.assertIn("fix/done-000", ids)                   # oldest archived
        self.assertNotIn("fix/done-019", ids)                # among the last 5 kept
        self.assertTrue(all(i.startswith("fix/done") for i in ids))  # never an active task

    def test_never_archives_terminal_depended_on_by_remaining(self):
        text = _registry(20, 0) + ("  - id: feat/live\n    title: \"a live task needing an old dep\"\n"
                                    "    status: active\n    deps: [fix/done-000]\n")
        data = yaml.safe_load(text)
        ids = tasks.select_for_archive(data, threshold=10, keep=5)
        self.assertNotIn("fix/done-000", ids)  # protected: a remaining task still depends on it

    def test_archive_main_moves_accumulates_and_stays_valid(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(_registry(20, 2))
            self.assertEqual(tasks.main(["archive", str(root), "--threshold", "10", "--keep", "5"]), 0)
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            self.assertEqual(validate.validate(data), [])
            self.assertEqual(len(data["tasks"]), 7)          # 5 kept done + 2 active
            arch = yaml.safe_load((root / "tasks.archive.yaml").read_text())
            self.assertEqual(len(arch["tasks"]), 15)
            # registry now has 7 tasks (< threshold 10): a second run is a clean no-op
            self.assertEqual(tasks.main(["archive", str(root), "--threshold", "10", "--keep", "5"]), 0)
            self.assertEqual(len(yaml.safe_load((root / "tasks.archive.yaml").read_text())["tasks"]), 15)


class ParkedTaskContractTests(unittest.TestCase):
    def test_registry_and_task_cli_accept_parked(self):
        parked = {"version": 1, "project": "x", "tasks": [{
            "id": "feat/parked-one", "title": "an intentionally parked task", "status": "parked",
        }]}
        self.assertEqual(validate.validate(parked), [])

        with tempfile.TemporaryDirectory() as d:
            import contextlib
            import io

            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            self.assertEqual(tasks.main([
                "add", "feat/parked-two", str(root), "--title", "another parked task",
                "--status", "parked",
            ]), 0)
            self.assertEqual(tasks.main([
                "set", "feat/alpha", "status", "parked", str(root),
            ]), 0)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(tasks.main([
                    "list", str(root), "--status", "parked",
                ]), 0)
            self.assertEqual(
                {line.split()[0] for line in out.getvalue().splitlines()},
                {"feat/alpha", "feat/parked-two"},
            )

    def test_parked_is_neither_actionable_nor_dependency_satisfying(self):
        data = {"tasks": [
            {"id": "feat/done", "title": "done", "status": "done"},
            {"id": "feat/parked", "title": "parked", "status": "parked",
             "deps": ["feat/done"]},
            {"id": "feat/waiting", "title": "waiting", "status": "pending",
             "deps": ["feat/parked"]},
            {"id": "feat/ready", "title": "ready", "status": "pending", "deps": []},
        ]}
        self.assertEqual(common.next_actionable(data), [("feat/ready", "ready")])

    def test_archive_selects_only_done_and_dropped(self):
        data = {"tasks": [
            {"id": "fix/done-one", "status": "done"},
            {"id": "fix/dropped-one", "status": "dropped"},
            {"id": "feat/parked-one", "status": "parked"},
            {"id": "feat/pending-one", "status": "pending"},
        ]}
        self.assertEqual(
            tasks.select_for_archive(data, threshold=0, keep=0),
            ["fix/done-one", "fix/dropped-one"],
        )

    def test_roadmap_and_dashboard_render_parked_distinctly(self):
        import contextlib
        import io

        data = {"version": 1, "project": "demo", "tasks": [
            {"id": "feat/parked-one", "title": "an intentionally parked task", "status": "parked"},
            {"id": "feat/pending-one", "title": "an ordinary pending task", "status": "pending"},
            {"id": "feat/blocked-one", "title": "a dependency blocked task", "status": "blocked"},
        ]}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            (root / "tasks.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
            rendered = roadmap.render(root)
        self.assertIn("classDef parked", rendered)
        self.assertIn("class feat_parked_one parked", rendered)
        self.assertIn("⏸ parked", rendered)
        self.assertIn("1 parked", rendered)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dashboard.render_tasks(data)
        status = out.getvalue()
        self.assertIn("⏸ parked", status)
        self.assertIn("⛔ blocked", status)
        self.assertIn("… 1 pending", status)

    def test_session_context_does_not_inject_parked_tasks(self):
        import contextlib
        import io
        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            home = Path(d) / "home"
            root.mkdir()
            home.mkdir()
            init_repo(root)
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks:\n"
                "  - id: decision/parked-one\n    title: an intentionally parked decision\n"
                "    status: parked\n"
                "  - id: feat/parked-two\n    title: an intentionally parked feature\n"
                "    status: parked\n"
                "  - id: feat/ready-one\n    title: an ordinary ready task\n"
                "    status: pending\n"
            )
            old_argv = sys.argv
            out = io.StringIO()
            try:
                sys.argv = ["session_context.py", str(root)]
                with contextlib.redirect_stdout(out):
                    self.assertEqual(_run_with_home(home, session_context.main), 0)
            finally:
                sys.argv = old_argv
        context = _json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("decision/parked-one", context)
        self.assertNotIn("feat/parked-two", context)
        self.assertIn("feat/ready-one", context)

    def test_public_docs_define_parked_contract(self):
        root = SCRIPTS.parent
        conventions = (root / "docs" / "CONVENTIONS.md").read_text()
        self.assertEqual(conventions, (root / "references" / "conventions.md").read_text())
        for phrase in ("`parked`", "intentionally deferred", "`notes`", "not actionable",
                       "not auto-archived"):
            self.assertIn(phrase, conventions)
        readme = (root / "README.md").read_text()
        self.assertIn("parked", readme)
        self.assertIn("intentionally deferred", readme)


class TaskReadNudgeTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import tasks_read_nudge
        self.nudge = tasks_read_nudge

    def test_denies_read_of_canonical_tasks_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            out = self.nudge.decide({"tool_name": "Read",
                                     "tool_input": {"file_path": str(root / "tasks.yaml")}})
            self.assertIsNotNone(out)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("waystone task", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_legacy_config_still_activates_nudge(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".jahns-workflow.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            out = self.nudge.decide({"tool_name": "Read",
                                     "tool_input": {"file_path": str(root / "tasks.yaml")}})
            self.assertIsNotNone(out)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_allows_other_files_and_tools(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
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
        out = tasks.append_task_block(self.NO_NL, {"id": "fix/added", "title": "an added fix task"})
        data = yaml.safe_load(out)
        self.assertEqual(validate.validate(data), [])
        byid = {t["id"]: t for t in data["tasks"]}
        self.assertEqual(byid["feat/last"]["status"], "active")  # not stolen by the inserted block
        self.assertIn("fix/added", byid)

    def test_set_last_field_no_trailing_newline_updates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(self.NO_NL)
            self.assertEqual(tasks.main(["set", "feat/last", "status", "done", str(root)]), 0)
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            self.assertEqual(data["tasks"][0]["status"], "done")  # actually updated, not a silent no-op

    def test_remove_last_task_no_trailing_newline(self):
        out = tasks.remove_task_blocks(
            self.NO_NL + '\n  - id: fix/tail\n    title: "the tail done task"\n    status: done',
            ["fix/tail"])
        data = yaml.safe_load(out)
        self.assertEqual([t["id"] for t in data["tasks"]], ["feat/last"])
        self.assertEqual(data["tasks"][0]["status"], "active")  # tail's status not re-parented onto it

    def test_add_preserves_crlf(self):
        base = ('version: 1\r\nproject: x\r\ntasks:\r\n'
                '  - id: feat/win\r\n    title: "a windows task"\r\n    status: active\r\n')
        out = tasks.append_task_block(base, {"id": "fix/win2", "title": "another windows task"})
        self.assertEqual(validate.validate(yaml.safe_load(out)), [])
        self.assertNotIn("\n", out.replace("\r\n", ""))  # no bare LF introduced

    def test_set_value_with_colon(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            self.assertEqual(tasks.main(["set", "feat/alpha", "notes", "blocked by X: see ticket 5", str(root)]), 0)
            data = {t["id"]: t for t in yaml.safe_load((root / "tasks.yaml").read_text())["tasks"]}
            self.assertEqual(data["feat/alpha"]["notes"], "blocked by X: see ticket 5")

    def test_set_invalid_value_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            before = (root / "tasks.yaml").read_text()
            self.assertEqual(tasks.main(["set", "feat/alpha", "status", "bogus", str(root)]), 2)
            self.assertEqual((root / "tasks.yaml").read_text(), before)

    def test_transitive_deps_protected(self):
        text = ("version: 1\nproject: x\ntasks:\n"
                '  - id: fix/leaf\n    title: "oldest done leaf task"\n    status: done\n'
                '  - id: fix/mid\n    title: "middle done task here"\n    status: done\n    deps: [fix/leaf]\n'
                '  - id: feat/top\n    title: "active task at the top"\n    status: active\n    deps: [fix/mid]\n')
        ids = tasks.select_for_archive(yaml.safe_load(text), threshold=3, keep=0)
        self.assertEqual(ids, [])  # mid pinned by top, leaf pinned transitively by mid → registry stays valid

    def test_recency_by_round_keeps_latest_closed(self):
        text = ("version: 1\nproject: x\ntasks:\n"
                '  - id: fix/early-file\n    title: "closed recently but early in file"\n    status: done\n    round: 2026-06-01-z\n'
                '  - id: fix/late-file\n    title: "closed long ago but late in file"\n    status: done\n    round: 2026-01-01-a\n')
        ids = tasks.select_for_archive(yaml.safe_load(text), threshold=2, keep=1)
        self.assertEqual(ids, ["fix/late-file"])  # earlier round archived despite later file position

    def test_negative_keep_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(_registry(20, 2))
            before = (root / "tasks.yaml").read_text()
            self.assertEqual(tasks.main(["archive", str(root), "--threshold", "10", "--keep", "-1"]), 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)

    def test_malformed_archive_file_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(_registry(20, 2))
            (root / "tasks.archive.yaml").write_text("just a string, not a registry\n")
            before = (root / "tasks.yaml").read_text()
            self.assertEqual(tasks.main(["archive", str(root), "--threshold", "10", "--keep", "5"]), 2)
            self.assertEqual((root / "tasks.yaml").read_text(), before)                       # live registry untouched
            self.assertEqual((root / "tasks.archive.yaml").read_text(), "just a string, not a registry\n")  # history preserved

    def test_symlinked_tasks_yaml_is_denied(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "real.yaml").write_text(TASKS_FIXTURE)
            (root / "tasks.yaml").symlink_to(root / "real.yaml")
            out = self.nudge.decide({"tool_name": "Read",
                                     "tool_input": {"file_path": str(root / "tasks.yaml")}})
            self.assertIsNotNone(out)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


# ============================================================ v0.7.0 M1: cclog / improve
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
    return cclog.parse_transcript_file(path, **defaults)


def _run_with_home(home: Path, fn, *, isolate_storage: bool = True):
    import os
    names = ("HOME", "CODEX_HOME", "WAYSTONE_HOME")
    before = {name: os.environ.get(name) for name in names}
    os.environ["HOME"] = str(home)
    if isolate_storage:
        os.environ["CODEX_HOME"] = str(home / ".codex")
        os.environ["WAYSTONE_HOME"] = str(home / ".waystone")
    try:
        return fn()
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


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
            g = [x for x in cclog.coalesce_messages(out["events"], out["tool_calls"])
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
        return cclog.detect_kind(parts)

    def _s(self, *parts):
        return cclog.scope_of(parts)

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
            kinds_all = sorted(k for _, _, k in improve.discover([src], set()))
            self.assertIn("main_transcript", kinds_all)
            self.assertIn("tool_result", kinds_all)
            only_a = improve.discover([src], {"slug-a"})
            self.assertEqual([k for _, _, k in only_a], ["main_transcript"])
            # a spurious project surfaces as zero transcripts, no special-casing
            only_b = improve.discover([src], {"slug-b"})
            self.assertEqual([k for _, _, k in only_b], ["tool_result"])

    def test_cli_user_wide_out_honors_home(self):
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
                    rc = improve.main(["trace", "--user-wide", "--source", str(src)])
                return rc

            rc = _run_with_home(home, run)
            self.assertEqual(rc, 0)
            out_dir = home / ".waystone" / "improve"
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
            improve.run_trace([src], set(), out)
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
            self.assertEqual(cov["parser_version"], cclog.PARSER_VERSION)

    def test_byte_identical_reruns(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "projects"
            src.mkdir()
            self._fixture(src)
            out1, out2 = Path(d) / "o1", Path(d) / "o2"
            improve.run_trace([src], set(), out1)
            improve.run_trace([src], set(), out2)
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
                "content": "<command-name>/waystone:improve</command-name>\n<command-args></command-args>"}}

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
            return improve.run_trace(list(sources), set(), out)

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
                self._bash("a5", "t5", "uv run waystone.py improve trace"),  # 5 excluded
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
                self._bash("a4", "t4", "uv run /x/waystone.py improve trace --out /tmp/o"),  # 4 ANCHOR
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
                self._u("u2", "I ran <command-name>/waystone:improve</command-name> earlier"),  # 2
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
            cov = {"parser_version": cclog.PARSER_VERSION, "files_skipped": 0,
                   "record_parse_errors": 0, "replayed_records_skipped": 0, "partial_tail_lines": 0,
                   "unknown_raw_types": {}, "row_totals": {"sessions": 0, "delegations": 0},
                   "self_session": {"session_id": _UUID, "file_found": True,
                                    "anchor": "command-tag", "lines_excluded": 3}}
            (d / "parse_coverage.json").write_text(_json.dumps(cov))
            facts = improve.run_audit(d)
            cc = [l for l in facts["lenses"] if l["lens"] == "coverage_caveats"][0]
            self.assertEqual(cc["summary"]["self_session"],
                             {"session_id": _UUID, "file_found": True,
                              "anchor": "command-tag", "lines_excluded": 3})


# feedback file exactly as review.ingest writes it: metadata header, byte-exact reviewer body
# (which itself contains `### JW-GPT-NNN` blocks + `- Severity:` lines we must NOT parse), then an
# APPENDED triage table under `## Findings (triage skeleton …)` — the only thing improve reviews reads.
_TRIAGE_FEEDBACK = """<!-- waystone feedback: verbatim body below; triage skeleton appended. -->
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
        (proj / ".waystone.yml").write_text("version: 1\nproject: a\n")  # reviews_dir=docs/reviews
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
            improve.run_reviews(registry, out)
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
            expected = {"id": "JW-GPT-001", "severity": "blocker", "status": "REAL",
                        "type": "unknown", "source": "triage", "provenance": "explicit",
                        "task_id": "fix/thing"}
            self.assertEqual({key: byid["JW-GPT-001"][key] for key in expected}, expected)
            self.assertEqual(byid["JW-GPT-001"]["review_origin"], "2026-07-01-alpha")
            self.assertTrue(byid["JW-GPT-001"]["source_pointer"].endswith(
                "2026-07-01-alpha-feedback.md:22"))
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
        findings = improve._parse_triage(_TRIAGE_FEEDBACK)
        self.assertEqual([f["id"] for f in findings], ["JW-GPT-001", "JW-GPT-002", "JW-GPT-003"])
        # a feedback body with NO appended skeleton yields nothing
        self.assertEqual(improve._parse_triage("just prose, no table\n### JW-GPT-9 — x"), [])

    def test_triage_type_column_accepts_taxonomy_and_preserves_unknown(self):
        feedback = """## Findings (triage skeleton v2)

| finding | severity | type | verdict | evidence | task id |
|---|---|---|---|---|---|
| JW-GPT-101 — typed | major | architecture | REAL | ev | fix/typed |
| JW-GPT-102 — blank | minor | | REJECTED | ev | |
| JW-GPT-103 — noncanonical | blocker | performance | NEEDS-RULING | ev | |
"""
        findings = improve._parse_triage(feedback)
        self.assertEqual(improve.FINDING_TYPES, (
            "correctness", "scope", "architecture", "verification",
            "reproducibility", "reporting",
        ))
        self.assertEqual([finding["type"] for finding in findings], [
            "architecture", "unknown", "unknown",
        ])
        self.assertEqual(findings[0]["task_id"], "fix/typed")
        self.assertEqual(findings[0]["status"], "REAL")

    def test_byte_identical_reruns(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            registry = self._fixture(d)
            o1, o2 = d / "o1", d / "o2"
            improve.run_reviews(registry, o1)
            improve.run_reviews(registry, o2)
            for name in ("reviews.jsonl", "reviews_coverage.json"):
                self.assertEqual((o1 / name).read_bytes(), (o2 / name).read_bytes(),
                                 f"{name} not byte-identical across re-runs")

    def test_cli_user_wide_out_honors_home(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            (home / ".waystone").mkdir(parents=True)
            # place the registry where the runtime path resolves it under the fake HOME
            reg = self._fixture(d)
            (home / ".waystone" / "projects.json").write_text(reg.read_text())

            def run():
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = improve.main(["reviews", "--user-wide"])
                return rc

            rc = _run_with_home(home, run)
            self.assertEqual(rc, 0)
            out_dir = home / ".waystone" / "improve"
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
        return {"parser_version": "waystone-trace-2", "generated_from": ["/x"],
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
            facts = improve.run_audit(d)
            self.assertEqual({item["lens"] for item in facts["skipped_lenses"]}, {
                "delegation_opportunity", "warn_friction", "worker_scope_drift",
            })
            lenses = {l["lens"]: l for l in facts["lenses"]}
            self.assertEqual(sorted(lenses), [
                "context_heavy", "coverage_caveats", "delegation_pattern", "env_unpreparedness",
                "error_landscape", "finding_concentration", "main_direct_work", "retry_loops",
                "review_association", "verification_debt"])
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
            facts = improve.run_audit(d)
            self.assertEqual({s["lens"] for s in facts["skipped_lenses"]},
                             {"coverage_caveats", "delegation_opportunity", "delegation_pattern",
                              "finding_concentration", "review_association", "warn_friction",
                              "worker_scope_drift"})
            self.assertEqual({l["lens"] for l in facts["lenses"]},
                             {"main_direct_work", "verification_debt", "retry_loops",
                              "context_heavy", "env_unpreparedness", "error_landscape"})
            self.assertEqual(facts["inputs"],
                             {"sessions": True, "delegations": False, "reviews": False,
                              "parse_coverage": False})

    def test_byte_identical_reruns(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self._write_inputs(d)
            improve.run_audit(d)
            first = (d / "facts.json").read_bytes()
            improve.run_audit(d)
            self.assertEqual(first, (d / "facts.json").read_bytes())


class ImproveDecideTests(unittest.TestCase):
    """Append-only user-decision log for improve recommendations (synthetic fixtures)."""

    def _lines(self, path: Path):
        return [_json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    @staticmethod
    def _user_wide(home: Path, argv: list[str]) -> int:
        return _run_with_home(home, lambda: improve.main([*argv, "--user-wide"]))

    def test_append_shape(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            out = home / ".waystone" / "improve" / "shape"
            rc = self._user_wide(home,
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
            home = Path(d) / "home"
            out = home / ".waystone" / "improve" / "optional"
            rc = self._user_wide(
                home, ["decide", "retry_loops/same-cmd", "reject", "--out", str(out)])
            self.assertEqual(rc, 0)
            rec = self._lines(out / "decisions.jsonl")[0]
            self.assertNotIn("title", rec)
            self.assertNotIn("note", rec)
            self.assertEqual(rec["decision"], "reject")

    def test_redecision_appends_history_latest_wins(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            out = home / ".waystone" / "improve" / "history"
            rid = "verification_debt/add-verify"
            self.assertEqual(self._user_wide(
                home, ["decide", rid, "reject", "--out", str(out)]), 0)
            self.assertEqual(self._user_wide(
                home, ["decide", rid, "accept", "--out", str(out)]), 0)
            lines = self._lines(out / "decisions.jsonl")
            self.assertEqual(len(lines), 2)  # both preserved (append-only history)
            self.assertEqual([l["decision"] for l in lines], ["reject", "accept"])
            self.assertTrue(all(l["rec_id"] == rid for l in lines))
            latest = [l for l in lines if l["rec_id"] == rid][-1]
            self.assertEqual(latest["decision"], "accept")  # latest row is the effective decision

    def test_missing_and_invalid_args(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            out = home / ".waystone" / "improve" / "invalid"
            # missing decision verb
            self.assertEqual(self._user_wide(
                home, ["decide", "main_direct_work/x", "--out", str(out)]), 1)
            # decision must be accept|reject
            self.assertEqual(self._user_wide(
                home, ["decide", "main_direct_work/x", "maybe", "--out", str(out)]), 1)
            # rec-id must be <lens>/<kebab-gist> (single slash)
            self.assertEqual(self._user_wide(
                home, ["decide", "noslash", "accept", "--out", str(out)]), 1)
            # no uppercase / non-kebab gist
            self.assertEqual(self._user_wide(
                home, ["decide", "Lens/Bad_Gist", "accept", "--out", str(out)]), 1)
            # gist may not end in a hyphen
            self.assertEqual(self._user_wide(
                home, ["decide", "lens/bad-", "accept", "--out", str(out)]), 1)
            # unknown flag rejected
            self.assertEqual(self._user_wide(
                home, ["decide", "lens/ok", "accept", "--bogus", "x", "--out", str(out)]), 1)
            # a precondition failure never creates the log
            self.assertFalse((out / "decisions.jsonl").exists())

    def test_out_override_and_home_default(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            (home / ".waystone").mkdir(parents=True)

            def run():
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    return improve.main(
                        ["decide", "context_heavy/trim", "accept", "--user-wide"])
            rc = _run_with_home(home, run)
            self.assertEqual(rc, 0)
            default_log = home / ".waystone" / "improve" / "decisions.jsonl"
            self.assertTrue(default_log.is_file())  # default --out honors HOME

            explicit = home / ".waystone" / "improve" / "elsewhere"
            rc2 = _run_with_home(home, lambda: improve.main(
                ["decide", "context_heavy/trim", "reject", "--out", str(explicit),
                 "--user-wide"]))
            self.assertEqual(rc2, 0)
            self.assertTrue((explicit / "decisions.jsonl").is_file())  # override lands elsewhere
            self.assertEqual(len(self._lines(default_log)), 1)  # default log untouched by the override


class ImproveMetricsTests(unittest.TestCase):
    """L3-1 G9: named metric groups, honest nulls, and longitudinal snapshots."""

    @staticmethod
    def _fixture(out: Path) -> None:
        out.mkdir(parents=True, exist_ok=True)
        _write_jsonl(out / "sessions.jsonl", [
            {"project": "demo", "kind": "main", "session_id": "s1",
             "tools": {"by_category": {"file_write": 1, "shell": 1}},
             "retry_loops": {"count": 0}, "context_heavy": {"max_result_bytes": 0},
             "usage": {"input": 10}},
            {"project": "demo", "kind": "main", "session_id": "s2",
             "tools": {"by_category": {"file_write": 5, "shell": 1}},
             "retry_loops": {"count": 0}, "context_heavy": {"max_result_bytes": 0},
             "usage": {"input": 10}},
        ])
        _write_jsonl(out / "delegations.jsonl", [
            {"project": "demo", "status": "completed"},
            {"project": "demo", "status": "failed"},
        ])
        _write_jsonl(out / "reviews.jsonl", [
            {"project": "demo", "round_id": "r1", "round_at": "2026-07-15T01:00:00Z",
             "findings": [{"id": "f1", "task_id": "fix/one", "type": "correctness",
                           "status": "REAL", "source": "triage", "fixing_rounds": ["fix-r1"],
                           "reopen_count": 1}]},
            {"project": "demo", "round_id": "r2", "round_at": "2026-07-15T04:00:00Z",
             "findings": [{"id": "f2", "task_id": "fix/two", "type": "correctness",
                           "status": "REAL", "source": "triage"}]},
        ])
        _write_jsonl(out / "evidence.jsonl", [
            {"project": "demo", "task_id": "fix/one",
             "task_context": {"session_id": "s1"},
             "findings": [{"round": "r1", "type": "correctness", "status": "REAL"}],
             "delegations": [{"did": "d1", "state": "applied"}],
             "acceptance": {"accepted_at": "2026-07-15T03:00:00Z", "resolved": True,
                            "provenance": "explicit"}},
            {"project": "demo", "task_id": "feat/opportunity",
             "task_context": {"session_id": "s2"}, "findings": [], "delegations": [],
             "acceptance": {"accepted_at": None, "resolved": False,
                            "provenance": "current-task-state-approximation"}},
            {"project": "demo", "task_id": "fix/env",
             "task_context": {"session_id": None}, "findings": [],
             "delegations": [{"did": "d2", "state": "failed-env"}],
             "acceptance": {"accepted_at": None, "resolved": False,
                            "provenance": "current-task-state-approximation"}},
            {"coverage": {"warning_observations": [{
                "project": "demo", "records": 0, "fire": 0, "conflict": 0,
                "by_rule": {}, "by_boundary": {}, "by_rule_boundary": {},
                "recent_rounds": [], "coverage": {"warnings_file_present": True},
            }]}}
        ])
        _write_jsonl(out / "evidence_warnings.jsonl", [
            {"project": "demo", "event": "evaluation", "rule": "env-manifest-mutation-v1",
             "context": {"fired": True}},
            {"project": "demo", "event": "evaluation", "rule": "done-without-evidence-v1",
             "context": {"fired": False}},
            {"project": "demo", "event": "conflict", "rule": "done-without-evidence-v1",
             "context": {}},
        ])
        (out / "adaptive_feedback.json").write_text(_json.dumps([{
            "project": "demo", "facts": {
                "deltas": [{
                    "before": {"fires": 1, "opportunities": 2,
                               "finding_recurrences": {"scope": 1}},
                    "after": {"fires": 1, "opportunities": 4,
                              "finding_recurrences": {"scope": 0}},
                }],
                "decision_follow_through": {"accepted": 1, "materialized": 1,
                                             "accepted_without_delta": 0,
                                             "rejected_decisions": 1},
                "coverage": {"accept_delta_conflicts": {}},
            },
        }]))
        _write_jsonl(out / "decisions.jsonl", [
            {"rec_id": "scope/accept", "decision": "accept", "at": "2026-07-15T01:00:00Z"},
            {"rec_id": "scope/reject", "decision": "reject", "at": "2026-07-15T02:00:00Z"},
        ])
        improve.run_audit(out, improve.PROJECT_LENS_SCOPE)

    @staticmethod
    def _metric(snapshot: dict, group: str, name: str) -> dict:
        return snapshot["metrics"][group][name]

    def test_four_groups_longitudinal_comparison_and_facts_bound(self):
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            self._fixture(out)
            first = improve.run_metrics(
                out, improve.PROJECT_LENS_SCOPE,
                now=datetime(2026, 7, 15, 5, tzinfo=timezone.utc))
            self.assertEqual(set(first["metrics"]), {
                "quality", "delegation_effectiveness",
                "reproducibility_environment", "governance",
            })
            recurrence = self._metric(first, "quality", "finding_recurrence_rate")
            self.assertEqual((recurrence["numerator"], recurrence["denominator"],
                              recurrence["value"]), (1, 2, 0.5))
            self.assertEqual(self._metric(first, "quality", "reopen_count")["value"], 1)
            self.assertEqual(
                self._metric(first, "quality", "post_acceptance_defect_rate")["value"], 1.0)
            self.assertEqual(
                self._metric(first, "delegation_effectiveness", "completion_rate")["value"], 0.5)
            self.assertEqual(
                self._metric(first, "delegation_effectiveness", "useful_artifact_rate")["value"],
                0.5)
            self.assertEqual(self._metric(
                first, "delegation_effectiveness",
                "opportunity_adjusted_useful_artifact_rate")["denominator"], 3)
            self.assertEqual(self._metric(
                first, "reproducibility_environment", "environment_failure_rate")["value"], 0.5)
            self.assertEqual(self._metric(
                first, "reproducibility_environment",
                "ad_hoc_manifest_mutation_fire_rate")["value"], 1.0)
            self.assertEqual(
                self._metric(first, "governance", "decision_rejection_rate")["value"], 0.5)
            self.assertEqual(self._metric(
                first, "governance", "rejected_materialization_suppression_rate")["value"], 1.0)

            reviews = [_json.loads(line) for line in
                       (out / "reviews.jsonl").read_text().splitlines()]
            reviews.append({
                "project": "demo", "round_id": "r3", "round_at": "2026-07-15T06:00:00Z",
                "findings": [{"id": "f3", "type": "correctness", "status": "REAL",
                              "source": "triage"}],
            })
            _write_jsonl(out / "reviews.jsonl", reviews)
            improve.run_audit(out, improve.PROJECT_LENS_SCOPE)
            second = improve.run_metrics(
                out, improve.PROJECT_LENS_SCOPE,
                now=datetime(2026, 7, 15, 7, tzinfo=timezone.utc))
            change = second["comparison"]["changes"]["quality.finding_recurrence_rate"]
            self.assertEqual(change, {"previous": 0.5, "current": 0.6667, "delta": 0.1667})
            self.assertEqual(len((out / "metrics.jsonl").read_text().splitlines()), 2)
            facts = _json.loads((out / "facts.json").read_text())
            self.assertLessEqual(len(facts["metrics"]["facts"]), 5)
            self.assertIs(facts["metrics"]["causal_claims"], False)

    def test_unavailable_is_null_with_reason_and_denominator(self):
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            out.joinpath("facts.json").write_text(_json.dumps({
                "generated_from": "fixture", "inputs": {}, "skipped_lenses": [], "lenses": [],
            }))
            snapshot = improve.run_metrics(
                out, improve.PROJECT_LENS_SCOPE,
                now=datetime(2026, 7, 15, tzinfo=timezone.utc))
            for group in snapshot["metrics"].values():
                for metric in group.values():
                    self.assertIn("denominator", metric)
                    self.assertIn("coverage", metric)
                    self.assertIn(metric["provenance"], ("observed", "inferred", "unavailable"))
                    self.assertIn("first_measured_version", metric)
                    if metric["value"] is None:
                        self.assertEqual(metric["provenance"], "unavailable")
                        self.assertTrue(metric["unavailable_reason"])
            for name in ("waiver_rate", "hard_block_rate"):
                metric = self._metric(snapshot, "governance", name)
                self.assertIsNone(metric["value"])
                self.assertEqual(metric["unavailable_reason"], "enforce arc not shipped")
            self.assertIsNone(self._metric(
                snapshot, "reproducibility_environment", "acceptance_reproducibility")["value"])

    def test_raw_reviews_and_sessions_do_not_replace_uncomputed_lenses(self):
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            out.joinpath("facts.json").write_text(_json.dumps({
                "generated_from": "fixture", "inputs": {}, "skipped_lenses": [], "lenses": [],
            }))
            _write_jsonl(out / "sessions.jsonl", [{
                "project": "demo", "kind": "main", "session_id": "main",
                "tools": {"by_category": {"file_write": 8, "shell": 5}},
                "retry_loops": {"count": 4},
                "context_heavy": {"tool_results_over_100kb": 2,
                                  "max_result_bytes": 200000},
                "usage": {"input": 123456},
            }])
            _write_jsonl(out / "reviews.jsonl", [
                {"project": "demo", "round_id": "r1", "findings": [
                    {"id": "report-1", "status": "REAL", "type": "reporting"}]},
                {"project": "demo", "round_id": "r2", "findings": [
                    {"id": "report-2", "status": "REAL", "type": "reporting"}]},
            ])
            snapshot = improve.run_metrics(
                out, improve.PROJECT_LENS_SCOPE,
                now=datetime(2026, 7, 15, tzinfo=timezone.utc))
            for group, name in (
                ("quality", "report_grounding_finding_trend"),
                ("delegation_effectiveness", "main_direct_work"),
                ("delegation_effectiveness", "main_context_inflow"),
                ("delegation_effectiveness", "blind_retry_count"),
                ("delegation_effectiveness", "worker_retry_context_load"),
            ):
                metric = self._metric(snapshot, group, name)
                self.assertIsNone(metric["value"])
                self.assertEqual(metric["unavailable_reason"], "lens-not-computed")
            duplicate = self._metric(
                snapshot, "delegation_effectiveness", "worker_duplicate_exploration")
            self.assertIsNone(duplicate["value"])
            self.assertEqual(
                duplicate["unavailable_reason"],
                "inter-worker overlap source unavailable")

    def test_remaining_metrics_snapshot_existing_taxonomy_and_lens_values(self):
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            sessions = [
                {"project": "demo", "kind": "main", "session_id": "main",
                 "tools": {"by_category": {"file_write": 3, "shell": 2}},
                 "retry_loops": {"count": 1},
                 "context_heavy": {"tool_results_over_100kb": 0, "max_result_bytes": 10},
                 "usage": {"input": 0}},
                {"project": "demo", "kind": "subagent", "session_id": "worker-1",
                 "tools": {"by_category": {}}, "retry_loops": {"count": 2},
                 "context_heavy": {"tool_results_over_100kb": 3,
                                   "max_result_bytes": 150000}},
                {"project": "demo", "kind": "workflow_subagent", "session_id": "worker-2",
                 "tools": {"by_category": {}}, "retry_loops": {"count": 0},
                 "context_heavy": {"tool_results_over_100kb": 1,
                                   "max_result_bytes": 200000}},
            ]
            _write_jsonl(out / "sessions.jsonl", sessions)
            _write_jsonl(out / "reviews.jsonl", [
                {"project": "demo", "round_id": "r1", "findings": [
                    {"id": "report-1", "status": "REAL", "type": "reporting"},
                    {"id": "report-2", "status": "REAL", "type": "reporting"},
                    {"id": "rejected", "status": "REJECTED", "type": "reporting"},
                ]},
                {"project": "demo", "round_id": "r2", "findings": [
                    {"id": "report-3", "status": "REAL", "type": "reporting"},
                ]},
            ])
            improve.run_audit(out, improve.PROJECT_LENS_SCOPE)
            first = improve.run_metrics(
                out, improve.PROJECT_LENS_SCOPE,
                now=datetime(2026, 7, 15, tzinfo=timezone.utc))
            self.assertEqual(self._metric(
                first, "quality", "report_grounding_finding_trend")["value"],
                {"first": 2, "last": 1, "delta": -1})
            duplicate = self._metric(
                first, "delegation_effectiveness", "worker_duplicate_exploration")
            self.assertIsNone(duplicate["value"])
            self.assertEqual(
                duplicate["unavailable_reason"],
                "inter-worker overlap source unavailable")
            self.assertEqual(self._metric(
                first, "delegation_effectiveness", "worker_retry_context_load")["value"], {
                    "retry_loops": {"sessions_with_retry": 1, "retry_loops_total": 2},
                    "context_heavy": {"sessions_over_100kb": 2,
                                      "results_over_100kb_total": 4,
                                      "max_result_bytes": 200000},
                })
            self.assertEqual(self._metric(
                first, "delegation_effectiveness", "blind_retry_count")["value"], 3)

            sessions[1]["retry_loops"]["count"] = 0
            _write_jsonl(out / "sessions.jsonl", sessions)
            second = improve.run_metrics(
                out, improve.PROJECT_LENS_SCOPE,
                now=datetime(2026, 7, 16, tzinfo=timezone.utc))
            self.assertEqual(second["comparison"]["changes"][
                "delegation_effectiveness.blind_retry_count"], {
                    "previous": 3, "current": 3, "delta": 0,
                })

            improve.run_audit(out, improve.PROJECT_LENS_SCOPE)
            third = improve.run_metrics(
                out, improve.PROJECT_LENS_SCOPE,
                now=datetime(2026, 7, 17, tzinfo=timezone.utc))
            self.assertEqual(third["comparison"]["changes"][
                "delegation_effectiveness.blind_retry_count"], {
                    "previous": 3, "current": 1, "delta": -2,
                })

            improve.run_audit(out, improve.USER_HABIT_LENS_SCOPE)
            user_wide = improve.run_metrics(
                out, improve.USER_HABIT_LENS_SCOPE,
                now=datetime(2026, 7, 18, tzinfo=timezone.utc))
            self.assertEqual(self._metric(
                user_wide, "delegation_effectiveness", "main_direct_work")["value"], 5)

    def test_same_inputs_and_clock_are_byte_stable(self):
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as d:
            left, right = Path(d) / "left", Path(d) / "right"
            self._fixture(left)
            self._fixture(right)
            now = datetime(2026, 7, 15, 5, tzinfo=timezone.utc)
            improve.run_metrics(left, improve.PROJECT_LENS_SCOPE, now=now)
            improve.run_metrics(right, improve.PROJECT_LENS_SCOPE, now=now)
            self.assertEqual((left / "metrics.jsonl").read_bytes(),
                             (right / "metrics.jsonl").read_bytes())

    def test_cli_user_wide_uses_machine_improve_residence(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            out = home / ".waystone" / "improve"
            out.mkdir(parents=True)
            (out / "facts.json").write_text(_json.dumps({
                "generated_from": "fixture", "inputs": {}, "skipped_lenses": [], "lenses": [],
            }))

            def run():
                with contextlib.redirect_stdout(io.StringIO()):
                    return improve.main(["metrics", "--user-wide"])

            self.assertEqual(_run_with_home(home, run), 0)
            snapshot = _json.loads((out / "metrics.jsonl").read_text().splitlines()[0])
            self.assertEqual(snapshot["scope"], improve.USER_HABIT_LENS_SCOPE)


class ImproveScopeTests(unittest.TestCase):
    """0.9.0-a C3: project-first improve storage, scope isolation, and user-wide opt-in."""

    @staticmethod
    def _project(root: Path, name: str) -> None:
        root.mkdir(parents=True)
        (root / ".waystone.yml").write_text(f"version: 1\nproject: {name}\n")
        (root / "tasks.yaml").write_text(f"version: 1\nproject: {name}\ntasks: []\n")
        reviews = root / "docs" / "reviews"
        reviews.mkdir(parents=True)
        (reviews / f"2026-07-15-{name}-request.md").write_text(f"# {name}\n")

    @staticmethod
    def _claude_slug(root: Path) -> str:
        import re
        return re.sub(r"[^A-Za-z0-9]", "-", str(root.resolve()))

    @staticmethod
    def _claude_session(source: Path, root: Path, session_id: str) -> None:
        project = source / ImproveScopeTests._claude_slug(root)
        project.mkdir(parents=True, exist_ok=True)
        _write_jsonl(project / f"{session_id}.jsonl", [
            {"type": "user", "uuid": f"u-{session_id}", "cwd": str(root),
             "message": {"role": "user", "content": "work"}},
        ])

    @staticmethod
    def _codex_session(source: Path, root: Path, session_id: str) -> None:
        _write_jsonl(source / f"rollout-2026-07-15T00-00-00-{session_id}.jsonl", [
            {"timestamp": "2026-07-15T00:00:00Z", "type": "session_meta", "payload": {
                "id": session_id, "cwd": str(root), "thread_source": "user"}},
            {"timestamp": "2026-07-15T00:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "work"}]}},
        ])

    @staticmethod
    def _run(home: Path, cwd: Path, argv: list[str], *, dispatcher: bool = False,
             waystone_home: Path | None = None, stderr=None) -> int:
        import contextlib
        import io
        import os
        import waystone

        previous = Path.cwd()
        previous_waystone_home = os.environ.get("WAYSTONE_HOME")
        try:
            os.chdir(cwd)
            if waystone_home is not None:
                os.environ["WAYSTONE_HOME"] = str(waystone_home)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    stderr if stderr is not None else io.StringIO()):
                entry = (lambda: waystone.main(["improve", *argv])) if dispatcher else (
                    lambda: improve.main(argv))
                return _run_with_home(home, entry, isolate_storage=waystone_home is None)
        finally:
            os.chdir(previous)
            if previous_waystone_home is None:
                os.environ.pop("WAYSTONE_HOME", None)
            else:
                os.environ["WAYSTONE_HOME"] = previous_waystone_home

    @staticmethod
    def _rows(path: Path) -> list[dict]:
        return [_json.loads(line) for line in path.read_text().splitlines() if line]

    def _fixture(self, directory: str) -> tuple[Path, Path, Path, Path]:
        base = Path(directory)
        home = base / "home"
        home.mkdir()
        alpha, beta = base / "alpha", base / "beta"
        self._project(alpha, "alpha")
        self._project(beta, "beta")
        registry = home / ".waystone" / "projects.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(_json.dumps({"projects": [
            {"name": "alpha", "path": str(alpha)},
            {"name": "beta", "path": str(beta)},
        ]}))
        return home, alpha, beta, registry

    def test_project_default_filters_claude_and_keeps_outputs_and_decisions_local(self):
        with tempfile.TemporaryDirectory() as d:
            home, alpha, beta, _registry = self._fixture(d)
            source = Path(d) / "claude-projects"
            self._claude_session(source, alpha, "11111111-1111-1111-1111-111111111111")
            self._claude_session(source, beta, "22222222-2222-2222-2222-222222222222")
            machine = home / ".waystone" / "improve"
            machine.mkdir()
            sentinel = machine / "sentinel"
            sentinel.write_text("legacy-user-wide")

            self.assertEqual(self._run(
                home, alpha, ["trace", "--source", str(source), "--host", "claude"],
                dispatcher=True), 0)
            project_out = alpha / ".waystone" / "improve"
            rows = self._rows(project_out / "sessions.jsonl")
            self.assertEqual([row["project"] for row in rows], ["alpha"])
            self.assertEqual((alpha / ".waystone" / ".gitignore").read_text(), "*\n")
            self.assertEqual(sentinel.read_text(), "legacy-user-wide")
            self.assertEqual(set(machine.iterdir()), {sentinel})

            self.assertEqual(self._run(
                home, alpha, ["decide", "verification_debt/add-tests", "accept"]), 0)
            self.assertTrue((project_out / "decisions.jsonl").is_file())
            self.assertFalse((machine / "decisions.jsonl").exists())

    def test_project_default_filters_codex_by_current_root(self):
        with tempfile.TemporaryDirectory() as d:
            home, alpha, beta, _registry = self._fixture(d)
            source = Path(d) / "codex-sessions"
            source.mkdir()
            self._codex_session(source, alpha, "33333333-3333-3333-3333-333333333333")
            self._codex_session(source, beta, "44444444-4444-4444-4444-444444444444")

            self.assertEqual(self._run(
                home, alpha, ["trace", "--source", str(source), "--host", "codex"]), 0)
            rows = self._rows(alpha / ".waystone" / "improve" / "sessions.jsonl")
            self.assertEqual([row["project"] for row in rows], [alpha.name])

    def test_project_codex_filter_excludes_same_basename_other_root(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            home = base / "home"
            home.mkdir()
            target, other = base / "a" / "repo", base / "b" / "repo"
            self._project(target, "target")
            self._project(other, "other")
            source = base / "codex-sessions"
            source.mkdir()
            self._codex_session(source, target, "10101010-1010-1010-1010-101010101010")
            self._codex_session(source, other, "20202020-2020-2020-2020-202020202020")

            self.assertEqual(self._run(
                home, target, ["trace", "--source", str(source), "--host", "codex"]), 0)
            rows = self._rows(target / ".waystone" / "improve" / "sessions.jsonl")
            self.assertEqual([row["cwd"] for row in rows], [str(target)])

    def test_project_claude_filter_excludes_punctuation_slug_collision(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            home = base / "home"
            home.mkdir()
            target, other = base / "foo-bar", base / "foo_bar"
            self._project(target, "target")
            self._project(other, "other")
            self.assertEqual(self._claude_slug(target), self._claude_slug(other))
            source = base / "claude-projects"
            self._claude_session(source, target, "30303030-3030-3030-3030-303030303030")
            self._claude_session(source, other, "40404040-4040-4040-4040-404040404040")

            self.assertEqual(self._run(
                home, target, ["trace", "--source", str(source), "--host", "claude"]), 0)
            rows = self._rows(target / ".waystone" / "improve" / "sessions.jsonl")
            self.assertEqual([row["cwd"] for row in rows], [str(target)])

    def test_project_codex_filter_includes_session_started_in_subdirectory(self):
        with tempfile.TemporaryDirectory() as d:
            home, alpha, _beta, _registry = self._fixture(d)
            nested = alpha / "src" / "feature"
            nested.mkdir(parents=True)
            source = Path(d) / "codex-sessions"
            source.mkdir()
            self._codex_session(source, nested, "50505050-5050-5050-5050-505050505050")

            self.assertEqual(self._run(
                home, alpha, ["trace", "--source", str(source), "--host", "codex"]), 0)
            rows = self._rows(alpha / ".waystone" / "improve" / "sessions.jsonl")
            self.assertEqual([row["cwd"] for row in rows], [str(nested)])

    def test_project_filters_include_registered_alias_cwds_for_both_hosts(self):
        with tempfile.TemporaryDirectory() as d:
            home, alpha, _beta, registry = self._fixture(d)
            alias = Path(d) / "alpha-old-checkout"
            alias_nested = alias / "src"
            data = _json.loads(registry.read_text())
            data["projects"][0]["aliases"] = [str(alias.resolve())]
            registry.write_text(_json.dumps(data))

            claude_source = Path(d) / "claude-projects"
            self._claude_session(
                claude_source, alias_nested,
                "51515151-5151-5151-5151-515151515151")
            self.assertEqual(self._run(
                home, alpha, ["trace", "--source", str(claude_source), "--host", "claude"]), 0)
            rows = self._rows(alpha / ".waystone" / "improve" / "sessions.jsonl")
            self.assertEqual([row["cwd"] for row in rows], [str(alias_nested)])

            codex_source = Path(d) / "codex-sessions"
            codex_source.mkdir()
            self._codex_session(
                codex_source, alias_nested,
                "52525252-5252-5252-5252-525252525252")
            self.assertEqual(self._run(
                home, alpha, ["trace", "--source", str(codex_source), "--host", "codex"]), 0)
            rows = self._rows(alpha / ".waystone" / "improve" / "sessions.jsonl")
            self.assertEqual([row["cwd"] for row in rows], [str(alias_nested)])

    def test_project_filter_excludes_unknown_cwd_and_records_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            home, alpha, _beta, _registry = self._fixture(d)
            source = Path(d) / "claude-projects"
            project = source / self._claude_slug(alpha)
            project.mkdir(parents=True)
            session = project / "60606060-6060-6060-6060-606060606060.jsonl"
            _write_jsonl(session, [{
                "type": "user", "uuid": "u-missing-cwd",
                "message": {"role": "user", "content": "work"},
            }])

            self.assertEqual(self._run(
                home, alpha, ["trace", "--source", str(source), "--host", "claude"]), 0)
            out = alpha / ".waystone" / "improve"
            self.assertEqual(self._rows(out / "sessions.jsonl"), [])
            coverage = _json.loads((out / "parse_coverage.json").read_text())
            self.assertEqual(coverage["project_filter"], {
                "project_root": str(alpha.resolve()),
                "sessions_excluded": {
                    "cwd_outside_project": [],
                    "cwd_unknown": [str(session.resolve())],
                },
            })

    def test_user_wide_scans_all_projects_and_never_touches_project_improve(self):
        with tempfile.TemporaryDirectory() as d:
            home, alpha, beta, _registry = self._fixture(d)
            source = Path(d) / "claude-projects"
            self._claude_session(source, alpha, "55555555-5555-5555-5555-555555555555")
            self._claude_session(source, beta, "66666666-6666-6666-6666-666666666666")
            project_out = alpha / ".waystone" / "improve"
            project_out.mkdir(parents=True)
            sentinel = project_out / "sentinel"
            sentinel.write_text("project-only")
            legacy = (home / ".claude" / "waystone.pre-0.9" / "start_here" /
                      f"{common._project_slug(alpha)}.md")
            legacy.parent.mkdir(parents=True)
            legacy.write_text("must-stay-user-wide-ignored")

            self.assertEqual(self._run(home, alpha, [
                "trace", "--user-wide", "--source", str(source), "--host", "claude"],
                dispatcher=True), 0)
            machine = home / ".waystone" / "improve"
            rows = self._rows(machine / "sessions.jsonl")
            self.assertEqual(
                {row["project"] for row in rows},
                {self._claude_slug(alpha), self._claude_slug(beta)},
            )
            self.assertEqual(self._run(home, alpha, [
                "decide", "main_direct_work/delegate-more", "accept", "--user-wide"]), 0)
            self.assertTrue((machine / "decisions.jsonl").is_file())
            self.assertEqual(set(project_out.iterdir()), {sentinel})
            self.assertEqual(sentinel.read_text(), "project-only")
            self.assertTrue(legacy.is_file())
            self.assertFalse((alpha / ".waystone" / "start-here.md").exists())

    def test_review_and_evidence_sources_follow_mode_scope(self):
        with tempfile.TemporaryDirectory() as d:
            home, alpha, _beta, _registry = self._fixture(d)
            for subcommand in ("reviews", "evidence"):
                self.assertEqual(self._run(home, alpha, [subcommand]), 0)
            project_out = alpha / ".waystone" / "improve"
            self.assertEqual(
                _json.loads((project_out / "reviews_coverage.json").read_text())["projects_scanned"],
                ["alpha"],
            )
            self.assertEqual(
                self._rows(project_out / "evidence.jsonl")[-1]["coverage"]["projects_scanned"],
                ["alpha"],
            )

            for subcommand in ("reviews", "evidence"):
                self.assertEqual(self._run(home, alpha, [subcommand, "--user-wide"]), 0)
            machine = home / ".waystone" / "improve"
            self.assertEqual(
                _json.loads((machine / "reviews_coverage.json").read_text())["projects_scanned"],
                ["alpha", "beta"],
            )
            self.assertEqual(
                self._rows(machine / "evidence.jsonl")[-1]["coverage"]["projects_scanned"],
                ["alpha", "beta"],
            )

    def test_audit_lens_classification_and_scope_data_are_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            home, alpha, _beta, _registry = self._fixture(d)
            project_out = alpha / ".waystone" / "improve"
            machine_out = home / ".waystone" / "improve"
            project_out.mkdir(parents=True)
            machine_out.mkdir(parents=True)
            project_sessions = [{
                "project": "alpha", "kind": "main", "session_id": "a", "file": "/a",
                "tools": {"by_category": {}}, "delegations": 0, "verification": {"runs": 0},
                "build": {"runs": 0}, "retry_loops": {"count": 0}, "context_heavy": {},
                "errors": {},
            }]
            _write_jsonl(project_out / "sessions.jsonl", project_sessions)
            _write_jsonl(machine_out / "sessions.jsonl", [
                *project_sessions, {**project_sessions[0], "project": "beta", "session_id": "b"},
            ])
            _write_jsonl(project_out / "delegations.jsonl", [])
            _write_jsonl(machine_out / "delegations.jsonl", [])
            _write_jsonl(project_out / "reviews.jsonl", [])
            _write_jsonl(machine_out / "reviews.jsonl", [])
            coverage = {"row_totals": {"sessions": 1, "delegations": 0}}
            (project_out / "parse_coverage.json").write_text(_json.dumps(coverage))
            (machine_out / "parse_coverage.json").write_text(_json.dumps(coverage))

            self.assertEqual(self._run(home, alpha, ["audit"]), 0)
            self.assertEqual(self._run(home, alpha, ["audit", "--user-wide"]), 0)
            project_facts = _json.loads((project_out / "facts.json").read_text())
            user_facts = _json.loads((machine_out / "facts.json").read_text())
            project_lenses = {lens["lens"]: lens for lens in project_facts["lenses"]}
            user_lenses = {lens["lens"]: lens for lens in user_facts["lenses"]}
            self.assertEqual(improve.LENS_SCOPES, {
                "main_direct_work": frozenset({"user-habit"}),
                "verification_debt": frozenset({"project"}),
                "retry_loops": frozenset({"project", "user-habit"}),
                "context_heavy": frozenset({"project", "user-habit"}),
                "delegation_pattern": frozenset({"user-habit"}),
                "delegation_opportunity": frozenset({"project"}),
                "worker_scope_drift": frozenset({"project"}),
                "warn_friction": frozenset({"project"}),
                "adaptive_feedback": frozenset({"project"}),
                "error_landscape": frozenset({"project"}),
                "env_unpreparedness": frozenset({"project"}),
                "review_association": frozenset({"project"}),
                "finding_concentration": frozenset({"project"}),
                "coverage_caveats": frozenset({"project", "user-habit"}),
                "evidence_link": frozenset({"project"}),
            })
            self.assertEqual(project_facts["scope"], "project")
            self.assertEqual(user_facts["scope"], "user-habit")
            self.assertEqual(set(project_lenses), {
                name for name, scopes in improve.LENS_SCOPES.items() if "project" in scopes
                and name not in {"delegation_opportunity", "evidence_link", "warn_friction",
                                 "worker_scope_drift", "adaptive_feedback"}
            })
            self.assertEqual(set(user_lenses), {
                name for name, scopes in improve.LENS_SCOPES.items() if "user-habit" in scopes
                and name != "evidence_link"
            })
            for lens in set(project_lenses) & set(user_lenses):
                if lens == "coverage_caveats":
                    continue
                self.assertEqual(set(project_lenses[lens]["per_project"]), {"alpha"})
                self.assertEqual(set(user_lenses[lens]["per_project"]), {"alpha", "beta"})

    def test_residence_guard_rejects_cross_scope_out_and_in(self):
        with tempfile.TemporaryDirectory() as d:
            home, alpha, _beta, _registry = self._fixture(d)
            source = Path(d) / "claude-projects"
            self._claude_session(source, alpha, "77777777-7777-7777-7777-777777777777")
            project_out = alpha / ".waystone" / "improve"
            machine_out = home / ".waystone" / "improve"

            self.assertEqual(self._run(home, alpha, [
                "trace", "--source", str(source), "--host", "claude",
                "--out", str(machine_out / "project-attempt")]), 1)
            self.assertEqual(self._run(home, alpha, [
                "trace", "--user-wide", "--source", str(source), "--host", "claude",
                "--out", str(project_out / "user-attempt")]), 1)
            self.assertEqual(self._run(home, alpha, [
                "audit", "--in", str(machine_out)]), 1)
            self.assertEqual(self._run(home, alpha, [
                "audit", "--user-wide", "--in", str(project_out)]), 1)
            self.assertFalse((machine_out / "project-attempt").exists())
            self.assertFalse((project_out / "user-attempt").exists())

    def test_improve_rejects_waystone_home_equal_to_project_state(self):
        import io

        with tempfile.TemporaryDirectory() as d:
            home, alpha, _beta, _registry = self._fixture(d)
            err = io.StringIO()
            rc = self._run(
                home, alpha, ["decide", "context_heavy/trim", "accept"],
                waystone_home=alpha / ".waystone", stderr=err,
            )
            self.assertEqual(rc, 1)
            self.assertIn("WAYSTONE_HOME", err.getvalue())
            self.assertIn("outside the project", err.getvalue())

    def test_improve_rejects_project_below_machine_root(self):
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            home = base / "home"
            home.mkdir()
            waystone_home = base / "machine"
            root = waystone_home / "nested-project"
            self._project(root, "nested")
            err = io.StringIO()
            rc = self._run(
                home, root, ["decide", "context_heavy/trim", "accept"],
                waystone_home=waystone_home, stderr=err,
            )
            self.assertEqual(rc, 1)
            self.assertIn("WAYSTONE_HOME", err.getvalue())
            self.assertIn("outside the project", err.getvalue())


class ImproveM1DefectTests(unittest.TestCase):
    """Regression tests for the 0.7.0 M1 adversarial-review defects (RED-turned-GREEN)."""

    def _quiet(self, fn):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn()

    # ---- finding 3: verify-cmd regex must require a runner/verb, not a bare tests/ path ----
    def test_verify_cmd_requires_runner(self):
        c = improve.classify_verification
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
            facts = improve.run_audit(d)
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
            facts = improve.run_audit(d)
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
            (proj / ".waystone.yml").write_text("version: 1\nproject: a\n")
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
            improve.run_reviews(registry, out)
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
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            run = lambda argv: _run_with_home(
                home, lambda: self._quiet(lambda: improve.main([*argv, "--user-wide"])))
            self.assertEqual(run(["trace", "--source", "/tmp", "--out", "rel/out"]), 1)
            self.assertEqual(run(["reviews", "--out", "rel/out"]), 1)
            self.assertEqual(run(["audit", "--in", "rel/in"]), 1)
            self.assertEqual(run(["decide", "lens/x", "accept", "--out", "rel/out"]), 1)

    # ---- finding 8: registry MISSING is soft (exit 0); EXISTING but corrupt fails loud ----
    def test_registry_fail_loud(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "out"
            cov = improve.run_reviews(d / "nope.json", out)  # MISSING -> 0 projects, no raise
            self.assertEqual(cov["projects_total"], 0)
            bad = d / "bad.json"
            bad.write_text("{ not json ")
            with self.assertRaises(common.WorkflowError):
                improve.run_reviews(bad, out)                # unparseable -> fail loud
            wrong = d / "wrong.json"
            wrong.write_text("[1, 2, 3]")
            with self.assertRaises(common.WorkflowError):
                improve.run_reviews(wrong, out)              # wrong shape -> fail loud
            # exit-code contract via the CLI (corrupt registry under a fake HOME) -> rc 1, not 0
            home = d / "home"
            (home / ".waystone").mkdir(parents=True)
            (home / ".waystone" / "projects.json").write_text("{ nope ")
            rc = _run_with_home(home, lambda: self._quiet(
                lambda: improve.main([
                    "reviews", "--user-wide", "--out",
                    str(home / ".waystone" / "improve" / "o2")])))
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
                cov = improve.run_trace([src], set(), out)
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
            home = d / "home"
            src = d / "projects"
            slug = src / "s"
            slug.mkdir(parents=True)
            _write_jsonl(slug / f"{_UUID}.jsonl",
                         [{"type": "user", "uuid": "u", "message": {"role": "user", "content": "hi"}}])
            locked = home / ".waystone" / "improve" / "locked"
            locked.mkdir(parents=True)
            os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)  # no write bit
            try:
                rc = _run_with_home(home, lambda: self._quiet(lambda: improve.main([
                    "trace", "--user-wide", "--source", str(src),
                    "--out", str(locked / "sub")])))
            finally:
                os.chmod(locked, stat.S_IRWXU)
            self.assertEqual(rc, 2)

    # ---- finding 12: explicit non-dir --source exits 1; a missing source is soft in run_trace ----
    def test_explicit_missing_source_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            rc = _run_with_home(home, lambda: self._quiet(lambda: improve.main([
                "trace", "--user-wide", "--source", str(d / "does-not-exist"),
                "--out", str(home / ".waystone" / "improve" / "out")])))
            self.assertEqual(rc, 1)

    def test_missing_source_recorded_soft(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            missing = d / "gone"
            cov = improve.run_trace([missing], set(), d / "out")
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
            improve.run_trace([src], set(), out)
            sess = [_json.loads(ln) for ln in
                    (out / "sessions.jsonl").read_text().splitlines() if ln][0]
            ch = sess["context_heavy"]
            self.assertEqual(ch["tool_results_over_100kb"], 1)  # 180KB bytes > 100KiB
            self.assertEqual(ch["max_result_bytes"], len(big.encode("utf-8")))


class AcceptFieldTests(unittest.TestCase):
    """Acceptance stays a string list; only repeated task-set --accept-add mutates it safely."""

    def test_validate_accepts_string_list(self):
        data = {"version": 1, "project": "x", "tasks": [
            {"id": "feat/alpha", "title": "a valid task here", "status": "active",
             "accept": ["uv run pytest passes", "no new ruff findings"]}]}
        self.assertEqual(validate.validate(data), [])

    def test_validate_rejects_non_list_accept(self):
        data = {"version": 1, "project": "x", "tasks": [
            {"id": "feat/alpha", "title": "a valid task here", "status": "active",
             "accept": "just a string"}]}
        errs = validate.validate(data)
        self.assertTrue(any("accept" in e for e in errs))

    def test_validate_rejects_non_str_element(self):
        data = {"version": 1, "project": "x", "tasks": [
            {"id": "feat/alpha", "title": "a valid task here", "status": "active",
             "accept": ["ok", 42]}]}
        errs = validate.validate(data)
        self.assertTrue(any("accept" in e for e in errs))

    def test_task_add_rejects_accept_flag(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            before = (root / "tasks.yaml").read_text()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = tasks.main(["add", "feat/new", str(root), "--title",
                                 "a fresh task here", "--accept", "some criterion"])
            self.assertEqual(rc, 1)
            self.assertIn("--accept-add", err.getvalue())
            self.assertEqual((root / "tasks.yaml").read_text(), before)  # nothing written

    def test_task_set_rejects_accept_field(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            before = (root / "tasks.yaml").read_text()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = tasks.main(["set", "feat/alpha", "accept", "some criterion", str(root)])
            self.assertEqual(rc, 1)
            self.assertIn("--accept-add", err.getvalue())
            self.assertEqual((root / "tasks.yaml").read_text(), before)

    def test_accept_add_repeats_round_trips_and_packet_records_provenance(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            rc = tasks.main([
                "set", "feat/alpha", str(root),
                "--accept-add", "criterion with, comma",
                "--accept-add", "criterion: exact text",
            ])
            self.assertEqual(rc, 0)
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            task = next(t for t in data["tasks"] if t["id"] == "feat/alpha")
            self.assertEqual(task["accept"], ["criterion with, comma", "criterion: exact text"])
            packet, acceptance = delegate._build_packet(
                data, "feat/alpha", ["one-off criterion"], root)
            self.assertEqual(acceptance, [
                "criterion with, comma", "criterion: exact text", "one-off criterion"])
            self.assertEqual(packet["accept_provenance"], [
                {"criterion": "criterion with, comma", "source": "task --accept-add"},
                {"criterion": "criterion: exact text", "source": "task --accept-add"},
                {"criterion": "one-off criterion", "source": "delegate run --accept"},
            ])


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
            sha, dirty = delegate._snapshot(root, "snap")
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
            sha, dirty = delegate._snapshot(root, "snap")
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
            delegate._snapshot(root, "snap")
            self.assertEqual(git(root, "status", "--porcelain").stdout, status_before)
            self.assertEqual(git(root, "rev-parse", "HEAD").stdout, head_before)

    def test_precondition_unborn_head(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            git(root, "init", "-q", "-b", "main")
            with self.assertRaises(delegate.WorkflowError):
                delegate._check_snapshot_preconditions(root)

    def test_precondition_submodule(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / ".gitmodules").write_text("[submodule \"x\"]\n")
            with self.assertRaises(delegate.WorkflowError):
                delegate._check_snapshot_preconditions(root)

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
            with self.assertRaises(delegate.WorkflowError):
                delegate._check_snapshot_preconditions(root)

    def test_precondition_rebase_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / ".git" / "rebase-merge").mkdir()
            with self.assertRaises(delegate.WorkflowError):
                delegate._check_snapshot_preconditions(root)

    def test_precondition_cherry_pick_head(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / ".git" / "CHERRY_PICK_HEAD").write_text("deadbeef\n")
            with self.assertRaises(delegate.WorkflowError):
                delegate._check_snapshot_preconditions(root)

    def test_preconditions_pass_on_clean_repo(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            delegate._check_snapshot_preconditions(root)  # no raise

    def test_precondition_reserved_report_filename(self):
        # H2: a pre-existing JW_REPORT.yaml would be baked into the base, then consumed as the
        # delegate's report and phantom-deleted by the patch — refuse up front.
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / "JW_REPORT.yaml").write_text("stale: report\n")
            with self.assertRaises(delegate.WorkflowError) as cm:
                delegate._check_snapshot_preconditions(root)
            self.assertIn("reserved", str(cm.exception))

    def test_make_did_shape(self):
        did = delegate._make_did("feat/xyz")
        self.assertRegex(did, r"^\d{8}T\d{6}Z-feat-xyz$")


_PROFILE_BODY = ('schema: waystone-profile-1\nbindings:\n'
                 '  implementer: {execution: external-runner, backend: "codex:gpt-5.4-codex"}\n')


def _write_profile(root: Path, body: str = _PROFILE_BODY):
    (common.ensure_project_state_dir(root) / "profile.yml").write_text(body, encoding="utf-8")


class DelegateProfileTests(unittest.TestCase):
    """0.8.0 M1 §11 — profile binding resolution (fail-loud, no default-model guessing)."""

    @staticmethod
    def _schema_accepts(schema: dict, instance: object) -> bool:
        """Interpret only the JSON Schema vocabulary used by profile-schema.json."""
        import re

        def resolve(ref: str) -> dict:
            node = schema
            for part in ref.removeprefix("#/").split("/"):
                node = node[part]
            return node

        def valid(node: dict, value: object) -> bool:
            if "$ref" in node and not valid(resolve(node["$ref"]), value):
                return False
            if "allOf" in node and not all(valid(part, value) for part in node["allOf"]):
                return False
            if "oneOf" in node and sum(valid(part, value) for part in node["oneOf"]) != 1:
                return False
            expected_type = node.get("type")
            type_matches = {
                "object": isinstance(value, dict),
                "string": isinstance(value, str),
                "null": value is None,
            }
            if expected_type is not None and not type_matches.get(expected_type, False):
                return False
            if "const" in node and value != node["const"]:
                return False
            if "enum" in node and value not in node["enum"]:
                return False
            if isinstance(value, str):
                if len(value) < node.get("minLength", 0):
                    return False
                if "pattern" in node and re.search(node["pattern"], value) is None:
                    return False
            if isinstance(value, dict):
                required = node.get("required", [])
                if any(field not in value for field in required):
                    return False
                if len(value) < node.get("minProperties", 0):
                    return False
                properties = node.get("properties", {})
                if node.get("additionalProperties") is False and any(
                        field not in properties for field in value):
                    return False
                if any(field in value and not valid(rule, value[field])
                       for field, rule in properties.items()):
                    return False
            return True

        return valid(schema, instance)

    @staticmethod
    def _schema_role_executions(schema: dict, role: str) -> list[str]:
        definition = schema["$defs"][role]
        branch = definition["oneOf"][0] if role == "verifier" else definition
        execution = branch["allOf"][1]["properties"]["execution"]
        return execution["enum"] if "enum" in execution else [execution["const"]]

    def test_missing_profile_raises(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            root = Path(d) / "repo"
            root.mkdir()
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate._load_profile(root))
            self.assertIn(str(root / ".waystone" / "profile.yml"), str(cm.exception))
            self.assertIn("verifier: {execution: external-runner, backend:", str(cm.exception))

    def test_resolve_binding_ok_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            root = Path(d) / "repo"
            root.mkdir()
            _write_profile(root)
            profile, fp = _run_with_home(home, lambda: delegate._load_profile(root))
            self.assertTrue(fp.startswith("sha256:"))
            b = delegate._resolve_binding(profile, "implementer", root)
            self.assertEqual(b["backend"], "codex:gpt-5.4-codex")
            self.assertEqual(b["execution"], "external-runner")
            self.assertEqual(b["source"], "profile")

    def test_profile_schema_matches_runtime_combinations_and_profile_corpus(self):
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  main: {execution: main-session, backend: 'claude:opus'}\n"
                "  orchestrator: {execution: deterministic-workflow, backend: 'claude:opus'}\n"
                "  implementer: {execution: external-runner, backend: 'codex:gpt'}\n"
                "  clerk: {execution: forked-subagent, backend: 'local-runner:small'}\n"
                "  verifier: {backend: 'gemini:pro'}\n"
                "  reviewer: {execution: forked-subagent, backend: 'future.runner:model'}\n"
            ))
            profile, _fingerprint = delegate._load_profile(root)
            self.assertEqual(set(profile["bindings"]), set(delegate.PROFILE_ROLES))
            schema = _json.loads(
                (SCRIPTS.parent / "templates" / "profile-schema.json").read_text())
            self.assertEqual(
                set(schema["properties"]["bindings"]["properties"]),
                set(delegate.PROFILE_ROLES),
            )
            standard = schema["$defs"]["binding"]["properties"]["execution"]["oneOf"][0]
            self.assertEqual(standard["enum"], list(delegate.PROFILE_EXECUTIONS))
            self.assertEqual(
                set(delegate.WAYSTONE_EXECUTABLE_EXECUTIONS)
                | set(delegate.HOST_GUIDED_EXECUTIONS),
                set(delegate.PROFILE_EXECUTIONS),
            )
            self.assertFalse(
                set(delegate.WAYSTONE_EXECUTABLE_EXECUTIONS)
                & set(delegate.HOST_GUIDED_EXECUTIONS))
            for role in delegate.PROFILE_ROLES:
                self.assertEqual(
                    self._schema_role_executions(schema, role),
                    list(delegate.VALID_ROLE_EXECUTIONS[role]),
                )

            corpus = [profile]
            for legacy_execution in delegate._LEGACY_VERIFIER_EXECUTIONS:
                corpus.append({
                    "schema": "waystone-profile-1",
                    "bindings": {
                        "implementer": {
                            "execution": "external-runner", "backend": "codex:gpt-test"},
                        "verifier": {
                            "execution": legacy_execution, "backend": "codex:gpt-test"},
                    },
                })
            for instance in corpus:
                delegate._validate_profile(instance, Path("profile.yml"))
                self.assertTrue(self._schema_accepts(schema, instance), instance)

            legacy_branch = schema["$defs"]["verifier"]["oneOf"][1]
            legacy_execution = legacy_branch["allOf"][1]["properties"]["execution"]
            self.assertEqual(
                legacy_execution["enum"], list(delegate._LEGACY_VERIFIER_EXECUTIONS))
            self.assertIs(legacy_execution["deprecated"], True)

            for role in delegate.PROFILE_ROLES:
                for execution in delegate.PROFILE_EXECUTIONS:
                    instance = {"schema": "waystone-profile-1", "bindings": {role: {
                        "execution": execution, "backend": "runner:model"}}}
                    try:
                        delegate._validate_profile(instance, Path("profile.yml"))
                        runtime_accepts = True
                    except delegate.WorkflowError:
                        runtime_accepts = False
                    self.assertEqual(
                        self._schema_accepts(schema, instance), runtime_accepts,
                        f"schema/runtime mismatch for {role}/{execution}",
                    )

    def test_role_execution_combination_violation_fails_loud(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  main: {execution: external-runner, backend: 'codex:gpt'}\n"
            ))
            with self.assertRaisesRegex(delegate.WorkflowError, "not valid for role 'main'"):
                delegate._load_profile(root)

    def test_schema_valid_but_unimplemented_execution_fails_loud(self):
        profile = {"bindings": {"implementer": {
            "execution": "clean-subagent", "backend": "claude:sonnet"}}}
        with self.assertRaisesRegex(
                delegate.WorkflowError,
                "routing contract.*skill routing.*observation attribution"):
            delegate._resolve_binding(profile, "implementer", Path("/project"))

    def test_missing_role_binding_raises(self):
        with tempfile.TemporaryDirectory() as d:
            profile = yaml.safe_load(_PROFILE_BODY)
            root = Path(d) / "repo"
            root.mkdir()
            with self.assertRaises(delegate.WorkflowError):
                delegate._resolve_binding(profile, "verifier", root)

    def test_unsupported_execution_raises(self):
        profile = {"bindings": {"implementer": {"execution": "in-process", "backend": "codex:x"}}}
        with self.assertRaises(delegate.WorkflowError):
            delegate._resolve_binding(profile, "implementer", Path("/project"))

    def test_bad_backend_format_raises(self):
        profile = {"bindings": {"implementer": {"execution": "external-runner", "backend": "codexonly"}}}
        with self.assertRaises(delegate.WorkflowError):
            delegate._resolve_binding(profile, "implementer", Path("/project"))

    def test_external_runner_backend_tokens_are_explicit(self):
        with self.assertRaises(delegate.WorkflowError):
            delegate._runner_model("gemini:pro")
        self.assertEqual(delegate._runner_model("claude:sonnet"), "sonnet")
        self.assertEqual(delegate._runner_model("codex:gpt-5.4-codex"), "gpt-5.4-codex")

    def test_profile_schema_documents_waystone_executable_vs_host_guided(self):
        schema = _json.loads(
            (SCRIPTS.parent / "templates" / "profile-schema.json").read_text())
        description = schema["$defs"]["binding"]["properties"]["execution"]["description"]
        self.assertIn("waystone-executable", description)
        self.assertIn("host-guided", description)

    def test_claude_verifier_binding_selects_claude_transport(self):
        profile = {"bindings": {"verifier": {
            "execution": "external-runner", "backend": "claude:sonnet",
            "entry": "adversarial-review"}}}
        binding = delegate._resolve_verifier_binding(profile, Path("/project"))
        self.assertEqual(binding["execution"], "claude-cli")
        self.assertEqual(binding["backend"], "claude:sonnet")

    def test_invalid_effort_field_is_rejected(self):
        for effort in ("extreme", "pro"):
            with self.subTest(effort=effort):
                profile = {"bindings": {"implementer": {
                    "execution": "external-runner", "backend": "codex:x", "effort": effort}}}
                with self.assertRaises(delegate.WorkflowError) as cm:
                    delegate._resolve_binding(profile, "implementer", Path("/project"))
                self.assertIn("effort", str(cm.exception))

    def test_effort_vocabulary_matches_profile_schema(self):
        schema = _json.loads(
            (SCRIPTS.parent / "templates" / "profile-schema.json").read_text())
        schema_values = schema["$defs"]["binding"]["properties"]["effort"]["enum"]
        expected = ["none", "minimal", "low", "medium", "high", "xhigh", "ultra"]
        self.assertEqual(schema_values, expected)
        self.assertEqual(list(delegate._EFFORT_VALUES), expected)

    def test_ultra_effort_is_rejected_by_claude_external_runner(self):
        profile = {"bindings": {"implementer": {
            "execution": "external-runner", "backend": "claude:sonnet", "effort": "ultra"}}}
        with self.assertRaisesRegex(delegate.WorkflowError, "claude external-runner effort"):
            delegate._resolve_binding(profile, "implementer", Path("/project"))

    def test_ultra_verifier_effort_requires_codex_cli_transport(self):
        import os

        profile = {"bindings": {"verifier": {
            "execution": "external-runner", "backend": "codex:gpt-test", "effort": "ultra",
            "entry": "adversarial-review"}}}
        old_host = os.environ.pop("WAYSTONE_HOST", None)
        try:
            with self.assertRaisesRegex(delegate.WorkflowError, "ultra.*Codex CLI"):
                delegate._resolve_verifier_binding(profile, Path("/project"))
            os.environ["WAYSTONE_HOST"] = "codex"
            binding = delegate._resolve_verifier_binding(profile, Path("/project"))
        finally:
            if old_host is None:
                os.environ.pop("WAYSTONE_HOST", None)
            else:
                os.environ["WAYSTONE_HOST"] = old_host
        self.assertEqual(binding["execution"], "codex-cli")
        self.assertEqual(binding["effort"], "ultra")

    def test_verifier_execution_absent_is_derived_from_host(self):
        import os

        profile = {"bindings": {"verifier": {
            "backend": "codex:x", "entry": "adversarial-review"}}}
        old_host = os.environ.pop("WAYSTONE_HOST", None)
        try:
            binding = delegate._resolve_verifier_binding(profile, Path("/project"))
        finally:
            if old_host is not None:
                os.environ["WAYSTONE_HOST"] = old_host
        self.assertEqual(binding["execution"], "codex-companion")

    def test_verifier_external_runner_axis_preserves_codex_transport(self):
        import os

        profile = {"bindings": {"verifier": {
            "execution": "external-runner", "backend": "codex:x",
            "entry": "adversarial-review"}}}
        old_host = os.environ.get("WAYSTONE_HOST")
        os.environ["WAYSTONE_HOST"] = "codex"
        try:
            binding = delegate._resolve_verifier_binding(profile, Path("/project"))
        finally:
            if old_host is None:
                os.environ.pop("WAYSTONE_HOST", None)
            else:
                os.environ["WAYSTONE_HOST"] = old_host
        self.assertEqual(binding["execution"], "codex-cli")
        self.assertEqual(binding["backend"], "codex:x")

    def test_matching_verifier_execution_warns_and_is_accepted(self):
        import contextlib
        import io
        import os

        profile = {"bindings": {"verifier": {
            "execution": "codex-cli", "backend": "codex:x", "entry": "adversarial-review"}}}
        old_host = os.environ.get("WAYSTONE_HOST")
        os.environ["WAYSTONE_HOST"] = "codex"
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                binding = delegate._resolve_verifier_binding(profile, Path("/project"))
        finally:
            if old_host is None:
                os.environ.pop("WAYSTONE_HOST", None)
            else:
                os.environ["WAYSTONE_HOST"] = old_host
        self.assertEqual(binding["execution"], "codex-cli")
        self.assertIn("deprecated", err.getvalue())

    def test_conflicting_verifier_execution_is_rejected(self):
        import os

        profile = {"bindings": {"verifier": {
            "execution": "codex-companion", "backend": "codex:x",
            "entry": "adversarial-review"}}}
        old_host = os.environ.get("WAYSTONE_HOST")
        os.environ["WAYSTONE_HOST"] = "codex"
        try:
            with self.assertRaises(delegate.WorkflowError) as cm:
                delegate._resolve_verifier_binding(profile, Path("/project"))
        finally:
            if old_host is None:
                os.environ.pop("WAYSTONE_HOST", None)
            else:
                os.environ["WAYSTONE_HOST"] = old_host
        self.assertIn("remove the execution key", str(cm.exception))


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
        packet, acceptance = delegate._build_packet(data, "feat/xyz",
                                                       ["flag criterion", "registry criterion one"], Path("/x"))
        self.assertEqual(packet["schema"], "waystone-packet-1")
        self.assertEqual(acceptance, ["registry criterion one", "flag criterion"])  # order + dedup
        self.assertEqual(packet["task"]["deps"], [{"id": "feat/dep", "status": "done"}])
        self.assertEqual(packet["project"]["name"], "demo")

    def test_empty_acceptance_raises(self):
        data = {"project": "d", "tasks": [{"id": "feat/na", "title": "no acceptance here", "status": "active"}]}
        with self.assertRaises(delegate.WorkflowError) as cm:
            delegate._build_packet(data, "feat/na", [], Path("/x"))
        self.assertIn("no acceptance criteria", str(cm.exception))

    def test_blocked_task_message(self):
        with self.assertRaises(delegate.WorkflowError) as cm:
            delegate._build_packet(_packet_registry(), "feat/blk", ["c"], Path("/x"))
        msg = str(cm.exception)
        self.assertIn("blocked", msg)
        # R10: must NOT assert deps are unmet (stale-blocked exists) — offer the conditional path
        self.assertIn("if its deps are now satisfied", msg)
        self.assertNotIn("unmet", msg.lower())

    def test_done_task_rejected(self):
        with self.assertRaises(delegate.WorkflowError):
            delegate._build_packet(_packet_registry(), "feat/dn", ["c"], Path("/x"))

    def test_unknown_task_rejected(self):
        with self.assertRaises(delegate.WorkflowError):
            delegate._build_packet(_packet_registry(), "feat/nope", ["c"], Path("/x"))


class DelegateRunTests(unittest.TestCase):
    """Full run flow with injected Codex/Claude runners; never invokes a real runner."""

    def _project(self, d) -> tuple[Path, Path]:
        root = Path(d) / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
        (root / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks:\n"
            '  - id: feat/xyz\n    title: "implement xyz feature"\n    status: active\n'
            '    accept:\n      - "criterion alpha here"\n')
        git(root, "add", "-A")
        git(root, "commit", "-qm", "setup")
        home = Path(d) / "home"
        _write_profile(root)
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
        orig = delegate._run_codex
        delegate._run_codex = fake
        try:
            return _run_with_home(home, lambda: delegate.run_delegation(root, task, "implementer", accept or []))
        finally:
            delegate._run_codex = orig

    def _run_claude_backend(self, root, home, fake, task="feat/xyz", accept=None):
        _write_profile(root, (
            "schema: waystone-profile-1\nbindings:\n"
            "  implementer: {execution: external-runner, backend: 'claude:sonnet'}\n"))
        orig = delegate._run_claude
        delegate._run_claude = fake
        try:
            return _run_with_home(
                home, lambda: delegate.run_delegation(
                    root, task, "implementer", accept or [],
                    allow_unsandboxed_runner=True,
                    unsandboxed_reason="synthetic test transport"))
        finally:
            delegate._run_claude = orig

    def _record_dir(self, root, home):
        return _run_with_home(home, lambda: sorted(delegate._delegations_dir(root).iterdir())[-1])

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
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            # prompt carries acceptance criterion text
            self.assertIn("criterion alpha here", (rec / "prompt.txt").read_text())
            # JW_REPORT consumed from the worktree (not left to pollute the patch)
            wt = _run_with_home(home, lambda: delegate._worktree_path(root, rec.name))
            self.assertFalse((wt / "JW_REPORT.yaml").exists())
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["schema"], "waystone-artifact-1")
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
            self.assertEqual(exp["schema"], "waystone-exposure-1")
            self.assertEqual(exp["sandbox"], "workspace-write")
            self.assertEqual(exp["binding"]["backend"], "codex:gpt-5.4-codex")
            self.assertEqual(
                {row["identity"]["layer"] for row in exp["overlays"]}, {"base"})
            # result ref exists
            self.assertTrue(git(root, "rev-parse", "--verify",
                                f"refs/waystone/delegations/{rec.name}-result").returncode == 0)

    def test_claude_runner_injected_success_and_delta_warning(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            report = "verification: []\nlimitations: []\nrisks: []\nescalations: []\n"
            fake = self._fake_runner({"impl.py": "claude\n"}, report=report)
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = self._run_claude_backend(root, home, fake)
            self.assertEqual(rc, 0)
            self.assertIn("waystone warn:", err.getvalue())
            for delta in ("filesystem", "process", "network"):
                self.assertIn(delta, err.getvalue().lower())
            rec = self._record_dir(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["runner"]["backend"], "claude:sonnet")
            exp = _json.loads((rec / "exposure.json").read_text())
            self.assertEqual(exp["sandbox"], "none")

    def test_claude_implementer_refuses_without_explicit_unsandboxed_override(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: 'claude:sonnet'}\n"))
            called = []
            original = delegate._run_claude
            delegate._run_claude = lambda *args, **kwargs: (called.append(True) or (0, 0.1))
            try:
                with self.assertRaisesRegex(
                        delegate.WorkflowError, "--allow-unsandboxed-runner.*--reason"):
                    _run_with_home(home, lambda: delegate.run_delegation(
                        root, "feat/xyz", "implementer", []))
            finally:
                delegate._run_claude = original
            self.assertEqual(called, [])
            self.assertFalse(delegate._delegations_dir(root).exists())

    def test_claude_unsandboxed_override_records_packet_exposure_and_full_delta(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: 'claude:sonnet'}\n"))
            fake = self._fake_runner({"impl.py": "claude\n"})
            original = delegate._run_claude
            delegate._run_claude = fake
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: delegate.run_delegation(
                        root, "feat/xyz", "implementer", [],
                        allow_unsandboxed_runner=True,
                        unsandboxed_reason="legacy Claude-only backend"))
            finally:
                delegate._run_claude = original
            self.assertEqual(rc, 0)
            rec = self._record_dir(root, home)
            packet = yaml.safe_load((rec / "packet.yaml").read_text())
            exposure = _json.loads((rec / "exposure.json").read_text())
            expected = {"kind": "allow-unsandboxed-runner",
                        "reason": "legacy Claude-only backend", "provenance": "user"}
            self.assertEqual(packet["runner_override"], expected)
            self.assertEqual(exposure["runner_override"], expected)
            self.assertEqual(exposure["sandbox"], "none")
            for delta in ("filesystem", "process", "network"):
                self.assertIn(delta, err.getvalue().lower())

    def test_claude_unsandboxed_cli_override_requires_paired_reason(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: 'claude:sonnet'}\n"))
            cases = (
                (["--allow-unsandboxed-runner"], "requires --reason"),
                (["--reason", "because"], "only valid with --allow-unsandboxed-runner"),
            )
            for extra, needle in cases:
                err = io.StringIO()
                with self.subTest(extra=extra), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: delegate.main([
                        "run", "feat/xyz", "--root", str(root), *extra]))
                self.assertEqual(rc, 1)
                self.assertIn(needle, err.getvalue())
            self.assertFalse(delegate._delegations_dir(root).exists())

    def test_claude_runner_injected_failure_preserves_failed_runner(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({"impl.py": "partial\n"}, rc=9)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError):
                    self._run_claude_backend(root, home, fake)
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "failed-runner")

    def test_claude_runner_missing_report_uses_shared_contract_path(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({"impl.py": "no report\n"}, report=None)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self._run_claude_backend(root, home, fake), 0)
            rec = self._record_dir(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertIs(contract["delegate_report"]["present"], False)

    def test_run_claude_transport_is_injectable_and_confined(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            prompt = root / "prompt.txt"
            prompt.write_text("implement")
            calls = []

            def transport(cmd, **kwargs):
                calls.append((cmd, kwargs))
                kwargs["stdout"].write('{"type":"result","result":"done"}\n')
                return subprocess.CompletedProcess(cmd, 0)

            rc, _duration = delegate._run_claude(
                root, "sonnet", prompt, root, runner=transport)
            self.assertEqual(rc, 0)
            cmd, kwargs = calls[0]
            self.assertEqual(cmd[:4], ["claude", "-p", "--model", "sonnet"])
            for flag in ("--safe-mode", "--strict-mcp-config", "--no-chrome",
                         "--allowedTools", "--disallowedTools", "--no-session-persistence"):
                self.assertIn(flag, cmd)
            self.assertIn("WebFetch", cmd[cmd.index("--disallowedTools") + 1])
            self.assertEqual(Path(kwargs["cwd"]), root)
            self.assertNotIn("WAYSTONE_VERIFIER_SESSION", kwargs["env"])
            self.assertEqual((root / "last_message.md").read_text(), "done")

    def test_run_claude_rejects_non_object_stream_event_as_transport_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            prompt = root / "prompt.txt"
            prompt.write_text("implement")

            def transport(cmd, **kwargs):
                kwargs["stdout"].write("[]\n")
                return subprocess.CompletedProcess(cmd, 0)

            rc, _duration = delegate._run_claude(
                root, "sonnet", prompt, root, runner=transport)
            self.assertNotEqual(rc, 0)
            self.assertIn("object", (root / "runner.stderr").read_text().lower())

    def test_unexpected_runner_exception_transitions_failed_runner(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def boom(*_args, **_kwargs):
                raise RuntimeError("transport exploded")

            with self.assertRaisesRegex(delegate.WorkflowError, "transport exploded"):
                self._run(root, home, boom)
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "failed-runner")

    def test_cli_prepares_slow_inputs_before_claim_lock_and_revalidates_inside(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            depth = {"value": 0}
            observed = {"preflight": [], "packet": [], "overlay": [], "composition": []}
            originals = (delegate.hold_lock, delegate._check_snapshot_preconditions,
                         delegate._build_packet, delegate._active_overlays,
                         delegate._run_codex)

            @contextlib.contextmanager
            def tracked_lock(path, timeout=None):
                depth["value"] += 1
                try:
                    yield
                finally:
                    depth["value"] -= 1

            def preflight(project):
                observed["preflight"].append(depth["value"])
                return originals[1](project)

            def build_packet(*args, **kwargs):
                observed["packet"].append(depth["value"])
                return originals[2](*args, **kwargs)

            def active_overlays(project, composition):
                observed["overlay"].append(depth["value"])
                observed["composition"].append(composition)
                return originals[3](project, composition)

            delegate.hold_lock = tracked_lock
            delegate._check_snapshot_preconditions = preflight
            delegate._build_packet = build_packet
            delegate._active_overlays = active_overlays
            delegate._run_codex = self._fake_runner({"impl.py": "x\n"})
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = _run_with_home(home, lambda: delegate.main(
                        ["run", "feat/xyz", "--root", str(root)]))
            finally:
                (delegate.hold_lock, delegate._check_snapshot_preconditions,
                 delegate._build_packet, delegate._active_overlays,
                 delegate._run_codex) = originals
            self.assertEqual(rc, 0)
            self.assertEqual(observed["preflight"], [0])
            self.assertEqual(observed["packet"], [0, 1])
            self.assertEqual(observed["overlay"], [1])
            self.assertEqual(len(observed["composition"]), 1)
            self.assertIn("effective", observed["composition"][0])

    def test_cli_rejects_task_state_changed_after_packet_preparation(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            original_prepare = delegate._prepare_run
            original_runner = delegate._run_codex
            runner_calls = []

            def prepare(*args, **kwargs):
                plan = original_prepare(*args, **kwargs)
                tasks_path = root / "tasks.yaml"
                tasks_path.write_text(tasks_path.read_text().replace(
                    "status: active", "status: done"))
                return plan

            def runner(*args, **kwargs):
                runner_calls.append(True)
                return (0, 0.1)

            delegate._prepare_run = prepare
            delegate._run_codex = runner
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: delegate.main(
                        ["run", "feat/xyz", "--root", str(root)]))
            finally:
                delegate._prepare_run = original_prepare
                delegate._run_codex = original_runner
            self.assertEqual(rc, 1)
            self.assertEqual(runner_calls, [])
            self.assertIn("changed while preparing", err.getvalue())
            self.assertFalse(delegate._delegations_dir(root).exists())

    def test_exposure_replace_failure_leaves_no_partial_or_temp(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            record = root / ".waystone" / "delegations" / "did"
            record.mkdir(parents=True)
            original_replace = common.os.replace

            def fail_replace(_source, _target):
                raise OSError("injected exposure replace failure")

            common.os.replace = fail_replace
            try:
                with self.assertRaises(OSError):
                    delegate._write_exposure(
                        record, "did", root, {"project": {"name": "demo"}}, "feat/xyz",
                        "head", "base", False, {}, "sha256:test", [])
            finally:
                common.os.replace = original_replace
            self.assertFalse((record / "exposure.json").exists())
            self.assertEqual(list(record.glob(".exposure.json.*.tmp")), [])

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
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")

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
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\ndelegation:\n  env_prep:\n    - \"false\"\n")
            called = {"n": 0}

            def fake(*a, **k):
                called["n"] += 1
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError):
                    self._run(root, home, fake)
            self.assertEqual(called["n"], 0)  # runner never invoked
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "failed-env")

    def test_runner_failure_is_failed_runner_with_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({"impl.py": "x\n"}, rc=3)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError):
                    self._run(root, home, fake)
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "failed-runner")
            self.assertTrue((rec / "exposure.json").exists())  # exposure recorded before runner

    def test_run_refuses_preexisting_jw_report(self):
        # H2 repro: an untracked JW_REPORT.yaml in the user's tree must refuse the run entirely —
        # before any record is created (no phantom deletion via the patch).
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            (root / "JW_REPORT.yaml").write_text("stale: report\n")  # untracked user file
            with self.assertRaises(delegate.WorkflowError) as cm:
                self._run(root, home, self._fake_runner({"impl.py": "x\n"}))
            self.assertIn("JW_REPORT.yaml", str(cm.exception))
            self.assertFalse(_run_with_home(home, lambda: delegate._delegations_dir(root)).exists())

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
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
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
        # evidence, and keeps the lock. A broken git worktree must make discard cleanup fail loud.
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def fake(worktree, model, prompt_path, record_dir):
                (record_dir / "last_message.md").write_text("x")
                (worktree / ".git").unlink()  # breaks the result snapshot
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError):
                    self._run(root, home, fake)
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "failed-artifact")
            wt = _run_with_home(home, lambda: delegate._worktree_path(root, rec.name))
            self.assertTrue(wt.exists())  # evidence preserved
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError) as cm:
                    self._run(root, home, self._fake_runner({"impl.py": "y\n"}))
                self.assertIn("already has active delegation", str(cm.exception))
                with self.assertRaises(delegate.WorkflowError) as cleanup:
                    _run_with_home(
                        home, lambda: delegate.discard_delegation(
                            root, rec.name, "clear failed artifact"))
            self.assertIn("git worktree remove", str(cleanup.exception))
            status = delegate._read_status(rec)
            self.assertEqual(status["state"], "discarding")
            self.assertEqual(status["at_transitions"][-1]["reason"], "clear failed artifact")

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
            orig = delegate._make_did
            delegate._make_did = lambda tid: "20260713T120000Z-fixed"
            import contextlib
            import io
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc1 = self._run(root, home, self._fake_runner({"a.py": "x\n"}), task="feat/xyz")
                    rc2 = self._run(root, home, self._fake_runner({"b.py": "y\n"}), task="feat/two")
            finally:
                delegate._make_did = orig
            self.assertEqual((rc1, rc2), (0, 0))
            ddir = _run_with_home(home, lambda: delegate._delegations_dir(root))
            names = sorted(p.name for p in ddir.iterdir())
            self.assertEqual(names, ["20260713T120000Z-fixed", "20260713T120000Z-fixed-2"])
            st1 = delegate._read_status(ddir / names[0])
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
                with self.assertRaises(delegate.WorkflowError):
                    self._run(root, home, fake_fail)  # -> failed-runner (non-terminal, holds lock)
                with self.assertRaises(delegate.WorkflowError) as cm:
                    self._run(root, home, self._fake_runner({"impl.py": "y\n"}))
            self.assertIn("already has active delegation", str(cm.exception))

    def test_claim_without_exposure_blocks_second_cli_run(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({"impl.py": "x\n"})
            original_snapshot = delegate._snapshot
            original_runner = delegate._run_codex
            observed = {}

            def snapshot(cwd, message, *, exclude_uv_cache=False):
                if Path(cwd).resolve() == root.resolve() and not observed:
                    records = sorted(delegate._delegations_dir(root).iterdir())
                    self.assertEqual(len(records), 1)
                    claim = records[0] / "claim.json"
                    self.assertTrue(claim.is_file())
                    self.assertFalse((records[0] / "exposure.json").exists())
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        observed["second_rc"] = delegate.main(
                            ["run", "feat/xyz", "--root", str(root)])
                    observed["second_err"] = err.getvalue()
                return original_snapshot(cwd, message, exclude_uv_cache=exclude_uv_cache)

            delegate._snapshot = snapshot
            delegate._run_codex = fake
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    first_rc = _run_with_home(
                        home, lambda: delegate.main(
                            ["run", "feat/xyz", "--root", str(root)]))
            finally:
                delegate._snapshot = original_snapshot
                delegate._run_codex = original_runner
            self.assertEqual(first_rc, 0)
            self.assertEqual(observed["second_rc"], 1)
            self.assertIn("already has active delegation", observed["second_err"])

    def test_claim_only_crash_remnant_is_discardable(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def claim():
                plan = delegate._prepare_run(root, "feat/xyz", "implementer", [])
                with common.hold_lock(common.project_lock_path(root)):
                    return delegate._claim_run(root, plan)

            did, rec = _run_with_home(home, claim)
            self.assertTrue((rec / "claim.json").is_file())
            self.assertFalse((rec / "exposure.json").exists())
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["discard", did, "--root", str(root), "--reason", "clear incomplete claim"]))
            self.assertEqual(rc, 0)
            self.assertEqual(delegate._read_status(rec)["state"], "discarded")
            self.assertIsNone(delegate._active_delegation_for_task(root, "feat/xyz"))

    def test_base_ref_creation_uses_cas_and_detects_collision(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            original_snapshot = delegate._snapshot
            original_runner = delegate._run_codex
            injected = {"done": False}
            runner_calls = {"count": 0}

            def snapshot(cwd, message, *, exclude_uv_cache=False):
                result = original_snapshot(cwd, message, exclude_uv_cache=exclude_uv_cache)
                if Path(cwd).resolve() == root.resolve() and not injected["done"]:
                    injected["done"] = True
                    did = next(delegate._delegations_dir(root).iterdir()).name
                    self.assertEqual(git(
                        root, "update-ref", f"refs/waystone/delegations/{did}", result[0]
                    ).returncode, 0)
                return result

            def runner(*args, **kwargs):
                runner_calls["count"] += 1
                return (0, 0.1)

            delegate._snapshot = snapshot
            delegate._run_codex = runner
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: delegate.main(
                        ["run", "feat/xyz", "--root", str(root)]))
            finally:
                delegate._snapshot = original_snapshot
                delegate._run_codex = original_runner
            self.assertEqual(rc, 1)
            self.assertTrue(injected["done"])
            self.assertEqual(runner_calls["count"], 0)
            self.assertIn("git update-ref failed", err.getvalue())
            rec = next(delegate._delegations_dir(root).iterdir())
            self.assertTrue((rec / "claim.json").is_file())
            self.assertFalse((rec / "exposure.json").exists())


def _deleg_project(d) -> tuple[Path, Path]:
    root = Path(d) / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
    (root / "tasks.yaml").write_text(
        "version: 1\nproject: demo\ntasks:\n"
        '  - id: feat/xyz\n    title: "implement xyz feature"\n    status: active\n'
        '    accept:\n      - "criterion alpha here"\n')
    git(root, "add", "-A")
    git(root, "commit", "-qm", "setup")
    home = Path(d) / "home"
    _write_profile(root)
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
    orig = delegate._run_codex
    delegate._run_codex = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return _run_with_home(
                home, lambda: delegate.run_delegation(root, task, "implementer", accept or []))
    finally:
        delegate._run_codex = orig


def _latest_rec(root, home):
    return _run_with_home(home, lambda: sorted(delegate._delegations_dir(root).iterdir())[-1])


def _write_apply_verdict(rec):
    """Install a valid verdict fixture for tests whose subject is the apply mechanics, not judging."""
    packet = yaml.safe_load((rec / "packet.yaml").read_text())
    exposure = _json.loads((rec / "exposure.json").read_text())
    verify_paths = delegate._verify_paths(rec)
    verify_number = (delegate._artifact_number(verify_paths[-1], "verify")
                     if verify_paths else None)
    contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
    verdict = {
        "schema": "waystone-verdict-1",
        "decision": "apply",
        "decided_by": "main-session",
        "criteria": [{"criterion": criterion, "met": True, "evidence": ["agent_checks[0]"]}
                     for criterion in packet["acceptance"]],
        "agent_checks": [{"cmd": "fixture", "exit": 0, "summary": "fixture"}],
        "warnings_seen": [],
        "rationale": "apply mechanics fixture",
        "limitations": [],
        "judged_at": "2026-07-15T00:00:00+00:00",
        "provenance": "main-session",
        "verify_number": verify_number,
        "profile_fingerprint": exposure["profile_fingerprint"],
        "artifact_digests": delegate._current_artifact_digests(
            rec, contract, verify_number),
    }
    (rec / "artifact" / "verdict-1.json").write_text(
        _json.dumps(verdict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
            original_resolve_root = delegate._resolve_root
            acquired = []

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
            delegate._resolve_root = lambda _explicit: root
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = _run_with_home(home, lambda: delegate.main(
                        ["apply", rec.name, "--root", str(root)]))
            finally:
                delegate.apply_delegation = original_apply
                delegate.hold_lock = original_hold_lock
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
            '  verifier: {backend: "codex:gpt-test", entry: adversarial-review}\n'
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

    def test_discard_records_intent_before_cleanup_and_resumes_after_interruption(self):
        with tempfile.TemporaryDirectory() as d:
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
                    _run_with_home(
                        home, lambda: delegate.discard_delegation(root, rec.name, "review rejected"))
            finally:
                delegate._cleanup = original_cleanup

            status = delegate._read_status(rec)
            self.assertEqual(status["state"], "discarding")
            self.assertEqual(status["at_transitions"][-1]["reason"], "review rejected")
            self.assertEqual(_run_with_home(
                home, lambda: delegate.discard_delegation(root, rec.name, "review rejected")), 0)
            transitions = delegate._read_status(rec)["at_transitions"]
            self.assertEqual([item["state"] for item in transitions[-2:]],
                             ["discarding", "discarded"])
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
        '  verifier: {backend: "codex:gpt-test", entry: adversarial-review}\n'
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


class DelegateCliTests(unittest.TestCase):
    """0.8.0 M1 §2 — arg parsing, exit codes, status/show surfaces (incl. R11 no-artifact refusal)."""

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


# ============================================================ v0.8.0 M2: overlay (C1 store+rules)
def _overlay_project(d):
    root = Path(d) / "proj"
    root.mkdir()
    (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
    home = Path(d) / "home"
    home.mkdir()
    return root, home


def _add_delta(root, home, delta_id="verification_debt/skip", rule="delegation-verification-evidence-v1",
               **kw):
    kw.setdefault("summary", "observed 3/5 delegations without verification")
    return _run_with_home(home, lambda: overlay.add_delta(root, delta_id, rule=rule, **kw))


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


def _rule2_project(d):
    root = Path(d) / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".waystone.yml").write_text("version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
    (root / "tasks.yaml").write_text(
        "version: 1\nproject: demo\ntasks:\n"
        "  - id: fix/finding-a\n    title: open severe finding task\n    status: active\n"
        "    severity: blocker\n    origin: review-2026-01-01-r1\n"
        "  - id: fix/finding-b\n    title: closed finding task\n    status: done\n"
        "    severity: major\n    origin: review-2026-01-01-r1\n"
        "  - id: fix/finding-c\n    title: rejected but open finding\n    status: active\n"
        "    severity: blocker\n    origin: review-2026-01-01-r1\n"
        "  - id: fix/finding-d\n    title: open minor finding\n    status: active\n"
        "    severity: minor\n    origin: review-2026-01-01-r1\n")
    rdir = root / "docs" / "reviews"
    rdir.mkdir(parents=True)
    (rdir / "2026-01-01-r1-feedback.md").write_text(
        "meta\n\n## Findings (triage skeleton v1)\n"
        "| Finding | Severity | Verdict | Evidence | Task |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| JW-GPT-001 — a | `blocker` | REAL | ev | `fix/finding-a` |\n"
        "| JW-GPT-002 — b | `major` | REAL | ev | `fix/finding-b` |\n"
        "| JW-GPT-003 — c | `blocker` | REJECTED | ev | `fix/finding-c` |\n"
        "| JW-GPT-004 — u | `major` | NEEDS-RULING | ev | |\n")
    home = Path(d) / "home"
    home.mkdir()
    return root, home


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


# ==================================================== v0.8.0 M2: boundary warn engine + exposure (C2)
def _force_status(root, home, delta_id, status):
    p = _run_with_home(home, lambda: overlay._delta_path(root, delta_id))
    delta = _json.loads(p.read_text())
    delta["status"] = status
    p.write_text(_json.dumps(delta))


def _read_warnings(root, home):
    wp = _run_with_home(home, lambda: overlay._warnings_path(root))
    if not wp.exists():
        return []
    return [_json.loads(ln) for ln in wp.read_text().splitlines() if ln.strip()]


_M2_TRIAGE_FEEDBACK = (
    "meta\n\n## Findings (triage skeleton v1)\n"
    "| Finding | Severity | Verdict | Evidence | Task |\n"
    "| --- | --- | --- | --- | --- |\n"
    "| JW-GPT-001 — a | `blocker` | REAL | ev | `fix/finding-a` |\n")


def _check_project(d):
    root = Path(d) / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".waystone.yml").write_text("version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
    (root / "tasks.yaml").write_text(
        "version: 1\nproject: demo\ntasks:\n"
        "  - id: feat/xyz\n    title: task one here\n    status: active\n    accept:\n      - c1\n"
        "  - id: feat/two\n    title: task two here\n    status: active\n    accept:\n      - c2\n"
        "  - id: feat/three\n    title: task three here\n    status: active\n    accept:\n      - c3\n"
        "  - id: fix/finding-a\n    title: open severe finding\n    status: active\n"
        "    severity: blocker\n    origin: review-2026-01-01-r1\n")
    rdir = root / "docs" / "reviews"
    rdir.mkdir(parents=True)
    (rdir / "2026-01-01-r1-feedback.md").write_text(_M2_TRIAGE_FEEDBACK)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "setup")
    home = Path(d) / "home"
    _write_profile(root)
    return root, home


def _round_review_project(d):
    root = Path(d) / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".waystone.yml").write_text(
        "version: 1\nproject: demo\nreviews_dir: docs/reviews\nstate:\n  last_round_commit: null\n")
    (root / "tasks.yaml").write_text(
        "version: 1\nproject: demo\ntasks:\n"
        "  - id: chore/close-me\n    title: a task to close now\n    status: active\n    deps: []\n"
        "  - id: fix/finding-a\n    title: open severe finding\n    status: active\n"
        "    severity: blocker\n    origin: review-2026-01-01-r1\n")
    rdir = root / "docs" / "reviews"
    rdir.mkdir(parents=True)
    (rdir / "2026-01-01-r1-feedback.md").write_text(_M2_TRIAGE_FEEDBACK)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "setup")
    home = Path(d) / "home"
    home.mkdir()
    return root, home


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
                    root, "2026-01-02-close", done=["chore/close-me"], touched=[], commit="HEAD"))
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
                        root, "2026-01-02-close", done=["chore/close-me"], touched=[], commit="HEAD"))
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
                        root, "2026-01-02-close", done=["chore/close-me"], touched=[], commit="HEAD"))
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
                    root, "2026-01-02-close", done=["chore/close-me"], touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            p = self._exposure_dir(root, home) / "round-2026-01-02-close.json"
            self.assertTrue(p.exists())
            exp = _json.loads(p.read_text())
            self.assertEqual(exp["schema"], "waystone-round-exposure-1")
            self.assertEqual(exp["round_id"], "2026-01-02-close")
            self.assertIsNotNone(exp["profile_fingerprint"])
            self.assertEqual(exp["bindings"]["implementer"], "codex:gpt-5.4-codex")
            self.assertEqual(exp["guards"], None)
            self.assertEqual(exp["waivers"], [])
            bindings = list((root / "docs" / "reviews").glob(
                "2026-01-02-close-request.binding*.json"))
            self.assertEqual(bindings, [])

            git(root, "add", "-A")
            git(root, "commit", "-qm", "closeout")
            closeout = git(root, "rev-parse", "HEAD").stdout.strip()
            (root / "docs" / "reviews" / "2026-01-02-close-request.md").write_text(
                "# Review Request\n\n"
                f"- Reviewing: {closeout}   (diff against (root))\n")
            self.assertEqual(review.prepare_packet_request(root, "2026-01-02-close"), 0)
            binding_path = next((root / "docs" / "reviews").glob(
                "2026-01-02-close-request.binding*.json"))
            binding = _json.loads(binding_path.read_text())
            self.assertEqual(binding["round_id"], "2026-01-02-close")
            self.assertEqual(binding["target_sha"], closeout)
            self.assertEqual(binding["canonical_store"], "local-packet")

    def test_profile_absent_null_bindings(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)  # no profile written
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                _run_with_home(home, lambda: round.close(
                    root, "2026-01-02-close", done=["chore/close-me"], touched=[], commit="HEAD"))
            p = self._exposure_dir(root, home) / "round-2026-01-02-close.json"
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
                        root, "2026-01-02-close", done=["chore/close-me"], touched=[], commit="HEAD"))
            edir = self._exposure_dir(root, home)
            self.assertTrue((edir / "round-2026-01-02-close.json").exists())
            self.assertTrue((edir / "round-2026-01-02-close-2.json").exists())

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
                        root, "2026-01-02-close", done=["chore/close-me"], touched=[], commit="HEAD"))
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
                    root, "2026-01-02-close", done=["chore/close-me"],
                    touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            task = common.load_tasks(root)["tasks"][0]
            self.assertEqual(task["session_id"], session_id)
            exposure = _json.loads(
                (self._exposure_dir(root, home) / "round-2026-01-02-close.json").read_text())
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
                    root, "2026-01-02-close", done=["chore/close-me"],
                    touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            task = common.load_tasks(root)["tasks"][0]
            self.assertIsNone(task["session_id"])
            exposure = _json.loads(
                (self._exposure_dir(root, home) / "round-2026-01-02-close.json").read_text())
            self.assertIsNone(exposure["session_id"])


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


class EvidenceTests(unittest.TestCase):
    """0.8.0 M2 §8 — task-id evidence projection and the evidence_link audit lens."""

    def _fixture(self, d):
        d = Path(d)
        root = d / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text(
            "version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
        (root / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks:\n"
            "  - id: fix/open\n    title: open severe finding task\n    status: active\n"
            "    severity: blocker\n    origin: review-2026-01-01-r1\n"
            "  - id: fix/task-only\n    title: task source finding here\n    status: done\n"
            "    severity: major\n    origin: review-2026-01-01-r1\n"
            "  - id: feat/deleg-only\n    title: delegation only task here\n    status: active\n")
        rdir = root / "docs" / "reviews"
        rdir.mkdir(parents=True)
        (rdir / "2026-01-01-r1-feedback.md").write_text(
            "meta\n\n## Findings (triage skeleton v1)\n"
            "| Finding | Severity | Verdict | Evidence | Task |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| JW-GPT-001 — linked | `blocker` | REAL | ev | `fix/open` |\n"
            "| JW-GPT-002 — unknown link | `major` | NEEDS-RULING | ev | |\n")
        home = d / "home"
        home.mkdir()
        ddir = _run_with_home(home, lambda: delegate._delegations_dir(root))
        self._delegation(ddir / "did-unverified", "fix/open", "needs-review", verified=False)
        self._delegation(ddir / "did-verified", "feat/deleg-only", "applied", verified=True)
        registry = d / "projects.json"
        registry.write_text(_json.dumps({"projects": [
            {"name": "proj-a", "path": str(root)},
            {"name": "remote-only", "repo": "owner/repo"},
            {"name": "gone", "path": str(d / "missing")},
        ]}))
        default_registry = home / ".waystone" / "projects.json"
        default_registry.parent.mkdir(parents=True, exist_ok=True)
        default_registry.write_bytes(registry.read_bytes())
        return root, home, registry

    def _delegation(self, rec, task_id, state, *, verified):
        (rec / "artifact").mkdir(parents=True)
        (rec / "exposure.json").write_text(_json.dumps({"task_id": task_id}))
        (rec / "status.json").write_text(_json.dumps({"state": state}))
        verification = [{"cmd": "pytest", "rc": 0}] if verified else []
        (rec / "artifact" / "contract.yaml").write_text(yaml.safe_dump({
            "delegate_report": {"present": True, "verification": verification}}))

    def _rows(self, out):
        return [_json.loads(ln) for ln in (out / "evidence.jsonl").read_text().splitlines() if ln]

    def test_projection_normalizes_both_review_sources_and_delegations(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, registry = self._fixture(d)
            out = Path(d) / "out"
            coverage = _run_with_home(
                home, lambda: improve.run_evidence(registry, out, set()))
            rows = self._rows(out)
            task_rows = {r["task_id"]: r for r in rows if "task_id" in r}
            self.assertEqual(sorted(task_rows), ["feat/deleg-only", "fix/open", "fix/task-only"])
            # source=triage uses task_id; source=task uses id. Both become the same task_id field.
            basic = ("round", "severity", "status", "type")
            self.assertEqual([{key: finding[key] for key in basic}
                              for finding in task_rows["fix/open"]["findings"]], [
                {"round": "2026-01-01-r1", "severity": "blocker", "status": "REAL",
                 "type": "unknown"}])
            self.assertEqual([{key: finding[key] for key in basic}
                              for finding in task_rows["fix/task-only"]["findings"]], [
                {"round": "2026-01-01-r1", "severity": "major", "status": "done",
                 "type": "unknown"}])
            self.assertEqual(task_rows["fix/open"]["findings"][0]["review_origin"],
                             "2026-01-01-r1")
            self.assertEqual(task_rows["fix/open"]["delegations"], [
                {"binding": None, "did": "did-unverified", "env": None,
                 "overlays_active": [],
                "scope_drift": {
                    "changed_files": [], "coverage_reason": "missing-packet",
                    "declared_scope": [], "evaluable": False, "fired": False,
                    "outside_scope": [],
                    "provenance": "unknown", "rule": "packet-declared-scope-v2",
                },
                 "start_level": None,
                 "state": "needs-review", "verification_present": False}])
            self.assertTrue(task_rows["feat/deleg-only"]["delegations"][0]["verification_present"])
            for row in task_rows.values():
                self.assertEqual(row["join_key"], "task-id")
                self.assertEqual(row["provenance"], "explicit")
            self.assertEqual(coverage["unlinked_findings"], 1)
            self.assertEqual(coverage["projects_scanned"], ["proj-a"])
            self.assertEqual([x["project"] for x in coverage["projects_skipped"]],
                             ["gone", "remote-only"])
            self.assertEqual(rows[-1]["coverage"], coverage)

    def test_evidence_link_lens_counts_join_candidates_without_causality_claim(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, registry = self._fixture(d)
            out = Path(d) / "out"
            _run_with_home(home, lambda: improve.run_evidence(registry, out, set()))
            facts = improve.run_audit(out)
            lens = next(x for x in facts["lenses"] if x["lens"] == "evidence_link")
            self.assertEqual(lens["rule"], "evidence-link-v1")
            self.assertEqual(lens["provenance"], "explicit")
            self.assertEqual(lens["per_project"]["proj-a"], {
                "tasks_with_findings": 2,
                "tasks_with_delegations": 2,
                "tasks_joined": 1,
                "unverified_delegations_with_open_severe_findings": 1,
                "acceptance_event_bound_tasks": 0,
                "acceptance_status_approximation_tasks": 3,
            })
            self.assertLessEqual(len(lens["examples"]), 5)
            self.assertEqual(lens["round_session_mapping"], {"provenance": "unknown"})

    def test_evidence_link_uses_joined_registry_status_for_openness(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, registry = self._fixture(d)
            tasks = yaml.safe_load((root / "tasks.yaml").read_text())
            next(t for t in tasks["tasks"] if t["id"] == "fix/open")["status"] = "done"
            (root / "tasks.yaml").write_text(yaml.safe_dump(tasks, sort_keys=False))
            out = Path(d) / "out"
            _run_with_home(home, lambda: improve.run_evidence(registry, out, set()))
            rows = {r["task_id"]: r for r in self._rows(out) if "task_id" in r}
            facts = improve.run_audit(out)
            lens = next(x for x in facts["lenses"] if x["lens"] == "evidence_link")
            self.assertEqual(rows["fix/open"]["findings"][0]["status"], "REAL")
            self.assertEqual(rows["fix/open"]["task_status"], "done")
            self.assertEqual(
                lens["per_project"]["proj-a"]
                    ["unverified_delegations_with_open_severe_findings"],
                0)

    def test_non_review_task_raw_triage_link_projects_and_joins(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, registry = self._fixture(d)
            feedback = root / "docs" / "reviews" / "2026-01-01-r1-feedback.md"
            feedback.write_text(feedback.read_text().replace(
                "| JW-GPT-002 — unknown link | `major` | NEEDS-RULING | ev | |",
                "| JW-GPT-002 — external task | `major` | NEEDS-RULING | ev | `feat/deleg-only` |"))
            ddir = _run_with_home(home, lambda: delegate._delegations_dir(root))
            self._delegation(
                ddir / "did-unverified-external", "feat/deleg-only", "needs-review", verified=False)
            out = Path(d) / "out"
            _run_with_home(home, lambda: improve.run_evidence(registry, out, set()))
            rows = {r["task_id"]: r for r in self._rows(out) if "task_id" in r}
            basic = ("round", "severity", "status", "type")
            self.assertEqual([{key: finding[key] for key in basic}
                              for finding in rows["feat/deleg-only"]["findings"]], [{
                "round": "2026-01-01-r1", "severity": "major", "status": "NEEDS-RULING",
                "type": "unknown"}])
            facts = improve.run_audit(out)
            lens = next(x for x in facts["lenses"] if x["lens"] == "evidence_link")
            self.assertEqual(lens["per_project"]["proj-a"]["tasks_joined"], 2)
            self.assertEqual(
                lens["per_project"]["proj-a"]
                    ["unverified_delegations_with_open_severe_findings"],
                2)

    def test_byte_identical_reruns_and_cli_project_filter(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, registry = self._fixture(d)
            o1, o2 = Path(d) / "o1", Path(d) / "o2"
            _run_with_home(home, lambda: improve.run_evidence(registry, o1, {"proj-a"}))
            _run_with_home(home, lambda: improve.run_evidence(registry, o2, {"proj-a"}))
            self.assertEqual((o1 / "evidence.jsonl").read_bytes(),
                             (o2 / "evidence.jsonl").read_bytes())
            import contextlib
            import io
            out = home / ".waystone" / "improve" / "cli"
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: improve.main(
                    ["evidence", "--user-wide", "--out", str(out),
                     "--project", "proj-a"]))
            self.assertEqual(rc, 0)
            self.assertEqual([r for r in self._rows(o1) if "task_id" in r],
                             [r for r in self._rows(out) if "task_id" in r])


class ImproveL2BTests(unittest.TestCase):
    """Design-fidelity L2-B: five observation lenses and four evidence joins."""

    @staticmethod
    def _project(base: Path, tasks_text: str) -> tuple[Path, Path]:
        root = base / "repo"
        root.mkdir()
        (root / ".waystone.yml").write_text(
            "version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
        (root / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks:\n" + tasks_text)
        (root / "docs" / "reviews").mkdir(parents=True)
        registry = base / "projects.json"
        registry.write_text(_json.dumps({"projects": [{"name": "demo", "path": str(root)}]}))
        return root, registry

    @staticmethod
    def _evidence_rows(path: Path) -> list[dict]:
        return [_json.loads(line) for line in path.read_text().splitlines() if line]

    @staticmethod
    def _audit_twice(out: Path) -> dict:
        facts = improve.run_audit(out)
        first = (out / "facts.json").read_bytes()
        improve.run_audit(out)
        if first != (out / "facts.json").read_bytes():
            raise AssertionError("facts.json is not byte-stable")
        return facts

    def test_worker_scope_drift_helper_and_lens_are_byte_stable(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            record = out / "record"
            (record / "artifact").mkdir(parents=True)
            (record / "packet.yaml").write_text(yaml.safe_dump({
                "schema": "waystone-packet-1",
                "task": {"id": "feat/x", "notes": "Only `src/` and `tests/test_api.py`."},
                "declared_scope": ["src", "tests/test_api.py"],
                "acceptance": ["Keep changes inside `src/` and `tests/test_api.py`."],
            }))
            (record / "artifact" / "contract.yaml").write_text(yaml.safe_dump({
                "changed_files": [
                    {"path": "src/api.py", "status": "M"},
                    {"path": "tests/test_api.py", "status": "M"},
                    {"path": "docs/readme.md", "status": "M"},
                ],
            }))
            drift = common.delegation_scope_drift(record)
            self.assertEqual(drift["outside_scope"], ["docs/readme.md"])
            self.assertEqual(drift["provenance"], "explicit")
            unknown_record = out / "record-unknown"
            (unknown_record / "artifact").mkdir(parents=True)
            (unknown_record / "packet.yaml").write_text(yaml.safe_dump({
                "schema": "waystone-packet-1", "task": {"id": "feat/y", "notes": "refactor"},
                "acceptance": ["tests pass"],
            }))
            (unknown_record / "artifact" / "contract.yaml").write_text(yaml.safe_dump({
                "changed_files": [{"path": "src/y.py", "status": "M"}],
            }))
            unknown = common.delegation_scope_drift(unknown_record)
            self.assertEqual(unknown["coverage_reason"], "scope-unknown")
            _write_jsonl(out / "evidence.jsonl", [{
                "project": "demo", "task_id": "feat/x", "findings": [],
                "delegations": [{"did": "d1", "scope_drift": drift},
                                {"did": "d2", "scope_drift": unknown}],
            }])
            lens = {item["lens"]: item for item in self._audit_twice(out)["lenses"]}[
                "worker_scope_drift"]
            self.assertEqual(lens["per_project"]["demo"]["delegations_drifted"], 1)
            self.assertEqual(lens["per_project"]["demo"]["outside_files"], 1)
            self.assertEqual(lens["per_project"]["demo"]["coverage"],
                             {"scope-unknown": 1})

    def test_warn_friction_counts_rule_boundary_and_round_trends_byte_stably(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, registry = self._project(base, "  - id: feat/x\n    title: x\n    status: active\n")
            state = root / ".waystone"
            (state / "overlay").mkdir(parents=True)
            _write_jsonl(state / "overlay" / "warnings.jsonl", [
                {"at": "2026-07-01T00:30:00+00:00", "boundary": "delegate-run",
                 "policy_identity": {"layer": "project", "id": "d/a"},
                 "origin_delta_id": "d/a", "rule": "rule-a", "delta_status": "warning",
                 "event": "fire", "message": "x", "context": {"task_ids": ["feat/x"]}},
                {"at": "2026-07-02T00:30:00+00:00", "boundary": "round-close",
                 "policy_identity": {"layer": "project", "id": "d/a"},
                 "origin_delta_id": "d/a", "rule": "rule-a", "delta_status": "observing",
                 "event": "fire", "message": "x", "context": {"task_ids": ["feat/x"]}},
                {"at": "2026-07-02T00:40:00+00:00", "boundary": "round-close",
                 "policy_identity": {"layer": "project", "id": "d/a"},
                 "origin_delta_id": "d/a", "rule": "rule-a", "delta_status": "observing",
                 "event": "conflict", "message": "x", "context": {"task_ids": ["feat/x"]}},
            ])
            exposure = state / "exposure"
            exposure.mkdir()
            for rid, at in (("2026-07-01-r1", "2026-07-01T01:00:00+00:00"),
                            ("2026-07-02-r2", "2026-07-02T01:00:00+00:00")):
                (exposure / f"round-{rid}.json").write_text(_json.dumps({
                    "schema": "waystone-round-exposure-1", "round_id": rid, "at": at,
                    "session_id": None, "overlays_active": [],
                }))
            o1, o2 = base / "o1", base / "o2"
            improve.run_evidence(registry, o1, set())
            improve.run_evidence(registry, o2, set())
            self.assertEqual((o1 / "evidence.jsonl").read_bytes(),
                             (o2 / "evidence.jsonl").read_bytes())
            lens = {item["lens"]: item for item in self._audit_twice(o1)["lenses"]}[
                "warn_friction"]
            project = lens["per_project"]["demo"]
            self.assertEqual(project["by_rule"]["rule-a"], {"conflict": 1, "fire": 2})
            self.assertEqual(project["by_boundary"]["round-close"], {"conflict": 1, "fire": 1})
            self.assertEqual(project["by_rule_boundary"]["rule-a"]["round-close"],
                             {"conflict": 1, "fire": 1})
            self.assertEqual([row["fire"] for row in project["recent_rounds"]], [1, 1])

    def test_delegation_opportunity_joins_task_session_and_reports_unknown_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            _write_jsonl(out / "sessions.jsonl", [{
                "project": "demo", "kind": "main", "session_id": "s-main", "file": "/s",
                "tools": {"by_category": {"file_write": 4, "shell": 3}}, "delegations": 0,
                "retry_loops": {"count": 1},
                "context_heavy": {"max_result_bytes": 150000},
                "usage": {"input": 120000, "output": 1000, "cache_read": 0,
                          "cache_creation": 0},
            }])
            _write_jsonl(out / "evidence.jsonl", [
                {"project": "demo", "task_id": "feat/direct", "findings": [], "delegations": [],
                 "task_context": {"session_id": "s-main", "acceptance_criteria": 2}},
                {"project": "demo", "task_id": "feat/unknown", "findings": [], "delegations": [],
                 "task_context": {"session_id": None, "acceptance_criteria": 1}},
            ])
            lens = {item["lens"]: item for item in self._audit_twice(out)["lenses"]}[
                "delegation_opportunity"]
            candidates = [row for row in self._evidence_rows(out / "audit_candidates.jsonl")
                          if row["lens"] == "delegation_opportunity"]
            self.assertEqual([row["task_id"] for row in candidates], ["feat/direct"])
            self.assertEqual(candidates[0]["provenance"], "inferred")
            self.assertEqual(lens["coverage"]["task_session_unknown"], 1)

    def test_env_unpreparedness_separates_env_prep_and_dependency_signatures(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            signatures = improve._env_error_facts([
                {"is_error": True, "content_text": "ModuleNotFoundError: No module named 'demo'",
                 "stderr_head": None, "ordinal": 9},
                {"is_error": True, "content_text": "zsh: command not found: demo",
                 "stderr_head": None, "ordinal": 10},
            ])
            self.assertEqual(signatures["rule"], "env-error-signature-v1")
            _write_jsonl(out / "sessions.jsonl", [{
                "project": "demo", "kind": "main", "session_id": "s1", "file": "/s1",
                "tools": {"by_category": {}}, "errors": {"api": 0, "tool": 2, "parse": 0},
                "env_unpreparedness": signatures,
            }])
            _write_jsonl(out / "evidence.jsonl", [{
                "project": "demo", "task_id": "feat/x", "findings": [],
                "delegations": [{"did": "d1", "state": "failed-env",
                                 "env": {"prep": "detected:python", "rc": 1}}],
            }])
            lens = {item["lens"]: item for item in self._audit_twice(out)["lenses"]}[
                "env_unpreparedness"]
            project = lens["per_project"]["demo"]
            self.assertEqual(project["env_prep_failures"], 1)
            self.assertEqual(project["dependency_error_signatures"],
                             {"command-not-found": 1, "python-module-missing": 1})

    def test_finding_concentration_binds_role_and_explicit_path_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, registry = self._project(base, "  - id: feat/x\n    title: x\n    status: active\n")
            rid = "2026-07-01-r1"
            request = root / "docs" / "reviews" / f"{rid}-request.md"
            request.write_text("# request\n")
            (root / "docs" / "reviews" / f"{rid}-feedback.md").write_text(
                "## Findings (triage skeleton v2)\n"
                "| finding | severity | type | verdict | evidence | task id |\n"
                "|---|---|---|---|---|---|\n"
                "| JW-GPT-001 — x | major | correctness | REAL | `scripts/improve.py:10` | feat/x |\n")
            exposure = root / ".waystone" / "exposure"
            exposure.mkdir(parents=True)
            (exposure / f"round-{rid}.json").write_text(_json.dumps({
                "schema": "waystone-round-exposure-1", "round_id": rid,
                "at": "2026-07-01T01:00:00+00:00", "session_id": "s-main",
                "overlays_active": [],
            }))
            out = base / "out"
            improve.run_reviews(registry, out)
            _write_jsonl(out / "sessions.jsonl", [{
                "project": "demo", "kind": "main", "session_id": "s-main", "file": "/s",
                "tools": {"by_category": {}},
            }])
            lens = {item["lens"]: item for item in self._audit_twice(out)["lenses"]}[
                "finding_concentration"]
            project = lens["per_project"]["demo"]
            self.assertEqual(project["tasks_with_findings"], 1)
            candidates = [row for row in self._evidence_rows(out / "audit_candidates.jsonl")
                          if row["lens"] == "finding_concentration"]
            self.assertEqual([(row["task_id"], row["findings"]) for row in candidates],
                             [("feat/x", 1)])
            self.assertEqual(project["by_role"], {"unknown": 1})
            self.assertEqual(project["by_project_area"], {"scripts": 1})
            self.assertEqual(project["round_session_unknown"], 1)

    def test_reviewed_sha_projection_is_byte_stable(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, registry = self._project(base, "  - id: feat/x\n    title: x\n    status: active\n")
            rid = "2026-07-01-r1"
            marker = review.emit_marker("review-cycle", {
                "round_id": rid, "cycle": 1, "target_sha": "a" * 40,
                "base_sha": "b" * 40, "reviewers": ["codex"],
            })
            (root / "docs" / "reviews" / f"{rid}-request.md").write_text(marker)
            o1, o2 = base / "o1", base / "o2"
            improve.run_reviews(registry, o1)
            improve.run_reviews(registry, o2)
            self.assertEqual((o1 / "reviews.jsonl").read_bytes(),
                             (o2 / "reviews.jsonl").read_bytes())
            row = self._evidence_rows(o1 / "reviews.jsonl")[0]
            self.assertEqual(row["target_sha"], "a" * 40)
            self.assertEqual(row["base_sha"], "b" * 40)
            self.assertEqual(row["review_binding_provenance"], "explicit")

    def test_taxonomy_recurrence_excludes_unknown_and_is_byte_stable(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            _write_jsonl(out / "reviews.jsonl", [
                {"project": "demo", "round_id": "r1", "findings": [
                    {"id": "f1", "type": "correctness", "status": "REAL", "source": "triage"},
                    {"id": "u1", "type": "unknown", "status": "REAL", "source": "triage"}], "counts": {}},
                {"project": "demo", "round_id": "r2", "findings": [
                    {"id": "f2", "type": "correctness", "status": "REAL", "source": "triage"},
                    {"id": "u2", "type": "unknown", "status": "REAL", "source": "triage"}], "counts": {}},
            ])
            lens = {item["lens"]: item for item in self._audit_twice(out)["lenses"]}[
                "finding_concentration"]
            project = lens["per_project"]["demo"]
            self.assertEqual(project["recurring_types"], [{
                "type": "correctness", "round_count": 2,
                "first_round": "r1", "last_round": "r2",
                "occurrences": 2, "recurrences": 1,
            }])
            self.assertEqual(project["unknown_type_excluded"], 2)
            self.assertEqual(project["round_session_unknown"], 2)
            self.assertEqual(project["by_role"], {"unknown": 4})

    def test_route_guard_join_links_round_exposure_and_warning_context(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            tasks_text = (
                "  - id: feat/x\n    title: x\n    status: active\n"
                "    round: 2026-07-01-r1\n    session_id: s-main\n")
            root, registry = self._project(base, tasks_text)
            exposure = root / ".waystone" / "exposure"
            exposure.mkdir(parents=True)
            (exposure / "round-2026-07-01-r1.json").write_text(_json.dumps({
                "schema": "waystone-round-exposure-1", "round_id": "2026-07-01-r1",
                "at": "2026-07-01T01:00:00+00:00", "session_id": "s-main",
                "overlays_active": [{
                    "identity": {"layer": "project", "id": "verification_debt/x"},
                    "origin_delta_id": "verification_debt/x", "status": "warning"}],
                "bindings": {"implementer": "codex:gpt-5.6"},
            }))
            warnings = root / ".waystone" / "overlay" / "warnings.jsonl"
            warnings.parent.mkdir(parents=True)
            _write_jsonl(warnings, [{
                "at": "2026-07-01T00:30:00+00:00", "boundary": "round-close",
                "policy_identity": {"layer": "project", "id": "review_association/x"},
                "origin_delta_id": "review_association/x",
                "rule": "round-close-open-findings-v1",
                "delta_status": "warning", "event": "fire", "message": "x",
                "context": {"task_ids": ["feat/x"], "round_id": "2026-07-01-r1"},
            }])
            alias = base / "repo-old-checkout"
            registry_data = _json.loads(registry.read_text())
            registry_data["projects"][0]["aliases"] = [str(alias.resolve())]
            registry.write_text(_json.dumps(registry_data))
            o1, o2 = base / "o1", base / "o2"
            improve.run_evidence(registry, o1, set(), project_root=alias)
            improve.run_evidence(registry, o2, set(), project_root=alias)
            self.assertEqual((o1 / "evidence.jsonl").read_bytes(),
                             (o2 / "evidence.jsonl").read_bytes())
            row = next(row for row in self._evidence_rows(o1 / "evidence.jsonl")
                       if row.get("task_id") == "feat/x")
            self.assertEqual(row["route_guard"]["round_exposure"]["overlays_active"],
                             [{"identity": {"layer": "project", "id": "verification_debt/x"},
                               "origin_delta_id": "verification_debt/x",
                               "status": "warning"}])
            warning_id = row["route_guard"]["warning_refs"][0]
            normalized = {item["warning_id"]: item for item in
                          self._evidence_rows(o1 / "evidence_warnings.jsonl")}
            self.assertEqual(normalized[warning_id]["rule"], "round-close-open-findings-v1")

    def test_acceptance_event_binding_precedes_honest_status_approximation(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            tasks_text = (
                "  - id: fix/verdict\n    title: verdict\n    status: active\n"
                "    severity: major\n    origin: review-r1\n"
                "  - id: fix/round\n    title: round\n    status: done\n"
                "    severity: major\n    origin: review-r1\n    round: r2\n"
                "  - id: fix/fallback\n    title: fallback\n    status: done\n"
                "    severity: major\n    origin: review-r1\n")
            root, registry = self._project(base, tasks_text)
            record = root / ".waystone" / "delegations" / "did-1"
            (record / "artifact").mkdir(parents=True)
            (record / "exposure.json").write_text(_json.dumps({"task_id": "fix/verdict"}))
            (record / "status.json").write_text(_json.dumps({
                "state": "applied", "accepted_at": "2026-07-02T01:00:00+00:00",
            }))
            (record / "artifact" / "verdict-1.json").write_text(_json.dumps({
                "schema": "waystone-verdict-1", "decision": "apply",
                "decided_by": "main-session", "criteria": [], "agent_checks": [],
                "warnings_seen": [], "rationale": "accepted", "limitations": [],
                "judged_at": "2026-07-02T00:00:00+00:00", "provenance": "main-session",
                "verify_number": None, "profile_fingerprint": None,
                "artifact_digests": {
                    "contract_sha256": "sha256:" + "a" * 64,
                    "patch_sha256": None, "verify_sha256": None,
                },
            }))
            exposure = root / ".waystone" / "exposure"
            exposure.mkdir(parents=True)
            (exposure / "round-r2.json").write_text(_json.dumps({
                "schema": "waystone-round-exposure-1", "round_id": "r2",
                "at": "2026-07-03T00:00:00+00:00", "session_id": "s2",
                "overlays_active": [],
            }))
            out = base / "out"
            improve.run_evidence(registry, out, set())
            first = (out / "evidence.jsonl").read_bytes()
            improve.run_evidence(registry, out, set())
            self.assertEqual(first, (out / "evidence.jsonl").read_bytes())
            rows = {row["task_id"]: row for row in self._evidence_rows(out / "evidence.jsonl")
                    if row.get("task_id")}
            self.assertEqual(rows["fix/verdict"]["acceptance"]["event"], "delegation-apply")
            self.assertEqual(rows["fix/verdict"]["acceptance"]["provenance"], "explicit")
            self.assertEqual(rows["fix/round"]["acceptance"]["event"], "round-close")
            self.assertEqual(rows["fix/fallback"]["acceptance"]["provenance"],
                             "current-task-state-approximation")


class ImproveL2BAdversarialTests(unittest.TestCase):
    """Adversarial review findings F1-F12: evidence must never be upgraded by guesswork."""

    @staticmethod
    def _project(base: Path, tasks_text: str, *, mode: str = "packet") -> tuple[Path, Path]:
        root, registry = ImproveL2BTests._project(base, tasks_text)
        config = yaml.safe_load((root / ".waystone.yml").read_text())
        config["review"] = {"mode": mode}
        (root / ".waystone.yml").write_text(yaml.safe_dump(config, sort_keys=False))
        return root, registry

    @staticmethod
    def _verdict(*, decision: str = "apply", at: str = "2026-07-02T00:00:00+00:00") -> dict:
        return {
            "schema": "waystone-verdict-1", "decision": decision,
            "decided_by": "main-session", "criteria": [], "agent_checks": [],
            "warnings_seen": [], "rationale": "main accepted", "limitations": [],
            "judged_at": at, "provenance": "main-session", "verify_number": None,
            "profile_fingerprint": None,
            "artifact_digests": {
                "contract_sha256": "sha256:" + "a" * 64,
                "patch_sha256": None, "verify_sha256": None,
            },
        }

    @staticmethod
    def _rows(path: Path) -> list[dict]:
        return [_json.loads(line) for line in path.read_text().splitlines() if line]

    def test_f1_round_binding_rejects_foreign_marker_and_pr_projection_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, registry = self._project(
                base, "  - id: feat/alpha\n    title: alpha task here\n    status: active\n")
            rid = "2026-07-15-l2-b"
            request = root / "docs" / "reviews" / f"{rid}-request.md"
            request.write_text(review.emit_marker("review-cycle", {
                "round_id": "2026-07-14-other", "cycle": 1,
                "target_sha": "a" * 40, "base_sha": "b" * 40,
                "reviewers": ["codex"],
            }))
            out = base / "out"
            improve.run_reviews(registry, out)
            row = self._rows(out / "reviews.jsonl")[0]
            self.assertEqual(row["review_binding_provenance"], "unknown")
            self.assertEqual(row["review_binding_reason"], "round-mismatched-review-marker")

            review.write_round_request_binding(
                root, rid, "c" * 40, "d" * 40, ["codex"], mode="packet")
            improve.run_reviews(registry, out)
            row = self._rows(out / "reviews.jsonl")[0]
            self.assertEqual(row["review_binding_reason"], "missing-structured-reviewing-line")
            request.write_text(
                f"# Review Request — {rid}\n\n"
                f"- Reviewing: {'c' * 40}   (diff against {'d' * 40})\n")
            improve.run_reviews(registry, out)
            row = self._rows(out / "reviews.jsonl")[0]
            self.assertEqual((row["target_sha"], row["base_sha"]), ("c" * 40, "d" * 40))
            self.assertEqual(row["review_binding_source"], "round-request-sidecar")

            config = yaml.safe_load((root / ".waystone.yml").read_text())
            config["review"]["mode"] = "pr"
            (root / ".waystone.yml").write_text(yaml.safe_dump(config, sort_keys=False))
            improve.run_reviews(registry, out)
            row = self._rows(out / "reviews.jsonl")[0]
            self.assertEqual(row["review_binding_provenance"], "unknown")
            self.assertEqual(row["review_binding_reason"], "missing-pr-freeze-sidecar")

    def test_f2_only_delegate_canonical_verdict_becomes_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            record = Path(d) / "delegation"
            (record / "artifact").mkdir(parents=True)
            bad = record / "artifact" / "verdict-1.json"
            bad.write_text(_json.dumps({
                "schema": "waystone-verdict-1", "decision": "apply",
                "at": "2026-07-01T00:00:00+00:00",
            }))
            acceptance, skipped = improve._latest_verdict_acceptance(record)
            self.assertIsNone(acceptance)
            self.assertEqual(skipped, 1)
            bad.write_text(_json.dumps(self._verdict()))
            loaded = delegate.load_canonical_verdict(bad)
            self.assertEqual(loaded["decision"], "apply")
            acceptance, skipped = improve._latest_verdict_acceptance(record)
            self.assertIsNone(acceptance)
            self.assertEqual(skipped, 0)
            (record / "status.json").write_text(_json.dumps({
                "state": "applied", "accepted_at": "2026-07-02T01:00:00+00:00",
            }))
            acceptance, skipped = improve._latest_verdict_acceptance(record)
            self.assertEqual(acceptance["event"], "delegation-apply")
            self.assertEqual(acceptance["accepted_at"], "2026-07-02T01:00:00+00:00")

    def test_f3_latest_reclose_exposure_and_verdict_kind_authority(self):
        exposures = [
            {"round_id": "r1", "at": "2026-07-03T00:00:00+00:00", "session_id": "old"},
            {"round_id": "r1", "at": "2026-07-04T00:00:00+00:00", "session_id": "new"},
        ]
        self.assertEqual(improve._round_session_binding("r1", exposures)[:2], ("new", "explicit"))
        same_time = [
            {"round_id": "r2", "at": "2026-07-04T00:00:00+00:00", "session_id": "two",
             "_file": "/x/round-r2-2.json"},
            {"round_id": "r2", "at": "2026-07-04T00:00:00+00:00", "session_id": "ten",
             "_file": "/x/round-r2-10.json"},
        ]
        self.assertEqual(improve._round_session_binding("r2", same_time)[:2],
                         ("ten", "explicit"))
        task = {"status": "done", "round": "r1"}
        delegations = [{"did": "d1", "judgment": {
            "event": "delegation-verdict", "judged_at": "2026-07-02T00:00:00+00:00",
            "decision": "apply", "resolved": False, "provenance": "explicit",
        }}]
        self.assertEqual(
            improve._task_acceptance(task, delegations, exposures)["event"],
            "round-close")

    def test_f4_conflicting_warning_context_is_quarantined(self):
        warning = {
            "boundary": "delegate-run", "rule": "delegation-verification-evidence-v1",
            "context": {"task_id": "feat/a", "delegation_id": "did-b"},
        }
        refs, reason = improve._warning_task_ids(warning, {"did-b": "feat/b"})
        self.assertEqual(refs, set())
        self.assertEqual(reason, "conflicting-context")
        malformed = {**warning, "context": {"task_ids": "feat/a"}}
        refs, reason = improve._warning_task_ids(malformed, {})
        self.assertEqual(refs, set())
        self.assertEqual(reason, "invalid-context-schema")

    def test_f5_execution_kind_never_masquerades_as_profile_role(self):
        self.assertEqual(improve._session_role({
            "kind": "subagent", "agent_meta": {"agentType": "Explore"},
        }), "unknown")
        self.assertEqual(improve._session_role({"kind": "main"}), "main")
        self.assertNotIn("Explore", delegate.PROFILE_ROLES)

    def test_f6_recurrence_counts_verified_real_only(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            _write_jsonl(out / "reviews.jsonl", [
                {"project": "demo", "round_id": "r1", "findings": [{
                    "id": "f1", "type": "correctness", "status": "REAL",
                    "source": "triage"}], "counts": {}},
                {"project": "demo", "round_id": "r2", "findings": [{
                    "id": "f2", "type": "correctness", "status": "NEEDS-RULING",
                    "source": "triage"}], "counts": {}},
            ])
            lens = next(row for row in improve.run_audit(out)["lenses"]
                        if row["lens"] == "finding_concentration")
            project = lens["per_project"]["demo"]
            self.assertEqual(project["recurring_types"], [])
            self.assertEqual(project["recurrence_status_coverage"], {"NEEDS-RULING": 1})

    def test_f7_remediation_rounds_and_reopen_transition_survive_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, registry = self._project(base, (
                "  - id: fix/reopened\n    title: reopened finding task\n    status: active\n"
                "    severity: major\n    origin: review-r1\n    round: 2026-07-03-r3\n"))
            (root / "tasks.archive.yaml").write_text(yaml.safe_dump({
                "version": 1, "project": "demo", "tasks": [{
                    "id": "fix/reopened", "title": "reopened finding task", "status": "done",
                    "severity": "major", "origin": "review-r1", "round": "2026-07-02-r2",
                }],
            }, sort_keys=False))
            out = base / "out"
            improve.run_reviews(registry, out)
            finding = self._rows(out / "reviews.jsonl")[0]["findings"][0]
            self.assertEqual(finding["review_origin"], "r1")
            self.assertEqual(finding["fixing_rounds"], ["2026-07-02-r2", "2026-07-03-r3"])
            self.assertEqual(finding["reopen_count"], 1)
            lens = next(row for row in improve.run_audit(out)["lenses"]
                        if row["lens"] == "finding_concentration")
            self.assertEqual(lens["per_project"]["demo"]["distinct_remediation_rounds"], 2)
            self.assertEqual(lens["per_project"]["demo"]["reopens"], 1)

    def test_f8_missing_evidence_keeps_env_prep_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            _write_jsonl(out / "sessions.jsonl", [{
                "project": "demo", "kind": "main", "session_id": "s1", "file": "/s1",
                "tools": {"by_category": {}},
                "env_unpreparedness": {"signatures": {"command-not-found": 1}, "examples": []},
            }])
            lens = next(row for row in improve.run_audit(out)["lenses"]
                        if row["lens"] == "env_unpreparedness")
            project = lens["per_project"]["demo"]
            self.assertIsNone(project["env_prep_failures"])
            self.assertFalse(project["evidence_available"])
            self.assertEqual(project["dependency_error_signatures"], {"command-not-found": 1})

    def test_f9_orphan_rows_are_in_final_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, registry = self._project(
                base, "  - id: feat/known\n    title: known task here\n    status: active\n")
            record = root / ".waystone" / "delegations" / "did-orphan"
            (record / "artifact").mkdir(parents=True)
            (record / "exposure.json").write_text(_json.dumps({"task_id": "feat/orphan"}))
            (record / "status.json").write_text(_json.dumps({"state": "needs-review"}))
            out = base / "out"
            coverage = improve.run_evidence(registry, out, set())
            self.assertEqual(coverage["row_totals"], {"tasks": 2, "findings": 0, "delegations": 1})
            self.assertEqual(coverage["task_session_unknown"], 2)

    def test_f10_warning_sweep_is_linear_and_rows_are_normalized_once(self):
        import time

        count = 4000
        warnings = [{
            "at": f"2026-07-15T00:{i // 60:02d}:{i % 60:02d}+00:00",
            "boundary": "round-close", "rule": "round-close-open-findings-v1",
            "event": "fire", "context": {},
        } for i in range(count)]
        exposures = [{
            "round_id": f"r{i}", "at": warning["at"], "_file": str(i),
        } for i, warning in enumerate(warnings)]
        started = time.monotonic()
        observation = improve._warning_observation("demo", warnings, 0, True, exposures, 0)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(observation["fire"], count)
        self.assertLessEqual(len(observation["recent_rounds"]), 5)

    def test_f11_facts_are_bounded_and_path_line_is_preserved(self):
        parsed = improve._parse_triage(
            "## Findings (triage skeleton v2)\n"
            "| finding | severity | type | verdict | evidence | task id |\n"
            "|---|---|---|---|---|---|\n"
            "| JW-GPT-001 — x | major | scope | REAL | `scripts/improve.py:731` | fix/x |\n")
        self.assertEqual(parsed[0]["evidence_pointers"], ["scripts/improve.py:731"])
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            _write_jsonl(out / "sessions.jsonl", [{
                "project": "demo", "kind": "main", "session_id": f"s{i}",
                "file": f"/logs/s{i}.jsonl", "tools": {"by_category": {"file_write": 4}},
                "delegations": 0, "retry_loops": {"count": 1},
                "context_heavy": {"max_result_bytes": 0}, "usage": {"input": 0},
            } for i in range(12)])
            _write_jsonl(out / "evidence.jsonl", [{
                "project": "demo", "task_id": f"feat/t{i}", "findings": [], "delegations": [],
                "task_context": {"session_id": f"s{i}", "acceptance_criteria": 1},
            } for i in range(12)])
            facts = improve.run_audit(out)
            lens = next(row for row in facts["lenses"] if row["lens"] == "delegation_opportunity")
            self.assertNotIn("candidates", lens)
            self.assertLessEqual(len(lens["examples"]), 5)
            self.assertTrue(all(example.get("pointer") for example in lens["examples"]))
            candidates = self._rows(out / "audit_candidates.jsonl")
            self.assertEqual(len(candidates), 12)

    def test_f12_scope_is_structured_and_packet_text_is_never_mined(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks:\n"
                "  - id: feat/scope\n    title: structured scope task\n    status: active\n"
                "    notes: run https://example.com/a and uv run scripts/improve.py\n"
                "    accept: [tests pass]\n")
            self.assertEqual(tasks.main([
                "set", "feat/scope", str(root), "--scope-add", "scripts",
                "--scope-add", "hooks/scripts/session_context.py",
            ]), 0)
            task = common.load_tasks(root)["tasks"][0]
            self.assertEqual(task["scope"], ["scripts", "hooks/scripts/session_context.py"])
            packet, _ = delegate._build_packet(
                common.load_tasks(root), "feat/scope", [], root)
            self.assertEqual(packet["declared_scope"], task["scope"])
            task.pop("scope")
            packet, _ = delegate._build_packet(
                {"project": "demo", "tasks": [task]}, "feat/scope", [], root)
            self.assertEqual(packet["declared_scope"], [])


class DelegateVerifyTests(unittest.TestCase):
    """0.8.0 M2 §11/§12 — same-base independent verifier transport (synthetic only)."""

    _PROFILE = (
        "schema: waystone-profile-1\nbindings:\n"
        "  implementer: {execution: external-runner, backend: \"codex:gpt-5.6-sol\"}\n"
        "  verifier: {backend: \"codex:gpt-5.6-sol\", "
        "entry: adversarial-review}\n")

    def _setup(self, d, *, committed=True):
        root, home = _deleg_project(d)
        _write_profile(root, self._PROFILE)
        (root / ".gitignore").write_text(".ignored-cache/\n")
        git(root, "add", ".gitignore")
        git(root, "commit", "-qm", "ignore fixture")
        plugin = home / "codex-plugin"
        (plugin / "scripts").mkdir(parents=True)
        (plugin / "scripts" / "codex-companion.mjs").write_text("// synthetic fixture\n")
        registry = home / ".claude" / "plugins" / "installed_plugins.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(_json.dumps({"plugins": {"codex@openai-codex": [
            {"installPath": str(plugin)}]}}))

        def runner(worktree, model, prompt_path, record_dir):
            (worktree / "f.txt").write_text("delegate result\n")
            (worktree / "new.txt").write_text("new result\n")
            (worktree / "blob.bin").write_bytes(bytes(range(256)))
            (record_dir / "last_message.md").write_text("summary")
            if committed:
                git(worktree, "add", "-A")
                git(worktree, "commit", "-qm", "delegate local commit")
            return (0, 0.1)

        _deleg_run(root, home, runner)
        rec = _latest_rec(root, home)
        worktree = _run_with_home(home, lambda: delegate._worktree_path(root, rec.name))
        ignored = worktree / ".ignored-cache" / "keep.txt"
        ignored.parent.mkdir()
        ignored.write_text("keep")
        return root, home, rec, worktree, plugin

    def _with_companion(self, fake, fn):
        orig = delegate._run_companion
        delegate._run_companion = fake
        try:
            return fn()
        finally:
            delegate._run_companion = orig

    def _with_claude_verifier(self, fake, fn):
        orig = delegate._run_claude_verifier
        delegate._run_claude_verifier = fake
        try:
            return fn()
        finally:
            delegate._run_claude_verifier = orig

    def test_claude_verifier_transport_and_schema_artifact(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _plugin = self._setup(d, committed=False)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: 'codex:gpt'}\n"
                "  verifier: {execution: external-runner, backend: 'claude:sonnet', "
                "entry: adversarial-review}\n"))
            payload = {
                "summary": "checked", "findings": [], "limitations": [],
            }
            calls = []

            def fake(worktree, model, focus, record_dir):
                calls.append((worktree, model, focus, record_dir))
                return (0, _json.dumps(payload))

            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: self._with_claude_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(rc, 0)
            self.assertEqual(calls[0][1], "sonnet")
            artifact = _json.loads((rec / "artifact" / "verify-1.json").read_text())
            self.assertEqual(artifact["transport"], "claude-print:read-only")
            self.assertEqual(artifact["backend"], "claude:sonnet")
            self.assertEqual(artifact["payload"], payload)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(artifact["profile_fingerprint"],
                             delegate._load_profile(root)[1])
            self.assertEqual(artifact["base_sha"], contract["base_sha"])
            self.assertEqual(artifact["result_sha"], contract["result_sha"])
            self.assertEqual(artifact["effective_tool_policy"], {
                "tools": ["Read", "Glob", "Grep"], "bash": False,
                "filesystem_postcondition": "git-status+untracked-content-unchanged",
            })
            for delta in ("filesystem", "process", "network"):
                self.assertIn(delta, err.getvalue().lower())

    def test_run_claude_verifier_transport_is_injectable_and_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "review-worktree"
            record = root / "record"
            worktree.mkdir()
            record.mkdir()
            calls = []
            payload = {"summary": "ok", "findings": [], "limitations": []}

            def transport(cmd, **kwargs):
                calls.append((cmd, kwargs))
                wrapped = {"type": "result", "subtype": "success",
                           "structured_output": payload}
                return subprocess.CompletedProcess(cmd, 0, stdout=_json.dumps(wrapped), stderr="")

            rc, output = delegate._run_claude_verifier(
                worktree, "sonnet", "review", record, runner=transport)
            self.assertEqual(rc, 0)
            self.assertEqual(_json.loads(output), payload)
            cmd, kwargs = calls[0]
            self.assertIn("--json-schema", cmd)
            self.assertIn("--permission-mode", cmd)
            self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
            denied = cmd[cmd.index("--disallowedTools") + 1]
            for tool in ("Edit", "Write", "Bash", "WebFetch", "WebSearch"):
                self.assertIn(tool, denied)
            allowed = cmd[cmd.index("--allowedTools") + 1]
            self.assertNotIn("Bash", allowed)
            self.assertNotIn("Bash", cmd[cmd.index("--tools") + 1])
            self.assertEqual(Path(kwargs["cwd"]), worktree)
            self.assertEqual(kwargs["env"]["WAYSTONE_VERIFIER_SESSION"], "1")
            cache = Path(kwargs["env"]["UV_CACHE_DIR"])
            self.assertEqual(cache, record / "runtime" / "uv-cache")
            self.assertFalse(cache.resolve().is_relative_to(worktree.resolve()))

    def test_companion_transport_inherits_guard_and_record_local_cache(self):
        import types

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "review-worktree"
            record = root / "record"
            worktree.mkdir()
            record.mkdir()
            calls = []
            original = delegate.subprocess.run

            def fake(*args, **kwargs):
                calls.append((args, kwargs))
                return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")

            delegate.subprocess.run = fake
            try:
                rc, output = delegate._run_companion(worktree, ["node", "companion"], record)
            finally:
                delegate.subprocess.run = original
            self.assertEqual((rc, output), (0, "{}"))
            env = calls[0][1]["env"]
            self.assertEqual(env["WAYSTONE_VERIFIER_SESSION"], "1")
            cache = Path(env["UV_CACHE_DIR"])
            self.assertEqual(cache, record / "runtime" / "uv-cache")
            self.assertFalse(cache.resolve().is_relative_to(worktree.resolve()))

    def test_claude_verifier_requires_success_structured_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            payload = {"summary": "ok", "findings": [], "limitations": []}
            envelopes = (
                {"type": "result", "structured_output": payload},
                {"type": "result", "subtype": "success", "result": _json.dumps(payload)},
                payload,
            )
            for envelope in envelopes:
                with self.subTest(envelope=envelope):
                    def transport(cmd, **kwargs):
                        return subprocess.CompletedProcess(
                            cmd, 0, stdout=_json.dumps(envelope), stderr="")

                    rc, output = delegate._run_claude_verifier(
                        root, "sonnet", "review", root, runner=transport)
                    self.assertNotEqual(rc, 0)
                    self.assertEqual(output, "")

    def test_success_normalizes_committed_delegate_and_preserves_labels(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, plugin = self._setup(d, committed=True)
            calls = []

            def fake(wt, args, record_dir):
                calls.append((wt, args, record_dir))
                return (0, _json.dumps({
                    "summary": "challenged", "findings": [], "limitations": []}))

            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(rc, 0)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(git(worktree, "rev-parse", "HEAD").stdout.strip(), contract["base_sha"])
            self.assertEqual((worktree / "f.txt").read_text(), "delegate result\n")
            self.assertEqual((worktree / "blob.bin").read_bytes(), bytes(range(256)))
            self.assertTrue((worktree / ".ignored-cache" / "keep.txt").exists())
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            self.assertEqual(len(calls), 1)
            args = calls[0][1]
            self.assertEqual(args[:-1], [
                "node", str(plugin / "scripts" / "codex-companion.mjs"),
                "adversarial-review", "--json", "--wait", "--scope", "working-tree",
                "-C", str(worktree), "-m", "gpt-5.6-sol"])
            self.assertLessEqual(len(args[-1].encode("utf-8")), 1024)
            artifact = _json.loads((rec / "artifact" / "verify-1.json").read_text())
            self.assertEqual(artifact["schema"], "waystone-verify-1")
            self.assertEqual(artifact["backend"], "codex:gpt-5.6-sol")
            self.assertEqual(artifact["provenance"], "independent-verifier")
            self.assertEqual(artifact["payload"]["summary"], "challenged")

    def test_verify_session_hook_does_not_seed_state_in_review_worktree(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, plugin = self._setup(d, committed=False)
            legacy = home / ".codex" / "waystone.pre-0.9" / "profile.yml"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(self._PROFILE)
            legacy_mtime = legacy.stat().st_mtime_ns
            hooks = [
                SCRIPTS.parent / "hooks" / "scripts" / "session_context.sh",
                SCRIPTS.parent / "hooks" / "scripts" / "resume_snapshot.sh",
            ]
            companion = plugin / "scripts" / "codex-companion.mjs"
            companion.write_text(
                "import { spawnSync } from 'node:child_process';\n"
                f"const hooks = {_json.dumps([str(hook) for hook in hooks])};\n"
                f"const cwd = {_json.dumps(str(worktree))};\n"
                "for (const hook of hooks) {\n"
                "  const run = spawnSync('bash', [hook], {\n"
                "    input: JSON.stringify({cwd}), encoding: 'utf8', env: process.env,\n"
                "  });\n"
                "  if (run.status !== 0) {\n"
                "    process.stderr.write(run.stderr || 'verifier lifecycle hook failed');\n"
                "    process.exit(run.status ?? 1);\n"
                "  }\n"
                "}\n"
                "process.stdout.write(JSON.stringify({\n"
                "  summary: 'checked', findings: [], limitations: [],\n"
                "}));\n",
                encoding="utf-8",
            )
            self.assertFalse((worktree / ".waystone").exists())

            old_host = os.environ.pop("WAYSTONE_HOST", None)
            try:
                rc = _run_with_home(
                    home, lambda: delegate.verify_delegation(root, rec.name))
            finally:
                if old_host is not None:
                    os.environ["WAYSTONE_HOST"] = old_host

            self.assertEqual(rc, 0)
            self.assertFalse((worktree / ".waystone").exists())
            self.assertEqual(legacy.read_text(), self._PROFILE)
            self.assertEqual(legacy.stat().st_mtime_ns, legacy_mtime)
            self.assertTrue((rec / "artifact" / "verify-1.json").is_file())

    def test_verifier_session_all_manifest_hooks_are_hermetic(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "review-worktree"
            record = Path(d) / "record"
            root.mkdir()
            record.mkdir()
            init_repo(root)
            (root / ".gitignore").write_text(".waystone/\n")
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            (root / "ROADMAP.md").write_text("unchanged\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "verifier hook fixture")
            marker = root / ".waystone" / "boundary-hooks-enabled"
            marker.parent.mkdir()
            marker.touch()

            manifest = _json.loads(
                (SCRIPTS.parent / "hooks" / "hooks.json").read_text())["hooks"]
            payloads = {
                "PreToolUse": {
                    "hook_event_name": "PreToolUse", "tool_name": "Read", "cwd": str(root),
                    "tool_input": {"file_path": str(root / "tasks.yaml")},
                },
                "SessionStart": {"hook_event_name": "SessionStart", "cwd": str(root)},
                "PreCompact": {"hook_event_name": "PreCompact", "cwd": str(root)},
                "SessionEnd": {"hook_event_name": "SessionEnd", "cwd": str(root)},
                "PostToolUse": {
                    "hook_event_name": "PostToolUse", "tool_name": "Edit", "cwd": str(root),
                    "tool_input": {"file_path": str(root / "tasks.yaml")},
                },
                "Stop": {"hook_event_name": "Stop", "cwd": str(root)},
            }
            env = {
                **os.environ,
                "CLAUDE_PLUGIN_ROOT": str(SCRIPTS.parent),
                "UV_CACHE_DIR": str(record / "runtime" / "uv-cache"),
                "UV_OFFLINE": "1",
                "WAYSTONE_VERIFIER_SESSION": "1",
            }
            before = delegate._verify_worktree_state(root)
            for event, groups in manifest.items():
                for group in groups:
                    for hook in group["hooks"]:
                        with self.subTest(event=event, command=hook["command"]):
                            result = subprocess.run(
                                ["bash", "-c", hook["command"]],
                                input=_json.dumps(payloads[event]), cwd=root,
                                capture_output=True, text=True, env=env,
                            )
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(result.stdout, "")
                            self.assertEqual(result.stderr, "")
            self.assertEqual(delegate._verify_worktree_state(root), before)

    def test_verifier_worktree_mutation_is_fail_loud_and_records_no_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, _plugin = self._setup(d, committed=False)

            def fake(wt, _args, _record_dir):
                (wt / "verifier-write.txt").write_text("forbidden\n")
                return (0, _json.dumps({
                    "summary": "mutated", "findings": [], "limitations": []}))

            with self.assertRaisesRegex(delegate.WorkflowError, "modified.*worktree"):
                _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(list((rec / "artifact").glob("verify-*.json")), [])

    def test_companion_broker_lifetime_is_bounded_by_verifier_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, _plugin = self._setup(d, committed=False)
            events = []
            original_run = delegate._run_companion
            original_cleanup = delegate._cleanup_companion_broker

            def cleanup(wt):
                self.assertEqual(wt, worktree)
                events.append("cleanup")
                return "broker cleanup: fixture"

            def run(wt, _args, record_dir):
                self.assertEqual(wt, worktree)
                self.assertEqual(record_dir, rec)
                events.append("run")
                return (0, _json.dumps({
                    "summary": "checked", "findings": [], "limitations": [],
                }))

            delegate._run_companion = run
            delegate._cleanup_companion_broker = cleanup
            try:
                _run_with_home(home, lambda: delegate.verify_delegation(root, rec.name))
            finally:
                delegate._run_companion = original_run
                delegate._cleanup_companion_broker = original_cleanup
            self.assertEqual(events, ["cleanup", "run", "cleanup"])

    def test_verifier_detects_content_change_to_existing_ignored_untracked_file(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, _plugin = self._setup(d, committed=False)

            def fake(wt, _args, _record_dir):
                (wt / ".ignored-cache" / "keep.txt").write_text("mutated\n")
                return (0, _json.dumps({
                    "summary": "mutated", "findings": [], "limitations": []}))

            with self.assertRaisesRegex(delegate.WorkflowError, "modified.*worktree"):
                _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(list((rec / "artifact").glob("verify-*.json")), [])

    def test_invalid_verifier_payload_is_rejected_before_artifact_write(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _plugin = self._setup(d, committed=False)

            def fake(*_args):
                return (0, _json.dumps({"summary": "missing fields"}))

            with self.assertRaisesRegex(delegate.WorkflowError, "verify artifact schema"):
                _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(list((rec / "artifact").glob("verify-*.json")), [])

    def test_contract_empty_must_be_bool_before_normalization(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, plugin = self._setup(d, committed=False)
            contract_path = rec / "artifact" / "contract.yaml"
            contract = yaml.safe_load(contract_path.read_text())
            contract["empty"] = "false"
            contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))
            called = {"n": 0}

            def fake(*args):
                called["n"] += 1
                return (0, "{}")

            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertIn("empty", str(cm.exception))
            self.assertEqual(called["n"], 0)

    def test_contract_shas_are_strict_and_base_matches_exposure(self):
        cases = (
            ("base_sha", "short", "base_sha"),
            ("result_sha", "not-a-sha", "result_sha"),
            ("base_sha", "result", "exposure"),
        )
        for field, value, needle in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as d:
                root, home, rec, worktree, plugin = self._setup(d, committed=False)
                contract_path = rec / "artifact" / "contract.yaml"
                contract = yaml.safe_load(contract_path.read_text())
                contract[field] = contract["result_sha"] if value == "result" else value
                contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))
                called = {"n": 0}

                def fake(*args):
                    called["n"] += 1
                    return (0, "{}")

                with self.assertRaises(delegate.WorkflowError) as cm:
                    _run_with_home(home, lambda: self._with_companion(
                        fake, lambda: delegate.verify_delegation(root, rec.name)))
                self.assertIn(needle, str(cm.exception))
                self.assertEqual(called["n"], 0)

    def test_contract_nonempty_requires_named_patch_file(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, plugin = self._setup(d, committed=False)
            (rec / "artifact" / "changes.patch").unlink()
            called = {"n": 0}

            def fake(*args):
                called["n"] += 1
                return (0, "{}")

            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertIn("patch_file", str(cm.exception))
            self.assertEqual(called["n"], 0)

    def test_concurrent_verify_is_refused_by_record_lock(self):
        import contextlib
        import io
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, plugin = self._setup(d, committed=False)
            calls = {"n": 0}

            def fake(*args):
                calls["n"] += 1
                return (0, _json.dumps({"run": calls["n"]}))

            err = io.StringIO()
            with mock.patch.dict(os.environ, {"WAYSTONE_LOCK_TIMEOUT": "0.02"}, clear=False), \
                    common.hold_lock(rec / "record.lock", timeout=0.2), \
                    contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.main(
                        ["verify", rec.name, "--root", str(root)])))
            self.assertEqual(rc, 1)
            self.assertIn("record.lock is held", err.getvalue())
            self.assertEqual(calls["n"], 0)

    def test_unlocked_record_lock_marker_is_reused_and_preserved(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, plugin = self._setup(d, committed=False)
            lock = rec / "record.lock"
            lock.write_text("stale fixture\n")

            def fake(*args):
                return (0, _json.dumps({
                    "summary": "ok", "findings": [], "limitations": []}))

            rc = _run_with_home(home, lambda: self._with_companion(
                fake, lambda: delegate.main(["verify", rec.name, "--root", str(root)])))
            self.assertEqual(rc, 0)
            self.assertTrue(lock.exists())
            self.assertEqual(_json.loads(lock.read_text())["pid"], os.getpid())

    def test_verify_artifact_name_collision_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, plugin = self._setup(d, committed=False)
            sentinel = {"sentinel": True}
            injected = {"done": False}
            orig_paths = delegate._verify_paths

            def raced_paths(record_dir):
                paths = orig_paths(record_dir)
                if not injected["done"]:
                    injected["done"] = True
                    (record_dir / "artifact" / "verify-1.json").write_text(
                        _json.dumps(sentinel) + "\n")
                return paths

            def fake(*args):
                return (0, _json.dumps({
                    "summary": "new", "findings": [], "limitations": []}))

            delegate._verify_paths = raced_paths
            try:
                _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            finally:
                delegate._verify_paths = orig_paths
            self.assertEqual(_json.loads((rec / "artifact" / "verify-1.json").read_text()), sentinel)
            self.assertEqual(
                _json.loads((rec / "artifact" / "verify-2.json").read_text())["payload"]["summary"],
                "new")

    def test_repeated_verify_increments_and_show_surfaces_latest(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, plugin = self._setup(d, committed=False)
            n = {"value": 0}

            def fake(*args):
                n["value"] += 1
                return (0, _json.dumps({
                    "summary": f"run {n['value']}", "findings": [], "limitations": []}))

            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                for _ in range(2):
                    _run_with_home(home, lambda: self._with_companion(
                        fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertTrue((rec / "artifact" / "verify-1.json").exists())
            self.assertTrue((rec / "artifact" / "verify-2.json").exists())
            summary = io.StringIO()
            latest = io.StringIO()
            with contextlib.redirect_stdout(summary):
                _run_with_home(home, lambda: delegate.show(root, rec.name, None))
            with contextlib.redirect_stdout(latest):
                _run_with_home(home, lambda: delegate.show(root, rec.name, "verify"))
            self.assertIn("verify_artifacts: 2", summary.getvalue())
            self.assertEqual(_json.loads(latest.getvalue())["payload"]["summary"], "run 2")

    def test_plugin_missing_and_wrong_state_fail_before_companion(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, plugin = self._setup(d)
            (home / ".claude" / "plugins" / "installed_plugins.json").unlink()
            called = {"n": 0}

            def fake(*args):
                called["n"] += 1
                return (0, "{}")

            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertIn("codex plugin not installed", str(cm.exception))
            self.assertEqual(called["n"], 0)
            _run_with_home(home, lambda: delegate._set_state(rec, "applied"))
            with self.assertRaises(delegate.WorkflowError):
                _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(called["n"], 0)

    def test_unimplemented_execution_and_entry_fail_loud(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, plugin = self._setup(d)
            for verifier, needle in (
                ("{execution: clean-subagent, backend: \"codex:x\", entry: adversarial-review}",
                 "schema-valid but not executable"),
                ("{execution: codex-companion, backend: \"codex:x\", entry: review}",
                 "entry 'review' not implemented in M2"),
            ):
                body = ("schema: waystone-profile-1\nbindings:\n"
                        "  implementer: {execution: external-runner, backend: \"codex:x\"}\n"
                        f"  verifier: {verifier}\n")
                _write_profile(root, body)
                with self.assertRaises(delegate.WorkflowError) as cm:
                    _run_with_home(home, lambda: delegate.verify_delegation(root, rec.name))
                self.assertIn(needle, str(cm.exception))

    def test_normalization_failure_and_companion_failure_leave_state_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, plugin = self._setup(d)
            contract_path = rec / "artifact" / "contract.yaml"
            contract = yaml.safe_load(contract_path.read_text())
            contract["base_sha"] = "0" * 40
            contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))
            called = {"n": 0}

            def fake(*args):
                called["n"] += 1
                return (3, "failed")

            with self.assertRaises(delegate.WorkflowError):
                _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(called["n"], 0)
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            self.assertEqual(list((rec / "artifact").glob("verify-*.json")), [])

            contract["base_sha"] = _json.loads((rec / "exposure.json").read_text())["base"]["snapshot_sha"]
            contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))
            with self.assertRaises(delegate.WorkflowError):
                _run_with_home(home, lambda: self._with_companion(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(called["n"], 1)
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            self.assertEqual(list((rec / "artifact").glob("verify-*.json")), [])

    def test_broker_shutdown_rpc_targets_only_exact_worktree_key(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            init_repo(root)
            state_root = Path(d) / "state"
            state_dir = delegate._companion_state_dir(root, state_root=state_root)
            state_dir.mkdir(parents=True)
            other = state_root / "other-deadbeef"
            other.mkdir(parents=True)
            (other / "broker.json").write_text('{"sentinel": true}')
            sock_path = Path(d) / "broker.sock"
            calls = {"connect": [], "sent": []}

            class FakeSocket:
                def settimeout(self, value):
                    pass

                def connect(self, value):
                    calls["connect"].append(value)

                def sendall(self, value):
                    calls["sent"].append(value.decode())

                def recv(self, size):
                    return b'{"id":1,"result":{}}\n'

                def close(self):
                    pass

            (state_dir / "broker.json").write_text(_json.dumps({
                "endpoint": f"unix:{sock_path}", "pid": 999999, "cwd": str(root.resolve())}))
            orig = delegate.socket.socket
            delegate.socket.socket = lambda *args: FakeSocket()
            try:
                result = delegate._cleanup_companion_broker(root, state_root=state_root)
            finally:
                delegate.socket.socket = orig
            self.assertEqual(calls["connect"], [str(sock_path)])
            self.assertIn('"method":"broker/shutdown"', calls["sent"][0].replace(" ", ""))
            self.assertIn("shutdown", result)
            self.assertEqual((other / "broker.json").read_text(), '{"sentinel": true}')


class UvCacheTests(unittest.TestCase):
    """0.8.0 M2 §13 — worktree-local uv cache env and result-snapshot exclusion."""

    def test_env_is_passed_to_prep_and_codex_without_global_mutation(self):
        import os
        import types
        with tempfile.TemporaryDirectory() as d:
            worktree = Path(d) / "wt"
            record = Path(d) / "record"
            worktree.mkdir()
            record.mkdir()
            prompt = Path(d) / "prompt.txt"
            prompt.write_text("prompt")
            seen = []
            orig = delegate.subprocess.run

            def fake(*args, **kwargs):
                seen.append(kwargs.get("env"))
                return types.SimpleNamespace(returncode=0, stderr="")

            before = os.environ.get("UV_CACHE_DIR")
            delegate.subprocess.run = fake
            try:
                self.assertEqual(delegate._run_env_prep(worktree, ["true"])[0], 0)
                self.assertEqual(delegate._run_codex(
                    worktree, "gpt-5.6-sol", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = orig
            expected = str(worktree / ".waystone-uv-cache")
            self.assertEqual([env["UV_CACHE_DIR"] for env in seen], [expected, expected])
            self.assertTrue(all("WAYSTONE_VERIFIER_SESSION" not in env for env in seen))
            self.assertEqual(os.environ.get("UV_CACHE_DIR"), before)

    def test_cache_is_excluded_but_other_untracked_result_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            info_exclude = Path(git(root, "rev-parse", "--git-path", "info/exclude").stdout.strip())
            if not info_exclude.is_absolute():
                info_exclude = root / info_exclude
            before = info_exclude.read_bytes() if info_exclude.exists() else None

            def fake(worktree, model, prompt_path, record_dir):
                cache = worktree / ".waystone-uv-cache"
                cache.mkdir()
                (cache / "junk").write_text("cache")
                (worktree / "kept.txt").write_text("keep")
                (record_dir / "last_message.md").write_text("summary")
                return (0, 0.1)

            _deleg_run(root, home, fake)
            rec = _latest_rec(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["changed_files"], [{"path": "kept.txt", "status": "A"}])
            patch = (rec / "artifact" / "changes.patch").read_text()
            self.assertIn("kept.txt", patch)
            self.assertNotIn(".waystone-uv-cache", patch)
            after = info_exclude.read_bytes() if info_exclude.exists() else None
            self.assertEqual(after, before)

    def test_codex_effort_flag_is_exact_and_absent_when_unset(self):
        import types
        with tempfile.TemporaryDirectory() as d:
            worktree = Path(d) / "wt"
            record = Path(d) / "record"
            worktree.mkdir()
            record.mkdir()
            prompt = Path(d) / "prompt.txt"
            prompt.write_text("prompt")
            commands = []
            orig = delegate.subprocess.run

            def fake(cmd, **kwargs):
                commands.append(cmd)
                return types.SimpleNamespace(returncode=0)

            delegate.subprocess.run = fake
            try:
                delegate._run_codex(
                    worktree, "gpt-test", prompt, record, effort="ultra")
                delegate._run_codex(worktree, "gpt-test", prompt, record)
            finally:
                delegate.subprocess.run = orig
            self.assertIn("-c", commands[0])
            self.assertIn('model_reasoning_effort="ultra"', commands[0])
            self.assertNotIn("-c", commands[1])
            self.assertFalse(any(arg.startswith("model_reasoning_effort=") for arg in commands[1]))

    def test_codex_verifier_ultra_effort_flag_is_exact(self):
        import types
        with tempfile.TemporaryDirectory() as d:
            worktree = Path(d) / "wt"
            record = Path(d) / "record"
            worktree.mkdir()
            record.mkdir()
            commands = []
            orig = delegate.subprocess.run

            def fake(cmd, **kwargs):
                commands.append(cmd)
                return types.SimpleNamespace(returncode=1)

            delegate.subprocess.run = fake
            try:
                delegate._run_codex_verifier(
                    worktree, "gpt-test", "review", record, effort="ultra")
            finally:
                delegate.subprocess.run = orig
            self.assertIn("-c", commands[0])
            self.assertIn('model_reasoning_effort="ultra"', commands[0])


class ContractInjectTests(unittest.TestCase):
    """0.8.0 M2 §10 — bounded, best-effort main operating contract injection."""

    def _module(self):
        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context
        return session_context

    def _project(self, d):
        root = Path(d) / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
        (root / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks:\n"
            "  - id: feat/active\n    title: active task here\n    status: active\n")
        home = Path(d) / "home"
        home.mkdir()
        return root, home

    def _context(self, module, root, home):
        import contextlib
        import io
        old_argv = sys.argv
        try:
            sys.argv = ["session_context.py", str(root)]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = _run_with_home(home, module.main)
            payload = _json.loads(out.getvalue())
            return rc, payload["hookSpecificOutput"]["additionalContext"]
        finally:
            sys.argv = old_argv

    def _delegation(self, root, home, did, state):
        rec = _run_with_home(home, lambda: delegate._record_dir(root, did))
        rec.mkdir(parents=True)
        (rec / "exposure.json").write_text(_json.dumps({"task_id": "feat/active"}))
        (rec / "status.json").write_text(_json.dumps({"state": state}))
        return rec

    def test_block_precedes_start_here_and_summarizes_live_inputs(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            _write_profile(root, DelegateVerifyTests._PROFILE)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/warn", rule="delegation-verification-evidence-v1",
                summary="s"))
            _run_with_home(home, lambda: overlay.add_delta(
                root, "review_association/observe", rule="round-close-open-findings-v1",
                summary="s"))
            _force_status(root, home, "verification_debt/warn", "warning")
            self._delegation(root, home, "did-one", "needs-review")
            self._delegation(root, home, "did-two", "needs-review")
            self._delegation(root, home, "did-done", "applied")
            evidence = root / ".waystone" / "improve" / "evidence.jsonl"
            evidence.parent.mkdir(parents=True)
            _write_jsonl(evidence, [
                {"task_id": "feat/active", "project": "demo", "findings": [{"severity": "major"}],
                 "delegations": [{"verification_present": False}]},
                {"coverage": {"projects_scanned": ["demo"]}},
            ])
            sh = _run_with_home(home, lambda: common.start_here_path(root))
            sh.parent.mkdir(parents=True, exist_ok=True)
            sh.write_text("FRONTIER")

            rc, ctx = self._context(module, root, home)
            self.assertEqual(rc, 0)
            self.assertLess(ctx.index("◆ OPERATING CONTRACT"), ctx.index("▶ START HERE"))
            self.assertIn("implementer:", ctx)
            self.assertIn("verifier:", ctx)
            self.assertNotIn("gpt-5.6-sol", ctx)
            self.assertIn("warning 1 (verification_debt/warn)", ctx)
            self.assertIn("observing 1 (review_association/observe)", ctx)
            self.assertIn("needs-review delegations 2 (did-one did-two)", ctx)
            self.assertIn("unverified+finding tasks 1", ctx)

    def test_routing_policy_renders_all_axes_questions_and_is_bounded(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, _home = self._project(d)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  reviewer: {execution: external-runner, backend: 'gemini:pro', "
                "use_for: 'adversarial diff review'}\n"
                "  main: {execution: main-session, backend: 'claude:opus'}\n"
            ))
            block = module._routing_block(root)
            self.assertLessEqual(len(block), 12)
            rendered = "\n".join(block)
            for role in delegate.PROFILE_ROLES:
                self.assertIn(f"  {role}:", rendered)
            self.assertIn("bindings: see `waystone paths` → profile", rendered)
            for model in ("claude:opus", "gemini:pro"):
                self.assertNotIn(model, rendered)

            policy = yaml.safe_load(module.ROUTING_POLICY_PATH.read_text())
            self.assertEqual(policy["schema"], "waystone-routing-policy-1")
            self.assertEqual(len(policy["questions"]), 8)
            self.assertEqual(len({question["id"] for question in policy["questions"]}), 8)
            for question in policy["questions"]:
                preference = question["prefer"]
                self.assertTrue(preference["roles"])
                self.assertTrue(preference["executions"])
                self.assertLessEqual(set(preference["roles"]), set(delegate.PROFILE_ROLES))
                self.assertLessEqual(
                    set(preference["executions"]), set(delegate.PROFILE_EXECUTIONS))

    def test_machine_only_evidence_is_not_reported_as_project_evidence(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            _write_profile(root, DelegateVerifyTests._PROFILE)
            evidence = home / ".waystone" / "improve" / "evidence.jsonl"
            evidence.parent.mkdir(parents=True)
            _write_jsonl(evidence, [{
                "task_id": "feat/active", "project": "demo",
                "findings": [{"severity": "major"}],
                "delegations": [{"verification_present": False}],
            }])

            rc, ctx = self._context(module, root, home)
            self.assertEqual(rc, 0)
            self.assertNotIn("unverified+finding tasks", ctx)

    def test_profile_absence_marks_bindings_unavailable_and_constitution_absence_omits_contract(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            rc, ctx = self._context(module, root, home)
            self.assertEqual(rc, 0)
            self.assertIn("routing policy: role guidance", ctx)
            self.assertIn("bindings: unavailable; see `waystone paths` → profile", ctx)
            self.assertEqual(
                {path.name for path in (root / ".waystone").iterdir()},
                {".gitignore", "lock"},
            )
            original = module.CONTRACT_PATH
            module.CONTRACT_PATH = Path(d) / "missing-contract.md"
            try:
                rc, missing = self._context(module, root, home)
            finally:
                module.CONTRACT_PATH = original
            self.assertEqual(rc, 0)
            self.assertNotIn("◆ OPERATING CONTRACT", missing)

    def test_unwritable_project_state_never_breaks_session_start_json(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            state = root / ".waystone"
            state.write_text("not a directory")
            rc, ctx = self._context(module, root, home)
            self.assertEqual(rc, 0)
            self.assertIn("[waystone] project: demo", ctx)
            self.assertTrue(state.is_file())

    def test_corrupt_inputs_are_field_local_and_never_break_session_start(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            profile = common.ensure_project_state_dir(root) / "profile.yml"
            profile.write_text("bindings: [\n")
            delta = _run_with_home(home, lambda: overlay._deltas_dir(root)) / "bad.json"
            delta.parent.mkdir(parents=True)
            delta.write_text("{bad")
            rec = self._delegation(root, home, "did-corrupt", "needs-review")
            (rec / "status.json").write_text("{bad")
            evidence = root / ".waystone" / "improve" / "evidence.jsonl"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("{bad\n")
            rc, ctx = self._context(module, root, home)
            self.assertEqual(rc, 0)
            self.assertIn("◆ OPERATING CONTRACT", ctx)
            self.assertIn("unreadable", ctx)
            self.assertNotIn("config/tasks unreadable", ctx)

    def test_contract_has_its_own_1200_character_cap(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            long_contract = Path(d) / "contract.md"
            long_contract.write_text("\n".join("X" * 400 for _ in range(8)))
            original = module.CONTRACT_PATH
            module.CONTRACT_PATH = long_contract
            try:
                block = _run_with_home(home, lambda: module._operating_contract(root))
            finally:
                module.CONTRACT_PATH = original
            self.assertLessEqual(len("\n".join(block)), 1200)


class MigrationV2Phase1Tests(unittest.TestCase):
    def _run(self, home: Path, fn, *, codex_home: Path | None = None,
             waystone_home: Path | None = None, host: str | None = None):
        import os

        updates = {
            "CODEX_HOME": str(codex_home) if codex_home is not None else None,
            "WAYSTONE_HOME": str(waystone_home) if waystone_home is not None else None,
            "WAYSTONE_HOST": host,
        }
        before = {name: os.environ.get(name) for name in updates}
        try:
            for name, value in updates.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            return _run_with_home(home, fn, isolate_storage=False)
        finally:
            for name, value in before.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_registry_union_reports_every_entry_and_codex_host_runs_it(self):
        import contextlib
        import io
        import waystone

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            codex_home = d / "codex-home"
            machine = d / "machine"
            claude = home / ".claude" / "waystone"
            codex = codex_home / "waystone"
            claude.mkdir(parents=True)
            codex.mkdir(parents=True)
            local = str(d / "local")
            (claude / "projects.json").write_text(_json.dumps({"projects": [
                {"name": "local-primary", "path": local},
                {"name": "remote-primary", "repo": "org/primary"},
            ]}))
            (codex / "projects.json").write_text(_json.dumps({"projects": [
                {"name": "local-secondary", "path": local},
                {"name": "remote-secondary", "repo": "org/secondary"},
            ]}))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = self._run(
                    home, lambda: waystone.main([]), codex_home=codex_home,
                    waystone_home=machine, host="codex")
            self.assertEqual(rc, 1)
            self.assertEqual(_json.loads((machine / "projects.json").read_text())["projects"], [
                {"name": "local-primary", "path": local},
                {"name": "remote-primary", "repo": "org/primary"},
                {"name": "remote-secondary", "repo": "org/secondary"},
            ])
            report = err.getvalue()
            for label in ("local-primary", "remote-primary", "local-secondary", "remote-secondary"):
                self.assertIn(label, report)
            self.assertTrue((home / ".claude" / "waystone.pre-0.9" / "projects.json").is_file())
            self.assertTrue((codex_home / "waystone.pre-0.9" / "projects.json").is_file())

    def test_registry_migration_union_rejects_canonical_alias_collision(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            codex_home = d / "codex-home"
            claude = home / ".claude" / "waystone"
            codex = codex_home / "waystone"
            claude.mkdir(parents=True)
            codex.mkdir(parents=True)
            first = (d / "first").resolve()
            collision = (d / "collision").resolve()
            (claude / "projects.json").write_text(_json.dumps({"projects": [{
                "name": "first", "path": str(first), "aliases": [str(collision)],
            }]}))
            (codex / "projects.json").write_text(_json.dumps({"projects": [{
                "name": "second", "path": str(collision),
            }]}))

            with self.assertRaisesRegex(common.WorkflowError, "already belongs to"):
                self._run(
                    home, lambda: common.migrate_home_data(home), codex_home=codex_home)

            self.assertFalse((home / ".waystone" / "projects.json").exists())
            self.assertTrue((claude / "projects.json").is_file())
            self.assertTrue((codex / "projects.json").is_file())

    def test_decisions_concat_is_timestamp_sorted_and_codex_projection_is_preserved(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            codex_home = d / "codex-home"
            claude_improve = home / ".claude" / "waystone" / "improve"
            codex_improve = codex_home / "waystone" / "improve"
            claude_improve.mkdir(parents=True)
            codex_improve.mkdir(parents=True)
            _write_jsonl(claude_improve / "decisions.jsonl", [
                {"rec_id": "later", "at": "2026-07-15T02:00:00Z"},
            ])
            _write_jsonl(codex_improve / "decisions.jsonl", [
                {"rec_id": "earlier", "at": "2026-07-15T01:00:00Z"},
            ])
            (claude_improve / "sessions.jsonl").write_text("claude projection\n")
            (codex_improve / "sessions.jsonl").write_text("codex projection\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self._run(home, lambda: common.migrate_home_data(home), codex_home=codex_home)
            rows = [
                _json.loads(line)
                for line in (home / ".waystone" / "improve" / "decisions.jsonl")
                .read_text().splitlines()
            ]
            self.assertEqual([row["rec_id"] for row in rows], ["earlier", "later"])
            self.assertEqual(
                (home / ".waystone" / "improve" / "sessions.jsonl").read_text(),
                "claude projection\n")
            self.assertEqual(
                (codex_home / "waystone.pre-0.9" / "improve" / "sessions.jsonl").read_text(),
                "codex projection\n")
            self.assertIn("2 decision row", err.getvalue())
            self.assertIn("waystone improve trace --host codex", err.getvalue())

    def test_decisions_merge_marker_prevents_duplicate_rows_after_interruption(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            source = home / ".claude" / "waystone" / "improve" / "decisions.jsonl"
            source.parent.mkdir(parents=True)
            _write_jsonl(source, [{"rec_id": "one", "at": "2026-07-15T01:00:00Z"}])
            original = common._preserve_phase1_root
            common._preserve_phase1_root = lambda _root: (_ for _ in ()).throw(
                RuntimeError("injected after decisions merge"))
            try:
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    self._run(home, lambda: common.migrate_home_data(home))
            finally:
                common._preserve_phase1_root = original

            destination = home / ".waystone" / "improve"
            self.assertEqual(len(list(destination.glob(".merged-*"))), 1)
            self._run(home, lambda: common.migrate_home_data(home))
            rows = (destination / "decisions.jsonl").read_text().splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(_json.loads(rows[0])["rec_id"], "one")

    def test_decisions_merge_preserves_legitimate_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            row = {"rec_id": "same", "at": "2026-07-15T01:00:00Z"}
            for host in (".claude", ".codex"):
                source = home / host / "waystone" / "improve" / "decisions.jsonl"
                source.parent.mkdir(parents=True)
                _write_jsonl(source, [row])

            self._run(home, lambda: common.migrate_home_data(home))

            lines = (home / ".waystone" / "improve" / "decisions.jsonl").read_text().splitlines()
            self.assertEqual([_json.loads(line) for line in lines], [row, row])

    def test_profile_stays_preserved_worktrees_stay_at_original_path_and_orphans_report(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            root = d / "repo"
            root.mkdir()
            init_repo(root)
            legacy = home / ".claude" / "waystone"
            slug = common._project_slug(root)
            (legacy / "delegations" / slug / "did-known").mkdir(parents=True)
            (legacy / "delegations" / "unmapped-slug" / "did-orphan").mkdir(parents=True)
            old_worktree = legacy / "worktrees" / slug / "did-known"
            old_worktree.parent.mkdir(parents=True)
            self.assertEqual(
                git(root, "worktree", "add", "--detach", str(old_worktree), "HEAD").returncode, 0)
            profile = "bindings:\n  verifier: {execution: codex-companion, backend: 'codex:gpt-test'}\n"
            (legacy / "profile.yml").write_text(profile)
            (legacy / "projects.json").write_text(_json.dumps({"projects": [
                {"name": "demo", "path": str(root)},
            ]}))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self._run(home, lambda: common.migrate_home_data(home))
            preserved = home / ".claude" / "waystone.pre-0.9"
            self.assertEqual((preserved / "profile.yml").read_text(), profile)
            self.assertFalse((home / ".waystone" / "profile.yml").exists())
            self.assertTrue(old_worktree.is_dir())
            self.assertEqual(git(old_worktree, "status", "--porcelain").returncode, 0)
            self.assertTrue(legacy.is_dir())
            self.assertEqual([p.name for p in legacy.iterdir()], ["worktrees"])
            self.assertIn("unmapped-slug", err.getvalue())
            second = io.StringIO()
            with contextlib.redirect_stderr(second):
                self._run(home, lambda: common.migrate_home_data(home))
            self.assertEqual(second.getvalue(), "")
            self.assertEqual(git(old_worktree, "status", "--porcelain").returncode, 0)

    def test_jahns_workflow_chain_keeps_linked_worktree_valid_until_phase2(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            root = d / "repo"
            root.mkdir()
            init_repo(root)
            slug = common._project_slug(root)
            legacy = home / ".claude" / "jahns-workflow"
            record = legacy / "delegations" / slug / "did-chain"
            record.mkdir(parents=True)
            (record / "status.json").write_text(_json.dumps({"state": "needs-review"}))
            old_worktree = legacy / "worktrees" / slug / "did-chain"
            old_worktree.parent.mkdir(parents=True)
            self.assertEqual(
                git(root, "worktree", "add", "--detach", str(old_worktree), "HEAD").returncode, 0)

            self._run(home, lambda: common.migrate_home_data(home))

            self.assertTrue(old_worktree.is_dir())
            self.assertEqual(git(old_worktree, "rev-parse", "--git-dir").returncode, 0)
            self.assertEqual([path.name for path in legacy.iterdir()], ["worktrees"])

            self._run(home, lambda: common.migrate_project_state(root))
            new_worktree = home / ".waystone" / "cache" / "worktrees" / slug / "did-chain"
            self.assertFalse(old_worktree.exists())
            self.assertEqual(git(new_worktree, "rev-parse", "--git-dir").returncode, 0)
            listing = git(root, "worktree", "list", "--porcelain").stdout
            self.assertIn(str(new_worktree), listing)
            self.assertNotIn(str(old_worktree), listing)

    def test_symlinked_legacy_root_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            external = d / "external"
            external.mkdir()
            projects = external / "projects.json"
            projects.write_text(_json.dumps({"projects": [{"repo": "org/external"}]}))
            legacy = home / ".claude" / "waystone"
            legacy.parent.mkdir(parents=True)
            legacy.symlink_to(external, target_is_directory=True)

            with self.assertRaises(common.WorkflowError) as cm:
                self._run(home, lambda: common.migrate_home_data(home))

            self.assertIn("symlink", str(cm.exception).lower())
            self.assertTrue(legacy.is_symlink())
            self.assertEqual(projects.read_text(), _json.dumps({"projects": [{"repo": "org/external"}]}))
            self.assertFalse((home / ".waystone" / "projects.json").exists())

    def test_plain_legacy_root_is_rechecked_for_version_skew(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home = d / "home"
            first = home / ".claude" / "waystone"
            first.mkdir(parents=True)
            (first / "projects.json").write_text(_json.dumps({"projects": [
                {"name": "first", "repo": "org/first"},
            ]}))
            self._run(home, lambda: common.migrate_home_data(home))
            second = home / ".claude" / "waystone"
            second.mkdir(parents=True, exist_ok=True)
            (second / "projects.json").write_text(_json.dumps({"projects": [
                {"name": "second", "repo": "org/second"},
            ]}))
            self._run(home, lambda: common.migrate_home_data(home))
            projects = _json.loads((home / ".waystone" / "projects.json").read_text())["projects"]
            self.assertEqual([entry["name"] for entry in projects], ["first", "second"])
            self.assertFalse(second.exists())


class MigrationV2Phase2Tests(unittest.TestCase):
    PROFILE = (
        "schema: waystone-profile-1\nbindings:\n"
        "  implementer: {execution: external-runner, backend: 'codex:gpt-test'}\n"
        "  verifier: {execution: codex-companion, backend: 'codex:gpt-test'}\n"
    )

    def _project(self, d: Path) -> tuple[Path, Path]:
        root = d / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
        (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
        home = d / "home"
        home.mkdir()
        return root, home

    def _source(self, home: Path, host: str, *, plain: bool = False) -> Path:
        base = home / (".claude" if host == "claude" else ".codex")
        return base / ("waystone" if plain else "waystone.pre-0.9")

    def test_profile_seeds_without_consuming_or_removing_execution(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            for host in ("claude", "codex"):
                source = self._source(home, host)
                source.mkdir(parents=True)
                (source / "profile.yml").write_text(self.PROFILE)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                _run_with_home(home, lambda: common.migrate_project_state(root))
            self.assertEqual((root / ".waystone" / "profile.yml").read_text(), self.PROFILE)
            self.assertIn("execution: codex-companion", (root / ".waystone" / "profile.yml").read_text())
            for host in ("claude", "codex"):
                self.assertEqual((self._source(home, host) / "profile.yml").read_text(), self.PROFILE)
            self.assertIn("seeded", err.getvalue())

    def test_profile_seed_recovers_after_atomic_replace_commits_then_raises(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            source = self._source(home, "claude")
            source.mkdir(parents=True)
            profile = source / "profile.yml"
            profile.write_text(self.PROFILE)
            live = (root / ".waystone" / "profile.yml").resolve()
            original = common.os.replace

            def replace_then_raise(old, new):
                original(old, new)
                if Path(new) == live:
                    raise RuntimeError("injected after profile replace")

            common.os.replace = replace_then_raise
            try:
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    _run_with_home(home, lambda: common.migrate_project_state(root))
            finally:
                common.os.replace = original

            self.assertEqual(live.read_text(), self.PROFILE)
            self.assertEqual(profile.read_text(), self.PROFILE)
            _run_with_home(home, lambda: common.migrate_project_state(root))
            self.assertEqual(live.read_text(), self.PROFILE)
            self.assertEqual(profile.read_text(), self.PROFILE)

    def test_different_host_profiles_fail_loud_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            claude = self._source(home, "claude")
            codex = self._source(home, "codex")
            claude.mkdir(parents=True)
            codex.mkdir(parents=True)
            (claude / "profile.yml").write_text(self.PROFILE)
            (codex / "profile.yml").write_text(self.PROFILE.replace("gpt-test", "gpt-other"))
            with self.assertRaises(common.WorkflowError) as cm:
                _run_with_home(home, lambda: common.migrate_project_state(root))
            self.assertIn("profile", str(cm.exception))
            self.assertIn(str(claude / "profile.yml"), str(cm.exception))
            self.assertIn(str(codex / "profile.yml"), str(cm.exception))
            self.assertFalse((root / ".waystone" / "profile.yml").exists())

    def test_staged_legacy_profile_conflict_fails_even_when_live_exists(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            first = self._source(home, "claude")
            first.mkdir(parents=True)
            (first / "profile.yml").write_text(self.PROFILE)
            _run_with_home(home, lambda: common.migrate_project_state(root))
            live = root / ".waystone" / "profile.yml"
            self.assertEqual(live.read_text(), self.PROFILE)

            reentry = self._source(home, "codex", plain=True)
            reentry.mkdir(parents=True)
            conflicting = self.PROFILE.replace("gpt-test", "gpt-other")
            incoming = reentry / "profile.yml"
            incoming.write_text(conflicting)

            with self.assertRaises(common.WorkflowError) as cm:
                _run_with_home(home, lambda: common.migrate_project_state(root))

            self.assertIn("profile", str(cm.exception))
            self.assertEqual(live.read_text(), self.PROFILE)
            self.assertEqual(incoming.read_text(), conflicting)

    def test_symlinked_project_state_is_rejected_without_external_write(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            source = self._source(home, "claude") / "start_here" / f"{slug}.md"
            source.parent.mkdir(parents=True)
            source.write_text("keep")
            external = Path(d) / "external-state"
            external.mkdir()
            (root / ".waystone").symlink_to(external, target_is_directory=True)

            with self.assertRaises(common.WorkflowError) as cm:
                _run_with_home(home, lambda: common.migrate_project_state(root))

            self.assertIn("symlink", str(cm.exception).lower())
            self.assertEqual(source.read_text(), "keep")
            self.assertEqual(list(external.iterdir()), [])

    def test_symlinked_legacy_slug_and_delegation_record_are_rejected(self):
        for target_kind in ("slug", "record"):
            with self.subTest(target_kind=target_kind), tempfile.TemporaryDirectory() as d:
                root, home = self._project(Path(d))
                slug = common._project_slug(root)
                external = Path(d) / "external"
                external.mkdir()
                sentinel = external / "sentinel.json"
                sentinel.write_text("keep")
                source = self._source(home, "claude")
                if target_kind == "slug":
                    link = source / "overlay" / slug
                else:
                    link = source / "delegations" / slug / "did-link"
                link.parent.mkdir(parents=True)
                link.symlink_to(external, target_is_directory=True)

                with self.assertRaises(common.WorkflowError) as cm:
                    _run_with_home(home, lambda: common.migrate_project_state(root))

                self.assertIn("symlink", str(cm.exception).lower())
                self.assertTrue(link.is_symlink())
                self.assertEqual(sentinel.read_text(), "keep")
                self.assertFalse((root / ".waystone").exists())

    def test_unique_path_treats_dangling_symlink_as_occupied(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "conflict.json"
            target.symlink_to(Path(d) / "missing.json")
            self.assertEqual(common._unique_path(target), Path(d) / "conflict.2.json")

    def test_same_overlay_rule_across_hosts_fails_before_moving(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            paths = []
            for host, delta_id in (("claude", "alpha"), ("codex", "beta")):
                path = self._source(home, host) / "overlay" / slug / "deltas" / f"{delta_id}.json"
                path.parent.mkdir(parents=True)
                path.write_text(_json.dumps({"id": delta_id, "rule": "same-rule"}))
                paths.append(path)
            with self.assertRaises(common.WorkflowError) as cm:
                _run_with_home(home, lambda: common.migrate_project_state(root))
            self.assertIn("same-rule", str(cm.exception))
            self.assertTrue(all(path.is_file() for path in paths))
            self.assertFalse((root / ".waystone" / "overlay").exists())

    def test_staged_overlay_rule_conflict_with_live_fails_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            first = self._source(home, "claude") / "overlay" / slug / "deltas" / "alpha.json"
            first.parent.mkdir(parents=True)
            first.write_text(_json.dumps({"id": "alpha", "rule": "same-rule", "state": "warning"}))
            _run_with_home(home, lambda: common.migrate_project_state(root))
            live = root / ".waystone" / "overlay" / "deltas" / "alpha.json"
            live_body = live.read_bytes()

            incoming = (self._source(home, "codex", plain=True) / "overlay" / slug /
                        "deltas" / "beta.json")
            incoming.parent.mkdir(parents=True)
            incoming.write_text(
                _json.dumps({"id": "beta", "rule": "same-rule", "state": "suspended"}))

            with self.assertRaises(common.WorkflowError) as cm:
                _run_with_home(home, lambda: common.migrate_project_state(root))

            self.assertIn("same-rule", str(cm.exception))
            self.assertEqual(live.read_bytes(), live_body)
            self.assertTrue(incoming.is_file())
            self.assertFalse((live.parent / "beta.json").exists())

    def test_staged_byte_identical_overlay_rule_cleans_incoming_source(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            first = self._source(home, "claude") / "overlay" / slug / "deltas" / "alpha.json"
            first.parent.mkdir(parents=True)
            first.write_text(_json.dumps({"id": "alpha", "rule": "same-rule"}))
            _run_with_home(home, lambda: common.migrate_project_state(root))
            live = root / ".waystone" / "overlay" / "deltas" / "alpha.json"
            live_body = live.read_bytes()

            incoming = (self._source(home, "codex", plain=True) / "overlay" / slug /
                        "deltas" / "copy.json")
            incoming.parent.mkdir(parents=True)
            incoming.write_bytes(live.read_bytes())
            _run_with_home(home, lambda: common.migrate_project_state(root))

            self.assertFalse(incoming.exists())
            self.assertFalse((live.parent / "copy.json").exists())
            self.assertEqual(live.read_bytes(), live_body)

    def test_newer_general_conflict_wins_and_loser_is_quarantined(self):
        import contextlib
        import io
        import os

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            older = self._source(home, "claude") / "start_here" / f"{slug}.md"
            newer = self._source(home, "codex", plain=True) / "start_here" / f"{slug}.md"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.write_text("older")
            newer.write_text("newer")
            os.utime(older, ns=(1_000_000_000, 1_000_000_000))
            os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                _run_with_home(home, lambda: common.migrate_project_state(root))
            self.assertEqual((root / ".waystone" / "start-here.md").read_text(), "newer")
            quarantined = list((root / ".waystone" / "migration-conflicts" / "claude").rglob("*"))
            self.assertTrue(any(path.is_file() and path.read_text() == "older" for path in quarantined))
            self.assertFalse(older.exists())
            self.assertFalse(newer.exists())
            self.assertIn("conflict", err.getvalue().lower())

    def test_file_move_recovers_after_atomic_replace_commits_then_raises(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            source = self._source(home, "claude") / "start_here" / f"{slug}.md"
            source.parent.mkdir(parents=True)
            source.write_text("frontier")
            live = (root / ".waystone" / "start-here.md").resolve()
            original = common.os.replace

            def replace_then_raise(old, new):
                original(old, new)
                if Path(new) == live:
                    raise RuntimeError("injected after file replace")

            common.os.replace = replace_then_raise
            try:
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    _run_with_home(home, lambda: common.migrate_project_state(root))
            finally:
                common.os.replace = original

            self.assertEqual(live.read_text(), "frontier")
            self.assertEqual(source.read_text(), "frontier")
            _run_with_home(home, lambda: common.migrate_project_state(root))
            self.assertEqual(live.read_text(), "frontier")
            self.assertFalse(source.exists())
            conflicts = root / ".waystone" / "migration-conflicts"
            self.assertFalse(conflicts.exists() and any(
                path.is_file() and path.read_text() == "frontier"
                for path in conflicts.rglob("*")))

    def test_phase2_is_self_extinguishing_and_second_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            source = self._source(home, "claude")
            start = source / "start_here" / f"{slug}.md"
            start.parent.mkdir(parents=True)
            start.write_text("frontier")
            source.mkdir(parents=True, exist_ok=True)
            (source / "profile.yml").write_text(self.PROFILE)
            _run_with_home(home, lambda: common.migrate_project_state(root))

            def snapshot():
                return {
                    str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in root.rglob("*") if path.is_file()
                }

            first = snapshot()
            _run_with_home(home, lambda: common.migrate_project_state(root))
            self.assertEqual(snapshot(), first)
            self.assertFalse(start.exists())
            self.assertTrue((source / "profile.yml").is_file())

    def test_delegation_slug_is_removed_and_cross_host_did_collision_is_skipped(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            unique = self._source(home, "claude") / "delegations" / slug / "did-unique"
            unique.mkdir(parents=True)
            (unique / "exposure.json").write_text(_json.dumps({"task_id": "feat/unique"}))
            collisions = []
            for host in ("claude", "codex"):
                record = self._source(home, host) / "delegations" / slug / "did-collision"
                record.mkdir(parents=True)
                (record / "exposure.json").write_text(_json.dumps({"task_id": f"feat/{host}"}))
                collisions.append(record)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                _run_with_home(home, lambda: common.migrate_project_state(root))
            self.assertTrue((root / ".waystone" / "delegations" / "did-unique" /
                             "exposure.json").is_file())
            self.assertFalse(unique.exists())
            self.assertFalse((root / ".waystone" / "delegations" / "did-collision").exists())
            self.assertTrue(all(record.is_dir() for record in collisions))
            self.assertIn("did-collision", err.getvalue())
            self.assertIn("skipped", err.getvalue())

    def test_staged_different_live_did_skips_and_preserves_whole_record(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            first = self._source(home, "claude") / "delegations" / slug / "did-staged"
            first.mkdir(parents=True)
            (first / "exposure.json").write_text(_json.dumps({"task_id": "feat/demo"}))
            (first / "status.json").write_text(_json.dumps({"state": "needs-review"}))
            _run_with_home(home, lambda: common.migrate_project_state(root))
            live = root / ".waystone" / "delegations" / "did-staged"
            before = {
                path.relative_to(live): path.read_bytes()
                for path in live.rglob("*") if path.is_file()
            }

            incoming = (self._source(home, "codex", plain=True) / "delegations" / slug /
                        "did-staged")
            incoming.mkdir(parents=True)
            (incoming / "exposure.json").write_text(_json.dumps({"task_id": "feat/demo"}))
            (incoming / "status.json").write_text(_json.dumps({"state": "failed"}))
            (incoming / "incoming-only.json").write_text(_json.dumps({"keep": True}))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                _run_with_home(home, lambda: common.migrate_project_state(root))

            after = {
                path.relative_to(live): path.read_bytes()
                for path in live.rglob("*") if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertTrue((incoming / "exposure.json").is_file())
            self.assertTrue((incoming / "status.json").is_file())
            self.assertTrue((incoming / "incoming-only.json").is_file())
            self.assertIn("did-staged", err.getvalue())
            self.assertIn("skipped", err.getvalue())

    def test_staged_byte_identical_live_did_removes_source_as_one_record(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            first = self._source(home, "claude") / "delegations" / slug / "did-identical"
            first.mkdir(parents=True)
            (first / "exposure.json").write_text(_json.dumps({"task_id": "feat/demo"}))
            (first / "status.json").write_text(_json.dumps({"state": "needs-review"}))
            _run_with_home(home, lambda: common.migrate_project_state(root))
            live = root / ".waystone" / "delegations" / "did-identical"

            incoming = (self._source(home, "codex", plain=True) / "delegations" / slug /
                        "did-identical")
            incoming.mkdir(parents=True)
            for path in live.iterdir():
                if path.is_file():
                    (incoming / path.name).write_bytes(path.read_bytes())
            _run_with_home(home, lambda: common.migrate_project_state(root))

            self.assertFalse(incoming.exists())
            empty_copy = (root / ".waystone" / "migration-conflicts" / "codex" /
                          "empty-sources" / "delegations" / "did-identical")
            self.assertFalse(empty_copy.exists())

    def _legacy_record_and_worktree(self, root: Path, home: Path, did: str):
        slug = common._project_slug(root)
        record = self._source(home, "claude") / "delegations" / slug / did
        record.mkdir(parents=True)
        (record / "exposure.json").write_text(_json.dumps({"task_id": "feat/demo"}))
        (record / "status.json").write_text(_json.dumps({"state": "needs-review"}))
        worktree = self._source(home, "claude", plain=True) / "worktrees" / slug / did
        return slug, record, worktree

    def test_worktree_uses_git_move_first(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug, _record, old = self._legacy_record_and_worktree(root, home, "did-move")
            old.parent.mkdir(parents=True, exist_ok=True)
            self.assertEqual(git(root, "worktree", "add", "--detach", str(old), "HEAD").returncode, 0)
            _run_with_home(home, lambda: common.migrate_project_state(root))
            new = home / ".waystone" / "cache" / "worktrees" / slug / "did-move"
            self.assertTrue(new.is_dir())
            self.assertFalse(old.exists())
            self.assertEqual(git(new, "status", "--porcelain").returncode, 0)
            listing = git(root, "worktree", "list", "--porcelain").stdout
            self.assertIn(str(new), listing)
            self.assertNotIn(str(old), listing)

    def test_worktree_move_failure_uses_filesystem_move_then_real_repair(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug, _record, old = self._legacy_record_and_worktree(root, home, "did-repair")
            old.parent.mkdir(parents=True, exist_ok=True)
            self.assertEqual(git(root, "worktree", "add", "--detach", str(old), "HEAD").returncode, 0)
            original = common.git_rc

            def fake_git_rc(project, *args):
                if args[:2] == ("worktree", "move"):
                    return 1, "", "move failed"
                return original(project, *args)

            common.git_rc = fake_git_rc
            try:
                _run_with_home(home, lambda: common.migrate_project_state(root))
            finally:
                common.git_rc = original
            new = home / ".waystone" / "cache" / "worktrees" / slug / "did-repair"
            self.assertEqual(git(new, "rev-parse", "--git-dir").returncode, 0)
            listing = git(root, "worktree", "list", "--porcelain").stdout
            self.assertIn(str(new), listing)
            self.assertNotIn(str(old), listing)
            self.assertFalse(new.with_name(f"{new.name}.migrating").exists())

    def test_worktree_fallback_resumes_after_move_commits_then_raises(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug, _record, old = self._legacy_record_and_worktree(root, home, "did-resume")
            old.parent.mkdir(parents=True, exist_ok=True)
            self.assertEqual(git(root, "worktree", "add", "--detach", str(old), "HEAD").returncode, 0)
            new = home / ".waystone" / "cache" / "worktrees" / slug / "did-resume"
            marker = new.with_name(f"{new.name}.migrating")
            original_git_rc = common.git_rc
            original_move = common.shutil.move

            def fail_native_move(project, *args):
                if args[:2] == ("worktree", "move"):
                    return 1, "", "move failed"
                return original_git_rc(project, *args)

            def move_then_raise(source, destination):
                result = original_move(source, destination)
                if Path(source) == old and Path(destination) == new:
                    raise RuntimeError("injected after worktree move")
                return result

            common.git_rc = fail_native_move
            common.shutil.move = move_then_raise
            try:
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    _run_with_home(home, lambda: common.migrate_project_state(root))
            finally:
                common.shutil.move = original_move

            self.assertEqual(marker.read_text(), str(old))
            self.assertTrue(new.is_dir())
            self.assertFalse(old.exists())
            try:
                _run_with_home(home, lambda: common.migrate_project_state(root))
            finally:
                common.git_rc = original_git_rc

            self.assertFalse(marker.exists())
            self.assertEqual(git(new, "rev-parse", "--git-dir").returncode, 0)
            listing = git(root, "worktree", "list", "--porcelain").stdout
            self.assertIn(str(new), listing)
            self.assertNotIn(str(old), listing)

    def test_worktree_repair_failure_marks_record_discard_only_and_warns(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug, _record, old = self._legacy_record_and_worktree(root, home, "did-degrade")
            old.mkdir(parents=True)
            original = common.git_rc
            common.git_rc = lambda _root, *_args: (1, "", "git failed")
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    _run_with_home(home, lambda: common.migrate_project_state(root))
            finally:
                common.git_rc = original
            live = root / ".waystone" / "delegations" / "did-degrade" / "status.json"
            status = _json.loads(live.read_text())
            self.assertEqual(status["state"], "migration-worktree-failed")
            self.assertEqual(status["migration"]["disposition"], "discard-only")
            self.assertTrue((home / ".waystone" / "cache" / "worktrees" / slug / "did-degrade").is_dir())
            self.assertIn("WARNING", err.getvalue())
            self.assertIn("DISCARD-ONLY", err.getvalue())


class MigrationV2HookTests(unittest.TestCase):
    def _module(self):
        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context
        return session_context

    def _project(self, d: Path):
        root = d / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
        (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
        home = d / "home"
        home.mkdir()
        return root, home

    def _run_context(self, module, root: Path, home: Path):
        import contextlib
        import io

        old_argv = sys.argv
        out, err = io.StringIO(), io.StringIO()
        try:
            sys.argv = ["session_context.py", str(root)]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = _run_with_home(home, module.main)
        finally:
            sys.argv = old_argv
        return rc, _json.loads(out.getvalue()), err.getvalue()

    def test_hook_migrates_plain_legacy_source_before_phase1(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            source = home / ".claude" / "waystone" / "start_here" / f"{slug}.md"
            source.parent.mkdir(parents=True)
            source.write_text("HOOK-PLAINTEXT-FRONTIER")
            rc, payload, err = self._run_context(module, root, home)
            self.assertEqual((rc, err), (0, ""))
            self.assertIn("HOOK-PLAINTEXT-FRONTIER", payload["hookSpecificOutput"]["additionalContext"])
            self.assertFalse(source.exists())

    def test_hook_migration_failure_warns_but_always_emits_json_context(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            original = module.migrate_project_state
            module.migrate_project_state = lambda _root: (_ for _ in ()).throw(
                common.WorkflowError("migration exploded"))
            try:
                rc, payload, err = self._run_context(module, root, home)
            finally:
                module.migrate_project_state = original
            self.assertEqual(rc, 0)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn("migration exploded", err)
            self.assertIn("migration", err.lower())

    def test_hook_acquires_registry_then_project_with_one_three_second_budget(self):
        import contextlib

        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            original_hold = getattr(module, "hold_lock", None)
            original_time = getattr(module, "time", None)
            seen = []
            ticks = iter((100.0, 100.0, 101.25))

            @contextlib.contextmanager
            def tracked(path, timeout=None):
                seen.append((Path(path), timeout))
                yield

            class FakeTime:
                @staticmethod
                def monotonic():
                    return next(ticks)

                @staticmethod
                def time_ns():
                    return 1

            module.hold_lock = tracked
            module.time = FakeTime
            try:
                rc, payload, err = self._run_context(module, root, home)
            finally:
                if original_hold is None:
                    del module.hold_lock
                else:
                    module.hold_lock = original_hold
                if original_time is None:
                    del module.time
                else:
                    module.time = original_time
            self.assertEqual(rc, 0)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertEqual(err, "")
            self.assertEqual([path.resolve() for path, _timeout in seen], [
                (home / ".waystone" / "registry.lock").resolve(),
                common.project_lock_path(root).resolve(),
            ])
            self.assertEqual([timeout for _path, timeout in seen], [3.0, 1.75])

    def test_hook_registry_lock_failure_warns_without_running_migration(self):
        import contextlib

        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            original_hold = module.hold_lock
            original_migrate = module.migrate_project_state
            migrated = []

            @contextlib.contextmanager
            def blocked(_path, timeout=None):
                raise common.WorkflowError("synthetic registry lock timeout")
                yield

            module.hold_lock = blocked
            module.migrate_project_state = lambda _root: migrated.append(True)
            try:
                rc, payload, err = self._run_context(module, root, home)
            finally:
                module.hold_lock = original_hold
                module.migrate_project_state = original_migrate
            self.assertEqual(rc, 0)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertEqual(migrated, [])
            self.assertIn("synthetic registry lock timeout", err)

    def test_hook_oserror_warns_but_always_emits_json_context(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            original = module.migrate_project_state
            module.migrate_project_state = lambda _root: (_ for _ in ()).throw(
                OSError("migration filesystem exploded"))
            try:
                rc, payload, err = self._run_context(module, root, home)
            finally:
                module.migrate_project_state = original
            self.assertEqual(rc, 0)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn("migration filesystem exploded", err)


class MigrationTests(unittest.TestCase):
    def test_home_data_dir_moves_at_dispatcher_entry(self):
        import contextlib
        import io
        import waystone

        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            old = home / ".claude" / "jahns-workflow"
            old.mkdir(parents=True)
            (old / "sentinel").write_text("kept")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_run_with_home(home, lambda: waystone.main([])), 1)
            new = home / ".claude" / "waystone"
            preserved = home / ".claude" / "waystone.pre-0.9"
            self.assertFalse(old.exists())
            self.assertFalse(new.exists())
            self.assertEqual((preserved / "sentinel").read_text(), "kept")

    def test_home_data_dir_conflict_warns_without_moving(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            old = home / ".claude" / "jahns-workflow"
            new = home / ".claude" / "waystone"
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            (old / "legacy").write_text("old")
            (new / "current").write_text("new")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                common.migrate_home_data(home)
            self.assertTrue((old / "legacy").is_file())
            preserved = home / ".claude" / "waystone.pre-0.9"
            self.assertFalse(new.exists())
            self.assertTrue((preserved / "current").is_file())
            self.assertIn(str(old), err.getvalue())
            self.assertIn(str(new), err.getvalue())
            self.assertIn(str(preserved), err.getvalue())

    def test_legacy_config_is_found_and_renamed_on_load(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            legacy = root / ".jahns-workflow.yml"
            legacy.write_text("version: 1\nproject: legacy\n")
            self.assertEqual(common.find_project_root(nested), root.resolve())
            self.assertEqual(common.load_config(root)["project"], "legacy")
            self.assertFalse(legacy.exists())
            self.assertTrue((root / ".waystone.yml").is_file())

    def test_config_conflict_prefers_new_and_warns(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            legacy = root / ".jahns-workflow.yml"
            current = root / ".waystone.yml"
            legacy.write_text("version: 1\nproject: legacy\n")
            current.write_text("version: 1\nproject: current\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                cfg = common.load_config(root)
            self.assertEqual(cfg["project"], "current")
            self.assertTrue(legacy.is_file())
            self.assertIn(str(legacy), err.getvalue())
            self.assertIn(str(current), err.getvalue())

    def test_legacy_profile_and_delta_schema_load(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            _write_profile(root, _PROFILE_BODY.replace("waystone-profile-1", "jw-profile-1"))
            profile, _ = _run_with_home(home, lambda: delegate._load_profile(root))
            self.assertEqual(profile["schema"], "jw-profile-1")
            delta = _add_delta(root, home, "verification_debt/legacy")
            path = _run_with_home(home, lambda: overlay._delta_path(root, delta["id"]))
            record = _json.loads(path.read_text())
            record["schema"] = "jw-delta-1"
            path.write_text(_json.dumps(record))
            loaded = _run_with_home(home, lambda: overlay.active_deltas_for_exposure(root))
            self.assertEqual(loaded[0]["schema"], "jw-delta-1")

    def test_legacy_review_marker_reads_and_new_marker_writes(self):
        legacy = "<!-- jw-review-cycle:v1\ncycle: 7\ntarget_sha: abc\n-->"
        parsed = review.parse_markers(legacy)
        self.assertEqual(parsed[0]["_kind"], "review-cycle")
        self.assertTrue(review.emit_marker("review-cycle", {"cycle": 8}).startswith(
            "<!-- waystone-review-cycle:v1"))

    def test_init_skill_upgrades_legacy_managed_markers(self):
        text = (SCRIPTS.parent / "skills" / "init" / "SKILL.md").read_text()
        for marker in (
            "<!-- jahns-workflow:begin -->", "<!-- jahns-workflow:end -->",
            "<!-- waystone:begin -->", "<!-- waystone:end -->",
        ):
            self.assertIn(marker, text)
        self.assertIn("always write", text.lower())
        for surface in (
            "waystone consent record install.agents accept",
            "waystone consent record install.hooks accept",
            "waystone install agents", "waystone install hooks",
            "agent file is left uncommitted", ".waystone/boundary-hooks-enabled",
        ):
            self.assertIn(surface, text)


class M2DocsTests(unittest.TestCase):
    """0.8.0 M2 C7 — guided skills and public operating-surface documentation."""

    def test_delegate_skill_preserves_provenance_and_recorded_acceptance(self):
        text = (SCRIPTS.parent / "skills" / "delegate" / "SKILL.md").read_text()
        self.assertIn("name: delegate", text)
        self.assertIn("/waystone:delegate", text)
        for phrase in (
            "delegate-claimed", "independent-verifier", "delegate verify", "verdict",
            "main-session", "--reason", "Escalation", "apply", "discard", "runner.jsonl",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("AskUserQuestion", text)
        escalation = text.split("## Escalation table", 1)[1].split("## ", 1)[0]
        rows = [line for line in escalation.splitlines()
                if line.startswith("| ") and line.split("|", 2)[1].strip().isdigit()]
        self.assertEqual(len(rows), 10)
        for meaning in (
            "owner-authored", "profile is missing", "unresolved blocker", "Two run attempts",
            "after one retry", "Apply drift", "runner failure is deterministic",
            "waystone warn conflict", "--allow-unsandboxed-runner --reason",
            "user explicitly requested review",
        ):
            self.assertIn(meaning, escalation)
        self.assertIn("These are the only escalation cases. Otherwise, do not ask", escalation)
        self.assertIn("When a verifier binding exists, always run it", text)
        self.assertIn("Allow at most two total run attempts", text)
        self.assertIn("implementer` + `external-runner", text)
        self.assertIn("waystone task set <task-id> --scope-add", text)
        self.assertIn("host's native main-session", text)

    def test_delegate_report_summarizes_warnings_without_internal_delta_ids(self):
        text = (SCRIPTS.parent / "skills" / "delegate" / "SKILL.md").read_text()
        report = text.split("## Step 6", 1)[1].split("## Escalation table", 1)[0]
        self.assertIn("plain-language meaning", report)
        self.assertNotIn("verbatim", report)
        self.assertIn("warnings_seen", text)
        self.assertIn("verdict-input-schema.json", text)
        self.assertIn("verdict-schema.json", text)

    def test_improve_skill_has_current_lenses_metrics_and_consent_flows(self):
        text = (SCRIPTS.parent / "skills" / "improve" / "SKILL.md").read_text()
        self.assertIn("Step 3.5", text)
        self.assertIn("verification_debt/*", text)
        self.assertIn("delegation-verification-evidence-v1", text)
        self.assertIn("review_association/*", text)
        self.assertIn("round-close-open-findings-v1", text)
        for lens in (
            "delegation_opportunity", "worker_scope_drift", "warn_friction",
            "env_unpreparedness", "adaptive_feedback", "finding_concentration",
        ):
            self.assertIn(f"`{lens}`", text)
        self.assertIn("recommendation_tier: always-allowed", text)
        self.assertIn("evidence-strength label", text)
        self.assertIn("waystone improve metrics", text)
        self.assertIn("unavailable_reason", text)
        self.assertIn("previous/current/delta", text)
        self.assertIn("waystone overlay add", text)
        self.assertIn("waystone overlay promote-user", text)
        self.assertIn("waystone consent record materialize accept", text)
        self.assertIn("waystone overlay materialize", text)
        self.assertIn("Never write delta JSON", text)
        self.assertIn("prevented", text)
        self.assertIn("improved", text)
        self.assertIn("benefit", text)

    def test_readme_and_front_door_name_all_new_surfaces(self):
        readme = (SCRIPTS.parent / "README.md").read_text()
        for surface in (
            "waystone paths", "waystone project", "waystone overlay", "waystone check",
            "waystone improve evidence", "waystone delegate verify", "waystone delegate verdict",
            "waystone task set <id> --scope-add <prefix>", "waystone improve metrics",
            "waystone overlay compose", "waystone overlay promote-user",
            "waystone overlay materialize", "waystone consent record", "waystone install",
        ):
            self.assertIn(f"`{surface}`", readme)
        self.assertIn("**v0.10 — Bind & Compose**", readme)
        self.assertIn("Implemented — current release", readme)
        import waystone
        for surface in (
            "improve", "evidence", "metrics", "delegate", "verify", "overlay", "promote-user",
            "materialize", "compose", "consent", "install", "scope-add", "check",
        ):
            self.assertIn(surface, waystone.__doc__)
        for surface in ("paths", "project"):
            self.assertIn(surface, waystone.__doc__)

    def test_waystone_bin_front_door(self):
        wrapper = SCRIPTS.parent / "bin" / "waystone"
        self.assertTrue(wrapper.is_file())
        self.assertTrue(wrapper.stat().st_mode & 0o111)
        self.assertIn('exec uv run "$here/../scripts/waystone.py" "$@"', wrapper.read_text())
        for skill in (SCRIPTS.parent / "skills").glob("*/SKILL.md"):
            text = skill.read_text()
            self.assertNotIn("<plugin-root>", text)
            self.assertNotIn("scripts/waystone.py", text)

    def test_conventions_state_overlay_evidence_invariants_and_residence(self):
        text = (SCRIPTS.parent / "references" / "conventions.md").read_text()
        for phrase in (
            "non-blocking", "least-restrictive", "task-id", "estimated nuisance rate",
            "workspace-write", "read-only", "harness-computed", "delegate-claimed",
            "independent-verifier", "main-session", "waystone delegate verdict",
            "{project_root}/.waystone/overlay/", "{project_root}/.waystone/exposure/",
            "{project_root}/.waystone/improve/evidence.jsonl", "~/.waystone/",
            "~/.waystone/cache/", ".pre-0.9", "git clean -fdx", "waystone paths",
            "~/.waystone/overlay/", ".waystone/maturity.json", ".waystone/consents.jsonl",
            ".waystone/improve/metrics.jsonl", "docs/waystone-policy.yaml", "scope:",
            "waystone task set <id> --scope-add", "{layer, id}", "waystone overlay compose",
            "waystone overlay promote-user", "waystone consent record",
            "waystone overlay materialize", "waystone install agents",
            "waystone improve metrics", "unavailable_reason", "tri-state",
        ):
            self.assertIn(phrase, text)


# ============================================================ v0.8.3: Codex host compatibility
class CodexHookTests(unittest.TestCase):
    def _project(self, directory: str) -> Path:
        root = Path(directory) / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
        (root / "tasks.yaml").write_text(TASKS_FIXTURE)
        (root / "ROADMAP.md").write_text("stale\n")
        return root

    def _guard(self, root: Path, payload: dict):
        import os

        script = SCRIPTS.parent / "hooks" / "scripts" / "tasks_guard.sh"
        return subprocess.run(
            ["bash", str(script)], input=_json.dumps(payload), cwd=root,
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(root.parent / "home")},
        )

    def test_claude_and_codex_payloads_regenerate_roadmap(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            claude = {
                "tool_name": "Edit", "cwd": str(root),
                "tool_input": {"file_path": str(root / "tasks.yaml")},
            }
            result = self._guard(root, claude)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual((root / "ROADMAP.md").read_text(), "stale\n")

            (root / "ROADMAP.md").write_text("stale-again\n")
            codex = {
                "tool_name": "apply_patch", "cwd": str(root),
                "tool_input": {"command": "*** Begin Patch\n*** Update File: tasks.yaml\n@@\n*** End Patch"},
            }
            result = self._guard(root, codex)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual((root / "ROADMAP.md").read_text(), "stale-again\n")

    def test_codex_invalid_tasks_patch_fails_without_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            (root / "tasks.yaml").write_text(TASKS_FIXTURE.replace("status: active", "status: bogus"))
            before = (root / "ROADMAP.md").read_bytes()
            payload = {
                "tool_name": "apply_patch", "cwd": str(root),
                "tool_input": {"command": "*** Begin Patch\n*** Update File: tasks.yaml\n@@\n*** End Patch"},
            }
            result = self._guard(root, payload)
            self.assertEqual(result.returncode, 2)
            self.assertIn("violates the workflow convention", result.stderr)
            self.assertEqual((root / "ROADMAP.md").read_bytes(), before)

    def test_tasks_guard_lock_timeout_warns_skips_regen_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            payload = {
                "tool_name": "Edit", "cwd": str(root),
                "tool_input": {"file_path": str(root / "tasks.yaml")},
            }
            before = (root / "ROADMAP.md").read_bytes()
            with common.hold_lock(common.project_lock_path(root), timeout=0.2):
                result = self._guard(root, payload)
            self.assertEqual(result.returncode, 0)
            self.assertEqual((root / "ROADMAP.md").read_bytes(), before)
            self.assertIn("lock", result.stderr.lower())
            self.assertIn("skipping ROADMAP regeneration", result.stderr)

    def test_tasks_guard_oserror_warns_skips_regen_and_exits_zero(self):
        import contextlib
        import io

        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import tasks_guard

        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            payload = {
                "tool_name": "Edit", "cwd": str(root),
                "tool_input": {"file_path": str(root / "tasks.yaml")},
            }
            before = (root / "ROADMAP.md").read_bytes()
            original_hold = tasks_guard.hold_lock
            old_stdin = sys.stdin

            @contextlib.contextmanager
            def broken(_path, timeout=None):
                raise OSError("synthetic lock filesystem failure")
                yield

            tasks_guard.hold_lock = broken
            sys.stdin = io.StringIO(_json.dumps(payload))
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    rc = tasks_guard.main()
            finally:
                tasks_guard.hold_lock = original_hold
                sys.stdin = old_stdin
            self.assertEqual(rc, 0)
            self.assertEqual((root / "ROADMAP.md").read_bytes(), before)
            self.assertIn("synthetic lock filesystem failure", err.getvalue())
            self.assertIn("skipping ROADMAP regeneration", err.getvalue())

    def test_session_context_names_host_instruction_file(self):
        import contextlib
        import io
        import os

        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context

        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            home = Path(d) / "home"

            def capture(host: str) -> str:
                old_host = os.environ.get("WAYSTONE_HOST")
                old_argv = sys.argv
                try:
                    if host == "codex":
                        os.environ["WAYSTONE_HOST"] = "codex"
                    else:
                        os.environ.pop("WAYSTONE_HOST", None)
                    sys.argv = ["session_context.py", str(root)]
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        _run_with_home(home, session_context.main)
                    return _json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
                finally:
                    sys.argv = old_argv
                    if old_host is None:
                        os.environ.pop("WAYSTONE_HOST", None)
                    else:
                        os.environ["WAYSTONE_HOST"] = old_host

            self.assertIn("see CLAUDE.md workflow section", capture("claude"))
            codex_context = capture("codex")
            self.assertIn("see AGENTS.md workflow section", codex_context)
            self.assertNotIn("see CLAUDE.md workflow section", codex_context)


class CodexTraceTests(unittest.TestCase):
    def _fixture(self, source: Path) -> Path:
        session_id = "11111111-2222-3333-4444-555555555555"
        path = source / f"rollout-2026-07-14T00-00-00-{session_id}.jsonl"
        records = [
            {"timestamp": "2026-07-14T00:00:00Z", "type": "session_meta", "payload": {
                "id": session_id, "cwd": "/tmp/proj", "cli_version": "0.144.4",
                "thread_source": "user"}},
            {"timestamp": "2026-07-14T00:00:01Z", "type": "turn_context", "payload": {
                "turn_id": "turn-1", "cwd": "/tmp/proj", "model": "gpt-test"}},
            {"timestamp": "2026-07-14T00:00:02Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "run it"}]}},
            {"timestamp": "2026-07-14T00:00:03Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "id": "m1",
                "content": [{"type": "output_text", "text": "checking"}]}},
            {"timestamp": "2026-07-14T00:00:04Z", "type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command", "call_id": "call-1",
                "arguments": _json.dumps({"cmd": "uv run tests"})}},
            {"timestamp": "2026-07-14T00:00:05Z", "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "call-1",
                "output": _json.dumps({"exit_code": 0, "output": "ok"})}},
            {"timestamp": "2026-07-14T00:00:06Z", "type": "response_item", "payload": {
                "type": "function_call", "namespace": "collaboration", "name": "spawn_agent",
                "call_id": "call-2", "arguments": _json.dumps({
                    "task_name": "audit", "message": "inspect"})}},
            {"timestamp": "2026-07-14T00:00:07Z", "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "call-2",
                "output": _json.dumps({"status": "completed", "agent_id": "child-1"})}},
            {"timestamp": "2026-07-14T00:00:08Z", "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3,
                    "reasoning_output_tokens": 1, "total_tokens": 13}}}},
            {"timestamp": "2026-07-14T00:00:09Z", "type": "session_meta", "payload": {
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "cwd": "/wrong-parent"}},
        ]
        _write_jsonl(path, records)
        return path

    def test_codex_rollout_projects_deterministically(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "sessions"
            source.mkdir()
            self._fixture(source)
            first, second = Path(d) / "first", Path(d) / "second"
            old_thread = os.environ.pop("CODEX_THREAD_ID", None)
            try:
                coverage = improve.run_trace([source], set(), first, host="codex")
                improve.run_trace([source], set(), second, host="codex")
            finally:
                if old_thread is not None:
                    os.environ["CODEX_THREAD_ID"] = old_thread
            self.assertEqual((first / "sessions.jsonl").read_bytes(),
                             (second / "sessions.jsonl").read_bytes())
            self.assertEqual((first / "delegations.jsonl").read_bytes(),
                             (second / "delegations.jsonl").read_bytes())
            row = _json.loads((first / "sessions.jsonl").read_text())
            self.assertEqual(row["project"], "proj")
            self.assertEqual(row["kind"], "main")
            self.assertEqual(row["turns"]["value"], 1)
            self.assertEqual(row["verification"]["runs"], 1)
            self.assertEqual(row["delegations"], 1)
            self.assertEqual(row["errors"]["tool"], 0)
            self.assertEqual(row["parser_version"], codexlog.PARSER_VERSION)
            self.assertEqual(coverage["files_by_kind"], {"codex_main_transcript": 1})
            self.assertEqual(coverage["unknown_tool_result_status"], 0)


class CodexVerifierTests(unittest.TestCase):
    def test_native_verifier_never_resolves_claude_companion(self):
        import contextlib
        import io
        import os

        with tempfile.TemporaryDirectory() as d:
            old_host = os.environ.get("WAYSTONE_HOST")
            os.environ["WAYSTONE_HOST"] = "codex"
            try:
                root, home = _deleg_project(d)
                profile = common.ensure_project_state_dir(root) / "profile.yml"
                profile.write_text(
                    "schema: waystone-profile-1\nbindings:\n"
                    "  implementer: {execution: external-runner, backend: \"codex:gpt-test\"}\n"
                    "  verifier: {backend: \"codex:gpt-test\", "
                    "entry: adversarial-review}\n"
                )
                _deleg_run(root, home, _deleg_fake({"f.txt": "changed\n"}))
                rec = _latest_rec(root, home)
                calls = []
                original_native = delegate._run_codex_verifier
                original_companion = delegate._companion_script

                def fake_native(worktree, model, focus, record_dir):
                    calls.append((worktree, model, focus, record_dir))
                    return 0, _json.dumps({
                        "summary": "reviewed", "findings": [], "limitations": [],
                    })

                def companion_must_not_run():
                    raise AssertionError("Codex native verification touched the Claude registry")

                delegate._run_codex_verifier = fake_native
                delegate._companion_script = companion_must_not_run
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = _run_with_home(home, lambda: delegate.verify_delegation(root, rec.name))
                finally:
                    delegate._run_codex_verifier = original_native
                    delegate._companion_script = original_companion
                self.assertEqual(rc, 0)
                self.assertEqual(len(calls), 1)
                artifact = _json.loads((rec / "artifact" / "verify-1.json").read_text())
                self.assertEqual(artifact["transport"], "codex-exec:read-only")
                self.assertEqual(artifact["provenance"], "independent-verifier")
            finally:
                if old_host is None:
                    os.environ.pop("WAYSTONE_HOST", None)
                else:
                    os.environ["WAYSTONE_HOST"] = old_host


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
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "s-main"}), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: round.close(
                    root, "2026-07-15-r1", done=["chore/close-me"], touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            rows = _read_warnings(root, home)
            fires = {row["rule"]: row for row in rows if row["event"] == "fire"}
            self.assertEqual(fires["env-manifest-mutation-v1"]["context"]["manifest_paths"],
                             ["pyproject.toml"])
            self.assertEqual(fires["done-without-evidence-v1"]["context"]["task_ids"],
                             ["chore/close-me"])
            self.assertEqual(fires["env-manifest-mutation-v1"]["context"]["round_id"],
                             "2026-07-15-r1")

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
            self.assertEqual({key: loaded[0][key] for key in row}, row)
            self.assertTrue(loaded[0]["source_pointer"].endswith("review-ingests.jsonl:1"))


class L2CImproveFeedbackTests(unittest.TestCase):
    """L2-C G4: accepted recommendation follow-through, trends, conflicts, and staleness."""

    def test_feedback_fact_is_observed_bounded_and_byte_stable(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root = base / "repo"
            root.mkdir()
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\ndelegation:\n  env_prep: [uv sync --frozen]\n")
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks:\n"
                "  - id: feat/x\n    title: x\n    status: done\n    round: r2\n")
            reviews_dir = root / "docs" / "reviews"
            reviews_dir.mkdir(parents=True)
            for rid, fid in (("r1", "JW-GPT-001"), ("r2", "JW-GPT-002")):
                (reviews_dir / f"{rid}-feedback.md").write_text(
                    "## Findings (triage skeleton v2)\n"
                    "| finding | severity | type | verdict | evidence | task id |\n"
                    "|---|---|---|---|---|---|\n"
                    f"| {fid} — scope | major | scope | REAL | `x.py:1` | feat/x |\n")
            state = root / ".waystone"
            (state / "overlay" / "deltas").mkdir(parents=True)
            delta_id = "worker_scope_drift/outside"
            (state / "overlay" / "deltas" / "worker_scope_drift--outside.json").write_text(
                _json.dumps({
                    "schema": "waystone-delta-1", "id": delta_id,
                    "rule": "delegation-scope-drift-v1", "status": "observing",
                    "created_at": "2026-07-15T01:00:00+00:00",
                    "evidence": {"source": "improve-rec", "rec_id": delta_id},
                    "replay": {"by_round": [{"round_id": "r1", "opportunities": 2,
                                               "fires": 1}]},
                }))
            _write_jsonl(state / "overlay" / "warnings.jsonl", [
                {"at": "2026-07-15T02:10:00+00:00", "boundary": "delegate-run",
                 "policy_identity": {"layer": "project", "id": delta_id},
                 "origin_delta_id": delta_id, "rule": "delegation-scope-drift-v1",
                 "delta_status": "observing", "event": "evaluation", "message": "evaluated",
                 "context": {"round_id": "r2", "delegation_id": "d1", "task_id": "feat/x",
                             "fired": True}},
                {"at": "2026-07-15T02:10:00+00:00", "boundary": "delegate-run",
                 "policy_identity": {"layer": "project", "id": delta_id},
                 "origin_delta_id": delta_id, "rule": "delegation-scope-drift-v1",
                 "delta_status": "observing", "event": "fire", "message": "outside",
                 "context": {"round_id": "r2", "delegation_id": "d1", "task_id": "feat/x",
                             "outside_scope": ["docs/x"]}},
                {"at": "2026-07-15T02:11:00+00:00", "boundary": "delegate-run",
                 "policy_identity": {"layer": "project", "id": delta_id},
                 "origin_delta_id": delta_id, "rule": "delegation-scope-drift-v1",
                 "delta_status": "observing", "event": "conflict", "message": "conflict",
                 "context": {"round_id": "r2", "delegation_id": "d1", "task_id": "feat/x",
                             "policy_identities": [
                                 {"layer": "project", "id": delta_id},
                                 {"layer": "project", "id": "scope/other"}]}},
            ])
            exposure = state / "exposure"
            exposure.mkdir()
            for rid, at, profile, env, overlays in (
                    ("r1", "2026-07-15T00:30:00+00:00", "p1", None, []),
                    ("r2", "2026-07-15T02:30:00+00:00", "p2",
                     ["uv sync --frozen"], [{
                         "identity": {"layer": "project", "id": delta_id},
                         "origin_delta_id": delta_id, "status": "observing"}])):
                (exposure / f"round-{rid}.json").write_text(_json.dumps({
                    "schema": "waystone-round-exposure-1", "round_id": rid, "at": at,
                    "profile_fingerprint": profile, "env_prep": env,
                    "overlays_active": overlays,
                }))
            improve_dir = state / "improve"
            improve_dir.mkdir()
            _write_jsonl(improve_dir / "decisions.jsonl", [{
                "rec_id": delta_id, "decision": "accept", "at": "2026-07-15T00:45:00+00:00",
            }])
            registry = base / "projects.json"
            registry.write_text(_json.dumps({"projects": [{"name": "demo", "path": str(root)}]}))
            out = base / "out"
            out.mkdir()
            _write_jsonl(out / "reviews.jsonl", [
                {"project": "demo", "round_id": "r1", "findings": [
                    {"id": "f1", "type": "scope", "status": "REAL"}]},
                {"project": "demo", "round_id": "r2", "findings": [
                    {"id": "f2", "type": "scope", "status": "REAL"}]},
            ])
            improve.run_evidence(registry, out, set())
            first = improve.run_audit(out)
            second = improve.run_audit(out)
            self.assertEqual(first, second)
            lens = next(row for row in first["lenses"] if row["lens"] == "adaptive_feedback")
            self.assertEqual(lens["provenance"], "observed")
            project = lens["per_project"]["demo"]
            self.assertEqual(project["decision_follow_through"]["materialized"], 1)
            self.assertEqual(project["same_scope_conflicts"], 1)
            self.assertEqual(project["re_review_candidates"], 1)
            delta = project["deltas"][0]
            self.assertEqual(delta["identity"], {"layer": "project", "id": delta_id})
            self.assertEqual(delta["origin_delta_id"], delta_id)
            self.assertEqual(delta["before"]["fire_rate"], 0.5)
            self.assertEqual(delta["after"]["fire_rate"], 1.0)
            self.assertEqual(delta["after"]["finding_recurrences"]["scope"], 1)
            self.assertLessEqual(len(lens["examples"]), 5)


class L2CAdversarialFixTests(unittest.TestCase):
    """L2-C adversarial findings: honest coverage, canonical joins, and stable populations."""

    def test_f1_invalid_task_scope_is_unknown_and_never_blocks_close(self):
        import contextlib
        import io
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            tasks = (root / "tasks.yaml").read_text().replace(
                "    deps: []\n", "    deps: []\n    scope: [../outside]\n", 1)
            (root / "tasks.yaml").write_text(tasks)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: round.close(
                    root, "2026-07-15-invalid-scope", done=["chore/close-me"],
                    touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            exposure = _json.loads((overlay._exposure_dir(root) /
                                    "round-2026-07-15-invalid-scope.json").read_text())
            self.assertIs(exposure["round_evidence"]["evaluable"], False)
            self.assertEqual(exposure["round_evidence"]["coverage_reason"],
                             "task-scope-invalid")

        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            with mock.patch.object(
                    overlay, "_capture_round_evidence", side_effect=RuntimeError("snapshot")), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: round.close(
                    root, "2026-07-15-snapshot-error", done=["chore/close-me"],
                    touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            exposure = _json.loads((overlay._exposure_dir(root) /
                                    "round-2026-07-15-snapshot-error.json").read_text())
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
                    root, "2026-07-15-one-scan", done=["chore/close-me", "chore/second"],
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

    def test_round_and_delegate_exposures_capture_composed_policy(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _round_review_project(d)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "review_association/local", rule="round-close-open-findings-v1", summary="s"))
            _run_with_home(home, lambda: overlay.set_round_override(
                root, "2026-01-02-close", "round-close-open-findings-v1", "observing", "temporary"))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = _run_with_home(home, lambda: round.close(
                    root, "2026-01-02-close", done=["chore/close-me"], touched=[], commit="HEAD"))
            self.assertEqual(rc, 0)
            exposure = _json.loads((overlay._exposure_dir(root) /
                                    "round-2026-01-02-close.json").read_text())
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

            with mock.patch.object(overlay, "hold_lock", observed_lock), \
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
        self.assertEqual(codex["version"], "0.10.0")
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
                legacy = home / ".claude" / "jahns-workflow"
                legacy.mkdir(parents=True)
                (legacy / "sentinel").write_text("keep")
                self.assertEqual(_run_with_home(
                    home, common.migrate_home_data, isolate_storage=False),
                                 home / ".waystone")
                self.assertFalse(legacy.exists())
                self.assertEqual(
                    (home / ".claude" / "waystone.pre-0.9" / "sentinel").read_text(), "keep")
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
        self.assertIn('--reviewer "<resolved reviewer backend>"', review_skill)
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

    def test_pr_freeze_sidecar_is_local_reviewed_sha_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
            path = review.write_pr_freeze_binding(
                root, "2026-07-15-r", 7, 2, "a" * 40, "b" * 40,
                ["codex:test"], "sha256:abc", "docs/reviews")
            row = _json.loads(path.read_text())
            self.assertEqual(row["schema"], review.PR_FREEZE_BINDING_SCHEMA)
            sidecars = improve._round_review_sidecars(root / "docs" / "reviews")
            binding = improve._review_sha_binding(
                None, "2026-07-15-r", "pr", sidecars["2026-07-15-r"])
            self.assertEqual(binding, (
                "a" * 40, "b" * 40, "explicit", None, "pr-freeze-sidecar"))
            conflict = {**sidecars["2026-07-15-r"][0],
                        "target_sha": "c" * 40, "_file": "conflict.json"}
            self.assertEqual(improve._review_sha_binding(
                None, "2026-07-15-r", "pr",
                [*sidecars["2026-07-15-r"], conflict]), (
                    None, None, "unknown", "conflicting-pr-freeze-sidecars", None))

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
                '  verifier: {backend: "codex:gpt-test", entry: adversarial-review}\n'
            ))
            plugin = home / "codex-plugin"
            (plugin / "scripts").mkdir(parents=True)
            (plugin / "scripts" / "codex-companion.mjs").write_text("// fixture\n")
            registry = home / ".claude" / "plugins" / "installed_plugins.json"
            registry.parent.mkdir(parents=True)
            registry.write_text(_json.dumps({"plugins": {"codex@openai-codex": [
                {"installPath": str(plugin)}]}}))
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
                '  verifier: {backend: "codex:gpt-test", entry: adversarial-review}\n'
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
            })
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
