"""Canonical role and profile contracts."""
from __future__ import annotations

from support import *  # noqa: F401,F403

import waystone.jobs.domain as domain
from waystone.jobs.domain import ExecutionCategory, ExecutorKind, Role, RoleBinding
from waystone.jobs.profile import ProfileSchemaRefusal, ProfileUnreadable, read_profile


class RunDomainTests(unittest.TestCase):
    def _profile_path(self, directory: str, body: str) -> Path:
        path = Path(directory) / "repo" / ".waystone" / "profile.yml"
        path.parent.mkdir(parents=True)
        path.write_text(body, encoding="utf-8")
        return path

    def _canonical_profile(self) -> str:
        return (
            "schema: waystone-profile-2\n"
            "bindings:\n"
            "  coordinator: {execution: in-session, backend: 'host:current'}\n"
            "  worker: {execution: external, backend: 'codex:gpt-test'}\n"
            "  verifier: {execution: subagent, backend: 'host:independent'}\n"
            "  reviewer: {execution: external, backend: 'future.runner:model'}\n"
        )

    def test_canonical_role_and_execution_domains_are_closed(self):
        self.assertEqual([role.value for role in Role], ["coordinator", "worker", "verifier", "reviewer"])
        self.assertEqual([kind.value for kind in ExecutorKind], ["engine", "carrier", "user"])
        self.assertEqual([category.value for category in ExecutionCategory], ["in-session", "subagent", "external"])

    def test_profile_requires_all_roles_and_binds_each_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = read_profile(self._profile_path(directory, self._canonical_profile()))
        self.assertEqual([item.binding.role for item in profile.bindings], list(Role))
        self.assertEqual(profile.binding_for(Role.WORKER).binding,
                         RoleBinding(Role.WORKER, ExecutionCategory.EXTERNAL, "codex:gpt-test"))
        for binding in profile.bindings:
            self.assertRegex(binding.binding_digest, r"^sha256:[0-9a-f]{64}$")

    def test_partial_or_duplicated_canonical_profiles_fail_closed(self):
        bodies = (
            "schema: waystone-profile-2\nbindings:\n  worker: {execution: external, backend: 'codex:gpt'}\n",
            self._canonical_profile() + "  worker: {execution: external, backend: 'codex:other'}\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, body in enumerate(bodies):
                with self.subTest(index=index):
                    with self.assertRaises(ProfileSchemaRefusal):
                        read_profile(self._profile_path(f"{directory}/{index}", body))
        with self.assertRaises(ProfileUnreadable):
            read_profile("profile\0.yml")

    def test_role_binding_has_no_inference_api(self):
        self.assertEqual(list(RoleBinding.__dataclass_fields__), ["role", "execution_category", "backend"])
        for name in ("executor_kind_for_role", "role_for_executor_kind", "role_to_executor_kind", "executor_kind_to_role"):
            self.assertFalse(hasattr(domain, name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
