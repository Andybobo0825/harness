import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from personal_harness.flow_checkpoint import record_flow_checkpoint
from personal_harness.harness_state import PersonalHarnessRuntimeState, write_personal_harness_state
from personal_harness.memory import HOT_MEMORY_RELATIVE_PATH
from personal_harness import record_flow_checkpoint as exported_record_flow_checkpoint
from personal_harness.replay import ReplayStore


class TestFlowCheckpoint(unittest.TestCase):
    def test_record_flow_checkpoint_is_exported_as_package_api(self):
        self.assertIs(exported_record_flow_checkpoint, record_flow_checkpoint)

    def test_record_flow_checkpoint_writes_non_destructive_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "work.txt").write_text("hello", encoding="utf-8")
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
            commands = []

            def guarded_runner(command, **kwargs):
                commands.append(command)
                self.assertNotIn(command[:2], [["git", "reset"], ["git", "checkout"], ["git", "clean"]])
                return subprocess.run(command, **kwargs)

            path = record_flow_checkpoint(
                root,
                flow_id="implement-bootstrap",
                status="failed",
                evidence="unit tests failed before fix",
                skill_context={"requested": "test-driven-development", "selected": "debugger"},
                replay_refs=[".harness/replay/replay.jsonl#L1"],
                candidate_refs=[".harness/candidates/codex-candidate-request.json"],
                runner=guarded_runner,
            )

            [record] = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))
            [replay_record] = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())

        self.assertEqual(path, root / ".harness" / "flow-checkpoints" / "checkpoints.jsonl")
        self.assertEqual(record["flow_id"], "implement-bootstrap")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["evidence"], "unit tests failed before fix")
        self.assertEqual(record["skill_context"]["selected"], "debugger")
        self.assertEqual(record["replay_refs"], [".harness/replay/replay.jsonl#L1"])
        self.assertEqual(record["candidate_refs"], [".harness/candidates/codex-candidate-request.json"])
        self.assertRegex(record["git"]["summary"], r"untracked:[1-9]")
        self.assertEqual(state["phase"], "session")
        self.assertTrue(state["active"])
        self.assertEqual(state["metadata"]["flow_checkpoints"][-1]["flow_id"], "implement-bootstrap")
        self.assertEqual(replay_record.task_id, "flow:implement-bootstrap")
        self.assertFalse(replay_record.solved)
        self.assertEqual(replay_record.metadata["record_type"], "flow_checkpoint")
        self.assertTrue(commands)

    def test_record_flow_checkpoint_without_git_repo_still_records_no_repo(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            path = record_flow_checkpoint(root, flow_id="fresh", status="complete", evidence="created state")
            [record] = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(record["git"]["summary"], "git:no-repo")
        self.assertEqual(record["status"], "complete")

    def test_record_flow_checkpoint_can_sync_durable_memory_entry(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            path = record_flow_checkpoint(
                root,
                flow_id="todo-cli",
                status="complete",
                evidence="python3 -m unittest -v passed",
                memory_entry={
                    "date": "2026-07-04",
                    "category": "correction",
                    "text": "TODO_FILE must be read at runtime, not import time.",
                    "source": "flow:todo-cli",
                },
                sync_memory=True,
            )

            checkpoint = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            hot = (root / HOT_MEMORY_RELATIVE_PATH).read_text(encoding="utf-8")
            state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertEqual(checkpoint["memory"]["accepted"], True)
        self.assertIn("TODO_FILE must be read at runtime", hot)
        self.assertEqual(state["metadata"]["memory"]["last_sync"]["accepted"], True)

    def test_record_flow_checkpoint_preserves_malformed_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state_path = root / ".harness" / "state" / "personal-harness-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(json.JSONDecodeError):
                record_flow_checkpoint(root, flow_id="bad-state", status="failed", evidence="state decode failed")

            checkpoint = root / ".harness" / "flow-checkpoints" / "checkpoints.jsonl"
            replay = root / ".harness" / "replay" / "replay.jsonl"
            state_text = state_path.read_text(encoding="utf-8")
            checkpoint_text = checkpoint.read_text(encoding="utf-8")
            replay_exists = replay.exists()

        self.assertEqual(state_text, "{not-json")
        self.assertIn('"flow_id":"bad-state"', checkpoint_text)
        self.assertTrue(replay_exists)


if __name__ == "__main__":
    unittest.main()
