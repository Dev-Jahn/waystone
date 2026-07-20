"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


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
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
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
            (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
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
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
            entered = threading.Event()
            released = threading.Event()
            migrated = threading.Event()
            calls = []
            failures = []
            originals = (dashboard.hold_project_lock, dashboard.migrate_project_state,
                         dashboard.git_branch_info, dashboard.load_tasks)

            @contextlib.contextmanager
            def observed_lock(project, timeout=None):
                self.assertEqual(Path(project), root)
                entered.set()
                with originals[0](project, timeout=timeout):
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

            dashboard.hold_project_lock = observed_lock
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
                dashboard.hold_project_lock = originals[0]
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

    def test_dispatcher_refuses_pre_0_9_state_for_explicit_project_root(self):
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
            source.write_bytes(b"CLI-EXPLICIT-FRONTIER")
            rc, out, err = self._capture(
                home, outside, ["paths", "--root", str(root)])
            self.assertEqual((rc, out), (1, ""))
            self.assertIn("unsupported_pre_0_9_layout", err)
            self.assertIn("0.11.x", err)
            self.assertNotIn("Traceback", err)
            self.assertEqual(source.read_bytes(), b"CLI-EXPLICIT-FRONTIER")
            self.assertFalse((root / ".waystone" / "start-here.md").exists())

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
            registry_calls = [(src, dst) for src, dst in calls if dst == registry]
            self.assertEqual(len(registry_calls), 2)
            self.assertTrue(all(src.parent == registry.parent for src, _dst in registry_calls))
            self.assertEqual(len({src for src, _dst in registry_calls}), 2)
            self.assertTrue(all(src.name.startswith(".projects.json.") and src.suffix == ".tmp"
                                for src, _dst in registry_calls))
            self.assertTrue(all(not src.exists() for src, _dst in registry_calls))

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

            def fail_registry_replace(src, dst):
                if Path(dst) == registry:
                    raise OSError("replace failed")
                return original_replace(src, dst)

            waystone.os.replace = fail_registry_replace
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
        self.assertNotIn("codex_runner_verified", cfg["delegation"])
        self.assertNotIn("sandbox", cfg["delegation"])  # R7: no sandbox config knob in M1

    def test_delegation_env_prep_list_ok(self):
        cfg = self._cfg("version: 1\nproject: x\ndelegation:\n  env_prep:\n    - uv sync --frozen\n")
        self.assertEqual(cfg["delegation"]["env_prep"], ["uv sync --frozen"])

    def test_delegation_env_prep_must_be_str_list(self):
        with self.assertRaises(ValueError):
            self._cfg("version: 1\nproject: x\ndelegation:\n  env_prep: notalist\n")
        with self.assertRaises(ValueError):
            self._cfg("version: 1\nproject: x\ndelegation:\n  env_prep:\n    - 42\n")

    def test_legacy_codex_runner_verified_warns_only_for_real_config_source(self):
        import contextlib
        import io

        for value in ("true", "false", "'yes'", "1"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                config = root / ".waystone.yml"
                config.write_text(
                    "version: 1\nproject: x\ndelegation:\n"
                    f"  codex_runner_verified: {value}\n")
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    cfg = common.load_config(root)
                self.assertNotIn("codex_runner_verified", cfg["delegation"])
                self.assertIn(
                    f"remove the key from {config}", stderr.getvalue())
                self.assertIn(
                    str(root / ".waystone" / "codex-runner-verified"), stderr.getvalue())

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            projected = common.normalize_config({
                "version": 1,
                "project": "historical-ref",
                "delegation": {"codex_runner_verified": True},
            })
        self.assertNotIn("codex_runner_verified", projected["delegation"])
        self.assertEqual(stderr.getvalue(), "")

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

    def _four_lane_registry(self, root: Path) -> None:
        (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
        (root / "tasks.yaml").write_text(
            "version: 1\nproject: x\ntasks:\n"
            "  - id: feat/current-lane\n    title: current round lane task\n    status: active\n"
            "    round: 2026-07-17-current\n"
            "    lane: {branch: missing/current, base_sha: deadbeef}\n"
            "  - id: feat/unstamped-lane\n    title: unstamped current lane task\n    status: pending\n"
            "    lane: {branch: missing/unstamped, base_sha: deadbeef}\n"
            "  - id: feat/prior-round-lane\n    title: reopened lane keeping a prior round stamp\n"
            "    status: active\n"
            "    round: 2026-07-16-other\n"
            "    lane: {branch: missing/prior, base_sha: deadbeef}\n"
            "  - id: feat/parked-lane\n    title: parked historical lane task\n    status: parked\n"
            "    round: 2026-07-17-current\n"
            "    lane: {branch: missing/parked, base_sha: deadbeef}\n")

    def test_cli_verifies_every_nonterminal_lane_regardless_of_round_stamp(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            self._four_lane_registry(root)
            home = root / "home"
            env = os.environ.copy()
            env.update({"HOME": str(home), "WAYSTONE_HOME": str(home / ".waystone")})
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "waystone.py"), "lanes", "verify", str(root)],
                env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("feat/current-lane", result.stderr)
            self.assertIn("feat/unstamped-lane", result.stderr)
            self.assertIn("feat/prior-round-lane", result.stderr)
            self.assertNotIn("feat/parked-lane", result.stderr)

    def test_round_flag_is_rejected_loudly(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            self._four_lane_registry(root)
            home = root / "home"
            env = os.environ.copy()
            env.update({"HOME": str(home), "WAYSTONE_HOME": str(home / ".waystone")})
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "waystone.py"), "lanes", "verify", str(root),
                 "--round", "2026-07-17-current"],
                env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("round scoping was removed", result.stderr)

    def test_round_skill_runs_unscoped_lane_verification(self):
        text = (SCRIPTS.parent / "skills" / "round" / "SKILL.md").read_text()
        self.assertIn("`waystone lanes verify .`", text)
        self.assertNotIn("lanes verify . --round", text)


class RoundCloseTests(unittest.TestCase):
    ROUND_ID = f"{TEST_CURRENT_ROUND_DATE}-z"
    ROLE_ROUND_ID = f"{TEST_CURRENT_ROUND_DATE}-l2-a"

    def test_close_integration(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            init_repo(root)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            (root / "tasks.yaml").write_text(TASKS_FIXTURE)
            git(root, "add", "-A"); git(root, "commit", "-qm", "setup")
            rc = round.close(root, self.ROUND_ID, done=["feat/alpha"],
                             touched=["gate/beta"], commit="HEAD")
            self.assertEqual(rc, 0)
            txt = (root / "tasks.yaml").read_text()
            # feat/a flipped to done and stamped
            a = txt.split("gate/beta")[0]
            self.assertIn("status: done", a)
            self.assertIn(f"round: {self.ROUND_ID}", a)
            # gate/b stamped with round but NOT flipped to done
            b = "gate/beta" + txt.split("gate/beta")[1]
            self.assertIn(f"round: {self.ROUND_ID}", b)
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
                    root, self.ROLE_ROUND_ID, done=["feat/alpha"], touched=[], commit="HEAD")
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
                root, self.ROLE_ROUND_ID, done=["feat/alpha"], touched=[], commit="HEAD"), 1)
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
                    root, f"{TEST_CURRENT_ROUND_DATE}-atomic",
                    done=["feat/alpha"], touched=[], commit="HEAD")
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

    def test_close_rejects_backdated_and_nonexistent_round_dates_before_write(self):
        import contextlib
        import io

        for round_id, message in (
                ("2000-01-01-backdated", "must be today"),
                ("2026-99-99-impossible", "real calendar date")):
            with self.subTest(round_id=round_id), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self._setup(
                    root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
                before = (root / "tasks.yaml").read_bytes()
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = round.close(
                        root, round_id, done=["feat/alpha"], touched=[], commit="HEAD")
                self.assertEqual(rc, 1)
                self.assertIn(message, err.getvalue())
                self.assertEqual((root / "tasks.yaml").read_bytes(), before)

    def test_close_allows_next_day_reclose_for_existing_exposure(self):
        original_clock = round._current_date
        try:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self._setup(
                    root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
                round_id = "2026-07-19-existing-exposure"
                round._current_date = lambda: date(2026, 7, 19)
                self.assertEqual(round.close(
                    root, round_id, done=["feat/alpha"], touched=[], commit="HEAD"), 0)
                round._current_date = lambda: date(2026, 7, 20)
                self.assertEqual(round.close(
                    root, round_id, done=["feat/alpha"], touched=[], commit="HEAD"), 0)
        finally:
            round._current_date = original_clock

    def test_close_rejects_backdated_round_with_only_progress_heading(self):
        import contextlib
        import io

        original_clock = round._current_date
        try:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self._setup(
                    root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
                round_id = "2026-07-19-existing-progress"
                (root / "PROGRESS.md").write_text(f"# PROGRESS\n\n## {round_id}\n\n- prior\n")
                round._current_date = lambda: date(2026, 7, 20)
                before = (root / "tasks.yaml").read_bytes()
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = round.close(
                        root, round_id, done=["feat/alpha"], touched=[], commit="HEAD")
                self.assertEqual(rc, 1)
                self.assertIn("must be today", err.getvalue())
                self.assertEqual((root / "tasks.yaml").read_bytes(), before)
        finally:
            round._current_date = original_clock

    def test_missing_watermark_fails_closed_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\n")  # no state.last_round_commit
            before = (root / "tasks.yaml").read_text()
            rc = round.close(root, self.ROUND_ID, done=["feat/alpha"], touched=[], commit="HEAD")
            self.assertEqual(rc, 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)  # nothing written
            self.assertFalse((root / "ROADMAP.md").exists())

    def test_unresolvable_commit_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            before = (root / "tasks.yaml").read_text()
            rc = round.close(root, self.ROUND_ID, done=["feat/alpha"],
                             touched=[], commit="nope-not-a-ref")
            self.assertEqual(rc, 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)

    def test_done_task_with_unmet_dep_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            before = (root / "tasks.yaml").read_text()
            # gate/beta depends on feat/alpha (active) — closing gate/beta as done must fail
            rc = round.close(root, self.ROUND_ID, done=["gate/beta"], touched=[], commit="HEAD")
            self.assertEqual(rc, 1)
            self.assertEqual((root / "tasks.yaml").read_text(), before)

    def test_close_dependency_and_dependent_together(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._setup(root, "version: 1\nproject: x\nstate:\n  last_round_commit: null\n")
            # closing a dependency (feat/alpha) and its dependent (gate/beta) in ONE round is valid:
            # the dep is done in the final state
            rc = round.close(root, self.ROUND_ID, done=["feat/alpha", "gate/beta"],
                             touched=[], commit="HEAD")
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
                rc = round.close(root, self.ROUND_ID, done=["feat/alpha"],
                                 touched=["gate/beta"], commit="HEAD")
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
                rc = round.close(root, self.ROUND_ID, done=["feat/alpha"], touched=[], commit="HEAD")
            finally:
                ssot.regenerate = orig
            self.assertEqual(rc, 1)
            # generated dir fully rolled back: split/.hash/DIGEST all consistent at v1
            self.assertEqual((gen / ".hash").read_text(), v1_hash)
            self.assertEqual((gen / "DIGEST.md").read_text(), v1_digest)
            self.assertEqual((root / "tasks.yaml").read_text(), TASKS_FIXTURE)  # primary restored too
