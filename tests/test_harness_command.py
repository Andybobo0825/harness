import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from personal_harness.codex_agent import CodexAgentEvolver, CodexCandidateRequest
from personal_harness.harness_command import main, run_codex_candidate_gate
from personal_harness.memory import HOT_MEMORY_RELATIVE_PATH
from personal_harness.replay import ReplayStore, TrajectoryRecord


class TestHarnessCommand(unittest.TestCase):
    def test_command_records_flow_checkpoint_before_session_exit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--root",
                        str(root),
                        "--flow-checkpoint",
                        "--flow-id",
                        "fix-tests",
                        "--status",
                        "failed",
                        "--evidence",
                        "pytest failed before fix",
                        "--skill-context-json",
                        '{"selected":"systematic-debugging"}',
                        "--replay-ref",
                        ".harness/replay/replay.jsonl#L1",
                        "--candidate-ref",
                        ".harness/candidates/codex-candidate-request.json",
                        "--memory-category",
                        "correction",
                        "--memory-text",
                        "Tests must verify runtime TODO_FILE resolution.",
                        "--memory-source",
                        "flow:fix-tests",
                        "--json",
                    ]
                )

            checkpoint = json.loads((root / ".harness" / "flow-checkpoints" / "checkpoints.jsonl").read_text(encoding="utf-8").splitlines()[0])
            [record] = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())
            state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))
            output = json.loads(stdout.getvalue())
            hot = (root / HOT_MEMORY_RELATIVE_PATH).read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(checkpoint["flow_id"], "fix-tests")
        self.assertEqual(checkpoint["status"], "failed")
        self.assertEqual(checkpoint["memory"]["accepted"], True)
        self.assertEqual(checkpoint["skill_context"]["selected"], "systematic-debugging")
        self.assertEqual(record.task_id, "flow:fix-tests")
        self.assertEqual(record.metadata["record_type"], "flow_checkpoint")
        self.assertEqual(state["metadata"]["flow_checkpoints"][-1]["flow_id"], "fix-tests")
        self.assertEqual(state["metadata"]["memory"]["last_sync"]["accepted"], True)
        self.assertEqual(output["memory"]["accepted"], True)
        self.assertIn("Tests must verify runtime TODO_FILE resolution", hot)

    def test_command_can_run_memory_sync_without_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--root",
                        str(root),
                        "--memory-sync",
                        "--memory-category",
                        "verified-fact",
                        "--memory-text",
                        "capture-on-exit is a session-exit fallback, not the flow iteration driver.",
                        "--memory-source",
                        "user:memory-architecture",
                        "--json",
                    ]
                )

            output = json.loads(stdout.getvalue())
            hot = (root / HOT_MEMORY_RELATIVE_PATH).read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(output["memory"]["accepted"], True)
        self.assertIn("capture-on-exit is a session-exit fallback", hot)

    def test_command_flow_checkpoint_without_memory_does_not_create_memory_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "--root",
                        str(root),
                        "--flow-checkpoint",
                        "--flow-id",
                        "no-memory",
                        "--status",
                        "complete",
                        "--evidence",
                        "checkpoint only",
                        "--json",
                    ]
                )

            checkpoint = json.loads((root / ".harness" / "flow-checkpoints" / "checkpoints.jsonl").read_text(encoding="utf-8").splitlines()[0])
            memory_exists = (root / ".harness" / "memory").exists()

        self.assertEqual(exit_code, 0)
        self.assertNotIn("memory", checkpoint)
        self.assertFalse(memory_exists)

    def test_command_rejects_invalid_flow_checkpoint_skill_context(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stderr = StringIO()

            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "--root",
                        str(root),
                        "--flow-checkpoint",
                        "--flow-id",
                        "bad-json",
                        "--evidence",
                        "invalid skill context",
                        "--skill-context-json",
                        "[1]",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--skill-context-json must be a JSON object", stderr.getvalue())

    def test_command_reads_request_generates_response_and_runs_gate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            replay_path = root / ".harness" / "replay" / "replay.jsonl"
            ReplayStore(replay_path).append(TrajectoryRecord("task-a", "v1", 0.0, False))
            CodexAgentEvolver(root).write_request(
                CodexCandidateRequest(
                    current_version="v1",
                    target_tasks={"task-a"},
                    edit_buckets={"processor"},
                    failure_categories={"task-a": "processor"},
                    request_id="request-processor-1",
                )
            )

            result = run_codex_candidate_gate(root, replay_path=replay_path)
            response = json.loads((root / ".harness" / "candidates" / "codex-candidate-response.json").read_text(encoding="utf-8"))
            gate_result = json.loads((root / ".harness" / "candidates" / "codex-gate-result.json").read_text(encoding="utf-8"))
            records = list(ReplayStore(replay_path).read_all())

        self.assertTrue(result.accepted)
        self.assertEqual(result.harness_version, "v1+codex-processor")
        self.assertEqual(response["llm_owner"], "current-codex-agent")
        self.assertEqual(response["request_id"], "request-processor-1")
        self.assertEqual(gate_result["decision"], "accepted")
        self.assertEqual(gate_result["request_id"], "request-processor-1")
        self.assertEqual(gate_result["response_path"], ".harness/candidates/codex-candidate-response.json")
        self.assertEqual(records[-1].metadata["record_type"], "evolution_decision")

    def test_command_rejects_codex_response_that_regresses_solved_task(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            replay_path = root / ".harness" / "replay" / "replay.jsonl"
            ReplayStore(replay_path).append(TrajectoryRecord("solved-task", "v1", 1.0, True))
            CodexAgentEvolver(root).write_request(
                CodexCandidateRequest(
                    current_version="v1",
                    target_tasks={"new-task"},
                    edit_buckets={"processor"},
                    failure_categories={"new-task": "processor"},
                    request_id="request-new-task-1",
                )
            )
            response_path = root / ".harness" / "candidates" / "codex-candidate-response.json"
            response_path.write_text(json.dumps({
                "schema_version": "codex-agent-candidate-response/v1",
                "llm_owner": "current-codex-agent",
                "name": "bad-codex-edit",
                "target_version": "v2",
                "summary": "bad regression",
                "expected_improvements": ["new-task"],
                "expected_regressions": ["solved-task"],
                "smoke_ok": True,
                "request_id": "request-new-task-1",
            }), encoding="utf-8")

            result = run_codex_candidate_gate(root, replay_path=replay_path)
            gate_result = json.loads((root / ".harness" / "candidates" / "codex-gate-result.json").read_text(encoding="utf-8"))

        self.assertFalse(result.accepted)
        self.assertEqual(result.harness_version, "v1")
        self.assertEqual(gate_result["decision"], "rejected")
        self.assertEqual(gate_result["request_id"], "request-new-task-1")
        self.assertTrue(gate_result["rejections"][0]["reasons"][0].startswith("REJECT_SEESAW_REGRESSION"))

    def test_command_rejects_legacy_request_without_request_id(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            request_path = root / ".harness" / "candidates" / "codex-candidate-request.json"
            request_path.parent.mkdir(parents=True)
            request_path.write_text(json.dumps({
                "schema_version": "codex-agent-candidate-request/v1",
                "llm_owner": "current-codex-agent",
                "current_version": "v1",
                "target_tasks": ["current-task"],
                "edit_buckets": ["processor"],
                "failure_categories": {"current-task": "processor"},
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "missing request_id"):
                run_codex_candidate_gate(root)

    def test_command_ignores_mismatched_stale_response(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            replay_path = root / ".harness" / "replay" / "replay.jsonl"
            CodexAgentEvolver(root).write_request(
                CodexCandidateRequest(
                    current_version="v1",
                    target_tasks={"current-task"},
                    edit_buckets={"processor"},
                    failure_categories={"current-task": "processor"},
                    request_id="request-current-1",
                )
            )
            response_path = root / ".harness" / "candidates" / "codex-candidate-response.json"
            response_path.write_text(json.dumps({
                "schema_version": "codex-agent-candidate-response/v1",
                "llm_owner": "current-codex-agent",
                "name": "stale-other-task",
                "target_version": "v-stale",
                "summary": "old response",
                "expected_improvements": ["other-task"],
                "expected_regressions": [],
                "smoke_ok": True,
                "request_id": "request-other-1",
            }), encoding="utf-8")

            result = run_codex_candidate_gate(root, replay_path=replay_path)
            gate_result = json.loads((root / ".harness" / "candidates" / "codex-gate-result.json").read_text(encoding="utf-8"))

        self.assertFalse(result.accepted)
        self.assertEqual(result.harness_version, "v1")
        self.assertEqual(gate_result["decision"], "rejected")

    def test_script_wrapper_runs_from_repo_root(self):
        import subprocess

        completed = subprocess.run(
            ["python3", "scripts/harness-agent", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Run current-Codex-agent candidate response", completed.stdout)


if __name__ == "__main__":
    unittest.main()
