"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


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
            self.assertIsNone(alpha["reviewer"])
            self.assertIsNone(alpha["reviewer_effort"])
            self.assertIsNone(alpha["review_target"])

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

    def test_reviews_projection_consumes_rederived_reply_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root = base / "repo"
            root.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            rdir = root / "docs/reviews"
            rdir.mkdir(parents=True)
            round_id = "2026-07-18-r1"
            (rdir / f"{round_id}-request.md").write_text("# request\n")
            review.write_round_request_binding(
                root, round_id, "a" * 40, "b" * 40,
                ["codex:gpt-5.6-sol"], mode="packet",
                narrative_digest=TEST_NARRATIVE_DIGEST,
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
            reply = root / "reply.md"
            reply.write_text(
                "model: gpt-5.6-sol\neffort: xhigh\n"
                "review-target: bbbbbbbbbbbb-aaaaaaaaaaaa\n"
                f"request-digest: {TEST_RENDERED_REQUEST_DIGEST}\n"
                "foo: bar\n\nreview body\n")
            self.assertEqual(review.ingest(root, round_id, src=reply), 0)
            (rdir / f"{round_id}-request.md").write_text(
                f"# request\n\n- Reviewing: {'a' * 40}   (diff against {'b' * 40})\n")
            registry = base / "projects.json"
            registry.write_text(_json.dumps({
                "projects": [{"name": "demo", "path": str(root)}]}))
            out = base / "out"
            improve.run_reviews(registry, out)
            row = self._load(out)[0][0]
            self.assertEqual(row["reviewer"], "gpt-5.6-sol")
            self.assertEqual(row["reviewer_effort"], "xhigh")
            self.assertEqual(row["review_target"], "bbbbbbbbbbbb-aaaaaaaaaaaa")
            self.assertIs(row["review_target_matches"], True)
            self.assertIs(row["reviewer_configured"], True)
            self.assertEqual(row["reply_metadata"]["foo"], "bar")

    def test_corrupt_latest_binding_does_not_promote_old_target_in_projection(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root = base / "repo"
            root.mkdir()
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            rdir = root / "docs" / "reviews"
            rdir.mkdir(parents=True)
            round_id = "2026-07-18-corrupt-latest"
            base_sha = "b" * 40
            old_target = "a" * 40
            (rdir / f"{round_id}-request.md").write_text(
                f"# request\n\n- Reviewing: {old_target}   (diff against {base_sha})\n")
            review.write_round_request_binding(
                root, round_id, old_target, base_sha, ["old-reviewer"], mode="packet",
                narrative_digest=TEST_NARRATIVE_DIGEST,
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
            (rdir / f"{round_id}-feedback.md").write_text(
                "model: old-reviewer\neffort: high\n"
                f"review-target: {base_sha[:12]}-{old_target[:12]}\n\nreviewed\n")
            latest = review.write_round_request_binding(
                root, round_id, "c" * 40, base_sha, ["new-reviewer"], mode="packet",
                narrative_digest=TEST_NARRATIVE_DIGEST,
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
            latest.write_text('{"schema":')
            registry = base / "projects.json"
            registry.write_text(_json.dumps({
                "projects": [{"name": "demo", "path": str(root)}]}))
            out = base / "out"

            improve.run_reviews(registry, out)

            row = self._load(out)[0][0]
            self.assertIsNone(row["target_sha"])
            self.assertEqual(row["review_binding_provenance"], "unknown")
            self.assertEqual(row["review_binding_reason"], "corrupt-round-request-sidecar")

    def test_review_projection_rederives_stale_generation_digest_from_body(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root = base / "repo"
            root.mkdir()
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            rdir = root / "docs/reviews"
            rdir.mkdir(parents=True)
            target, base_sha = "a" * 40, "b" * 40
            round_id = "2026-07-18-r1"
            (rdir / f"{round_id}-request.md").write_text(
                f"# request\n\n- Reviewing: {target}   (diff against {base_sha})\n")
            binding_digest = "sha256:" + "2" * 64
            reply_digest = "sha256:" + "1" * 64
            reply_rendered_digest = "sha256:" + "3" * 64
            review.write_round_request_binding(
                root, round_id, target, base_sha, ["expected-reviewer"], mode="packet",
                narrative_digest=reply_digest,
                rendered_request_digest=reply_rendered_digest)
            reply = root / "reply.md"
            reply.write_text(
                "model: expected-reviewer\neffort: high\n"
                f"review-target: {base_sha[:12]}-{target[:12]}\n"
                f"request-digest: {reply_rendered_digest}\n\nreview body\n")
            self.assertEqual(review.ingest(root, round_id, src=reply), 0)
            review.write_round_request_binding(
                root, round_id, target, base_sha, ["expected-reviewer"], mode="packet",
                narrative_digest=binding_digest,
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
            registry = base / "projects.json"
            registry.write_text(_json.dumps({
                "projects": [{"name": "demo", "path": str(root)}]}))
            out = base / "out"

            improve.run_reviews(registry, out)

            row = self._load(out)[0][0]
            self.assertEqual(row["narrative_digest"], binding_digest)
            self.assertEqual(row["rendered_request_digest"],
                             TEST_RENDERED_REQUEST_DIGEST)
            self.assertEqual(row["review_binding_schema"],
                             review.ROUND_REQUEST_BINDING_SCHEMA)
            self.assertEqual(row["review_request_binding_provenance"], "explicit")
            self.assertEqual(row["reply_narrative_digest"], reply_digest)
            self.assertIs(row["narrative_digest_matches"], False)
            self.assertEqual(row["narrative_coverage_reason"],
                             "narrative-digest-mismatch")
            self.assertEqual(row["reply_rendered_request_digest"], reply_rendered_digest)
            self.assertIs(row["rendered_request_digest_matches"], False)
            self.assertEqual(row["rendered_request_coverage_reason"],
                             "request-digest-stale-generation")

    def test_legacy_label_survives_unconfigured_reviewer_in_real_file_projection(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root = base / "repo"
            root.mkdir()
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\nreviews_dir: docs/reviews\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            rdir = root / "docs/reviews"
            rdir.mkdir(parents=True)
            target, base_sha = "a" * 40, "b" * 40
            round_id = "2026-07-18-r1"
            (rdir / f"{round_id}-request.md").write_text(
                f"# request\n\n- Reviewing: {target}   (diff against {base_sha})\n")
            write_legacy_round_request_binding(
                root, round_id, target, base_sha, ["expected-reviewer"])
            reply = root / "reply.md"
            reply.write_text(
                "model: someone-else\neffort: high\n"
                f"review-target: {base_sha[:12]}-{target[:12]}\n\nreviewed\n")
            self.assertEqual(review.ingest(root, round_id, src=reply), 0)
            registry = base / "projects.json"
            registry.write_text(_json.dumps({
                "projects": [{"name": "demo", "path": str(root)}]}))
            out = base / "out"

            improve.run_reviews(registry, out)

            row = self._load(out)[0][0]
            self.assertIs(row["review_target_matches"], True)
            self.assertEqual(row["reviewer_coverage_reason"], "reviewer-not-configured")
            self.assertEqual(row["narrative_coverage_reason"], "legacy-pre-digest")
            self.assertEqual(
                row["rendered_request_coverage_reason"],
                "request-digest-missing-legacy-fallback")
            self.assertEqual(row["review_binding_schema"],
                             review.ROUND_REQUEST_BINDING_V1_SCHEMA)
            self.assertIsNone(row["narrative_digest"])
            self.assertIsNone(row["rendered_request_digest"])
            self.assertEqual(review.pending_reviews(root), [])

    def test_triage_ignores_verbatim_body(self):
        # the verbatim body's `### JW-GPT-*` blocks must not be parsed — only the appended table
        findings = improve._parse_triage(_TRIAGE_FEEDBACK)
        self.assertEqual([f["id"] for f in findings], ["JW-GPT-001", "JW-GPT-002", "JW-GPT-003"])
        # a feedback body with NO appended skeleton yields nothing
        self.assertEqual(improve._parse_triage("just prose, no table\n### JW-GPT-9 — x"), [])

    def test_triage_reads_preserved_archive_and_both_prefixes(self):
        review_dir = SCRIPTS.parent / "docs" / "reviews"
        preserved_names = (
            "2026-07-18-carrier-lanes-feedback.md",
            "2026-07-18-carrier-lanes-fixes-feedback.md",
            "2026-07-18-generation-binding-feedback.md",
            "2026-07-19-evidence-authority-feedback.md",
            "2026-07-19-evidence-authority-fixes-feedback.md",
            "2026-07-19-m0-contracts-feedback.md",
        )
        archived = []
        for name in preserved_names:
            text = (review_dir / name).read_text(encoding="utf-8", errors="replace")
            archived.extend(improve._parse_triage(text))
        self.assertGreater(len(archived), 0)
        self.assertEqual(len(archived), 21)
        self.assertTrue(all(finding["id"].startswith("JW-GPT-") for finding in archived))

        current = improve._parse_triage(
            "## Findings (triage skeleton v2)\n"
            "| finding | severity | type | verdict | evidence | task id |\n"
            "|---|---|---|---|---|---|\n"
            "| WS-GPT-901 — current | major | correctness | REAL | ev | fix/current |\n")
        self.assertEqual([finding["id"] for finding in current], ["WS-GPT-901"])
        prefixes = {finding["id"].split("-GPT-", 1)[0] for finding in archived + current}
        self.assertEqual(prefixes, {"JW", "WS"})

    def test_triage_type_column_accepts_taxonomy_and_preserves_unknown(self):
        feedback = """## Findings (triage skeleton v2)

| finding | severity | type | verdict | evidence | task id |
|---|---|---|---|---|---|
| WS-GPT-101 — typed | major | architecture | REAL | ev | fix/typed |
| WS-GPT-102 — blank | minor | | REJECTED | ev | |
| WS-GPT-103 — noncanonical | blocker | performance | NEEDS-RULING | ev | |
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
                 {"id": "WS-GPT-001", "severity": "blocker", "status": "REAL",
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
                "| WS-GPT-001 — the one bug | blocker | REAL | confirmed | fix/the-bug |\n")
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
            "| WS-GPT-001 — linked | `blocker` | REAL | ev | `fix/open` |\n"
            "| WS-GPT-002 — unknown link | `major` | NEEDS-RULING | ev | |\n")
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
                "| WS-GPT-002 — unknown link | `major` | NEEDS-RULING | ev | |",
                "| WS-GPT-002 — external task | `major` | NEEDS-RULING | ev | `feat/deleg-only` |"))
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
                "| WS-GPT-001 — x | major | correctness | REAL | `scripts/improve.py:10` | feat/x |\n")
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
                root, rid, "c" * 40, "d" * 40, ["codex"], mode="packet",
                narrative_digest=TEST_NARRATIVE_DIGEST,
                rendered_request_digest=TEST_RENDERED_REQUEST_DIGEST)
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
            "| WS-GPT-001 — x | major | scope | REAL | "
            "`../scripts/improve.py:731` | fix/x |\n")
        self.assertEqual(parsed[0]["evidence_pointers"], ["../scripts/improve.py:731"])
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
            for rid, fid in (("r1", "JW-GPT-001"), ("r2", "WS-GPT-002")):
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
