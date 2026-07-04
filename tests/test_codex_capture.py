import json
import os
import tempfile
import unittest
from pathlib import Path

from personal_harness.codex_capture import agent_execution_from_codex_session, find_latest_codex_session, record_codex_session
from personal_harness.replay import ReplayStore


def _write_jsonl(path: Path, records):
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


class TestCodexCapture(unittest.TestCase):
    def test_parses_session_jsonl_into_ordered_agent_execution(self):
        with tempfile.TemporaryDirectory() as d:
            session_path = Path(d) / "session.jsonl"
            _write_jsonl(
                session_path,
                [
                    {
                        "timestamp": "2026-06-30T10:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-1",
                            "cwd": "/repo",
                            "agent_role": "executor",
                            "cli_version": "0.1.0",
                        },
                    },
                    {
                        "timestamp": "2026-06-30T10:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "I will run tests."}],
                        },
                    },
                    {
                        "timestamp": "2026-06-30T10:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "functions.exec_command",
                            "arguments": json.dumps({"cmd": "python3 -m unittest tests.test_codex_capture -v"}),
                        },
                    },
                    {
                        "timestamp": "2026-06-30T10:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "Ran 1 test\n\nOK\nProcess exited with code 0",
                        },
                    },
                ],
            )

            execution = agent_execution_from_codex_session(session_path, task_id="capture-task")

        self.assertEqual(execution.task_id, "capture-task")
        self.assertEqual(execution.model_output, "I will run tests.")
        self.assertEqual(execution.metadata["source"], "codex_session_jsonl")
        self.assertEqual(execution.metadata["session_id"], "sess-1")
        self.assertEqual([event.event_type for event in execution.events], ["model_output", "tool_call", "tool_result", "verification_result"])
        self.assertEqual([event.sequence for event in execution.events], [1, 2, 3, 4])
        self.assertEqual(execution.events[1].correlation_id, "call-1")
        self.assertEqual(execution.events[2].payload["exit_code"], 0)
        self.assertEqual(execution.tool_calls[0].name, "functions.exec_command")
        self.assertEqual(execution.tool_calls[0].exit_code, 0)
        self.assertEqual(execution.tool_calls[0].metadata["command"], "python3 -m unittest tests.test_codex_capture -v")
        self.assertEqual(execution.verification_results[0].command, "python3 -m unittest tests.test_codex_capture -v")
        self.assertEqual(execution.verification_results[0].exit_code, 0)

    def test_missing_verification_becomes_unsolved_when_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_path = root / "session.jsonl"
            _write_jsonl(
                session_path,
                [
                    {
                        "timestamp": "2026-06-30T10:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "sess-missing"},
                    },
                    {
                        "timestamp": "2026-06-30T10:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "functions.exec_command",
                            "arguments": json.dumps({"cmd": "python3 scripts/generate.py"}),
                        },
                    },
                    {
                        "timestamp": "2026-06-30T10:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "generated\nProcess exited with code 0",
                        },
                    },
                ],
            )

            outcome = record_codex_session(
                root,
                session_path,
                harness_version="v1",
                model_version="model-v1",
                task_id="missing-verification",
            )

            [record] = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())
            request = json.loads((root / ".harness" / "candidates" / "codex-candidate-request.json").read_text(encoding="utf-8"))

        self.assertFalse(outcome.solved)
        self.assertEqual(outcome.failure_category, "verification_missing")
        self.assertEqual(record.metadata["failure_category"], "verification_missing")
        self.assertEqual(request["target_tasks"], ["missing-verification"])

    def test_searching_for_test_terms_does_not_count_as_verification(self):
        for command in ("rg pytest README.md", "cat pytest.ini", "grep unittest README.md"):
            with self.subTest(command=command):
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    session_path = root / "session.jsonl"
                    _write_jsonl(
                        session_path,
                        [
                            {"type": "session_meta", "payload": {"id": "sess-search"}},
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call",
                                    "call_id": "call-search",
                                    "name": "functions.exec_command",
                                    "arguments": json.dumps({"cmd": command}),
                                },
                            },
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call_output",
                                    "call_id": "call-search",
                                    "output": "pytest mentioned here\nProcess exited with code 0",
                                },
                            },
                        ],
                    )

                    outcome = record_codex_session(
                        root,
                        session_path,
                        harness_version="v1",
                        model_version="model-v1",
                        task_id="search-not-verification",
                    )

                self.assertFalse(outcome.solved)
                self.assertEqual(outcome.failure_category, "verification_missing")

    def test_verification_without_exit_status_is_not_solved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_path = root / "session.jsonl"
            _write_jsonl(
                session_path,
                [
                    {"type": "session_meta", "payload": {"id": "sess-no-exit"}},
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-verify",
                            "name": "functions.exec_command",
                            "arguments": json.dumps({"cmd": "pytest tests/test_codex_capture.py"}),
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-verify",
                            "output": "FAILED tests/test_codex_capture.py::test_example",
                        },
                    },
                ],
            )

            execution = agent_execution_from_codex_session(session_path, task_id="verification-no-exit")
            outcome = record_codex_session(
                root,
                session_path,
                harness_version="v1",
                model_version="model-v1",
                task_id="verification-no-exit",
            )

        self.assertEqual(execution.tool_calls[0].exit_code, 0)
        self.assertEqual(execution.tool_calls[0].metadata["exit_code_source"], "output_event_completed")
        self.assertEqual(execution.verification_results[0].exit_code, 1)
        self.assertEqual(execution.verification_results[0].metadata["exit_code_source"], "missing_verification_exit_code")
        self.assertFalse(outcome.solved)
        self.assertEqual(outcome.failure_category, "verification")

    def test_malformed_session_json_reports_line_number(self):
        with tempfile.TemporaryDirectory() as d:
            session_path = Path(d) / "session.jsonl"
            session_path.write_text('{"type": "session_meta", "payload": {}}\n{bad json}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "line 2"):
                agent_execution_from_codex_session(session_path, task_id="bad-session")

    def test_records_captured_session_through_controller(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_path = root / "session.jsonl"
            _write_jsonl(
                session_path,
                [
                    {
                        "timestamp": "2026-06-30T10:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "sess-pass"},
                    },
                    {
                        "timestamp": "2026-06-30T10:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Implemented and verified."}],
                        },
                    },
                    {
                        "timestamp": "2026-06-30T10:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-verify",
                            "name": "functions.exec_command",
                            "arguments": json.dumps({"cmd": "pytest tests/test_codex_capture.py"}),
                        },
                    },
                    {
                        "timestamp": "2026-06-30T10:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-verify",
                            "output": "1 passed\nProcess exited with code 0",
                        },
                    },
                ],
            )

            outcome = record_codex_session(
                root,
                session_path,
                harness_version="v1",
                model_version="model-v1",
                task_id="captured-pass",
            )

            [record] = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())
            candidate_request_exists = (root / ".harness" / "candidates" / "codex-candidate-request.json").exists()

        self.assertTrue(outcome.solved)
        self.assertEqual(outcome.stage, "task_complete")
        self.assertFalse(candidate_request_exists)
        self.assertTrue(record.solved)
        self.assertEqual(record.metadata["execution_metadata"]["source"], "codex_session_jsonl")
        self.assertEqual(record.events[-1]["type"], "verification_result")

    def test_tdd_red_then_green_verification_records_solved_execution(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_path = root / "session.jsonl"
            command = "python3 -m unittest -v"
            _write_jsonl(
                session_path,
                [
                    {"type": "session_meta", "payload": {"id": "sess-red-green"}},
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-red",
                            "name": "functions.exec_command",
                            "arguments": json.dumps({"cmd": command}),
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-red",
                            "output": "FAILED (errors=1)\nProcess exited with code 1",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-green",
                            "name": "functions.exec_command",
                            "arguments": json.dumps({"cmd": command}),
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-green",
                            "output": "Ran 1 test\n\nOK\nProcess exited with code 0",
                        },
                    },
                ],
            )

            execution = agent_execution_from_codex_session(session_path, task_id="red-green")
            outcome = record_codex_session(
                root,
                session_path,
                harness_version="v1",
                model_version="model-v1",
                task_id="red-green",
            )

            [record] = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())
            candidate_request_exists = (root / ".harness" / "candidates" / "codex-candidate-request.json").exists()

        self.assertEqual([result.exit_code for result in execution.verification_results], [1, 0])
        self.assertTrue(outcome.solved)
        self.assertEqual(outcome.stage, "task_complete")
        self.assertFalse(candidate_request_exists)
        self.assertTrue(record.solved)

    def test_finds_latest_session_for_relative_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cwd = root / "repo"
            sessions = root / "sessions" / "2026" / "06" / "30"
            cwd.mkdir()
            sessions.mkdir(parents=True)
            old_session = sessions / "old.jsonl"
            new_session = sessions / "new.jsonl"
            _write_jsonl(old_session, [{"type": "session_meta", "payload": {"cwd": str(cwd)}}])
            _write_jsonl(new_session, [{"type": "session_meta", "payload": {"cwd": str(cwd)}}])

            os.utime(old_session, (1, 1))
            os.utime(new_session, (2, 2))

            original_cwd = Path.cwd()
            try:
                os.chdir(cwd)
                latest = find_latest_codex_session(sessions, cwd=".")
            finally:
                os.chdir(original_cwd)

        self.assertEqual(latest, new_session)

    def test_finds_launch_bounded_session_for_cwd_instead_of_newest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cwd = root / "repo"
            other_cwd = root / "other"
            sessions = root / "sessions" / "2026" / "07" / "01"
            cwd.mkdir()
            other_cwd.mkdir()
            sessions.mkdir(parents=True)
            old_same = sessions / "old-same.jsonl"
            target = sessions / "target.jsonl"
            later_same = sessions / "later-same.jsonl"
            other_project = sessions / "other-project.jsonl"
            _write_jsonl(
                old_same,
                [{"timestamp": "2026-07-01T01:59:59Z", "type": "session_meta", "payload": {"id": "old", "cwd": str(cwd)}}],
            )
            _write_jsonl(
                target,
                [{"timestamp": "2026-07-01T02:00:01Z", "type": "session_meta", "payload": {"id": "target", "cwd": str(cwd)}}],
            )
            _write_jsonl(
                later_same,
                [{"timestamp": "2026-07-01T02:00:10Z", "type": "session_meta", "payload": {"id": "later", "cwd": str(cwd)}}],
            )
            _write_jsonl(
                other_project,
                [{"timestamp": "2026-07-01T02:00:00Z", "type": "session_meta", "payload": {"id": "other", "cwd": str(other_cwd)}}],
            )
            os.utime(old_same, (4, 4))
            os.utime(target, (2, 2))
            os.utime(later_same, (5, 5))
            os.utime(other_project, (6, 6))

            selected = find_latest_codex_session(
                sessions,
                cwd=cwd,
                started_after="2026-07-01T02:00:00Z",
            )

        self.assertEqual(selected, target)

    def test_launch_bounded_session_selection_can_exclude_captured_session_ids(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cwd = root / "repo"
            sessions = root / "sessions"
            cwd.mkdir()
            sessions.mkdir()
            first = sessions / "first.jsonl"
            second = sessions / "second.jsonl"
            _write_jsonl(
                first,
                [{"timestamp": "2026-07-01T02:00:01Z", "type": "session_meta", "payload": {"id": "sess-first", "cwd": str(cwd)}}],
            )
            _write_jsonl(
                second,
                [{"timestamp": "2026-07-01T02:00:02Z", "type": "session_meta", "payload": {"id": "sess-second", "cwd": str(cwd)}}],
            )

            selected = find_latest_codex_session(
                sessions,
                cwd=cwd,
                started_after="2026-07-01T02:00:00Z",
                exclude_session_ids={"sess-first"},
            )

        self.assertEqual(selected, second)


if __name__ == "__main__":
    unittest.main()
