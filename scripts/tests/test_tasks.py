"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


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

    def test_unknown_option_never_creates_state_at_a_bogus_root(self):
        # The `task drop --reason <text>` incident: an unknown option used to become a boolean
        # and its value fell through to the positionals, where it was treated as a project root.
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            bogus = Path(d) / "no such project: 사유 문장"
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = tasks.main(["drop", "fix/x", "--bogus", str(bogus)])
            self.assertEqual(rc, 1)
            self.assertIn("unknown option --bogus", err.getvalue())
            self.assertFalse(bogus.exists())

    def test_uninitialized_explicit_root_is_refused_without_state_creation(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            plain = Path(d) / "plain-dir"
            plain.mkdir()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = tasks.main(["set", "feat/alpha", "status", "done", str(plain)])
            self.assertEqual(rc, 1)
            self.assertIn("not an initialized waystone project", err.getvalue())
            self.assertFalse((plain / ".waystone").exists())

    def test_mutations_refuse_linked_worktree_before_state_check_but_allow_canonical_checkout(self):
        import contextlib
        import io
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root = base / "repo"
            linked = base / "linked"
            home = base / "home"
            root.mkdir()
            home.mkdir()
            init_repo(root)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            self.assertEqual(git(root, "add", ".waystone.yml", "tasks.yaml").returncode, 0)
            self.assertEqual(git(root, "commit", "-qm", "add waystone project").returncode, 0)
            added = git(root, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
            self.assertEqual(added.returncode, 0, added.stderr)
            before = (linked / "tasks.yaml").read_text()

            def invoke(argv, cwd):
                previous = Path.cwd()
                out, err = io.StringIO(), io.StringIO()
                try:
                    os.chdir(cwd)
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        rc = _run_with_home(home, lambda: tasks.main(argv))
                finally:
                    os.chdir(previous)
                return rc, out.getvalue(), err.getvalue()

            attempts = [
                ("cwd-set", ["set", "feat/alpha", "status", "done"], linked, {}),
                ("explicit-add", ["add", "fix/linked", str(linked), "--title", "must refuse"],
                 root, {}),
                ("explicit-set", ["set", "feat/alpha", "status", "done", str(linked)], root, {}),
                ("explicit-drop", ["drop", "gate/beta", str(linked)], root, {}),
                ("explicit-archive", ["archive", str(linked), "--threshold", "0", "--keep", "0"],
                 root, {}),
                ("ambient-git-env", ["set", "feat/alpha", "status", "done"], linked, {
                    "GIT_DIR": str(root / ".git"),
                    "GIT_WORK_TREE": str(root),
                    "GIT_COMMON_DIR": str(root / ".git"),
                }),
            ]
            with mock.patch.object(tasks, "migrate_project_state") as state_check:
                for label, argv, cwd, git_env in attempts:
                    with self.subTest(label=label), mock.patch.dict(os.environ, git_env):
                        rc, _out, err = invoke(argv, cwd)
                        self.assertEqual(rc, 1)
                        self.assertIn("noncanonical_intent_mutation", err)
                        self.assertIn("canonical checkout", err)
                        self.assertEqual((linked / "tasks.yaml").read_text(), before)
                        self.assertEqual((root / "tasks.yaml").read_text(), before)
                        self.assertFalse((linked / ".waystone").exists())
            state_check.assert_not_called()

            rc, _out, err = invoke(["set", "feat/alpha", "status", "done"], root)
            self.assertEqual(rc, 0, err)
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            alpha = next(t for t in data["tasks"] if t["id"] == "feat/alpha")
            self.assertEqual(alpha["status"], "done")

            rc, _out, err = invoke(
                ["set", "feat/alpha", "status", "active", str(root)], linked)
            self.assertEqual(rc, 0, err)
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            alpha = next(t for t in data["tasks"] if t["id"] == "feat/alpha")
            self.assertEqual(alpha["status"], "active")

    def test_linked_worktree_reads_use_canonical_checkout_without_linked_state(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root = base / "repo"
            linked = base / "linked"
            home = base / "home"
            root.mkdir()
            home.mkdir()
            init_repo(root)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            self.assertEqual(git(root, "add", ".waystone.yml", "tasks.yaml").returncode, 0)
            self.assertEqual(git(root, "commit", "-qm", "add waystone project").returncode, 0)
            added = git(root, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
            self.assertEqual(added.returncode, 0, added.stderr)
            linked_tasks = (linked / "tasks.yaml").read_text()

            canonical = yaml.safe_load(TASKS_FIXTURE)
            canonical["tasks"][0]["title"] = "canonical checkout only"
            (root / "tasks.yaml").write_text(
                yaml.safe_dump(canonical, sort_keys=False, allow_unicode=True))

            def invoke(argv, cwd):
                previous = Path.cwd()
                out, err = io.StringIO(), io.StringIO()
                try:
                    os.chdir(cwd)
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        rc = _run_with_home(home, lambda: tasks.main(argv))
                finally:
                    os.chdir(previous)
                return rc, out.getvalue(), err.getvalue()

            for cwd in (root, linked):
                for argv in (["list"], ["show", "feat/alpha"]):
                    with self.subTest(cwd=cwd.name, argv=argv):
                        rc, out, err = invoke(argv, cwd)
                        self.assertEqual(rc, 0, err)
                        self.assertIn("canonical checkout only", out)
            self.assertFalse((linked / ".waystone").exists())
            self.assertEqual((linked / "tasks.yaml").read_text(), linked_tasks)

    def test_linked_nested_project_read_maps_to_same_canonical_project(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo = base / "repo"
            linked_worktree = base / "linked"
            project = repo / "nested-project"
            home = base / "home"
            repo.mkdir()
            home.mkdir()
            init_repo(repo)
            project.mkdir()
            (project / ".waystone.yml").write_text("version: 1\nproject: nested\n")
            (project / "tasks.yaml").write_text(TASKS_FIXTURE)
            self.assertEqual(git(repo, "add", "nested-project").returncode, 0)
            self.assertEqual(git(repo, "commit", "-qm", "add nested project").returncode, 0)
            added = git(repo, "worktree", "add", "-q", "--detach", str(linked_worktree), "HEAD")
            self.assertEqual(added.returncode, 0, added.stderr)
            linked_project = linked_worktree / "nested-project"

            canonical = yaml.safe_load(TASKS_FIXTURE)
            canonical["tasks"][0]["title"] = "nested canonical checkout only"
            (project / "tasks.yaml").write_text(
                yaml.safe_dump(canonical, sort_keys=False, allow_unicode=True))
            previous = Path.cwd()
            out, err = io.StringIO(), io.StringIO()
            try:
                os.chdir(linked_project)
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: tasks.main(["list"]))
            finally:
                os.chdir(previous)

            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("nested canonical checkout only", out.getvalue())
            self.assertFalse((linked_project / ".waystone").exists())

    def test_linked_read_refuses_uninitialized_explicit_selector_before_canonical_redirect(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo = base / "repo"
            linked_worktree = base / "linked"
            canonical_project = repo / "nested-project"
            linked_project = linked_worktree / "nested-project"
            home = base / "home"
            repo.mkdir()
            home.mkdir()
            init_repo(repo)
            canonical_project.mkdir()
            (canonical_project / ".waystone.yml").write_text(
                "version: 1\nproject: canonical-decoy\n")
            canonical = yaml.safe_load(TASKS_FIXTURE)
            canonical_task = "canonical decoy only"
            canonical["tasks"][0]["title"] = canonical_task
            (canonical_project / "tasks.yaml").write_text(
                yaml.safe_dump(canonical, sort_keys=False, allow_unicode=True))
            self.assertEqual(git(repo, "add", "nested-project").returncode, 0)
            self.assertEqual(git(repo, "commit", "-qm", "add canonical project").returncode, 0)

            self.assertEqual(
                git(repo, "checkout", "-q", "--orphan", "selector").returncode, 0)
            self.assertEqual(git(repo, "rm", "-qrf", ".").returncode, 0)
            (repo / "selector.txt").write_text("selector branch has no project config\n")
            self.assertEqual(git(repo, "add", "selector.txt").returncode, 0)
            self.assertEqual(git(repo, "commit", "-qm", "add selector branch").returncode, 0)
            self.assertEqual(git(repo, "checkout", "-q", "main").returncode, 0)
            added = git(repo, "worktree", "add", "-q", str(linked_worktree), "selector")
            self.assertEqual(added.returncode, 0, added.stderr)
            linked_project.mkdir()

            canonical_state = canonical_project / ".waystone"
            linked_state = linked_project / ".waystone"
            self.assertFalse(canonical_state.exists())
            self.assertFalse(linked_state.exists())

            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = _run_with_home(
                    home, lambda: tasks.main(["list", str(linked_project)]))

            observed = (
                rc,
                canonical_task in out.getvalue(),
                "not an initialized waystone project" in err.getvalue(),
                linked_state.exists(),
                (canonical_state / "lock").exists(),
            )
            self.assertEqual(observed, (1, False, True, False, False))
            self.assertEqual(out.getvalue(), "")
            self.assertFalse(canonical_state.exists())
            self.assertFalse(linked_state.exists())

    def test_linked_read_refuses_unprovable_canonical_root_before_state_creation(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root = base / "repo"
            linked = base / "linked"
            home = base / "home"
            admin = root / "nested" / "admin"
            root.mkdir()
            home.mkdir()
            (root / "nested").mkdir()
            initialized = subprocess.run(
                ["git", "init", "-q", "-b", "main", f"--separate-git-dir={admin}", str(root)],
                capture_output=True, text=True,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(git(root, "config", "user.email", "t@t").returncode, 0)
            self.assertEqual(git(root, "config", "user.name", "t").returncode, 0)
            (root / "f.txt").write_text("0")
            (root / ".waystone.yml").write_text("version: 1\nproject: canonical\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            self.assertEqual(
                git(root, "add", "f.txt", ".waystone.yml", "tasks.yaml").returncode, 0)
            self.assertEqual(git(root, "commit", "-qm", "add waystone project").returncode, 0)
            added = git(root, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
            self.assertEqual(added.returncode, 0, added.stderr)

            # common_dir.parent is `nested`, but Git reports `root` as its worktree top-level.
            # A decoy project there must not make that administrative parent look canonical.
            (root / "nested" / ".waystone.yml").write_text("version: 1\nproject: decoy\n")
            (root / "nested" / "tasks.yaml").write_text(TASKS_FIXTURE)
            previous = Path.cwd()
            out, err = io.StringIO(), io.StringIO()
            try:
                os.chdir(linked)
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: tasks.main(["list"]))
            finally:
                os.chdir(previous)

            self.assertFalse((linked / ".waystone").exists())
            self.assertEqual(rc, 1)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("project_context_unavailable", err.getvalue())

    def test_dash_dash_values_use_equals_form_and_bare_form_refuses_loudly(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            self.assertEqual(tasks.main(
                ["add", "fix/dashy", str(root), "--title=-- follow-up work"]), 0)
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            self.assertEqual(
                next(t for t in data["tasks"] if t["id"] == "fix/dashy")["title"],
                "-- follow-up work")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = tasks.main(["add", "fix/dashy2", str(root), "--title", "--not-a-flag"])
            self.assertEqual(rc, 1)
            self.assertIn("--title=<value>", err.getvalue())
            self.assertNotIn("fix/dashy2", (root / "tasks.yaml").read_text())

    def test_value_option_at_end_of_argv_is_a_loud_error(self):
        # `task drop <id> --reason` (no value) must not silently drop without a reason.
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = tasks.main(["drop", "gate/beta", str(root), "--reason"])
            self.assertEqual(rc, 1)
            self.assertIn("--reason requires a value", err.getvalue())
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            beta = next(t for t in data["tasks"] if t["id"] == "gate/beta")
            self.assertNotEqual(beta["status"], "dropped")

    def test_misplaced_known_option_is_rejected_per_subcommand(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            cases = [
                (["drop", "gate/beta", str(root), "--title", "x"],
                 "--title is not valid for 'drop'"),
                (["list", str(root), "--reason", "x"],
                 "--reason is not valid for 'list'"),
            ]
            for args, expected in cases:
                with self.subTest(sub=args[0]):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err), \
                            contextlib.redirect_stdout(io.StringIO()):
                        rc = tasks.main(args)
                    self.assertEqual(rc, 1)
                    self.assertIn(expected, err.getvalue())
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            beta = next(t for t in data["tasks"] if t["id"] == "gate/beta")
            self.assertNotEqual(beta["status"], "dropped")

    def test_drop_reason_is_recorded_in_notes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            rc = tasks.main(["drop", "gate/beta", str(root), "--reason", "superseded by feat/x"])
            self.assertEqual(rc, 0)
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            beta = next(t for t in data["tasks"] if t["id"] == "gate/beta")
            self.assertEqual(beta["status"], "dropped")
            self.assertIn("dropped: superseded by feat/x", beta["notes"])
            self.assertEqual(validate.validate(data), [])

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


class UninitializedRootGateTests(unittest.TestCase):
    """The single chokepoint (hold_project_lock / ensure_project_state_dir) refuses to create
    project state under roots without a project config; module grammars stay untouched."""

    def test_state_primitives_refuse_uninitialized_roots_without_writes(self):
        with tempfile.TemporaryDirectory() as d:
            plain = Path(d) / "not-a-project"
            plain.mkdir()
            with self.assertRaisesRegex(common.WorkflowError, "not an initialized"):
                with common.hold_project_lock(plain):
                    pass
            with self.assertRaisesRegex(common.WorkflowError, "not an initialized"):
                common.ensure_project_state_dir(plain)
            self.assertFalse((plain / ".waystone").exists())

    def test_initialized_root_passes_gate_and_init_order_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            with common.hold_project_lock(root):
                pass
            self.assertTrue((root / ".waystone" / "lock").is_file())
            # init order: the skill writes .waystone.yml (Step 3) before the first
            # state-creating CLI call, so consent recording passes the gate.
            common.record_consent(root, "init.start-level", "warn-allowed", {})
            self.assertTrue((root / ".waystone" / "consents.jsonl").is_file())

    def test_state_self_ignore_is_restored_atomically_before_marker_write(self):
        from unittest import mock

        for initial in (None, "", "delegations/\n"):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
                init_repo(root)
                state = root / ".waystone"
                state.mkdir()
                ignore = state / ".gitignore"
                if initial is not None:
                    ignore.write_text(initial)

                with mock.patch.object(
                        common, "write_text_atomic", wraps=common.write_text_atomic) as atomic:
                    self.assertEqual(common.ensure_project_state_dir(root), state)
                    atomic.assert_called_once_with(ignore, "*\n")
                delegate._record_codex_runner_verified(
                    state / "codex-runner-verified",
                    {"schema": "waystone-codex-runner-proof-2"})

                self.assertEqual(ignore.read_text(), "*\n")
                self.assertEqual(git(
                    root, "check-ignore", "--quiet",
                    ".waystone/codex-runner-verified").returncode, 0)

    def test_delegate_entry_point_is_gated_via_the_same_chokepoint(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            plain = Path(d) / "stale-or-typo-path"
            plain.mkdir()
            home = Path(d) / "home"
            home.mkdir()
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(
                    home, lambda: delegate.main(["status", "--root", str(plain)]))
            self.assertNotEqual(rc, 0)
            self.assertIn("not an initialized waystone project", err.getvalue())
            self.assertFalse((plain / ".waystone").exists())

    def test_resume_refuses_uninitialized_explicit_root_without_state_creation(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            plain = Path(d) / "not-a-project"
            plain.mkdir()
            for args in (["resume.py", str(plain)],
                         ["resume.py", "--start-here-path", str(plain)]):
                with self.subTest(args=args[1:]):
                    old_argv = sys.argv
                    sys.argv = args
                    err = io.StringIO()
                    try:
                        with contextlib.redirect_stderr(err), \
                                contextlib.redirect_stdout(io.StringIO()):
                            rc = resume.main()
                    finally:
                        sys.argv = old_argv
                    self.assertEqual(rc, 1)
                    self.assertIn("not an initialized waystone project", err.getvalue())
                    self.assertFalse((plain / ".waystone").exists())

    def test_stale_registered_project_can_still_be_unregistered(self):
        import shutil
        import waystone

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
            home = Path(d) / "home"
            home.mkdir()
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(_run_with_home(
                    home, lambda: waystone.main(["project", "register", str(root)])), 0)
            shutil.rmtree(root)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = _run_with_home(
                    home, lambda: waystone.main(["project", "unregister", str(root)]))
            self.assertEqual(rc, 0)
            self.assertIn("unregistered:", out.getvalue())


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

    def test_claim_rejects_dependency_drift_after_prepare(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: x\ntasks:\n"
                "  - id: feat/dep\n    title: dep\n    status: done\n"
                "  - id: feat/child\n    title: child\n    status: pending\n"
                "    deps: [feat/dep]\n"
                "    accept: [does the thing]\n")
            data = yaml.safe_load((root / "tasks.yaml").read_text())
            packet, _ = delegate._build_packet(data, "feat/child", [], root)
            plan = {"task_id": "feat/child", "accept_flags": [], "retry_note": None,
                    "routing_note": None, "packet": packet, "runner_override": None}
            (root / "tasks.yaml").write_text(
                (root / "tasks.yaml").read_text().replace("status: done", "status: pending"))
            with self.assertRaises(delegate.WorkflowError) as ctx:
                delegate._claim_run(root, plan)
            self.assertIn("changed while preparing delegation", str(ctx.exception))
            self.assertIn("unmet dependencies", str(ctx.exception))
