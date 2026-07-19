import unittest

from personal_harness.codex_hooks_migration import (
    HookTrustMigrationError,
    unfence_legacy_omx_hook_trust_state,
)


class TestCodexHooksMigration(unittest.TestCase):
    def test_removes_owned_trust_block_and_unfences_other_omx_settings(self):
        config = "\n".join(
            [
                'model = "gpt-5.5"',
                "# oh-my-codex (OMX) Configuration",
                "# OMX-owned Codex hook trust state",
                "# Trusts only setup-managed native hook wrappers.",
                '[hooks.state."/tmp/hooks.json:stop:0:0"]',
                'trusted_hash = "sha256:managed"',
                "# End OMX-owned Codex hook trust state",
                "[features]",
                "multi_agent = true",
                "hooks = true",
                "[agents]",
                "max_threads = 6",
                "max_depth = 2",
                "[tui]",
                'status_line = ["git-branch"]',
                "# End oh-my-codex",
                "",
            ]
        )

        result = unfence_legacy_omx_hook_trust_state(config)

        self.assertTrue(result.migrated)
        self.assertNotIn("# OMX-owned Codex hook trust state", result.content)
        self.assertNotIn("# End OMX-owned Codex hook trust state", result.content)
        self.assertNotIn("# oh-my-codex (OMX) Configuration", result.content)
        self.assertNotIn("# End oh-my-codex", result.content)
        for statement in (
            'model = "gpt-5.5"',
            "[features]",
            "hooks = true",
            "[agents]",
            "[tui]",
            'status_line = ["git-branch"]',
        ):
            self.assertIn(statement, result.content)
        self.assertNotIn('[hooks.state."/tmp/hooks.json:stop:0:0"]', result.content)
        self.assertNotIn('trusted_hash = "sha256:managed"', result.content)
        self.assertNotIn("multi_agent = true", result.content)
        self.assertNotIn("max_threads = 6", result.content)
        self.assertNotIn("max_depth = 2", result.content)
        self.assertEqual(
            result.removed_legacy_keys,
            ("agents.max_depth", "agents.max_threads", "features.multi_agent"),
        )

    def test_does_not_remove_matching_legacy_values_outside_proven_outer_fence(self):
        config = "\n".join(
            [
                "[features]",
                "multi_agent = true",
                "[agents]",
                "max_threads = 6",
                "max_depth = 2",
                "# OMX-owned Codex hook trust state",
                '[hooks.state."/tmp/hooks.json:stop:0:0"]',
                'trusted_hash = "sha256:managed"',
                "# End OMX-owned Codex hook trust state",
                "",
            ]
        )

        result = unfence_legacy_omx_hook_trust_state(config)

        self.assertNotIn('[hooks.state."/tmp/hooks.json:stop:0:0"]', result.content)
        self.assertNotIn('trusted_hash = "sha256:managed"', result.content)
        self.assertIn("multi_agent = true", result.content)
        self.assertIn("max_threads = 6", result.content)
        self.assertIn("max_depth = 2", result.content)
        self.assertEqual(result.removed_legacy_keys, ())

    def test_clean_or_unfenced_config_is_noop(self):
        config = 'model = "gpt-5.5"\n[hooks.state]\n'

        result = unfence_legacy_omx_hook_trust_state(config)

        self.assertFalse(result.migrated)
        self.assertEqual(result.content, config)

    def test_ambiguous_or_unpaired_markers_fail_closed(self):
        fixtures = (
            "# OMX-owned Codex hook trust state\n[features]\nhooks = true\n",
            "# End OMX-owned Codex hook trust state\n",
            "\n".join(
                [
                    "# OMX-owned Codex hook trust state",
                    "# OMX-owned Codex hook trust state",
                    "# End OMX-owned Codex hook trust state",
                    "",
                ]
            ),
        )

        for config in fixtures:
            with self.subTest(config=config), self.assertRaises(HookTrustMigrationError):
                unfence_legacy_omx_hook_trust_state(config)

    def test_refuses_non_trust_toml_inside_owned_trust_fence(self):
        config = "\n".join(
            [
                "# OMX-owned Codex hook trust state",
                "[features]",
                "hooks = true",
                "# End OMX-owned Codex hook trust state",
                "",
            ]
        )

        with self.assertRaisesRegex(HookTrustMigrationError, "non-trust TOML"):
            unfence_legacy_omx_hook_trust_state(config)


if __name__ == "__main__":
    unittest.main()
