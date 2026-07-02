import tempfile
import unittest
from pathlib import Path

from personal_harness.replay import ReplayStore, TrajectoryRecord


class TestReplayStore(unittest.TestCase):
    def test_append_and_read_records(self):
        with tempfile.TemporaryDirectory() as d:
            store = ReplayStore(Path(d) / "replay.jsonl")
            store.append(TrajectoryRecord(task_id="task-1", harness_version="v1", reward=1.0, solved=True, events=[{"hook": "task_end"}], metadata={"source": "unit"}))
            records = list(store.read_all())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].task_id, "task-1")
        self.assertTrue(records[0].solved)

    def test_solved_task_ids_returns_only_solved_records(self):
        with tempfile.TemporaryDirectory() as d:
            store = ReplayStore(Path(d) / "replay.jsonl")
            store.append(TrajectoryRecord(task_id="solved", harness_version="v1", reward=1.0, solved=True))
            store.append(TrajectoryRecord(task_id="failed", harness_version="v1", reward=0.0, solved=False))
            self.assertEqual(store.solved_task_ids(), {"solved"})

    def test_malformed_jsonl_reports_line_number(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "replay.jsonl"
            path.write_text('{"task_id": "ok", "harness_version": "v1", "reward": 1, "solved": true}\nnot-json\n', encoding="utf-8")
            store = ReplayStore(path)
            with self.assertRaisesRegex(ValueError, "line 2"):
                list(store.read_all())


if __name__ == "__main__":
    unittest.main()
