#!/usr/bin/env python3
"""Focused semantic worker-prompt contracts."""
from __future__ import annotations

from support import *  # noqa: F401,F403
from types import SimpleNamespace

from waystone.runs.prompt import render_worker_prompt
from waystone.runs.spec import FrozenJobInput, ArtifactDescriptor


class RunPromptTests(unittest.TestCase):
    def spec(self):
        return SimpleNamespace(job_input=FrozenJobInput(
                task_id="fix/prompt-surface",
                title="Render the frozen worker intent",
                completion_contract=ArtifactDescriptor(
                    "completion-contract:fixture", "sha256:" + "4" * 64, 1),
                acceptance_criteria=("The goal and bounds remain verbatim.",),
                scope=("waystone/runs/prompt.py",),
                dependencies=(),
                input_digest="sha256:" + "1" * 64,
            ))

    def test_prompt_contains_semantic_goal_bounds_acceptance_and_report(self):
        prompt = render_worker_prompt(self.spec())
        for section in ("## Goal", "## Bounds", "## Acceptance criteria", "## Report (required)"):
            self.assertIn(section, prompt)
        self.assertIn("Render the frozen worker intent", prompt)
        self.assertNotIn("task count", prompt.lower())

    def test_prompt_is_deterministic_for_one_frozen_spec(self):
        self.assertEqual(render_worker_prompt(self.spec()), render_worker_prompt(self.spec()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
