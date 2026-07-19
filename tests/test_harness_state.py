import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_harness.harness_state import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    PersonalHarnessRuntimeState,
    migrate_personal_harness_state,
    read_personal_harness_state,
    write_personal_harness_state,
)


class TestHarnessState(unittest.TestCase):
    def test_writes_and_reads_personal_harness_runtime_state_under_harness_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.dict("os.environ", {"HARNESS_INSTALLATION_ID": "install-test"}):
                state = PersonalHarnessRuntimeState(active=True, harness_version="v2", model_version="model-v1", variant_id="default", phase="aegis")
                path = write_personal_harness_state(root, state)
                loaded = read_personal_harness_state(root)
            raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path, root / ".harness" / "state" / "personal-harness-state.json")
        self.assertEqual(raw["schema_version"], SCHEMA_VERSION)
        self.assertEqual(raw["installation_id"], "install-test")
        self.assertEqual(raw["state_revision"], 2)
        self.assertEqual(loaded.harness_version, "v2")
        self.assertEqual(loaded.phase, "aegis")

    def test_migrates_v1_state_to_v2_without_losing_runtime_or_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / ".harness" / "state" / "personal-harness-state.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": LEGACY_SCHEMA_VERSION,
                        "active": False,
                        "harness_version": "v1",
                        "model_version": "gpt-5.5",
                        "variant_id": "default",
                        "phase": "closed",
                        "metadata": {"capture_on_exit": {"status": "failed"}},
                        "updated_at": 123.0,
                    }
                )
            )

            result = migrate_personal_harness_state(root, installation_id="install-123", now=456.0)
            migrated = json.loads(path.read_text())

        self.assertTrue(result.migrated)
        self.assertEqual(migrated["schema_version"], SCHEMA_VERSION)
        self.assertEqual(migrated["installation_id"], "install-123")
        self.assertEqual(migrated["state_revision"], 2)
        self.assertEqual(migrated["migrated_at"], 456.0)
        self.assertEqual(migrated["updated_at"], 123.0)
        self.assertEqual(migrated["metadata"]["capture_on_exit"]["status"], "failed")

    def test_v2_migration_is_noop_and_malformed_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = PersonalHarnessRuntimeState(
                active=False,
                harness_version="v2",
                model_version="gpt-5.5",
                variant_id="default",
                phase="closed",
                installation_id="install-existing",
                migrated_at=10.0,
            )
            path = write_personal_harness_state(root, state)
            before = path.read_text()

            result = migrate_personal_harness_state(root, installation_id="install-new", now=20.0)

            self.assertFalse(result.migrated)
            self.assertEqual(path.read_text(), before)

            path.write_text("{not-json")
            with self.assertRaises(json.JSONDecodeError):
                migrate_personal_harness_state(root, installation_id="install-new", now=20.0)
            self.assertEqual(path.read_text(), "{not-json")

    def test_read_lazily_migrates_v1_using_installation_environment(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / ".harness" / "state" / "personal-harness-state.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": LEGACY_SCHEMA_VERSION,
                        "active": True,
                        "harness_version": "v1",
                        "model_version": "gpt-5.5",
                        "variant_id": "default",
                        "phase": "session",
                        "metadata": {},
                        "updated_at": 1.0,
                    }
                )
            )

            with patch.dict("os.environ", {"HARNESS_INSTALLATION_ID": "install-lazy"}):
                loaded = read_personal_harness_state(root)
            raw = json.loads(path.read_text())

        self.assertEqual(loaded.installation_id, "install-lazy")
        self.assertEqual(raw["schema_version"], SCHEMA_VERSION)
        self.assertEqual(raw["installation_id"], "install-lazy")

    def test_runtime_writes_preserve_original_migration_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_personal_harness_state(
                root,
                PersonalHarnessRuntimeState(
                    active=False,
                    harness_version="v2",
                    model_version="gpt-5.5",
                    variant_id="default",
                    phase="closed",
                    installation_id="stable-install",
                    migrated_at=10.0,
                ),
            )
            write_personal_harness_state(
                root,
                PersonalHarnessRuntimeState(
                    active=True,
                    harness_version="v2",
                    model_version="gpt-5.5",
                    variant_id="default",
                    phase="session",
                    installation_id="stable-install",
                    migrated_at=20.0,
                ),
            )
            raw = json.loads(
                (root / ".harness" / "state" / "personal-harness-state.json").read_text()
            )

        self.assertEqual(raw["migrated_at"], 10.0)


if __name__ == "__main__":
    unittest.main()
