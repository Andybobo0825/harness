import pathlib
import tomllib
import unittest


class TestPackaging(unittest.TestCase):
    def test_console_scripts_are_declared(self):
        data = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
        scripts = data["project"]["scripts"]

        self.assertEqual(data["project"]["version"], "1.1.0")
        self.assertEqual(scripts["harness"], "personal_harness.install_command:main")
        self.assertEqual(scripts["harness-codex"], "personal_harness.launcher:run_harness_codex")
        self.assertEqual(scripts["harness-status"], "personal_harness.launcher:status_main")
        self.assertEqual(scripts["harness-agent"], "personal_harness.harness_command:main")
        self.assertEqual(scripts["harness-capture-codex"], "personal_harness.codex_capture_command:main")
        self.assertEqual(data["tool"]["setuptools"]["packages"]["find"]["include"], ["personal_harness*"])

    def test_install_documentation_uses_github_releases_not_main(self):
        readme = pathlib.Path("README.md").read_text(encoding="utf-8")

        self.assertNotIn("pip install git+https://github.com/Andybobo0825/harness.git", readme)
        self.assertIn("/releases/download/v1.1.0/", readme)
        self.assertIn("harness update", readme)
        self.assertIn("harness rollback", readme)

    def test_repository_wrappers_prefer_checkout_source_over_installed_package(self):
        for relative in ("scripts/harness", "scripts/build-harness-release"):
            wrapper = pathlib.Path(relative).read_text(encoding="utf-8")
            self.assertIn("sys.path.insert", wrapper, relative)
            self.assertIn("parents[1]", wrapper, relative)


if __name__ == "__main__":
    unittest.main()
