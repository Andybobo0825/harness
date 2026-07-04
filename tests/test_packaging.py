import pathlib
import tomllib
import unittest


class TestPackaging(unittest.TestCase):
    def test_console_scripts_are_declared(self):
        data = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
        scripts = data["project"]["scripts"]

        self.assertEqual(data["project"]["version"], "1.0.0")
        self.assertEqual(scripts["harness-codex"], "personal_harness.launcher:run_harness_codex")
        self.assertEqual(scripts["harness-status"], "personal_harness.launcher:status_main")
        self.assertEqual(scripts["harness-agent"], "personal_harness.harness_command:main")
        self.assertEqual(scripts["harness-capture-codex"], "personal_harness.codex_capture_command:main")
        self.assertEqual(data["tool"]["setuptools"]["packages"]["find"]["include"], ["personal_harness*"])


if __name__ == "__main__":
    unittest.main()
