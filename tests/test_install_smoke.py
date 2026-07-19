import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from personal_harness.install_smoke import (
    run_custom_capture_smoke,
    run_install_smoke_tests,
    run_lifecycle_smoke,
    run_tmux_oversized_image_smoke,
)


class TestInstallSmoke(unittest.TestCase):
    def test_custom_capture_smoke_records_custom_exec_and_verification(self):
        result = run_custom_capture_smoke()

        self.assertTrue(result.passed, result.details)
        self.assertEqual(result.details["tool_call_count"], 1)
        self.assertEqual(result.details["verification_result_count"], 1)

    def test_tmux_oversized_image_smoke_drains_six_megabytes_without_epipe(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            hook = root / "hook.mjs"
            hook.write_text(
                "let raw=''; for await (const chunk of process.stdin) raw += chunk; "
                "const stop=raw.includes('\\\"hook_event_name\\\": \\\"Stop\\\"'); "
                "process.stdout.write(JSON.stringify(stop ? {decision:'block',stopReason:'native_stop_stdin_oversized_active_workflow'} : {})+'\\n');\n"
            )

            result = run_tmux_oversized_image_smoke(hook, cwd=root)

        self.assertTrue(result.passed, result.details)
        self.assertIsNone(result.details["producer_error"])
        self.assertEqual(result.details["hook_exit_code"], 0)
        self.assertEqual(result.details["stdout"], "{}")
        self.assertGreaterEqual(result.details["payload_bytes"], 6 * 1024 * 1024)
        self.assertEqual(result.details["payload_shape"], "image_attachment_before_event_name")
        self.assertTrue(result.details["stop_gate_passed"])
        self.assertGreaterEqual(result.duration_seconds, 0.0)

    def test_tmux_smoke_times_out_without_hanging_on_non_draining_hook(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            hook = root / "hang.mjs"
            hook.write_text("setTimeout(() => {}, 60000);\n")
            started = time.monotonic()

            result = run_tmux_oversized_image_smoke(hook, cwd=root, timeout_seconds=0.1)

        self.assertFalse(result.passed)
        self.assertTrue(result.details["timed_out"])
        self.assertLess(time.monotonic() - started, 2.0)

    def test_lifecycle_smoke_migrates_state_and_correlates_capture_failure(self):
        result = run_lifecycle_smoke()

        self.assertTrue(result.passed, result.details)
        self.assertEqual(result.details["state_schema"], "personal-harness-state/v2")
        self.assertEqual(result.details["checkpoint_count"], 2)
        self.assertTrue(result.details["session_ids_match"])
        self.assertIn("FileNotFoundError", result.details["capture_error"])

    def test_install_smoke_runner_returns_release_contract_order(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            hook = root / "dist" / "scripts" / "codex-native-hook.js"
            hook.parent.mkdir(parents=True)
            hook.write_text(
                "let raw=''; for await (const chunk of process.stdin) raw += chunk; "
                "const stop=raw.includes('\\\"hook_event_name\\\": \\\"Stop\\\"'); "
                "process.stdout.write(JSON.stringify(stop ? {decision:'block',stopReason:'native_stop_stdin_oversized_active_workflow'} : {})+'\\n');\n"
            )
            context = SimpleNamespace(omx_package_root=root)

            results = run_install_smoke_tests(context)

        self.assertEqual([result["name"] for result in results], ["custom_capture", "tmux_oversized_image", "lifecycle"])
        self.assertTrue(all(result["passed"] for result in results), results)
        self.assertTrue(all(result["duration_seconds"] >= 0 for result in results), results)


if __name__ == "__main__":
    unittest.main()
