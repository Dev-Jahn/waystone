"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


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
        launcher = root / "bin" / "waystone"
        launcher.write_bytes((SCRIPTS.parent / "bin" / "waystone").read_bytes())
        launcher.chmod(0o755)
        (root / "scripts").mkdir()
        (root / "scripts" / "waystone.py").write_text("# projected runtime\n")
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
        uv.write_text("""#!/bin/sh
case "${2-}" in
  */scripts/waystone.py) [ -f "$2" ] && [ "${3-}" = status ] || exit 66 ;;
esac
exit 0
""")
        uv.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
        return root, env

    def _git_wrapper(
            self, root: Path, env: dict[str, str], body: str,
    ) -> None:
        wrapper = Path(env["PATH"].split(os.pathsep, 1)[0]) / "git"
        wrapper.write_text(f"#!/bin/sh\nset -eu\n{body}\nexec \"$REAL_GIT\" \"$@\"\n")
        wrapper.chmod(0o755)
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        env["REAL_GIT"] = str(real_git)
        env["RACE_ROOT"] = str(root)

    def _run(
            self, root: Path, env: dict[str, str], *, script: Path | None = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(script or root / "release-to-main.sh")], cwd=root,
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

    def test_release_refuses_when_current_worktree_checks_out_main(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            git(root, "checkout", "-q", "main")
            main_before = git(root, "rev-parse", "main").stdout

            result = self._run(root, env, script=SCRIPTS.parent / "release-to-main.sh")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                f"refs/heads/main is checked out at {root.resolve()}", result.stderr)
            self.assertNotIn("running the test suite", result.stdout)
            self.assertEqual(git(root, "rev-parse", "main").stdout, main_before)

    def test_release_refuses_when_linked_worktree_checks_out_main(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            main_worktree = Path(d) / "main-worktree"
            git(root, "worktree", "add", "-q", str(main_worktree), "main")
            main_before = git(root, "rev-parse", "main").stdout

            result = self._run(root, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                f"refs/heads/main is checked out at {main_worktree.resolve()}", result.stderr)
            self.assertNotIn("running the test suite", result.stdout)
            self.assertEqual(git(root, "rev-parse", "main").stdout, main_before)

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

    def test_noop_uses_cas_and_rejects_concurrent_main_update(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=False)
            main_oid = git(root, "rev-parse", "main").stdout.strip()
            main_tree = git(root, "rev-parse", "main^{tree}").stdout.strip()
            race_oid = git(
                root, "commit-tree", main_tree, "-p", main_oid, "-m", "concurrent main",
            ).stdout.strip()
            env["RACE_OID"] = race_oid
            self._git_wrapper(root, env, """
if [ "${1-}" = "update-ref" ] && [ "${4-}" = "refs/heads/main" ] && \
   [ "${5-}" = "${6-}" ]; then
  "$REAL_GIT" -C "$RACE_ROOT" update-ref refs/heads/main "$RACE_OID"
fi
""")

            result = self._run(root, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("nothing to release", result.stdout)
            self.assertEqual(git(root, "rev-parse", "main").stdout.strip(), race_oid)

    def test_dev_gate_failure_does_not_advance_main(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            uv = Path(env["PATH"].split(os.pathsep, 1)[0]) / "uv"
            uv.write_text("#!/bin/sh\nexit 41\n")
            main_before = git(root, "rev-parse", "main").stdout

            result = self._run(root, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tests failed on dev", result.stderr)
            self.assertEqual(git(root, "rev-parse", "main").stdout, main_before)

    def test_projected_smoke_rejects_missing_or_nonexecutable_front_door(self):
        for case in ("missing", "nonexecutable"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                root, env = self._repo(d, shipped_change=True)
                launcher = root / "bin" / "waystone"
                if case == "missing":
                    launcher.unlink()
                else:
                    launcher.chmod(0o644)
                git(root, "add", "-A")
                git(root, "commit", "-qm", f"{case} launcher")
                main_before = git(root, "rev-parse", "main").stdout

                result = self._run(root, env)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("projected release smoke failed", result.stderr)
                self.assertEqual(git(root, "rev-parse", "main").stdout, main_before)

    def test_projected_smoke_runs_under_an_environment_allowlist(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            smoke_env = Path(d) / "smoke-env"
            uv = Path(env["PATH"].split(os.pathsep, 1)[0]) / "uv"
            # The observed-env report path is baked into the fake: under an env -i
            # allowlist no helper variable can reach the smoke process.
            uv.write_text(f"""#!/bin/sh
case "${{2-}}" in
  */scripts/waystone.py)
    [ -f "$2" ] || exit 66
    [ "${{3-}}" = status ] || exit 67
    : > "{smoke_env}"
    for name in PYTHONPATH PYTHONHOME VIRTUAL_ENV UV_PROJECT UV_WORKING_DIR \\
        UV_WORKING_DIRECTORY UV_PROJECT_ENVIRONMENT UV_ENV_FILE SNEAKED_BY_CALLER; do
      eval 'present=${{'"$name"'+x}}'
      if [ "$present" = x ]; then
        printf '%s\\n' "$name" >> "{smoke_env}"
      fi
    done
    [ "${{PYTHONNOUSERSITE-}}" = 1 ] || printf '%s\\n' PYTHONNOUSERSITE >> "{smoke_env}"
    [ ! -s "{smoke_env}" ] || exit 65
    ;;
esac
exit 0
""")
            uv.chmod(0o755)
            env.update({
                "PYTHONPATH": str(root / "scripts"),
                "PYTHONHOME": str(root / "python-home"),
                "VIRTUAL_ENV": str(root / ".venv"),
                "UV_PROJECT": str(root),
                "UV_WORKING_DIR": str(root),
                "UV_WORKING_DIRECTORY": str(root),
                "UV_PROJECT_ENVIRONMENT": str(root / ".venv"),
                "UV_ENV_FILE": str(root / ".env"),
                "SNEAKED_BY_CALLER": "1",
            })

            result = self._run(root, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(smoke_env.is_file())
            self.assertEqual(smoke_env.read_text(), "")

    def test_projected_smoke_executes_shipped_launcher_under_real_uv(self):
        self.assertIsNotNone(
            shutil.which("uv"), "real uv required by the release smoke contract")
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            # Runnable stand-ins with no inline deps, so real uv resolves offline.
            (root / "scripts" / "waystone.py").write_text(
                "import sys\nsys.exit(0 if sys.argv[1:] == ['status'] else 68)\n")
            (root / "scripts" / "tests").mkdir()
            (root / "scripts" / "tests" / "run_tests.py").write_text("print('gate ok')\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "runnable runtime")
            (Path(env["PATH"].split(os.pathsep, 1)[0]) / "uv").unlink()

            result = subprocess.run(
                ["bash", str(root / "release-to-main.sh")], cwd=root,
                env=env, capture_output=True, text=True, timeout=120)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("running the projected release smoke", result.stdout)
            released = git(root, "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
            self.assertIn("scripts/waystone.py", released)

    def test_unknown_manifest_warning_names_new_script_file(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=False)
            (root / "scripts" / "new-runtime.py").write_text("VALUE = 1\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "new unmanifested runtime")

            result = self._run(root, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "tracked path is outside release manifests: scripts/new-runtime.py",
                result.stderr,
            )

    def test_tmpdir_inside_repository_is_rejected_before_clean_tree_check(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            tmp_base = root / "release-tmp"
            tmp_base.mkdir()
            env["TMPDIR"] = str(tmp_base)
            main_before = git(root, "rev-parse", "main").stdout

            result = self._run(root, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("TMPDIR must be outside the repository", result.stderr)
            self.assertEqual(git(root, "rev-parse", "main").stdout, main_before)

    def test_tmpdir_inside_common_dir_or_sibling_worktree_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            linked = Path(d) / "linked-dev"
            git(root, "worktree", "add", "-q", "--detach", str(linked), "dev")
            main_before = git(root, "rev-parse", "main").stdout
            cases = {
                "primary worktree": (root / "release-tmp", linked),
                "git common dir": (root / ".git" / "release-tmp", linked),
                "sibling worktree": (linked / "release-tmp", root),
            }
            for label, (tmp, run_from) in cases.items():
                with self.subTest(label=label):
                    tmp.mkdir(parents=True, exist_ok=True)
                    env["TMPDIR"] = str(tmp)
                    result = subprocess.run(
                        ["bash", str(root / "release-to-main.sh")], cwd=run_from,
                        env=env, capture_output=True, text=True, timeout=20)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("TMPDIR must be outside the repository", result.stderr)
                    self.assertEqual(git(root, "rev-parse", "main").stdout, main_before)

    def test_manifest_enumeration_failure_aborts_release(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            main_before = git(root, "rev-parse", "main").stdout
            self._git_wrapper(root, env, """
if [ "${1-}" = "-C" ] && [ "${3-}" = "ls-files" ]; then
  exit 71
fi
""")

            result = self._run(root, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("could not enumerate tracked paths", result.stderr)
            self.assertEqual(git(root, "rev-parse", "main").stdout, main_before)

    def test_cleanup_failure_after_ref_update_is_only_a_warning(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            main_before = git(root, "rev-parse", "main").stdout
            self._git_wrapper(root, env, """
if [ "${1-}" = "worktree" ] && [ "${2-}" = "remove" ]; then
  exit 73
fi
""")

            result = self._run(root, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual(git(root, "rev-parse", "main").stdout, main_before)
            self.assertIn("failed to remove temporary worktree", result.stderr)
            self.assertIn("release result remains successful", result.stderr)

    def test_main_checkout_is_rechecked_immediately_before_ref_update(self):
        with tempfile.TemporaryDirectory() as d:
            root, env = self._repo(d, shipped_change=True)
            main_before = git(root, "rev-parse", "main").stdout
            late_worktree = Path(d) / "late-main"
            env["WORKTREE_LIST_COUNT"] = str(Path(d) / "worktree-list-count")
            env["LATE_WORKTREE"] = str(late_worktree)
            self._git_wrapper(root, env, """
if [ "${1-}" = "worktree" ] && [ "${2-}" = "list" ]; then
  count=0
  if [ -f "$WORKTREE_LIST_COUNT" ]; then count=$(cat "$WORKTREE_LIST_COUNT"); fi
  count=$((count + 1))
  printf '%s\n' "$count" > "$WORKTREE_LIST_COUNT"
  if [ "$count" -eq 2 ]; then
    "$REAL_GIT" -C "$RACE_ROOT" worktree add -q "$LATE_WORKTREE" main
  fi
fi
""")

            result = self._run(root, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                f"refs/heads/main is checked out at {late_worktree.resolve()}", result.stderr,
            )
            self.assertEqual(git(root, "rev-parse", "main").stdout, main_before)

    def test_release_script_records_commit_tree_and_manifest_limitations(self):
        script = (SCRIPTS.parent / "release-to-main.sh").read_text()
        self.assertIn("./bin/waystone status", script)
        self.assertIn("commit-tree does not run commit hooks", script)
        self.assertIn("includeIf onbranch:main", script)
        self.assertIn("lazy imports", script)
        self.assertIn("nested data", script)

    def test_release_script_has_isolated_staging_contract(self):
        script = (SCRIPTS.parent / "release-to-main.sh").read_text()
        self.assertIn("SHIP_PATHS=(", script)
        self.assertNotIn("git checkout", script)
        self.assertNotIn("git read-tree -u", script)
        self.assertNotIn('rm -rf -- "$p"', script)
