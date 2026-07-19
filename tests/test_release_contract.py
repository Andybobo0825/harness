import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from personal_harness.omx_overlay import OVERLAY_REVISION

from personal_harness.release_contract import (
    INSTALL_MANIFEST_SCHEMA,
    RELEASE_MANIFEST_SCHEMA,
    ReleaseContractError,
    atomic_write_json,
    load_release_manifest,
    sha256_file,
    verify_release_assets,
)


def _release_payload(wheel_name: str, wheel_sha256: str):
    return {
        "schema_version": RELEASE_MANIFEST_SCHEMA,
        "version": "1.1.0",
        "tag": "v1.1.0",
        "draft": False,
        "prerelease": False,
        "python_requires": ">=3.11",
        "platforms": ["macos", "linux"],
        "wheel": {"filename": wheel_name, "sha256": wheel_sha256},
        "omx": {
            "version": "0.20.2",
            "tarball_url": "https://registry.npmjs.org/oh-my-codex/-/oh-my-codex-0.20.2.tgz",
            "integrity": "sha512-example",
            "overlay_revision": OVERLAY_REVISION,
        },
        "state_schema": "personal-harness-state/v2",
        "smoke_tests": ["custom_capture", "tmux_oversized_image", "lifecycle"],
    }


class TestReleaseContract(unittest.TestCase):
    def test_load_release_manifest_validates_stable_versioned_contract(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harness-release.json"
            path.write_text(json.dumps(_release_payload("personal_harness-1.1.0-py3-none-any.whl", "a" * 64)))

            manifest = load_release_manifest(path)

        self.assertEqual(manifest.schema_version, RELEASE_MANIFEST_SCHEMA)
        self.assertEqual(manifest.version, "1.1.0")
        self.assertEqual(manifest.tag, "v1.1.0")
        self.assertEqual(manifest.omx_version, "0.20.2")
        self.assertEqual(manifest.smoke_tests, ("custom_capture", "tmux_oversized_image", "lifecycle"))

    def test_release_manifest_rejects_draft_prerelease_and_tag_mismatch(self):
        cases = [
            {"draft": True},
            {"prerelease": True},
            {"tag": "v1.2.0"},
        ]
        for update in cases:
            with self.subTest(update=update), tempfile.TemporaryDirectory() as d:
                payload = _release_payload("personal_harness-1.1.0-py3-none-any.whl", "a" * 64)
                payload.update(update)
                path = Path(d) / "harness-release.json"
                path.write_text(json.dumps(payload))

                with self.assertRaises(ReleaseContractError):
                    load_release_manifest(path)

    def test_release_manifest_rejects_wheel_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harness-release.json"
            path.write_text(
                json.dumps(_release_payload("../../personal_harness-1.1.0-py3-none-any.whl", "a" * 64))
            )

            with self.assertRaisesRegex(ReleaseContractError, "filename"):
                load_release_manifest(path)

    def test_release_manifest_rejects_unenforceable_python_requirement(self):
        with tempfile.TemporaryDirectory() as d:
            payload = _release_payload("personal_harness-1.1.0-py3-none-any.whl", "a" * 64)
            payload["python_requires"] = ">=3.11,<4"
            path = Path(d) / "harness-release.json"
            path.write_text(json.dumps(payload))

            with self.assertRaisesRegex(ReleaseContractError, "python_requires"):
                load_release_manifest(path)

    def test_verify_release_assets_requires_wheel_and_sha256sums_agreement(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            wheel = root / "personal_harness-1.1.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel-content")
            digest = hashlib.sha256(b"wheel-content").hexdigest()
            manifest_path = root / "harness-release.json"
            manifest_path.write_text(json.dumps(_release_payload(wheel.name, digest)))
            manifest = load_release_manifest(manifest_path)
            manifest_digest = sha256_file(manifest_path)
            (root / "SHA256SUMS").write_text(
                f"{manifest_digest}  {manifest_path.name}\n{digest}  {wheel.name}\n"
            )

            verify_release_assets(manifest, root)
            self.assertEqual(sha256_file(wheel), digest)

            (root / "SHA256SUMS").write_text(f"{'0' * 64}  {wheel.name}\n")
            with self.assertRaisesRegex(ReleaseContractError, "SHA256SUMS"):
                verify_release_assets(manifest, root)

            (root / "SHA256SUMS").write_text(
                f"{'0' * 64}  {manifest_path.name}\n{digest}  {wheel.name}\n"
            )
            with self.assertRaisesRegex(ReleaseContractError, "manifest"):
                verify_release_assets(manifest, root)

    def test_atomic_write_json_replaces_manifest_without_partial_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "install" / "manifest.json"
            with patch("personal_harness.release_contract.os.fsync", wraps=__import__("os").fsync) as fsync:
                atomic_write_json(
                    path,
                    {
                        "schema_version": INSTALL_MANIFEST_SCHEMA,
                        "installation_id": "install-123",
                        "harness_version": "1.1.0",
                    },
                )

            saved = json.loads(path.read_text())

        self.assertEqual(saved["schema_version"], INSTALL_MANIFEST_SCHEMA)
        self.assertEqual(saved["installation_id"], "install-123")
        self.assertGreaterEqual(fsync.call_count, 2)
        self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
