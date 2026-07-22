#!/usr/bin/env python3
"""Focused project-surface contracts; legacy dashboard/lanes/round assertions are retired."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import json
from waystone.cli import main as cli_main


class ProjectSurfaceTests(unittest.TestCase):
    def test_registry_registration_writes_opaque_project_id(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            init_repo(root)
            (root / ".waystone.yml").write_text("version: 1\nproject: demo\n", encoding="utf-8")
            (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n", encoding="utf-8")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "config")
            home = base / "home"
            home.mkdir()
            old = os.environ.get("WAYSTONE_HOME")
            try:
                os.environ["WAYSTONE_HOME"] = str(home / ".waystone")
                self.assertEqual(cli_main._project_main(["register", str(root)]), 0)
                rows = json.loads((home / ".waystone/projects.json").read_text())['projects']
            finally:
                if old is None:
                    os.environ.pop("WAYSTONE_HOME", None)
                else:
                    os.environ["WAYSTONE_HOME"] = old
            self.assertRegex(rows[0]["project_id"], r"^project:[0-9a-f]{32}$")

    def test_legacy_surface_is_not_wired(self):
        self.assertEqual(cli_main.main(["delegate"]), 1)
        self.assertEqual(cli_main.main(["round"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
