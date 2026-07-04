import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from personal_harness.harness_state import PersonalHarnessRuntimeState, write_personal_harness_state
from personal_harness.memory import (
    ARCHIVE_MEMORY_RELATIVE_PATH,
    HOT_MEMORY_RELATIVE_PATH,
    WARM_MEMORY_RELATIVE_PATH,
    MemoryEntry,
    sync_checkpoint_memory,
)


class TestHarnessMemory(unittest.TestCase):
    def test_sync_checkpoint_memory_writes_allowed_hot_entry_and_state_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_personal_harness_state(
                root,
                PersonalHarnessRuntimeState(
                    active=True,
                    harness_version="v1",
                    model_version="gpt-5.5",
                    variant_id="default",
                    phase="session",
                    metadata={"runtime_owner": "standalone-.harness"},
                ),
            )

            result = sync_checkpoint_memory(
                root,
                entry=MemoryEntry(
                    date="2026-07-04",
                    category="decision",
                    text="This repo uses harness-codex as the coding entrypoint, not direct codex.",
                    source="flow:entrypoint",
                ),
                now=datetime(2026, 7, 4, tzinfo=timezone.utc),
            )

            hot = (root / HOT_MEMORY_RELATIVE_PATH).read_text(encoding="utf-8")
            state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertTrue(result.accepted)
        self.assertIn("2026-07-04 | decision | This repo uses harness-codex", hot)
        self.assertIn("source: flow:entrypoint", hot)
        self.assertEqual(state["metadata"]["memory"]["last_sync"]["accepted"], True)
        self.assertEqual(state["metadata"]["memory"]["hot_count"], 1)

    def test_sync_checkpoint_memory_rejects_forbidden_category_and_secret_like_text(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            bad_category = sync_checkpoint_memory(
                root,
                entry=MemoryEntry(
                    date="2026-07-04",
                    category="chat",
                    text="casual discussion",
                    source="flow:chat",
                ),
                now=datetime(2026, 7, 4, tzinfo=timezone.utc),
            )
            secret = sync_checkpoint_memory(
                root,
                entry=MemoryEntry(
                    date="2026-07-04",
                    category="verified-fact",
                    text="api" + "_key=example-value was mentioned during the run.",
                    source="flow:secret",
                ),
                now=datetime(2026, 7, 4, tzinfo=timezone.utc),
            )
            hot_exists = (root / HOT_MEMORY_RELATIVE_PATH).exists()

        self.assertFalse(bad_category.accepted)
        self.assertIn("unsupported category", bad_category.reason)
        self.assertFalse(secret.accepted)
        self.assertIn("forbidden sensitive content", secret.reason)
        self.assertFalse(hot_exists)

    def test_sync_checkpoint_memory_rotates_hot_warm_and_archive_layers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            now = datetime(2026, 7, 31, tzinfo=timezone.utc)
            for day in range(1, 32):
                sync_checkpoint_memory(
                    root,
                    entry=MemoryEntry(
                        date=f"2026-07-{day:02d}",
                        category="milestone",
                        text=f"milestone {day}",
                        source=f"flow:{day}",
                    ),
                    now=now,
                )
            sync_checkpoint_memory(
                root,
                entry=MemoryEntry(
                    date="2026-06-01",
                    category="correction",
                    text="old correction",
                    source="flow:old",
                ),
                now=now,
            )

            hot = (root / HOT_MEMORY_RELATIVE_PATH).read_text(encoding="utf-8")
            warm = (root / WARM_MEMORY_RELATIVE_PATH).read_text(encoding="utf-8")
            archive = (root / ARCHIVE_MEMORY_RELATIVE_PATH).read_text(encoding="utf-8")

        hot_entries = [line for line in hot.splitlines() if line.startswith("- ")]
        self.assertLessEqual(len(hot_entries), 20)
        self.assertIn("2026-07-31 | milestone | milestone 31", hot)
        self.assertIn("2026-07-01 | milestone | milestone 1", warm)
        self.assertIn("2026-06-01 | correction | old correction", archive)


if __name__ == "__main__":
    unittest.main()
