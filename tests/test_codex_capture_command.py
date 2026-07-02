import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from personal_harness.replay import ReplayStore


def _write_jsonl(path: Path, records):
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


def _write_verified_session(path: Path):
    _write_jsonl(
        path,
        [
            {
                "timestamp": "2026-07-01T01:00:00Z",
                "type": "session_meta",
                "payload": {"id": "sess-cli", "cwd": str(path.parent), "agent_role": "executor"},
            },
            {
                "timestamp": "2026-07-01T01:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Implemented and verified."}],
                },
            },
            {
                "timestamp": "2026-07-01T01:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-verify",
                    "name": "functions.exec_command",
                    "arguments": json.dumps({"cmd": "python3 -m unittest tests.test_codex_capture_command -v"}),
                },
            },
            {
                "timestamp": "2026-07-01T01:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-verify",
                    "output": "OK\nProcess exited with code 0",
                },
            },
        ],
    )


class TestCodexCaptureCommand(unittest.TestCase):
    def test_dry_run_summarizes_session_without_writing_harness_state(self):
        from personal_harness.codex_capture_command import capture_codex_session_command

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_path = root / "session.jsonl"
            _write_verified_session(session_path)

            result = capture_codex_session_command(
                root,
                session_path=session_path,
                task_id="cli-dry-run",
                harness_version="v1",
                model_version="model-v1",
                dry_run=True,
            )

        self.assertTrue(result.dry_run)
        self.assertEqual(result.session_id, "sess-cli")
        self.assertEqual(result.task_id, "cli-dry-run")
        self.assertEqual(result.event_count, 4)
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(result.verification_result_count, 1)
        self.assertIsNone(result.outcome)
        self.assertFalse((root / ".harness").exists())

    def test_record_mode_writes_replay_and_state_from_session(self):
        from personal_harness.codex_capture_command import capture_codex_session_command

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_path = root / "session.jsonl"
            _write_verified_session(session_path)

            result = capture_codex_session_command(
                root,
                session_path=session_path,
                task_id="cli-record",
                harness_version="v1",
                model_version="model-v1",
            )

            [record] = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())
            state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertFalse(result.dry_run)
        self.assertEqual(result.session_id, "sess-cli")
        self.assertTrue(result.outcome is not None and result.outcome.solved)
        self.assertEqual(result.outcome.stage, "task_complete")
        self.assertEqual(record.task_id, "cli-record")
        self.assertTrue(record.solved)
        self.assertEqual(state["phase"], "task_complete")

    def test_main_prints_json_result(self):
        from personal_harness.codex_capture_command import main

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_path = root / "session.jsonl"
            _write_verified_session(session_path)

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main([
                    "--root",
                    str(root),
                    "--session",
                    str(session_path),
                    "--task-id",
                    "cli-json",
                    "--harness-version",
                    "v1",
                    "--model-version",
                    "model-v1",
                    "--json",
                ])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["session_id"], "sess-cli")
        self.assertEqual(payload["task_id"], "cli-json")
        self.assertEqual(payload["stage"], "task_complete")
        self.assertTrue(payload["solved"])
        self.assertEqual(payload["event_count"], 4)

    def test_script_wrapper_runs_help(self):
        repo = Path(__file__).resolve().parents[1]

        completed = subprocess.run(
            ["python3", "scripts/harness-capture-codex", "--help"],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Capture Codex session", completed.stdout)

    def test_main_requires_explicit_session_or_latest(self):
        from personal_harness.codex_capture_command import main

        stderr = StringIO()
        with redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                with patch("sys.stderr", stderr):
                    main(["--harness-version", "v1", "--model-version", "model-v1"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("either --session or --latest is required", stderr.getvalue())

    def test_script_records_latest_session_for_cwd(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sessions_root = root / "codex-sessions"
            session_dir = sessions_root / "2026" / "07" / "01"
            session_dir.mkdir(parents=True)
            old_session = session_dir / "old.jsonl"
            new_session = session_dir / "new.jsonl"
            _write_verified_session(old_session)
            _write_verified_session(new_session)
            os.utime(old_session, (1, 1))
            os.utime(new_session, (2, 2))

            completed = subprocess.run(
                [
                    "python3",
                    "scripts/harness-capture-codex",
                    "--root",
                    str(root),
                    "--latest",
                    "--sessions-root",
                    str(sessions_root),
                    "--cwd",
                    str(session_dir),
                    "--task-id",
                    "script-latest",
                    "--harness-version",
                    "v1",
                    "--model-version",
                    "model-v1",
                    "--json",
                ],
                cwd=repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            payload = json.loads(completed.stdout)
            [record] = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["session_path"], str(new_session))
        self.assertEqual(payload["task_id"], "script-latest")
        self.assertTrue(payload["solved"])
        self.assertEqual(record.task_id, "script-latest")


if __name__ == "__main__":
    unittest.main()
