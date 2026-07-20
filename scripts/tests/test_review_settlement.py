"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


class BasePolicyTests(unittest.TestCase):
    """B1: the merge-gate trust policy must come from the PR BASE SHA, never the candidate head —
    so a branch can't make itself an operator/approver, drop reviewers, or disable CI."""

    def test_policy_read_from_base_not_head(self):
        import contextlib
        import io

        STRICT_BASE = ("version: 1\nproject: x\ndelegation:\n  codex_runner_verified: true\n"
                       "review:\n  mode: pr\n  reviewers: [codex, gpt-5.5-pro]\n"
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
        stderr = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as d:
                # a local config must exist for the load_config fallback; the gate must ignore it
                # in favour of the base-SHA policy
                (Path(d) / ".waystone.yml").write_text("version: 1\nproject: x\nreview:\n  mode: pr\n")
                with contextlib.redirect_stderr(stderr):
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
        self.assertEqual(stderr.getvalue(), "")

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

    def _binding(self, root, round_id, *, target=None, base=None, reviewers=None):
        return review.write_round_request_binding(
            root, round_id, target or "a" * 40, "b" * 40 if base is None else base,
            reviewers or ["codex:gpt-5.6-sol"], mode="packet",
            narrative_digest=TEST_NARRATIVE_DIGEST,
            rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)

    def _reply(self, *, model="gpt-5.6-sol", effort="high", target=None, extra=""):
        target = target or f"{'b' * 12}-{'a' * 12}"
        return (f"model: {model}\neffort: {effort}\nreview-target: {target}\n"
                f"request-digest: {TEST_RENDERED_REQUEST_DIGEST}\n{extra}"
                "\n## Review\nNo major findings.\n").encode()

    def test_reply_header_parser_is_order_case_fence_and_unknown_key_tolerant(self):
        request_digest = "sha256:" + "c" * 64
        body = (f"\n```text\n  FOO : bar  \n REQUEST-DIGEST: {request_digest}\n"
                f" REVIEW-TARGET: {'b' * 13}-{'a' * 14}\n Effort : XHIGH\n"
                " Model: GPT-5.6-SOL\n```\n\nreview prose\n").encode()
        parsed = review.parse_review_reply_header(body)
        self.assertTrue(parsed["detected"])
        self.assertEqual(parsed["model"], "gpt-5.6-sol")
        self.assertEqual(parsed["effort"], "xhigh")
        self.assertEqual(parsed["review_target"], f"{'b' * 13}-{'a' * 14}")
        self.assertEqual(parsed["request_digest"], request_digest)
        self.assertEqual(parsed["metadata"]["foo"], "bar")
        self.assertEqual(parsed["warnings"], [])

    def test_reply_header_does_not_classify_leading_key_value_prose_without_anchor(self):
        parsed = review.parse_review_reply_header(
            b"Summary: looks sound\nEffort: high\nAudience: maintainers\n\nDetails follow\n")
        self.assertFalse(parsed["detected"])
        self.assertIsNone(parsed["model"])
        self.assertIsNone(parsed["review_target"])
        self.assertEqual(parsed["metadata"], {})

    def test_reply_header_invalid_utf8_block_is_absent_and_invalid_semantics_are_unknown(self):
        corrupt = review.parse_review_reply_header(
            b"model: gpt-5.6-\xff\neffort: extreme\nreview-target: not-a-range\n")
        self.assertFalse(corrupt["detected"])
        self.assertEqual(corrupt["metadata"], {})
        self.assertIsNone(corrupt["model"])
        self.assertIsNone(corrupt["effort"])
        self.assertIsNone(corrupt["review_target"])
        self.assertEqual(corrupt["warnings"], [])

        invalid = review.parse_review_reply_header(
            b"model: gpt-5.6-sol\neffort: extreme\nreview-target: not-a-range\n")
        self.assertEqual(invalid["model"], "gpt-5.6-sol")
        self.assertIsNone(invalid["effort"])
        self.assertIsNone(invalid["review_target"])
        self.assertIn("invalid-effort", invalid["warnings"])
        self.assertIn("invalid-review-target", invalid["warnings"])

        partial = review.parse_review_reply_header(b"model: gpt-5.6-sol\nfoo: kept\n")
        self.assertEqual(partial["model"], "gpt-5.6-sol")
        self.assertIsNone(partial["effort"])
        self.assertIsNone(partial["review_target"])
        self.assertIn("missing-effort", partial["warnings"])
        self.assertIn("missing-review-target", partial["warnings"])
        self.assertEqual(partial["metadata"]["foo"], "kept")

    def test_duplicate_standard_key_is_unknown_instead_of_last_wins(self):
        parsed = review.parse_review_reply_header(
            b"model: gpt-5.6-sol\nmodel: gpt-5.6-pro\neffort: high\n"
            + f"review-target: {'b' * 12}-{'a' * 12}\n".encode())
        self.assertTrue(parsed["detected"])
        self.assertIsNone(parsed["model"])
        self.assertNotIn("model", parsed["metadata"])
        self.assertIn("duplicate-model", parsed["warnings"])

    def test_reply_header_decode_excludes_long_non_ascii_body_at_byte_boundary(self):
        header = (b"model: gpt-5.6-sol\neffort: high\n"
                  + f"review-target: {'b' * 12}-{'a' * 12}\n\n".encode())
        padding = b"x" * (review.REVIEW_REPLY_HEADER_MAX_BYTES - len(header) - 1)
        body = header + padding + "한글 장문 회신".encode()
        with self.assertRaises(UnicodeDecodeError):
            body[:review.REVIEW_REPLY_HEADER_MAX_BYTES].decode("utf-8")

        parsed = review.parse_review_reply_header(body)
        self.assertTrue(parsed["detected"])
        self.assertEqual(parsed["model"], "gpt-5.6-sol")
        self.assertEqual(parsed["effort"], "high")
        self.assertEqual(parsed["review_target"], f"{'b' * 12}-{'a' * 12}")

    def test_reply_header_byte_limit_counts_exact_eof_and_crlf_bytes(self):
        lines = [
            b"model: gpt-5.6-sol",
            b"effort: high",
            f"review-target: {'b' * 12}-{'a' * 12}".encode(),
            f"request-digest: {'sha256:' + 'c' * 64}".encode(),
            b"padding: ",
        ]
        prefix = b"\r\n".join(lines)
        exact = prefix + b"x" * (review.REVIEW_REPLY_HEADER_MAX_BYTES - len(prefix))
        self.assertEqual(len(exact), review.REVIEW_REPLY_HEADER_MAX_BYTES)

        parsed = review.parse_review_reply_header(exact)
        self.assertTrue(parsed["detected"])
        self.assertNotIn("header-limit-exceeded", parsed["warnings"])

        over = review.parse_review_reply_header(exact + b"x")
        self.assertTrue(over["detected"])
        self.assertIn("header-limit-exceeded", over["warnings"])

    def test_reviewer_model_normalization_is_bounded_not_alias_guessing(self):
        self.assertTrue(review.reviewer_model_matches(
            "gpt-5.6-sol", "codex:gpt-5.6-sol"))
        self.assertTrue(review.reviewer_model_matches(
            "CODEX:GPT-5.6-SOL", "gpt-5.6-sol"))
        self.assertFalse(review.reviewer_model_matches(
            "openai:gpt-5.6-sol", "codex:gpt-5.6-sol"))
        self.assertFalse(review.reviewer_model_matches("codex", "codex:gpt-5.6-sol"))

    def test_reply_header_template_round_trips_and_all_surfaces_state_same_rules(self):
        import re

        template = (SCRIPTS.parent / "templates/review-request.md").read_text()
        block = re.search(
            r"Start the reply with this block.*?```text\n(.*?)\n```", template, re.DOTALL)
        self.assertIsNotNone(block)
        self.assertEqual(len(block.group(1).splitlines()), 4)
        rendered_block = (block.group(1)
                          .replace("[[REPLY_MODEL]]", "gpt-5.6-sol")
                          .replace("[[REVIEW_TARGET]]", "a1b2c3d4e5f6-a2b3c4d5e6f7")
                          .replace("[[REQUEST_DIGEST]]", TEST_RENDERED_REQUEST_DIGEST))
        parsed = review.parse_review_reply_header(rendered_block.encode())
        self.assertEqual(parsed["model"], "gpt-5.6-sol")
        self.assertEqual(parsed["effort"], "high")
        self.assertEqual(parsed["review_target"], "a1b2c3d4e5f6-a2b3c4d5e6f7")
        self.assertEqual(parsed["request_digest"], TEST_RENDERED_REQUEST_DIGEST)
        self.assertEqual(review.REVIEW_EFFORT_VALUES, delegate._EFFORT_VALUES)

        round_skill = (SCRIPTS.parent / "skills/round/SKILL.md").read_text()
        review_skill = (SCRIPTS.parent / "skills/review/SKILL.md").read_text()
        # Reviewer-facing semantics live ONCE, statically, in the template (user ruling
        # 2026-07-17); the round skill discusses only the narrative/render command and ingest
        # semantics stay in the review skill.
        for token in ("reply-header block verbatim", "Markdown fence", "ordinary prose"):
            self.assertNotIn(token, round_skill)
        self.assertIn("unconditional part of round closeout", round_skill)
        self.assertIn("--narrative", round_skill)
        for token in ("model", "effort", "review-target", "request-digest",
                      "Markdown fence", "extra keys", "ordinary prose"):
            self.assertIn(token, review_skill)
        self.assertIn("12–40 hex", review_skill)

    def test_stored_metadata_reader_projects_verbatim_body_header_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            binding_path = self._binding(root, round_id)
            src = root / "reply.md"
            body_payload = _json.dumps({"metadata": {"model": "forged"}},
                                       separators=(",", ":"))
            src.write_bytes(self._reply(extra="foo: bar\n") + (
                f"\nreply-metadata-json: {body_payload}\n").encode())
            self.assertEqual(review.ingest(root, round_id, src=src), 0)
            feedback = root / f"docs/reviews/{round_id}-feedback.md"
            projected = review.read_feedback_reply_metadata(
                feedback, expected_round_id=round_id,
                binding=review.read_round_request_binding(binding_path))
            self.assertEqual(projected["model"], "gpt-5.6-sol")
            self.assertEqual(projected["effort"], "high")
            self.assertEqual(
                projected["review_target"], f"{'b' * 12}-{'a' * 12}")
            self.assertIs(projected["review_target_matches"], True)
            self.assertIs(projected["reviewer_configured"], True)
            self.assertEqual(projected["metadata"]["foo"], "bar")

            only_body = Path(d) / "only-body.md"
            only_body.write_bytes(self._reply())
            missing = review.read_feedback_reply_metadata(only_body)
            self.assertIsNone(missing["model"])
            self.assertEqual(missing["metadata"], {})

    def test_feedback_separator_crossing_header_cap_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            binding_path = self._binding(root, round_id)
            src = root / "reply.md"
            src.write_bytes(self._reply())
            self.assertEqual(review.ingest(root, round_id, src=src), 0)
            feedback = root / f"docs/reviews/{round_id}-feedback.md"
            header, tail = feedback.read_bytes().split(
                review.FEEDBACK_HEADER_SEPARATOR, 1)
            padding_prefix = b"\ncache-padding: "
            padding = b"x" * (
                review.FEEDBACK_HEADER_MAX_BYTES - 1 - len(header) - len(padding_prefix))
            content = (header + padding_prefix + padding
                       + review.FEEDBACK_HEADER_SEPARATOR + tail)
            feedback.write_bytes(content)
            start = content.index(review.FEEDBACK_HEADER_SEPARATOR)
            self.assertLess(start, review.FEEDBACK_HEADER_MAX_BYTES)
            self.assertGreater(
                start + len(review.FEEDBACK_HEADER_SEPARATOR),
                review.FEEDBACK_HEADER_MAX_BYTES)
            projected = review.read_feedback_reply_metadata(
                feedback, expected_round_id=round_id,
                binding=review.read_round_request_binding(binding_path))
            self.assertIs(projected["review_target_matches"], True)
            self.assertIs(projected["reviewer_configured"], True)

    def test_feedback_body_boundary_damage_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            binding_path = self._binding(root, round_id)
            src = root / "reply.md"
            src.write_bytes(self._reply())
            self.assertEqual(review.ingest(root, round_id, src=src), 0)
            feedback = root / f"docs/reviews/{round_id}-feedback.md"
            binding = review.read_round_request_binding(binding_path)
            pristine = feedback.read_bytes()
            first = pristine.index(review.FEEDBACK_HEADER_SEPARATOR)

            damaged_files = {
                "missing": pristine[:first] + b"\n\n--\n\n" + pristine[
                    first + len(review.FEEDBACK_HEADER_SEPARATOR):],
                "duplicate": pristine[:first + len(review.FEEDBACK_HEADER_SEPARATOR)]
                    + review.FEEDBACK_HEADER_SEPARATOR
                    + pristine[first + len(review.FEEDBACK_HEADER_SEPARATOR):],
            }
            for label, damaged in damaged_files.items():
                with self.subTest(label=label):
                    feedback.write_bytes(damaged)
                    projected = review.read_feedback_reply_metadata(
                        feedback, expected_round_id=round_id, binding=binding)
                    self.assertIsNone(projected["review_target_matches"])
                    self.assertIsNone(projected["rendered_request_digest_matches"])
                    self.assertEqual(
                        projected["rendered_request_coverage_reason"],
                        "feedback-receipt-corrupt")

    def test_feedback_reader_does_not_load_arbitrary_reply_body(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            binding_path = self._binding(root, round_id)
            src = root / "reply.md"
            src.write_bytes(self._reply() + b"x" * (2 * 1024 * 1024))
            self.assertEqual(review.ingest(root, round_id, src=src), 0)
            feedback = root / f"docs/reviews/{round_id}-feedback.md"
            binding = review.read_round_request_binding(binding_path)

            with mock.patch.object(
                    Path, "read_bytes",
                    side_effect=AssertionError("receipt reader must use bounded reads")):
                projected = review.read_feedback_reply_metadata(
                    feedback, expected_round_id=round_id, binding=binding)
            self.assertIs(projected["rendered_request_digest_matches"], True)
            self.assertIs(projected["review_target_matches"], True)

    def test_overlay_pr_projection_uses_pr_request_generation_directory(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: x\nreview:\n  mode: pr\n"
                "  reviewers: [codex:gpt-5.6-sol]\n")
            round_id = "2026-07-19-pr-receipt"
            request_dir = common.ensure_project_state_dir(root) / "review-requests"
            review.write_round_request_binding(
                root, round_id, "a" * 40, "b" * 40,
                ["codex:gpt-5.6-sol"], mode="pr",
                narrative_digest=TEST_NARRATIVE_DIGEST,
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST,
                directory=request_dir)
            review.write_pr_freeze_binding(
                root, round_id, 7, 1, "a" * 40, "b" * 40,
                ["codex:gpt-5.6-sol"], None, "docs/reviews",
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
            src = root / "reply.md"
            src.write_bytes(self._reply())
            self.assertEqual(review.ingest(root, round_id, src=src), 0)

            with mock.patch.object(
                    review, "read_feedback_reply_metadata",
                    wraps=review.read_feedback_reply_metadata) as reader:
                events, skipped = overlay.load_review_ingests(root)
            self.assertEqual(skipped, 0)
            self.assertEqual(reader.call_args.kwargs["request_generation_dir"], request_dir)
            self.assertEqual(events[0]["reviewer"], "gpt-5.6-sol")
            self.assertIs(events[0]["review_target_matches"], True)
            self.assertIs(events[0]["reviewer_configured"], True)
            self.assertIsNone(events[0]["reviewer_coverage_reason"])

    def test_projection_recomputes_binding_and_rejects_feedback_round_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            self._binding(root, round_id)
            src = root / "reply.md"
            src.write_bytes(self._reply())
            self.assertEqual(review.ingest(root, round_id, src=src), 0)
            feedback = root / f"docs/reviews/{round_id}-feedback.md"
            binding, reason = review.ingest_round_binding(
                root, round_id, common.load_config(root))
            self.assertIsNone(reason)

            content = feedback.read_bytes()
            stored = _json.loads(next(
                line.removeprefix("reply-metadata-json: ")
                for line in content.split(review.FEEDBACK_HEADER_SEPARATOR, 1)[0]
                .decode().splitlines() if line.startswith("reply-metadata-json: ")))
            self.assertEqual(
                set(stored),
                {"metadata", "narrative_digest", "rendered_request_digest",
                 "rendered_request_digest_matches", "rendered_request_coverage_reason"})
            projected = review.read_feedback_reply_metadata(
                feedback, expected_round_id=round_id, binding=binding)
            self.assertIs(projected["review_target_matches"], True)
            self.assertIs(projected["reviewer_configured"], True)

            copied = feedback.with_name("r2-feedback.md")
            copied.write_bytes(content)
            mismatch = review.read_feedback_reply_metadata(
                copied, expected_round_id="r2", binding=binding)
            self.assertIsNone(mismatch["review_target_matches"])
            self.assertIsNone(mismatch["reviewer_configured"])
            self.assertEqual(mismatch["reviewer_coverage_reason"], "feedback-round-mismatch")

    def test_pre_echo_receipt_without_verbatim_envelope_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            binding_path = self._binding(root, round_id)
            binding = review.read_round_request_binding(binding_path)
            feedback = root / f"docs/reviews/{round_id}-feedback.md"
            feedback.write_text(
                "<!-- waystone feedback -->\n"
                f"round: {round_id}\n"
                "reply-metadata-json: " + _json.dumps({
                    "metadata": {
                        "model": "gpt-5.6-sol", "effort": "high",
                        "review-target": f"{'b' * 12}-{'a' * 12}",
                    },
                    "narrative_digest": TEST_NARRATIVE_DIGEST,
                    "rendered_request_digest": TEST_RENDERED_REQUEST_DIGEST,
                }, separators=(",", ":")) + "\n\n---\n\nreply body\n")

            projected = review.read_feedback_reply_metadata(
                feedback, expected_round_id=round_id, binding=binding)
            self.assertIsNot(projected["rendered_request_digest_matches"], True)
            self.assertEqual(
                projected["rendered_request_coverage_reason"],
                "feedback-receipt-corrupt")

    def test_projection_rejects_stored_metadata_that_disagrees_with_body(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            binding_path = self._binding(root, round_id)
            src = root / "reply.md"
            src.write_bytes(self._reply())
            self.assertEqual(review.ingest(root, round_id, src=src), 0)
            feedback = root / f"docs/reviews/{round_id}-feedback.md"
            header, tail = feedback.read_bytes().split(
                review.FEEDBACK_HEADER_SEPARATOR, 1)
            lines = header.decode().splitlines()
            index = next(index for index, line in enumerate(lines)
                         if line.startswith("reply-metadata-json: "))
            payload = _json.loads(lines[index].removeprefix("reply-metadata-json: "))
            payload["metadata"]["model"] = "bad value"
            lines[index] = "reply-metadata-json: " + _json.dumps(
                payload, sort_keys=True, separators=(",", ":"))
            feedback.write_bytes(
                "\n".join(lines).encode() + review.FEEDBACK_HEADER_SEPARATOR + tail)
            projected = review.read_feedback_reply_metadata(
                feedback, expected_round_id=round_id,
                binding=review.read_round_request_binding(binding_path))
            self.assertEqual(projected["metadata"], {})
            self.assertIsNone(projected["model"])
            self.assertEqual(
                projected["reviewer_coverage_reason"], "feedback-cache-body-mismatch")

    def test_overlay_skips_invalid_round_before_receipt_path_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            event_path = common.ensure_project_state_dir(root) / "overlay/review-ingests.jsonl"
            event_path.parent.mkdir(parents=True, exist_ok=True)
            event_path.write_text("\n".join(_json.dumps(row) for row in (
                {
                    "schema": overlay.REVIEW_FEEDBACK_SCHEMA,
                    "event": "review-feedback",
                    "at": "2026-07-18T00:00:00+00:00",
                    "round_id": "/",
                    "source": "packet-ingest",
                    "event_id": "packet:corrupt",
                    "reviewer": None,
                },
                {
                    "schema": overlay.REVIEW_FEEDBACK_SCHEMA,
                    "event": "review-feedback",
                    "at": "2026-07-18T00:01:00+00:00",
                    "round_id": "2026-07-18-valid",
                    "source": "pr-marker",
                    "event_id": "pr:valid",
                    "reviewer": "codex",
                },
            )) + "\n")

            events, skipped = overlay.load_review_ingests(root)

            self.assertEqual(skipped, 1)
            self.assertEqual([event["round_id"] for event in events],
                             ["2026-07-18-valid"])

    def test_invalid_utf8_header_ingests_as_absent_without_replacement(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            self._binding(root, round_id)
            src = root / "corrupt.md"
            src.write_bytes(
                b"model: gpt-5.6-\xff\neffort: high\n"
                + f"review-target: {'b' * 12}-{'a' * 12}\n".encode())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.ingest(root, round_id, src=src), 0)
            self.assertIn("structured reply header not found", err.getvalue())
            feedback = root / f"docs/reviews/{round_id}-feedback.md"
            binding, _reason = review.ingest_round_binding(
                root, round_id, common.load_config(root))
            projected = review.read_feedback_reply_metadata(
                feedback, expected_round_id=round_id, binding=binding)
            self.assertIsNone(projected["model"])
            self.assertIsNone(projected["effort"])
            self.assertIsNone(projected["review_target"])
            self.assertIsNone(projected["review_target_matches"])
            self.assertIsNone(projected["reviewer_configured"])
            self.assertNotIn("\ufffd", feedback.read_bytes().split(b"\n\n---\n\n", 1)[0]
                             .decode("utf-8"))
            events, skipped = overlay.load_review_ingests(root)
            self.assertEqual(skipped, 0)
            self.assertIsNone(events[0]["reviewer"])
            self.assertIsNone(events[0]["reviewer_configured"])

    def test_byte_exact_copy_and_consume(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            self._binding(root, "2026-06-22-x")
            src = root / "inbox.md"
            # tricky bytes: CRLF, trailing spaces, multibyte utf-8, NO final newline
            body = (f"model: gpt-5.6-sol\r\neffort: high\r\n"
                    f"review-target: {'b' * 12}-{'a' * 12}\r\nfoo: bar\r\n\r\n"
                    "## Review\r\n  trailing   \nutf8: é한\n\n---\n\n"
                    "body separator\nno final newline").encode("utf-8")
            src.write_bytes(body)
            rc = review.ingest(root, "2026-06-22-x", src=src)
            self.assertEqual(rc, 0)
            dest = root / "docs/reviews/2026-06-22-x-feedback.md"
            content = dest.read_bytes()
            self.assertIn(body, content)                     # body byte-exact, verbatim (within the file)
            # verbatim body sits between the header separator and the appended triage skeleton
            self.assertIn(
                body + b"\n\n---\n\n" + review.TRIAGE_BEGIN
                + b"\n## Findings (triage skeleton", content)
            self.assertIn(b"round: 2026-06-22-x", content)
            self.assertIn(b"reviewer: gpt-5.6-sol", content)
            self.assertFalse(src.exists())                   # drop-file consumed

            binding, _reason = review.ingest_round_binding(
                root, "2026-06-22-x", common.load_config(root))
            metadata = review.read_feedback_reply_metadata(
                dest, expected_round_id="2026-06-22-x", binding=binding)
            self.assertEqual(metadata["model"], "gpt-5.6-sol")
            self.assertEqual(metadata["effort"], "high")
            self.assertEqual(metadata["review_target"], f"{'b' * 12}-{'a' * 12}")
            self.assertEqual(metadata["metadata"]["foo"], "bar")
            self.assertIs(metadata["review_target_matches"], True)
            self.assertIs(metadata["reviewer_configured"], True)

    def test_ws_finding_blocks_build_triage_skeleton(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            self._binding(root, round_id)
            src = root / "reply.md"
            src.write_bytes(
                self._reply()
                + b"\n### WS-GPT-007 - exact issue\n- Severity: major\n")

            self.assertEqual(review.ingest(root, round_id, src=src), 0)
            feedback = root / f"docs/reviews/{round_id}-feedback.md"
            self.assertIn(
                "| WS-GPT-007 — exact issue | major |  |  |  |  |",
                feedback.read_text(encoding="utf-8"),
            )

    def test_triage_command_replaces_only_marked_tail_with_quoted_markers_in_reply(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            self._binding(root, round_id)
            src = root / "reply.md"
            body = self._reply() + (
                b"\nQuoted protocol text:\n" + review.TRIAGE_BEGIN + b"\n"
                + review.TRIAGE_END + b"\n")
            src.write_bytes(body)
            self.assertEqual(review.ingest(root, round_id, src=src), 0)
            feedback = root / f"docs/reviews/{round_id}-feedback.md"
            before = feedback.read_bytes()
            actual_begin = before.rfind(review.TRIAGE_BEGIN)
            self.assertGreater(actual_begin, before.index(body))
            immutable_prefix = before[:actual_begin]

            replacement = root / "triage.md"
            replacement.write_bytes(
                b"## Findings (triage skeleton v2)\n\n"
                b"| finding | severity | type | verdict | evidence | task id |\n"
                b"|---|---|---|---|---|---|\n")
            self.assertEqual(review.triage(root, round_id, replacement), 0)
            after = feedback.read_bytes()
            self.assertEqual(after[:actual_begin], immutable_prefix)
            self.assertIn(replacement.read_bytes(), after[actual_begin:])
            self.assertTrue(after.endswith(review.TRIAGE_END + b"\n"))
            self.assertIn(body, after[:actual_begin])

    def test_triage_command_refuses_missing_or_damaged_markers(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            rdir = root / "docs/reviews"
            rdir.mkdir(parents=True)
            replacement = root / "triage.md"
            replacement.write_text("## Findings (triage skeleton v2)\n")
            feedback = rdir / "r1-feedback.md"
            for damaged in (b"header\nreply\n", review.TRIAGE_BEGIN + b"\nno end\n",
                            review.TRIAGE_BEGIN + b"\ncontent\n" + review.TRIAGE_END
                            + b"\ntrailing bytes"):
                feedback.write_bytes(damaged)
                before = feedback.read_bytes()
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(review.main([
                        "triage", "--round", "r1", "--file", str(replacement), str(root),
                    ]), 1)
                self.assertEqual(feedback.read_bytes(), before)
                self.assertIn("triage marker", err.getvalue())

    def test_triage_refuses_masked_canonical_marker_via_offset_anchor(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            self._binding(root, round_id)
            src = root / "reply.md"
            body = self._reply() + (
                b"\nQuoted protocol line:\n" + review.TRIAGE_BEGIN + b"\n")
            src.write_bytes(body)
            self.assertEqual(review.ingest(root, round_id, src=src), 0)
            feedback = root / f"docs/reviews/{round_id}-feedback.md"
            content = feedback.read_bytes()
            # Hand-damage ONLY the canonical BEGIN (a discipline violation); the quoted BEGIN in
            # the verbatim reply and the canonical END at EOF remain — the masking case.
            canonical = content.rfind(b"\n" + review.TRIAGE_BEGIN + b"\n")
            damaged = (content[:canonical] + b"\n<!-- damaged -->\n"
                       + content[canonical + len(review.TRIAGE_BEGIN) + 2:])
            feedback.write_bytes(damaged)
            replacement = root / "triage.md"
            replacement.write_text("## Findings (triage v2)\n")

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.main([
                    "triage", "--round", round_id, "--file", str(replacement), str(root),
                ]), 1)
            self.assertEqual(feedback.read_bytes(), damaged)
            self.assertIn("triage marker", err.getvalue())

    def test_packet_feedback_identity_uses_declared_model_and_frozen_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: x\nreview:\n  reviewers: [role:reviewer]\n")
            (common.ensure_project_state_dir(root) / "profile.yml").write_text(
                "schema: waystone-profile-1\nbindings:\n"
                "  reviewer: {execution: external-runner, backend: 'codex:gpt-5.6-sol'}\n")
            r1, r2, r3 = "2026-07-18-r1", "2026-07-18-r2", "2026-07-18-r3"
            self._binding(root, r1, reviewers=["codex:gpt-5.6-sol"])
            self._binding(root, r2, reviewers=["codex:gpt-5.6-sol"])
            configured = root / "configured.md"
            configured.write_bytes(self._reply())
            self.assertEqual(review.ingest(root, r1, src=configured), 0)
            ad_hoc = root / "ad-hoc.md"
            ad_hoc.write_bytes(self._reply(model="other-model"))
            self.assertEqual(review.ingest(root, r2, src=ad_hoc), 0)

            events, skipped = overlay.load_review_ingests(root)
            self.assertEqual(skipped, 0)
            by_round = {event["round_id"]: event for event in events}
            # A declared bare model matches the provider-qualified backend frozen in the sidecar.
            self.assertEqual(by_round[r1]["reviewer"], "gpt-5.6-sol")
            self.assertIs(by_round[r1]["reviewer_configured"], True)
            self.assertEqual(by_round[r1]["reviewer_effort"], "high")
            self.assertEqual(by_round[r2]["reviewer"], "other-model")
            self.assertIsNone(by_round[r2]["reviewer_configured"])

            fixed_events = [
                {**by_round[r1], "at": "2026-07-15T01:00:00+00:00"},
                {**by_round[r2], "at": "2026-07-15T03:00:00+00:00"},
            ]
            rounds = [
                {"round_id": r1, "at": "2026-07-15T00:00:00+00:00",
                 "review_mode": "packet"},
                {"round_id": r2, "at": "2026-07-15T02:00:00+00:00",
                 "review_mode": "packet"},
                {"round_id": r3, "at": "2026-07-15T04:00:00+00:00",
                 "review_mode": "packet"},
            ]
            result = overlay.evaluate_review_skipped_closes(
                rounds, fixed_events, consecutive=2)
            self.assertEqual(result["fires"], [r3])
            self.assertIsNone(result["by_round"][-1]["feedback_observed"])
            self.assertEqual(result["unknown_reviewer_feedback"], 1)

    def test_overlay_projection_ignores_stored_packet_identity_assessment(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            self._binding(root, round_id)
            src = root / "reply.md"
            src.write_bytes(self._reply())
            self.assertEqual(review.ingest(root, round_id, src=src), 0)

            event_path = common.ensure_project_state_dir(root) / "overlay/review-ingests.jsonl"
            stored = _json.loads(event_path.read_text())
            stored.update({
                "reviewer": "forged-model",
                "review_target_matches": False,
                "reviewer_configured": None,
                "reviewer_coverage_reason": "forged-stored-result",
                "reply_metadata": {"model": "forged-model"},
            })
            event_path.write_text(_json.dumps(stored) + "\n")

            events, skipped = overlay.load_review_ingests(root)
            self.assertEqual(skipped, 0)
            self.assertEqual(events[0]["reviewer"], "gpt-5.6-sol")
            self.assertIs(events[0]["review_target_matches"], True)
            self.assertIs(events[0]["reviewer_configured"], True)
            self.assertEqual(events[0]["reply_metadata"]["model"], "gpt-5.6-sol")

    def test_target_mismatch_and_sidecarless_legacy_are_not_configured_feedback(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            wrong_round = "2026-07-18-wrong"
            self._binding(root, wrong_round)
            wrong = root / "wrong.md"
            wrong.write_bytes(self._reply(target=f"{'c' * 12}-{'d' * 12}"))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.ingest(root, wrong_round, src=wrong), 0)
            self.assertIn("review-target", err.getvalue())

            legacy_round = "2026-06-22-legacy"
            legacy = root / "legacy.md"
            legacy.write_bytes(self._reply())
            with contextlib.redirect_stderr(err):
                self.assertEqual(review.ingest(root, legacy_round, src=legacy), 0)
            self.assertEqual(list((root / "docs/reviews").glob(
                f"{legacy_round}-request.binding*.json")), [])
            events, skipped = overlay.load_review_ingests(root)
            self.assertEqual(skipped, 0)
            by_round = {event["round_id"]: event for event in events}
            self.assertIs(by_round[wrong_round]["review_target_matches"], False)
            self.assertIsNone(by_round[wrong_round]["reviewer_configured"])
            self.assertIsNone(by_round[legacy_round]["review_target_matches"])
            self.assertIsNone(by_round[legacy_round]["reviewer_configured"])
            self.assertEqual(by_round[legacy_round]["reviewer_coverage_reason"],
                             "round-binding-unavailable")

    def test_force_reingest_replaces_legacy_identity_event_for_round(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            self._binding(root, round_id)
            event_path = common.ensure_project_state_dir(root) / "overlay/review-ingests.jsonl"
            event_path.parent.mkdir(parents=True, exist_ok=True)
            event_path.write_text(_json.dumps({
                "schema": overlay.REVIEW_FEEDBACK_SCHEMA, "event": "review-feedback",
                "at": "2026-07-15T00:00:00+00:00", "round_id": round_id,
                "source": "packet-ingest", "event_id": f"packet:{round_id}:reviewer:old-model",
                "reviewer": "old-model", "reviewer_configured": True,
                "reviewer_coverage_reason": None, "provenance": "observed",
            }) + "\n")
            dest = root / f"docs/reviews/{round_id}-feedback.md"
            dest.write_text("old feedback")
            src = root / "corrected.md"
            src.write_bytes(self._reply())
            self.assertEqual(review.ingest(root, round_id, src=src, force=True), 0)

            raw_rows = [_json.loads(line) for line in event_path.read_text().splitlines()]
            self.assertEqual(len(raw_rows), 1)
            self.assertEqual(raw_rows[0]["event_id"], f"packet:{round_id}")
            self.assertEqual(raw_rows[0]["reviewer"], "gpt-5.6-sol")
            self.assertNotIn("reviewer_configured", raw_rows[0])
            self.assertNotIn("review_target_matches", raw_rows[0])

    def test_force_reingest_rolls_feedback_back_if_event_correction_fails(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-07-18-r1"
            self._binding(root, round_id)
            dest = root / f"docs/reviews/{round_id}-feedback.md"
            dest.write_bytes(b"old feedback")
            src = root / "corrected.md"
            src.write_bytes(self._reply())
            original = overlay.record_review_ingest
            overlay.record_review_ingest = lambda *a, **k: (_ for _ in ()).throw(
                OSError("synthetic event write failure"))
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    rc = review.ingest(root, round_id, src=src, force=True)
            finally:
                overlay.record_review_ingest = original
            self.assertEqual(rc, 1)
            self.assertEqual(dest.read_bytes(), b"old feedback")
            self.assertTrue(src.exists())
            self.assertIn("rolled back", err.getvalue())

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

    def test_packet_ingest_does_not_synthesize_missing_sidecar_from_request(self):
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
            self.assertEqual(sidecars, [])

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


class PendingReviewTests(unittest.TestCase):
    REQUEST_DIGEST_SENTINEL = "sha256:" + "0" * 64
    REQUEST = (
        "request\n\n## Response wanted\n\n```text\n"
        f"request-digest: {REQUEST_DIGEST_SENTINEL}\n```\n"
    )
    NARRATIVE = "\n\n".join(
        f"{heading}\n\ncontent" for heading in review.NARRATIVE_HEADINGS) + "\n"

    def _root(self, d: str) -> Path:
        root = Path(d) / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text(
            "version: 1\nproject: pending-review-test\nreviews_dir: docs/reviews\n"
            "state:\n  last_round_commit: null\n")
        (root / "tasks.yaml").write_text(
            "version: 1\nproject: pending-review-test\ntasks:\n"
            "  - id: chore/close-me\n    title: close task now\n    status: active\n    deps: []\n")
        (root / "docs" / "reviews").mkdir(parents=True)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "pending review setup")
        return root

    def _request(self, root: Path, round_id: str, target: str, *, base: str = "b" * 40,
                 reviewers: list[str] | None = None) -> Path:
        rdir = root / "docs" / "reviews"
        rendered_digest = self._projection_digests()["rendered_request_digest"]
        (rdir / f"{round_id}-request.md").write_text(
            self.REQUEST.replace(self.REQUEST_DIGEST_SENTINEL, rendered_digest))
        narrative = review.stored_narrative_path(root, round_id)
        narrative.parent.mkdir(parents=True, exist_ok=True)
        narrative.write_text(self.NARRATIVE)
        return review.write_round_request_binding(
            root, round_id, target, base, reviewers or ["gpt-5.6-sol"], mode="packet",
            **self._projection_digests())

    def _projection_digests(self) -> dict[str, str]:
        return {
            "narrative_digest": review._canonical_narrative_digest(self.NARRATIVE),
            "rendered_request_digest": review._canonical_rendered_request_digest(self.REQUEST),
        }

    def _reply(self, model: str, base: str, target: str, *, echo: bool = True) -> bytes:
        digest_line = (
            f"request-digest: {self._projection_digests()['rendered_request_digest']}\n"
            if echo else "")
        return (f"model: {model}\neffort: xhigh\n"
                f"review-target: {base[:12]}-{target[:12]}\n"
                f"{digest_line}\nreviewed\n").encode()

    def _legacy_feedback(self, root: Path, round_id: str) -> Path:
        feedback = root / "docs" / "reviews" / f"{round_id}-feedback.md"
        feedback.write_bytes(b"round: legacy\n\n---\n\npre-canonical review receipt\n")
        return feedback

    def _settlement(self, root: Path, round_id: str, *, suffix: str = "") -> Path:
        directory = root / "docs" / "reviews"
        binding_path, binding = review.latest_round_request_binding(
            sorted(directory.glob(f"{round_id}-request.binding*.json")),
            expected_round_id=round_id)
        self.assertIsNotNone(binding)
        settlement_dir = directory / "legacy-settlements"
        settlement_dir.mkdir(parents=True, exist_ok=True)

        def digest(path: Path) -> str:
            return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

        path = settlement_dir / f"{round_id}{suffix}.json"
        path.write_text(_json.dumps({
            "schema": "waystone-legacy-review-settlement-1",
            "disposition": "archived-unverifiable",
            "reason": "pre-canonical-feedback-envelope",
            "round_id": round_id,
            "request_sha256": digest(directory / f"{round_id}-request.md"),
            "binding_sha256": digest(binding_path),
            "feedback_sha256": digest(directory / f"{round_id}-feedback.md"),
            "decision_source": (
                "decision/pre-header-feedback-settlement-method ruling 2026-07-20"),
            "rationale": (
                "The historical receipt is no longer actionable without claiming completion."),
        }, sort_keys=True) + "\n")
        return path

    def test_pending_is_derived_from_request_and_ingest_header_not_file_existence(self):
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-first"
            target = "a" * 40
            binding_path = self._request(root, round_id, target)
            binding = _json.loads(binding_path.read_text())
            binding["at"] = "2026-01-01T23:30:00-08:00"
            binding_path.write_text(_json.dumps(binding) + "\n")
            (root / "docs" / "reviews" / f"{round_id}-feedback.md").write_text(
                "manually copied feedback without an ingest header\n")

            now = datetime(2026, 1, 2, 0, 15, tzinfo=timezone(timedelta(hours=-8)))
            pending = review.pending_reviews(root, now=now)

            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["round_id"], round_id)
            self.assertEqual(pending[0]["age_days"], 1)
            self.assertEqual(pending[0]["target_sha"], target)
            self.assertEqual(pending[0]["reviewers"], ["gpt-5.6-sol"])

    def test_exact_legacy_settlement_archives_without_synthesizing_completion(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-archived"
            binding_path = self._request(root, round_id, "a" * 40)
            feedback = self._legacy_feedback(root, round_id)
            self._settlement(root, round_id)

            self.assertEqual(review.pending_reviews(root), [])
            archived = review.archived_unverifiable_reviews(root)
            self.assertEqual(
                [(row["round_id"], row["disposition"], row["reason"]) for row in archived],
                [(round_id, "archived-unverifiable", "pre-canonical-feedback-envelope")])

            metadata = review.read_feedback_reply_metadata(
                feedback, expected_round_id=round_id,
                binding=review.read_round_request_binding(binding_path))
            self.assertIsNone(metadata["review_target_matches"])
            self.assertEqual(
                metadata["rendered_request_coverage_reason"], "feedback-receipt-corrupt")

    def test_corrupt_unknown_field_and_duplicate_settlements_fail_closed(self):
        cases = (
            "corrupt", "unknown-field", "invalid-digest", "duplicate-field",
            "duplicate-marker", "duplicate-marker-noncanonical",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                root = self._root(d)
                round_id = "2026-01-01-invalid-settlement"
                self._request(root, round_id, "a" * 40)
                self._legacy_feedback(root, round_id)
                marker = self._settlement(root, round_id)
                if case == "corrupt":
                    marker.write_text('{"schema":')
                elif case == "unknown-field":
                    row = _json.loads(marker.read_text())
                    row["unexpected"] = True
                    marker.write_text(_json.dumps(row) + "\n")
                elif case == "invalid-digest":
                    row = _json.loads(marker.read_text())
                    row["feedback_sha256"] = "sha256:not-a-digest"
                    marker.write_text(_json.dumps(row) + "\n")
                elif case == "duplicate-field":
                    marker.write_text(marker.read_text().replace(
                        '"schema":',
                        '"schema": "waystone-legacy-review-settlement-1", "schema":',
                        1))
                else:
                    suffix = ".02" if case == "duplicate-marker-noncanonical" else ".2"
                    duplicate = marker.with_name(f"{round_id}{suffix}.json")
                    duplicate.write_bytes(marker.read_bytes())

                self.assertEqual(
                    [row["round_id"] for row in review.pending_reviews(root)], [round_id])
                self.assertEqual(review.archived_unverifiable_reviews(root), [])

    def test_settlement_with_missing_referenced_file_fails_closed(self):
        for missing in ("request", "binding", "feedback"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as d:
                root = self._root(d)
                round_id = "2026-01-01-missing-reference"
                binding = self._request(root, round_id, "a" * 40)
                feedback = self._legacy_feedback(root, round_id)
                self._settlement(root, round_id)
                paths = {
                    "request": root / "docs/reviews" / f"{round_id}-request.md",
                    "binding": binding,
                    "feedback": feedback,
                }
                paths[missing].unlink()

                pending = review.pending_reviews(root)
                self.assertEqual([row["round_id"] for row in pending], [round_id])
                self.assertEqual(review.archived_unverifiable_reviews(root), [])

    def test_settlement_stales_on_feedback_bytes_or_new_binding(self):
        for mutation in ("feedback-bytes", "new-binding"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as d:
                root = self._root(d)
                round_id = "2026-01-01-stale-settlement"
                self._request(root, round_id, "a" * 40)
                feedback = self._legacy_feedback(root, round_id)
                self._settlement(root, round_id)
                if mutation == "feedback-bytes":
                    feedback.write_bytes(feedback.read_bytes() + b"changed\n")
                else:
                    review.write_round_request_binding(
                        root, round_id, "c" * 40, "b" * 40, ["new-reviewer"],
                        mode="packet", **self._projection_digests())

                pending = review.pending_reviews(root)
                self.assertEqual([row["round_id"] for row in pending], [round_id])
                self.assertEqual(review.archived_unverifiable_reviews(root), [])

    def test_binding_generation_alias_collision_demotes_settlement_to_pending_unknown(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-binding-alias"
            self._request(root, round_id, "a" * 40)
            canonical = review.write_round_request_binding(
                root, round_id, "c" * 40, "b" * 40, ["r2"], mode="packet",
                **self._projection_digests())
            self.assertEqual(
                canonical.name, f"{round_id}-request.binding-2.json")
            self._legacy_feedback(root, round_id)
            marker = self._settlement(root, round_id)
            self.assertEqual(
                _json.loads(marker.read_text())["binding_sha256"],
                "sha256:" + hashlib.sha256(canonical.read_bytes()).hexdigest())
            alias = canonical.with_name(f"{round_id}-request.binding-02.json")
            alias_row = _json.loads(canonical.read_text())
            alias_row["target_sha"] = "d" * 40
            alias.write_text(_json.dumps(alias_row, sort_keys=True) + "\n")

            dispositions = review.packet_review_dispositions(root)
            self.assertEqual([
                (row["round_id"], row["target_sha"], row["reviewers"], row["reason"])
                for row in dispositions["actionable"]
            ], [(round_id, None, [], "binding-generation-collision")])
            self.assertEqual(dispositions["archived_unverifiable"], [])

            pending_out, status_out = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(pending_out):
                self.assertEqual(review.pending(root), 0)
            with contextlib.redirect_stdout(status_out):
                self.assertEqual(review.status(root, None), 0)
            self.assertIn(
                "1 actionable, 0 archived-unverifiable", pending_out.getvalue())
            self.assertIn(
                "1 actionable awaiting feedback, 0 archived-unverifiable",
                status_out.getvalue())
            for rendered in (pending_out.getvalue(), status_out.getvalue()):
                self.assertIn(round_id, rendered)
                self.assertIn("reason binding-generation-collision", rendered)
            self.assertIsNone(review.round_request_binding_identity(alias))

    def test_binding_writer_rejects_renamed_noncanonical_generation(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-binding-writer-alias"
            canonical = self._request(root, round_id, "a" * 40)
            alias = canonical.with_name(f"{round_id}-request.binding-02.json")
            canonical.rename(alias)

            with self.assertRaises(review.WorkflowError) as raised:
                review.write_round_request_binding(
                    root, round_id, "a" * 40, "b" * 40, ["gpt-5.6-sol"],
                    mode="packet", **self._projection_digests())

            self.assertIn(str(alias), str(raised.exception))
            self.assertFalse(canonical.exists())

    def test_generation_lookups_ignore_noncanonical_binding_names(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            directory = root / "docs/reviews"
            round_id = "2026-01-01-digest-alias"
            canonical = self._request(root, round_id, "a" * 40)
            alias = canonical.with_name(f"{round_id}-request.binding-02.json")
            canonical.rename(alias)

            self.assertIsNone(review._request_generation_in_directory(
                directory, round_id,
                self._projection_digests()["rendered_request_digest"]))

            legacy_round_id = "2026-01-01-legacy-alias"
            legacy = write_legacy_round_request_binding(
                root, legacy_round_id, "a" * 40, "b" * 40, ["gpt-5.6-sol"])
            legacy.rename(legacy.with_name(
                f"{legacy_round_id}-request.binding-02.json"))
            self.assertFalse(review._round_has_legacy_request_generation(
                directory, legacy_round_id))

    def test_canonical_reingest_supersedes_stale_settlement(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-reingested"
            base = "b" * 40
            target = "a" * 40
            self._request(root, round_id, target, base=base)
            self._legacy_feedback(root, round_id)
            self._settlement(root, round_id)

            source = root / "reply.md"
            source.write_bytes(self._reply("gpt-5.6-sol", base, target))
            self.assertEqual(review.ingest(root, round_id, src=source, force=True), 0)

            self.assertEqual(review.pending_reviews(root), [])
            self.assertEqual(review.archived_unverifiable_reviews(root), [])

    def test_normal_completion_precedes_an_exact_current_settlement(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-complete-with-marker"
            base = "b" * 40
            target = "a" * 40
            self._request(root, round_id, target, base=base)
            source = root / "reply.md"
            source.write_bytes(self._reply("gpt-5.6-sol", base, target))
            self.assertEqual(review.ingest(root, round_id, src=source), 0)
            self._settlement(root, round_id)

            dispositions = review.packet_review_dispositions(root)
            self.assertEqual(dispositions["actionable"], [])
            self.assertEqual(dispositions["archived_unverifiable"], [])

    def test_tracked_pre_header_cohort_is_actionable_without_settlements(self):
        from unittest import mock

        root = SCRIPTS.parent
        cohort = {
            "2026-07-16-adopt-dogfooding",
            "2026-07-16-fix-wave",
            "2026-07-18-carrier-lanes",
        }
        with mock.patch.object(review, "_legacy_review_settlements", return_value={}):
            actionable = {
                row["round_id"]: row["reason"]
                for row in review.packet_review_dispositions(root)["actionable"]
            }
        self.assertEqual(
            {round_id: actionable[round_id] for round_id in cohort},
            {round_id: "feedback-receipt-corrupt" for round_id in cohort})

    def test_tracked_legacy_settlement_cohort_is_exactly_three_and_archived(self):
        root = SCRIPTS.parent
        cohort = {
            "2026-07-16-adopt-dogfooding",
            "2026-07-16-fix-wave",
            "2026-07-18-carrier-lanes",
        }
        marker_dir = root / "docs" / "reviews" / "legacy-settlements"
        self.assertEqual({path.stem for path in marker_dir.glob("*.json")}, cohort)

        dispositions = review.packet_review_dispositions(root)
        self.assertTrue(cohort.isdisjoint(
            row["round_id"] for row in dispositions["actionable"]))
        self.assertEqual(
            {row["round_id"] for row in dispositions["archived_unverifiable"]}, cohort)

    def test_request_filename_round_is_validated_before_binding_glob(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            rdir = root / "docs/reviews"
            invalid_round = "2026-01-01-[invalid]"
            (rdir / f"{invalid_round}-request.md").write_text("request\n")
            original_glob = Path.glob

            def guarded_glob(path, pattern):
                if pattern != "*-request.md" and "[" in pattern:
                    raise AssertionError("invalid round reached a glob pattern")
                return original_glob(path, pattern)

            with mock.patch.object(Path, "glob", guarded_glob):
                pending = review.pending_reviews(root)

            self.assertEqual(pending, [{
                "round_id": invalid_round,
                "age_days": None,
                "target_sha": None,
                "reviewers": [],
                "reason": "invalid-round-id",
            }])

    def test_latest_binding_controls_pending_and_old_packet_feedback_cannot_silence_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-reissued"
            base = "b" * 40
            old_target = "a" * 40
            new_target = "c" * 40
            self._request(root, round_id, old_target, base=base, reviewers=["old-reviewer"])
            source = root / "old-reply.md"
            source.write_bytes(self._reply("old-reviewer", base, old_target))
            self.assertEqual(review.ingest(root, round_id, src=source), 0)

            review.write_round_request_binding(
                root, round_id, new_target, base, ["new-reviewer"], mode="packet",
                **self._projection_digests())
            pending = review.pending_reviews(root)
            self.assertEqual([(row["target_sha"], row["reviewers"]) for row in pending],
                             [(new_target, ["new-reviewer"])])

            corrected = root / "new-reply.md"
            corrected.write_bytes(self._reply("new-reviewer", base, new_target))
            self.assertEqual(review.ingest(root, round_id, src=corrected, force=True), 0)
            self.assertEqual(review.pending_reviews(root), [])

    def test_corrupt_latest_binding_keeps_old_matching_feedback_pending_unknown(self):
        for corrupt_content in ("", '{"schema":'):
            with self.subTest(corrupt_content=corrupt_content), tempfile.TemporaryDirectory() as d:
                root = self._root(d)
                round_id = "2026-01-01-corrupt-latest"
                base = "b" * 40
                old_target = "a" * 40
                self._request(root, round_id, old_target, base=base, reviewers=["old-reviewer"])
                source = root / "old-reply.md"
                source.write_bytes(self._reply("old-reviewer", base, old_target))
                self.assertEqual(review.ingest(root, round_id, src=source), 0)

                latest = review.write_round_request_binding(
                    root, round_id, "c" * 40, base, ["new-reviewer"], mode="packet",
                    **self._projection_digests())
                latest.write_text(corrupt_content)

                pending = review.pending_reviews(root)

                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["round_id"], round_id)
                self.assertIsNone(pending[0]["target_sha"])
                self.assertEqual(pending[0]["reviewers"], [])

    def test_latest_binding_resolver_parses_only_highest_filename_sequence(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-resolver-order"
            first = self._request(root, round_id, "a" * 40)
            latest = review.write_round_request_binding(
                root, round_id, "c" * 40, "b" * 40, ["r2"], mode="packet",
                **self._projection_digests())
            latest.write_text('{"schema":')

            with mock.patch.object(
                    review, "read_round_request_binding",
                    wraps=review.read_round_request_binding) as read:
                path, row = review.latest_round_request_binding(
                    [first, latest], expected_round_id=round_id)

            self.assertEqual(path, latest)
            self.assertIsNone(row)
            read.assert_called_once_with(latest, expected_round_id=round_id)

    def test_latest_binding_resolver_rejects_ambiguous_candidate_names(self):
        for suffix in ("-1", "-02", "-draft"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as d:
                root = self._root(d)
                round_id = "2026-01-01-ambiguous-binding"
                first = self._request(root, round_id, "a" * 40)
                candidate = first.with_name(
                    f"{round_id}-request.binding{suffix}.json")
                candidate.write_bytes(first.read_bytes())

                path, row = review.latest_round_request_binding(
                    sorted(first.parent.glob(f"{round_id}-request.binding*.json")),
                    expected_round_id=round_id)

                self.assertIsNone(path)
                self.assertIsNone(row)

    def test_latest_binding_resolver_rejects_collision_in_stale_generation(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-stale-binding-collision"
            first = self._request(root, round_id, "a" * 40)
            review.write_round_request_binding(
                root, round_id, "c" * 40, "b" * 40, ["r2"], mode="packet",
                **self._projection_digests())
            first.with_name(f"{round_id}-request.binding-1.json").write_bytes(
                first.read_bytes())

            path, row = review.latest_round_request_binding(
                sorted(first.parent.glob(f"{round_id}-request.binding*.json")),
                expected_round_id=round_id)

            self.assertIsNone(path)
            self.assertIsNone(row)

    def test_binding_sequence_outranks_raw_timestamp_strings_across_offsets(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-offsets"
            base = "b" * 40
            first = self._request(root, round_id, "a" * 40, base=base, reviewers=["r1"])
            binding = _json.loads(first.read_text())
            binding["at"] = "2026-01-01T23:00:00+09:00"  # sorts AFTER the newer stamp as a string
            first.write_text(_json.dumps(binding) + "\n")
            second = review.write_round_request_binding(
                root, round_id, "c" * 40, base, ["r2"], mode="packet",
                **self._projection_digests())
            row2 = _json.loads(second.read_text())
            row2["at"] = "2026-01-01T15:00:00+00:00"  # chronologically one hour newer
            second.write_text(_json.dumps(row2) + "\n")

            pending = review.pending_reviews(root)

            self.assertEqual([(r["target_sha"], r["reviewers"]) for r in pending],
                             [("c" * 40, ["r2"])])

    def test_round_request_binding_publishes_complete_temp_via_exclusive_link(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-atomic"
            original_link = os.link
            observed = []

            def observe_publish(source, destination):
                source_path = Path(source)
                destination_path = Path(destination)
                row = _json.loads(source_path.read_text())
                self.assertEqual(row["round_id"], round_id)
                self.assertEqual(row["target_sha"], "a" * 40)
                self.assertFalse(destination_path.exists())
                observed.append((source_path, destination_path))
                return original_link(source, destination)

            with mock.patch.object(review.os, "link", side_effect=observe_publish):
                published = self._request(root, round_id, "a" * 40)

            self.assertEqual([destination for _source, destination in observed], [published])
            self.assertEqual(review.read_round_request_binding(published)["target_sha"], "a" * 40)
            self.assertFalse(observed[0][0].exists())

    def test_round_request_binding_publish_race_retries_next_sequence_exclusively(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-atomic-race"
            original_link = os.link
            lost_race = False

            def simulate_competing_publisher(source, destination):
                nonlocal lost_race
                if not lost_race:
                    lost_race = True
                    original_link(source, destination)
                    raise FileExistsError(destination)
                return original_link(source, destination)

            with mock.patch.object(review.os, "link", side_effect=simulate_competing_publisher):
                published = self._request(root, round_id, "a" * 40)

            base = published.with_name(f"{round_id}-request.binding.json")
            self.assertEqual(published.name, f"{round_id}-request.binding-2.json")
            self.assertEqual(review.read_round_request_binding(base)["target_sha"], "a" * 40)
            self.assertEqual(review.read_round_request_binding(published)["target_sha"], "a" * 40)

    def test_reply_matching_latest_target_completes_even_from_unconfigured_model(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-unconfigured"
            base = "b" * 40
            target = "a" * 40
            self._request(root, round_id, target, base=base, reviewers=["expected-reviewer"])
            source = root / "reply.md"
            source.write_bytes(self._reply("someone-else", base, target))
            self.assertEqual(review.ingest(root, round_id, src=source), 0)

            self.assertEqual(review.pending_reviews(root), [])

    def test_digestless_legacy_completion_is_labeled_independently_of_reviewer_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-legacy-unconfigured"
            base = "b" * 40
            target = "a" * 40
            binding_path = write_legacy_round_request_binding(
                root, round_id, target, base, ["expected-reviewer"])
            (root / "docs/reviews" / f"{round_id}-request.md").write_text("request\n")
            source = root / "reply.md"
            source.write_bytes(self._reply("someone-else", base, target, echo=False))
            self.assertEqual(review.ingest(root, round_id, src=source), 0)

            binding = review.read_round_request_binding(binding_path)
            metadata = review.read_feedback_reply_metadata(
                root / "docs/reviews" / f"{round_id}-feedback.md",
                expected_round_id=round_id, binding=binding)
            self.assertIs(metadata["review_target_matches"], True)
            self.assertEqual(metadata["reviewer_coverage_reason"], "reviewer-not-configured")
            self.assertIsNone(metadata["narrative_digest_matches"])
            self.assertEqual(metadata["narrative_coverage_reason"], "legacy-pre-digest")
            self.assertIsNone(metadata["rendered_request_digest_matches"])
            self.assertEqual(
                metadata["rendered_request_coverage_reason"],
                "request-digest-missing-legacy-fallback")
            self.assertEqual(review.pending_reviews(root), [])

            review.write_round_request_binding(
                root, round_id, target, base, ["expected-reviewer"], mode="packet",
                **self._projection_digests())
            latest_binding, reason = review.ingest_round_binding(
                root, round_id, common.load_config(root))
            self.assertIsNone(reason)
            after_reprepare = review.read_feedback_reply_metadata(
                root / "docs/reviews" / f"{round_id}-feedback.md",
                expected_round_id=round_id, binding=latest_binding)
            self.assertEqual(
                after_reprepare["rendered_request_coverage_reason"],
                "request-digest-missing-legacy-fallback")

    def test_one_damaged_binding_isolates_to_its_round(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            rdir = root / "docs" / "reviews"
            (rdir / "2026-01-01-damaged-request.md").write_text("request\n")
            (rdir / "2026-01-01-damaged-request.binding.json").write_text("{not json")
            self._request(root, "2026-01-02-healthy", "e" * 40, reviewers=["r"])

            pending = review.pending_reviews(root)

            self.assertEqual([row["round_id"] for row in pending],
                             ["2026-01-01-damaged", "2026-01-02-healthy"])
            self.assertIsNone(pending[0]["target_sha"])
            self.assertEqual(pending[0]["reviewers"], [])

    def test_review_pending_cli_lists_required_fields(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-cli"
            target = "d" * 40
            self._request(root, round_id, target, reviewers=["reviewer-one", "reviewer-two"])
            out = io.StringIO()
            home = Path(d) / "home"
            with contextlib.redirect_stdout(out):
                rc = _run_with_home(
                    home, lambda: review.main(["pending", str(root)]))
            rendered = out.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn(round_id, rendered)
            self.assertRegex(rendered, r"age \d+d")
            self.assertIn(target, rendered)
            self.assertIn("reviewer-one, reviewer-two", rendered)

    def test_review_surfaces_separate_archived_from_actionable_boundaries(self):
        import contextlib
        import io

        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            archived_round = "2026-01-01-archived-surface"
            actionable_round = "2026-01-02-actionable-surface"
            self._request(root, archived_round, "a" * 40)
            self._legacy_feedback(root, archived_round)
            self._settlement(root, archived_round)
            self._request(root, actionable_round, "c" * 40)

            pending_out = io.StringIO()
            with contextlib.redirect_stdout(pending_out):
                self.assertEqual(review.pending(root), 0)
            rendered_pending = pending_out.getvalue()
            self.assertIn("1 actionable, 1 archived-unverifiable", rendered_pending)
            self.assertIn(archived_round, rendered_pending)
            self.assertIn(actionable_round, rendered_pending)

            status_out = io.StringIO()
            with contextlib.redirect_stdout(status_out):
                self.assertEqual(review.status(root, None), 0)
            rendered_status = status_out.getvalue()
            self.assertIn("1 actionable awaiting feedback", rendered_status)
            self.assertIn("1 archived-unverifiable", rendered_status)
            self.assertIn(archived_round, rendered_status)
            self.assertIn(actionable_round, rendered_status)

            old_argv = sys.argv
            session_out = io.StringIO()
            try:
                sys.argv = ["session_context.py", str(root)]
                with contextlib.redirect_stdout(session_out):
                    self.assertEqual(_run_with_home(
                        Path(d) / "home", session_context.main), 0)
            finally:
                sys.argv = old_argv
            session_context_text = _json.loads(session_out.getvalue())[
                "hookSpecificOutput"]["additionalContext"]
            self.assertIn(actionable_round, session_context_text)
            self.assertNotIn(archived_round, session_context_text)

            round_err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(round_err):
                self.assertEqual(round.close(
                    root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"], touched=[],
                    commit="HEAD"), 0)
            self.assertIn(actionable_round, round_err.getvalue())
            self.assertNotIn(archived_round, round_err.getvalue())

    def test_session_start_adds_one_summary_line_and_omits_zero(self):
        import contextlib
        import io

        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            home = Path(d) / "home"

            def capture() -> str:
                old_argv = sys.argv
                output = io.StringIO()
                try:
                    sys.argv = ["session_context.py", str(root)]
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(_run_with_home(home, session_context.main), 0)
                finally:
                    sys.argv = old_argv
                return _json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]

            self.assertNotIn("pending reviews", capture())
            self._request(root, "2026-01-01-session", "e" * 40)
            pending_lines = [line for line in capture().splitlines()
                             if line.startswith("pending reviews")]
            self.assertEqual(len(pending_lines), 1)
            self.assertIn("2026-01-01-session", pending_lines[0])

    def test_check_and_round_close_warn_without_changing_success(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            round_id = "2026-01-01-boundaries"
            self._request(root, round_id, "f" * 40)
            err = io.StringIO()
            home = Path(d) / "home"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                check_rc = _run_with_home(
                    home, lambda: overlay.main(["check", "--root", str(root)]))
            self.assertEqual(check_rc, 0)
            self.assertIn(round_id, err.getvalue())

            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                close_rc = round.close(
                    root, TEST_CLOSE_ROUND_ID, done=["chore/close-me"], touched=[], commit="HEAD")
            self.assertEqual(close_rc, 0)
            self.assertIn(round_id, err.getvalue())
            self.assertIn("pending review", err.getvalue())


class StatuslineTests(unittest.TestCase):
    def _run(self, cwd: Path, home: Path) -> tuple[int, str, str, float]:
        import contextlib
        import io
        import time
        import waystone

        stdout = io.StringIO()
        stderr = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(cwd)
            started = time.monotonic()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = _run_with_home(home, lambda: waystone.main(["statusline"]))
            elapsed = time.monotonic() - started
        finally:
            os.chdir(previous)
        return rc, stdout.getvalue(), stderr.getvalue(), elapsed

    def test_unreadable_reviews_dir_renders_honest_token_not_zero(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            reviews = root / "docs" / "reviews"
            reviews.mkdir(parents=True, exist_ok=True)
            reviews.chmod(0o000)
            try:
                rc, out, err, _elapsed = self._run(root, Path(d) / "home")
            finally:
                reviews.chmod(0o755)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            self.assertIn("reviews unreadable", out)
            self.assertNotIn("reviews 0", out)

    def _project(self, directory: str) -> Path:
        root = Path(directory) / "repo"
        root.mkdir()
        (root / ".waystone.yml").write_text(
            "version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
        (root / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks:\n"
            "  - {id: feat/done-a, title: completed task a, status: done}\n"
            "  - {id: feat/done-b, title: completed task b, status: done}\n"
            "  - id: feat/live\n    title: active task now\n    status: active\n"
            "    round: 2026-07-17-live\n"
            "  - {id: fix/stuck, title: blocked task now, status: blocked}\n")
        reviews = root / "docs" / "reviews"
        reviews.mkdir(parents=True)
        (reviews / "2026-07-16-review-request.md").write_text("request\n")
        return root

    def test_statusline_renders_derived_one_line_fast_with_ansi_and_pending_helper(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            with (root / ".waystone.yml").open("a") as stream:
                stream.write("delegation:\n  codex_runner_verified: true\n")
            nested = root / "nested"
            nested.mkdir()
            with mock.patch.object(review, "pending_reviews", wraps=review.pending_reviews) as pending:
                rc, stdout, stderr, elapsed = self._run(nested, Path(d) / "home")

            self.assertEqual(rc, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(len(stdout.splitlines()), 1)
            self.assertIn("\033[", stdout)
            self.assertIn("2/4", stdout)
            self.assertIn("2026-07-17-live", stdout)
            self.assertRegex(stdout, r"reviews[^0-9]*1")
            self.assertRegex(stdout, r"blockers[^0-9]*1")
            self.assertLess(elapsed, 0.5)
            pending.assert_called_once_with(root.resolve())

    def test_statusline_is_empty_and_read_only_outside_project_with_bad_registry(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "outside"
            nested = root / "nested"
            nested.mkdir(parents=True)
            home = Path(d) / "home"
            machine = home / ".waystone"
            machine.mkdir(parents=True)
            registry = machine / "projects.json"
            registry.write_bytes(b"{not json")
            before_registry = registry.read_bytes()

            rc, stdout, stderr, _elapsed = self._run(nested, home)

            self.assertEqual((rc, stdout, stderr), (0, "", ""))
            self.assertEqual(registry.read_bytes(), before_registry)
            self.assertFalse((root / ".waystone.yml").exists())

    def test_statusline_degrades_corrupt_config_or_task_registry_to_stdout(self):
        for damaged, expected in (("config", "config unreadable"),
                                  ("tasks", "registry unreadable")):
            with self.subTest(damaged=damaged), tempfile.TemporaryDirectory() as d:
                root = Path(d) / "repo"
                root.mkdir()
                (root / ".waystone.yml").write_text(
                    "review: [\n" if damaged == "config" else "version: 1\nproject: demo\n")
                (root / "tasks.yaml").write_text(
                    "tasks: [\n" if damaged == "tasks" else
                    "version: 1\nproject: demo\ntasks: []\n")

                rc, stdout, stderr, _elapsed = self._run(root, Path(d) / "home")

                self.assertEqual(rc, 0)
                self.assertEqual(stderr, "")
                self.assertEqual(len(stdout.splitlines()), 1)
                self.assertIn(expected, stdout)


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
