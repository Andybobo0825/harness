import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_harness.deployment import BackupArchive
from personal_harness.install_command import (
    DownloadError,
    _assert_global_python_boundary,
    build_staged_install_command,
    doctor_installation,
    download_release,
    main,
    recover_installation,
    rollback_installation,
)
from personal_harness.release_contract import INSTALL_MANIFEST_SCHEMA, RELEASE_MANIFEST_SCHEMA
from personal_harness.omx_overlay import OFFICIAL_POSTIMAGE_CHECKSUMS, OVERLAY_REVISION


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class _Opener:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def __call__(self, request, timeout=30):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.urls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected URL: {url}")
        return _Response(self.responses[url])


def _release_bytes(wheel_bytes=b"wheel"):
    digest = hashlib.sha256(wheel_bytes).hexdigest()
    wheel_name = "personal_harness-1.1.0-py3-none-any.whl"
    manifest = {
        "schema_version": RELEASE_MANIFEST_SCHEMA,
        "version": "1.1.0",
        "tag": "v1.1.0",
        "draft": False,
        "prerelease": False,
        "python_requires": ">=3.11",
        "platforms": ["macos", "linux"],
        "wheel": {"filename": wheel_name, "sha256": digest},
        "omx": {
            "version": "0.20.2",
            "tarball_url": "https://registry.npmjs.org/oh-my-codex/-/oh-my-codex-0.20.2.tgz",
            "integrity": "sha512-example",
            "overlay_revision": OVERLAY_REVISION,
        },
        "state_schema": "personal-harness-state/v2",
        "smoke_tests": ["custom_capture", "tmux_oversized_image", "lifecycle"],
    }
    return manifest, wheel_name, digest


class TestInstallCommand(unittest.TestCase):
    def test_download_release_uses_only_stable_github_release_assets(self):
        manifest, wheel_name, digest = _release_bytes()
        api = "https://api.github.com/repos/Andybobo0825/harness/releases/latest"
        assets = {
            "harness-release.json": "https://assets/release.json",
            "SHA256SUMS": "https://assets/SHA256SUMS",
            wheel_name: "https://assets/wheel",
        }
        release = {
            "tag_name": "v1.1.0",
            "draft": False,
            "prerelease": False,
            "assets": [{"name": name, "browser_download_url": url} for name, url in assets.items()],
        }
        opener = _Opener(
            {
                api: json.dumps(release).encode(),
                assets["harness-release.json"]: json.dumps(manifest).encode(),
                assets["SHA256SUMS"]: (
                    f"{hashlib.sha256(json.dumps(manifest).encode()).hexdigest()}  harness-release.json\n"
                    f"{digest}  {wheel_name}\n"
                ).encode(),
                assets[wheel_name]: b"wheel",
            }
        )
        with tempfile.TemporaryDirectory() as d:
            downloaded = download_release(None, Path(d), opener=opener)

            self.assertEqual(downloaded.version, "1.1.0")
            self.assertTrue((Path(d) / wheel_name).is_file())
            self.assertEqual(opener.urls[0], api)

        release["draft"] = True
        opener = _Opener({api: json.dumps(release).encode()})
        with tempfile.TemporaryDirectory() as d, self.assertRaisesRegex(DownloadError, "stable"):
            download_release(None, Path(d), opener=opener)

    def test_download_release_rejects_checksum_mismatch(self):
        manifest, wheel_name, digest = _release_bytes()
        api = "https://api.github.com/repos/Andybobo0825/harness/releases/tags/v1.1.0"
        assets = {
            "harness-release.json": "https://assets/release.json",
            "SHA256SUMS": "https://assets/SHA256SUMS",
            wheel_name: "https://assets/wheel",
        }
        release = {
            "tag_name": "v1.1.0",
            "draft": False,
            "prerelease": False,
            "assets": [{"name": name, "browser_download_url": url} for name, url in assets.items()],
        }
        opener = _Opener(
            {
                api: json.dumps(release).encode(),
                assets["harness-release.json"]: json.dumps(manifest).encode(),
                assets["SHA256SUMS"]: (
                    f"{hashlib.sha256(json.dumps(manifest).encode()).hexdigest()}  harness-release.json\n"
                    f"{digest}  {wheel_name}\n"
                ).encode(),
                assets[wheel_name]: b"tampered",
            }
        )
        with tempfile.TemporaryDirectory() as d, self.assertRaisesRegex(DownloadError, "checksum"):
            download_release("1.1.0", Path(d), opener=opener)

    def test_staged_install_command_imports_downloaded_wheel_not_main(self):
        command = build_staged_install_command(
            Path("/tmp/release/personal_harness-1.1.0-py3-none-any.whl"),
            Path("/tmp/release/harness-release.json"),
            harness_home=Path("/tmp/harness-home"),
            codex_home=Path("/tmp/codex-home"),
            project_roots=(Path("/repo/one"),),
        )

        self.assertEqual(command[0], sys.executable)
        self.assertIn("sys.path.insert", command[2])
        self.assertIn("personal_harness.install_command", command[2])
        self.assertIn("--release-manifest", command)
        self.assertNotIn("main", " ".join(command[3:]))

    def test_doctor_detects_clean_install_and_checksum_drift(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            harness_home = root / "harness-home"
            wheel = harness_home / "releases" / "1.1.0" / "wheel.whl"
            hook = root / "omx" / "dist" / "scripts" / "codex-native-hook.js"
            source_hook = root / "omx" / "src" / "scripts" / "codex-native-hook.ts"
            hooks = root / "codex" / "hooks.json"
            wheel.parent.mkdir(parents=True)
            hook.parent.mkdir(parents=True)
            source_hook.parent.mkdir(parents=True)
            hooks.parent.mkdir(parents=True)
            wheel.write_bytes(b"wheel")
            hook.write_text("patched-dist")
            source_hook.write_text("patched-source")
            (root / "omx" / "package.json").write_text(json.dumps({"name": "oh-my-codex", "version": "0.20.2"}))
            hooks.write_text("{}")
            manifest_path = harness_home / "install" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": INSTALL_MANIFEST_SCHEMA,
                        "installation_id": "install-1",
                        "harness_version": "1.1.0",
                        "python_executable": sys.executable,
                        "wheel_path": str(wheel),
                        "wheel_sha256": hashlib.sha256(b"wheel").hexdigest(),
                        "omx": {
                            "version": "0.20.2",
                            "overlay_revision": OVERLAY_REVISION,
                            "after": {
                                "dist/scripts/codex-native-hook.js": hashlib.sha256(b"patched-dist").hexdigest(),
                                "src/scripts/codex-native-hook.ts": hashlib.sha256(b"patched-source").hexdigest(),
                            },
                            "package_root": str(root / "omx"),
                        },
                        "hooks": {"path": str(hooks), "sha256": hashlib.sha256(b"{}").hexdigest()},
                    }
                )
            )

            with patch("personal_harness.install_command._package_version", return_value="1.1.0"):
                clean = doctor_installation(harness_home)
                journal = harness_home / "install" / "transaction.json"
                journal.write_text(json.dumps({"backup_id": "20260719T000000Z-aaaaaaaaaaaa"}))
                incomplete = doctor_installation(harness_home)
                journal.unlink()
                hook.write_text("drift")
                drifted = doctor_installation(harness_home)

        self.assertTrue(clean["ok"], clean)
        self.assertFalse(incomplete["ok"], incomplete)
        self.assertTrue(any(check["name"] == "transaction_journal" and not check["passed"] for check in incomplete["checks"]))
        self.assertFalse(drifted["ok"])
        self.assertTrue(any(check["name"].startswith("omx:") and not check["passed"] for check in drifted["checks"]))

    def test_doctor_fails_closed_for_non_object_or_incomplete_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            manifest = home / "install" / "manifest.json"
            manifest.parent.mkdir(parents=True)
            for payload in ([], {"schema_version": INSTALL_MANIFEST_SCHEMA}):
                manifest.write_text(json.dumps(payload))
                result = doctor_installation(home)
                self.assertFalse(result["ok"], result)

    def test_rollback_restores_selected_backup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            harness_home = root / "harness-home"
            target = root / "target.txt"
            target.write_text("before")
            archive = BackupArchive.create(harness_home, [target])
            target.write_text("after")

            result = rollback_installation(harness_home, archive.backup_id, runner=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""))

            self.assertEqual(target.read_text(), "before")
            self.assertEqual(result["backup_id"], archive.backup_id)
            self.assertTrue(result["verification"]["ok"])

    def test_rollback_rejects_backup_id_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            harness_home = root / "harness-home"
            outside = root / "outside"
            outside.mkdir()
            (outside / "backup-manifest.json").write_text(
                json.dumps({"schema_version": "harness-backup/v1", "entries": []})
            )

            with self.assertRaisesRegex(Exception, "backup id"):
                rollback_installation(harness_home, "../../outside")

    def test_recover_uses_journal_backup_and_clears_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            harness_home = root / "harness-home"
            target = root / "target.txt"
            target.write_text("before")
            archive = BackupArchive.create(harness_home, [target])
            target.write_text("after")
            journal = harness_home / "install" / "transaction.json"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(json.dumps({"backup_id": archive.backup_id, "status": "installing_omx"}))

            result = recover_installation(
                harness_home,
                runner=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""),
            )
            restored_text = target.read_text()
            journal_exists = journal.exists()

        self.assertEqual(result["backup_id"], archive.backup_id)
        self.assertEqual(restored_text, "before")
        self.assertFalse(journal_exists)

    def test_recover_holds_one_lock_while_selecting_and_restoring_backup(self):
        locked = False
        selected_backup = "20260719T000000Z-aaaaaaaaaaaa"

        @contextmanager
        def guarded_lock(_home):
            nonlocal locked
            self.assertFalse(locked)
            locked = True
            try:
                yield
            finally:
                locked = False

        def guarded_read(_path):
            self.assertTrue(locked)
            return {"backup_id": selected_backup, "status": "installing_omx"}

        def guarded_rollback(_home, backup_id, *, runner):
            self.assertTrue(locked)
            self.assertEqual(backup_id, selected_backup)
            return {"backup_id": backup_id, "restored": True}

        with (
            patch("personal_harness.install_command.deployment_lock", guarded_lock),
            patch("personal_harness.install_command._optional_json", guarded_read),
            patch("personal_harness.install_command._rollback_installation_locked", guarded_rollback),
            patch("pathlib.Path.exists", return_value=True),
        ):
            result = recover_installation("/tmp/harness-home")

        self.assertTrue(result["restored"])
        self.assertFalse(locked)

    def test_global_install_rejects_virtual_environment_scope(self):
        with patch("personal_harness.install_command.sys.prefix", "/tmp/harness-venv"):
            with self.assertRaisesRegex(Exception, "virtual environment"):
                _assert_global_python_boundary()

    def test_version_cli_reports_package_and_install_versions_as_json(self):
        with tempfile.TemporaryDirectory() as d:
            harness_home = Path(d)
            manifest_path = harness_home / "install" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(json.dumps({"schema_version": INSTALL_MANIFEST_SCHEMA, "harness_version": "1.1.0"}))
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(["--harness-home", str(harness_home), "version", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["installed_version"], "1.1.0")
        self.assertIn("package_version", payload)


if __name__ == "__main__":
    unittest.main()
