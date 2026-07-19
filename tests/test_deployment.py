import json
import os
import base64
import hashlib
import io
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from personal_harness.deployment import (
    BackupArchive,
    DeploymentContext,
    DeploymentError,
    _download_verified_omx_tarball,
    _foreign_hooks_snapshot,
    _verify_automatic_rollback,
    deployment_lock,
    _validate_hooks,
    deploy_release,
)
from personal_harness.release_contract import INSTALL_MANIFEST_SCHEMA, RELEASE_MANIFEST_SCHEMA, load_release_manifest
from personal_harness.omx_overlay import OMX_TARBALL_INTEGRITY, OVERLAY_REVISION
from tests.test_omx_overlay import _make_package


def _write_release(root: Path):
    wheel = root / "personal_harness-1.1.0-py3-none-any.whl"
    wheel.write_bytes(b"test-wheel")
    import hashlib

    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest_path = root / "harness-release.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": RELEASE_MANIFEST_SCHEMA,
                "version": "1.1.0",
                "tag": "v1.1.0",
                "draft": False,
                "prerelease": False,
                "python_requires": ">=3.11",
                "platforms": ["macos", "linux"],
                "wheel": {"filename": wheel.name, "sha256": digest},
                "omx": {
                    "version": "0.20.2",
                    "tarball_url": "https://registry.npmjs.org/oh-my-codex/-/oh-my-codex-0.20.2.tgz",
                    "integrity": OMX_TARBALL_INTEGRITY,
                    "overlay_revision": OVERLAY_REVISION,
                },
                "state_schema": "personal-harness-state/v2",
                "smoke_tests": ["custom_capture", "tmux_oversized_image", "lifecycle"],
            }
        )
    )
    (root / "SHA256SUMS").write_text(f"{digest}  {wheel.name}\n")
    return load_release_manifest(manifest_path), wheel


def _write_v1_state(root: Path):
    path = root / ".harness" / "state" / "personal-harness-state.json"
    path.parent.mkdir(parents=True)
    payload = {
        "schema_version": "personal-harness-state/v1",
        "active": False,
        "harness_version": "v1",
        "model_version": "gpt-5.5",
        "variant_id": "default",
        "phase": "closed",
        "metadata": {"preserve": True},
        "updated_at": 1.0,
    }
    path.write_text(json.dumps(payload))
    return path, payload


class TestBackupArchive(unittest.TestCase):
    def test_restores_files_directories_symlinks_and_missing_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            harness_home = root / "harness-home"
            file_path = root / "config.json"
            directory = root / "package"
            link = root / "omx"
            missing = root / "created-later"
            file_path.write_text("before")
            directory.mkdir()
            (directory / "nested.txt").write_text("nested-before")
            link.symlink_to(directory)

            archive = BackupArchive.create(harness_home, [file_path, directory, link, missing])
            file_path.write_text("after")
            (directory / "nested.txt").write_text("nested-after")
            link.unlink()
            link.symlink_to(file_path)
            missing.write_text("new")

            BackupArchive.load(archive.root).restore()

            self.assertEqual(file_path.read_text(), "before")
            self.assertEqual((directory / "nested.txt").read_text(), "nested-before")
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(directory))
            self.assertFalse(missing.exists())

    def test_corrupt_backup_fails_before_overwriting_current_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "config.json"
            target.write_text("before")
            archive = BackupArchive.create(root / "harness-home", [target])
            target.write_text("current")
            (archive.root / "data" / "0").write_text("corrupt")

            with self.assertRaisesRegex(DeploymentError, "checksum"):
                archive.restore()

            self.assertEqual(target.read_text(), "current")

    def test_deployment_lock_rejects_concurrent_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            harness_home = Path(d) / "harness-home"

            with deployment_lock(harness_home):
                with self.assertRaisesRegex(DeploymentError, "already in progress"):
                    with deployment_lock(harness_home, timeout=0.02):
                        self.fail("concurrent lock unexpectedly acquired")


class TestOmxArtifactVerification(unittest.TestCase):
    def test_downloads_only_tarball_matching_release_sri(self):
        payload = b"official npm tarball fixture"
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode()

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.close()

        with tempfile.TemporaryDirectory() as d:
            destination = Path(d) / "omx.tgz"
            result = _download_verified_omx_tarball(
                "https://registry.example/omx.tgz",
                integrity,
                destination,
                opener=lambda request, timeout=60: Response(payload),
            )
            self.assertEqual(result.read_bytes(), payload)

            with self.assertRaisesRegex(DeploymentError, "integrity"):
                _download_verified_omx_tarball(
                    "https://registry.example/omx.tgz",
                    "sha512-" + base64.b64encode(b"wrong").decode(),
                    destination,
                    opener=lambda request, timeout=60: Response(payload),
                )

    def test_hook_validation_rejects_any_stale_omx_command(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            omx_root = root / "omx"
            hooks = root / "hooks.json"
            expected = omx_root / "dist" / "scripts" / "codex-native-hook.js"
            hooks.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {"command": f'node "{expected}"'},
                                {"command": 'node "/old/oh-my-codex/dist/scripts/codex-native-hook.js"'},
                            ]
                        }
                    }
                )
            )

            with self.assertRaisesRegex(DeploymentError, "stale OMX"):
                _validate_hooks(hooks, omx_root, {})

    def test_hook_validation_detects_foreign_hook_metadata_changes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            omx_root = root / "omx"
            hooks = root / "hooks.json"
            expected = omx_root / "dist" / "scripts" / "codex-native-hook.js"
            before = {
                "hooks": {
                    "UserPromptSubmit": [
                        {"matcher": "*", "timeout": 10, "hooks": [{"command": "user-hook --keep"}]}
                    ]
                }
            }
            preserved = _foreign_hooks_snapshot(before)
            changed = json.loads(json.dumps(before))
            changed["hooks"]["UserPromptSubmit"][0]["timeout"] = 20
            changed["hooks"]["Stop"] = [{"hooks": [{"command": f'node "{expected}"'}]}]
            hooks.write_text(json.dumps(changed))

            with self.assertRaisesRegex(DeploymentError, "user-managed hook definitions"):
                _validate_hooks(hooks, omx_root, preserved)


class TestDeployment(unittest.TestCase):
    def _context(self, root: Path, *, fail_smoke: bool = False):
        harness_home = root / "harness-home"
        codex_home = root / "codex-home"
        omx_root = root / "global" / "oh-my-codex"
        omx_checksums = _make_package(omx_root)
        omx_bin = root / "bin" / "omx"
        omx_bin.parent.mkdir(parents=True)
        omx_bin.symlink_to(omx_root / "dist" / "cli" / "omx.js")
        codex_home.mkdir()
        hooks_path = codex_home / "hooks.json"
        hooks_path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"command": "user-hook --keep"}]}}))
        project_root = root / "project"
        state_path, original_state = _write_v1_state(project_root)
        release_dir = root / "release"
        release_dir.mkdir()
        release, wheel = _write_release(release_dir)
        previous_source = root / "previous-source"
        previous_source.mkdir()
        (previous_source / "pyproject.toml").write_text("[project]\nname='personal-harness'\nversion='1.0.0'\n")
        commands = []

        def runner(command, **kwargs):
            commands.append(list(command))
            if command[0] == str(omx_bin):
                if "cwd" not in kwargs or Path(kwargs["cwd"]) == project_root:
                    raise AssertionError("OMX setup must run outside project roots")
                current = json.loads(hooks_path.read_text())
                current["hooks"]["Stop"] = [
                    {"command": f'node "{omx_root / "dist" / "scripts" / "codex-native-hook.js"}"'}
                ]
                hooks_path.write_text(json.dumps(current))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        def smoke_runner(_context):
            if fail_smoke:
                raise DeploymentError("injected smoke failure")
            return [
                {"name": "custom_capture", "passed": True},
                {"name": "tmux_oversized_image", "passed": True},
                {"name": "lifecycle", "passed": True},
            ]

        context = DeploymentContext(
            harness_home=harness_home,
            codex_home=codex_home,
            omx_package_root=omx_root,
            omx_executable=omx_bin,
            wheel_path=wheel,
            release=release,
            project_roots=(project_root,),
            python_executable="python-test",
            runner=runner,
            smoke_runner=smoke_runner,
            accepted_omx_preimage_checksums=omx_checksums,
            previous_harness_source=previous_source,
            omx_installer=lambda _context: None,
        )
        return context, commands, hooks_path, state_path, original_state

    def test_success_commits_manifest_after_overlay_hooks_migration_and_smoke(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            context, commands, hooks_path, state_path, _ = self._context(root)

            result = deploy_release(context)
            install_manifest = json.loads((context.harness_home / "install" / "manifest.json").read_text())
            hooks = json.loads(hooks_path.read_text())
            state = json.loads(state_path.read_text())
            saved_release = context.harness_home / "releases" / "1.1.0"
            backup_manifest = json.loads(
                (context.harness_home / "backups" / install_manifest["backup_id"] / "backup-manifest.json").read_text()
            )
            saved_manifest_exists = (saved_release / "harness-release.json").is_file()
            saved_sums_exists = (saved_release / "SHA256SUMS").is_file()

        self.assertEqual(result["schema_version"], INSTALL_MANIFEST_SCHEMA)
        self.assertEqual(install_manifest["harness_version"], "1.1.0")
        self.assertEqual(install_manifest["omx"]["version"], "0.20.2")
        self.assertEqual(install_manifest["omx"]["overlay_revision"], OVERLAY_REVISION)
        self.assertEqual([item["name"] for item in install_manifest["smoke_tests"]], list(context.release.smoke_tests))
        self.assertEqual(hooks["hooks"]["UserPromptSubmit"][0]["command"], "user-hook --keep")
        self.assertIn(str(context.omx_package_root / "dist" / "scripts" / "codex-native-hook.js"), json.dumps(hooks))
        self.assertEqual(state["schema_version"], "personal-harness-state/v2")
        self.assertEqual(state["installation_id"], install_manifest["installation_id"])
        self.assertTrue(saved_manifest_exists)
        self.assertTrue(saved_sums_exists)
        self.assertEqual(backup_manifest["deployment"]["previous_harness_source"], str(context.previous_harness_source))
        self.assertTrue(any(command[:4] == ["python-test", "-m", "pip", "install"] for command in commands))
        setup_commands = [command for command in commands if command[0] == str(context.omx_executable)]
        self.assertTrue(setup_commands)
        self.assertIn("--force", setup_commands[0])

    def test_smoke_failure_rolls_back_omx_hooks_state_and_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            context, _, hooks_path, state_path, original_state = self._context(root, fail_smoke=True)
            original_hooks = hooks_path.read_text()
            original_dist = (context.omx_package_root / "dist" / "scripts" / "codex-native-hook.js").read_text()

            with self.assertRaisesRegex(DeploymentError, "injected smoke failure"):
                deploy_release(context)

            self.assertEqual(hooks_path.read_text(), original_hooks)
            self.assertEqual(json.loads(state_path.read_text()), original_state)
            self.assertEqual(
                (context.omx_package_root / "dist" / "scripts" / "codex-native-hook.js").read_text(),
                original_dist,
            )
            self.assertFalse((context.harness_home / "install" / "manifest.json").exists())
            backups = list((context.harness_home / "backups").iterdir())

        self.assertEqual(len(backups), 1)

    def test_manifest_commit_failure_restores_existing_release_cache(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            context, _, _, _, _ = self._context(root)
            release_cache = context.harness_home / "releases" / context.release.version
            release_cache.mkdir(parents=True)
            (release_cache / "existing.whl").write_text("previous-release")
            import personal_harness.deployment as deployment_module

            real_atomic_write = deployment_module.atomic_write_json

            def fail_install_manifest(path, payload):
                if Path(path) == context.harness_home / "install" / "manifest.json":
                    raise OSError("injected manifest commit failure")
                return real_atomic_write(path, payload)

            with patch("personal_harness.deployment.atomic_write_json", side_effect=fail_install_manifest):
                with self.assertRaisesRegex(DeploymentError, "manifest commit failure"):
                    deploy_release(context)

            self.assertEqual((release_cache / "existing.whl").read_text(), "previous-release")
            self.assertEqual(sorted(path.name for path in release_cache.iterdir()), ["existing.whl"])

    def test_unfences_legacy_omx_hook_trust_before_setup_and_records_migration(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            context, _, _, _, _ = self._context(root)
            config_path = context.codex_home / "config.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[hooks.state]",
                        "# OMX-owned Codex hook trust state",
                        '[hooks.state."/tmp/hooks.json:stop:0:0"]',
                        'trusted_hash = "sha256:foreign"',
                        "# End OMX-owned Codex hook trust state",
                        "",
                    ]
                )
            )
            original_runner = context.runner
            setup_saw_unfenced_config = False

            def runner(command, **kwargs):
                nonlocal setup_saw_unfenced_config
                if command[0] == str(context.omx_executable):
                    current = config_path.read_text()
                    setup_saw_unfenced_config = (
                        "# OMX-owned Codex hook trust state" not in current
                        and "# End OMX-owned Codex hook trust state" not in current
                        and 'trusted_hash = "sha256:foreign"' not in current
                    )
                return original_runner(command, **kwargs)

            context = DeploymentContext(**{**context.__dict__, "runner": runner})

            result = deploy_release(context)

        self.assertTrue(setup_saw_unfenced_config)
        self.assertEqual(result["codex_hook_trust_migration"]["status"], "unfenced")

    def test_deployment_error_reports_successful_rollback_and_backup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            context, _, _, _, _ = self._context(root, fail_smoke=True)

            with self.assertRaises(DeploymentError) as raised:
                deploy_release(context)

            transaction_journal_exists = (context.harness_home / "install" / "transaction.json").exists()
            transactions = [
                json.loads(line)
                for line in (context.harness_home / "logs" / "transactions.jsonl").read_text().splitlines()
            ]

        message = str(raised.exception)
        self.assertIn("was rolled back", message)
        self.assertIn("backup=", message)
        self.assertFalse(transaction_journal_exists)
        self.assertEqual(transactions[-1]["status"], "rolled_back")

    def test_rejects_release_outside_verified_compatibility_boundary_before_backup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            context, _, _, _, _ = self._context(root)
            incompatible = (
                replace(context.release, omx_version="0.20.3"),
                replace(context.release, overlay_revision="different-overlay"),
                replace(context.release, state_schema="personal-harness-state/v3"),
                replace(context.release, omx_tarball_url="https://example.invalid/omx.tgz"),
                replace(context.release, omx_integrity="sha512-wrong"),
            )

            for release in incompatible:
                with self.subTest(release=release), self.assertRaisesRegex(DeploymentError, "compatibility"):
                    deploy_release(DeploymentContext(**{**context.__dict__, "release": release}))

            backups_root = context.harness_home / "backups"
            self.assertFalse(backups_root.exists())

    def test_update_preserves_installation_id_and_uses_unique_deployment_id(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            context, _, _, _, _ = self._context(root)
            manifest_path = context.harness_home / "install" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": INSTALL_MANIFEST_SCHEMA,
                        "installation_id": "stable-installation",
                        "harness_version": "1.0.0",
                    }
                )
            )

            result = deploy_release(context)

        self.assertEqual(result["installation_id"], "stable-installation")
        self.assertNotEqual(result["deployment_id"], result["installation_id"])

    def test_update_rejects_different_recorded_python_before_backup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            context, _, _, _, _ = self._context(root)
            manifest_path = context.harness_home / "install" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": INSTALL_MANIFEST_SCHEMA,
                        "python_executable": "/different/global/python",
                    }
                )
            )

            with self.assertRaisesRegex(DeploymentError, "interpreter"):
                deploy_release(context)
            backups_exist = (context.harness_home / "backups").exists()

        self.assertFalse(backups_exist)

    def test_automatic_rollback_runs_full_doctor_contract(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            context, _, _, _, _ = self._context(root)
            commands = []

            def runner(command, **kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")

            context = DeploymentContext(**{**context.__dict__, "runner": runner})
            _verify_automatic_rollback(context, {"python_executable": sys.executable})

        self.assertIn("personal_harness.install_command", commands[0])
        self.assertNotIn("--allow-transaction-journal", commands[0])


if __name__ == "__main__":
    unittest.main()
