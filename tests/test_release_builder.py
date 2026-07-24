import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from personal_harness.release_builder import ReleaseBuildError, build_release_artifacts
from personal_harness.release_contract import RELEASE_MANIFEST_SCHEMA, load_release_manifest, verify_release_assets


class TestReleaseBuilder(unittest.TestCase):
    def test_builds_manifest_and_deterministic_checksums_from_wheel(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            wheel = root / "personal_harness-1.1.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel-1.1.0")
            output = root / "release"

            result = build_release_artifacts(wheel, output, tag="v1.1.0")
            manifest = load_release_manifest(output / "harness-release.json")
            sums = (output / "SHA256SUMS").read_text().splitlines()
            verify_release_assets(manifest, output)

        self.assertEqual(result["version"], "1.1.0")
        self.assertEqual(manifest.schema_version, RELEASE_MANIFEST_SCHEMA)
        self.assertEqual(manifest.omx_version, "0.20.2")
        self.assertEqual(manifest.wheel_sha256, hashlib.sha256(b"wheel-1.1.0").hexdigest())
        self.assertEqual([line.split(maxsplit=1)[1].strip() for line in sums], ["harness-release.json", wheel.name])

    def test_rejects_mismatched_tag_and_non_harness_wheel(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            wheel = root / "personal_harness-1.1.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            with self.assertRaisesRegex(ReleaseBuildError, "tag"):
                build_release_artifacts(wheel, root / "release", tag="v1.2.0")

            other = root / "other-1.1.0-py3-none-any.whl"
            other.write_bytes(b"wheel")
            with self.assertRaisesRegex(ReleaseBuildError, "wheel"):
                build_release_artifacts(other, root / "other-release", tag="v1.1.0")


if __name__ == "__main__":
    unittest.main()
