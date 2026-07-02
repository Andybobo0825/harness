import json
import tempfile
import unittest
from pathlib import Path

from personal_harness.codex_agent import CodexAgentEvolver, CodexCandidateRequest
from personal_harness.harness_command import run_codex_candidate_gate
from personal_harness.replay import ReplayStore, TrajectoryRecord


class TestHarnessCommand(unittest.TestCase):
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
