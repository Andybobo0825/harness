import tempfile
import unittest
from pathlib import Path

from personal_harness.omx_adapter import snapshot_omx_compatibility


class TestOmxAdapter(unittest.TestCase):
    def test_snapshot_reports_absent_omx(self):
        with tempfile.TemporaryDirectory() as d:
            snapshot = snapshot_omx_compatibility(Path(d))
        self.assertFalse(snapshot.present)
        self.assertEqual(snapshot.state_files, [])

    def test_snapshot_lists_state_and_log_files_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".omx" / "state").mkdir(parents=True)
            (root / ".omx" / "logs").mkdir(parents=True)
            (root / ".omx" / "state" / "mode.json").write_text("{}", encoding="utf-8")
            (root / ".omx" / "logs" / "turns.jsonl").write_text("", encoding="utf-8")
            snapshot = snapshot_omx_compatibility(root)
        self.assertTrue(snapshot.present)
        self.assertEqual(snapshot.state_files, ["state/mode.json"])
        self.assertEqual(snapshot.log_files, ["logs/turns.jsonl"])


if __name__ == "__main__":
    unittest.main()
