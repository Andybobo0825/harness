import json
import tempfile
import unittest
from pathlib import Path

from personal_harness.codex_agent import CodexCandidateResponse
from personal_harness.execution_controller import (
    AgentExecution,
    AgentExecutionController,
    ExecutionEvent,
    ToolCallResult,
    VerificationResult,
)
from personal_harness.launcher import mark_harness_session_started
from personal_harness.replay import ReplayStore


class TestAgentExecutionController(unittest.TestCase):
    def test_records_solved_execution_without_candidate_request(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            controller = AgentExecutionController(root, harness_version="v1", model_version="model-v1")

            outcome = controller.record_execution(
                AgentExecution(
                    task_id="task-pass",
                    model_output="implemented fix",
                    tool_calls=[ToolCallResult("apply_patch", 0, "patched")],
                    verification_results=[VerificationResult("python3 -m unittest", 0, "OK")],
                )
            )

            records = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())
            state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))
            candidate_request_exists = (root / ".harness" / "candidates" / "codex-candidate-request.json").exists()

        self.assertTrue(outcome.solved)
        self.assertEqual(outcome.failure_category, None)
        self.assertIsNone(outcome.candidate_request_path)
        self.assertFalse(candidate_request_exists)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].task_id, "task-pass")
        self.assertEqual(records[0].reward, 1.0)
        self.assertTrue(records[0].solved)
        self.assertEqual([event["type"] for event in records[0].events], ["model_output", "tool_call", "verification_result"])
        self.assertEqual(state["phase"], "task_complete")
        self.assertEqual(state["harness_version"], "v1")

    def test_failed_execution_writes_replay_and_codex_candidate_request(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            controller = AgentExecutionController(root, harness_version="v1", model_version="model-v1")

            outcome = controller.record_execution(
                AgentExecution(
                    task_id="task-fail",
                    model_output="attempted fix",
                    tool_calls=[ToolCallResult("python3", 0, "ran tests")],
                    verification_results=[VerificationResult("python3 -m unittest", 1, "FAILED")],
                )
            )

            records = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())
            request = json.loads((root / ".harness" / "candidates" / "codex-candidate-request.json").read_text(encoding="utf-8"))
            state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertFalse(outcome.solved)
        self.assertEqual(outcome.failure_category, "verification")
        self.assertEqual(outcome.stage, "candidate_requested")
        self.assertEqual(outcome.candidate_request_path, root / ".harness" / "candidates" / "codex-candidate-request.json")
        self.assertEqual(records[0].metadata["failure_category"], "verification")
        self.assertEqual(request["current_version"], "v1")
        self.assertEqual(request["target_tasks"], ["task-fail"])
        self.assertEqual(request["edit_buckets"], ["verification"])
        self.assertEqual(request["failure_categories"], {"task-fail": "verification"})
        self.assertEqual(outcome.request_id, request["request_id"])
        self.assertEqual(records[0].metadata["request_id"], request["request_id"])
        self.assertEqual(state["phase"], "candidate_requested")
        self.assertEqual(state["metadata"]["last_task"]["task_id"], "task-fail")
        self.assertEqual(state["metadata"]["last_task"]["request_id"], request["request_id"])

    def test_failed_execution_with_codex_response_runs_gate_and_updates_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            response_path = root / ".harness" / "candidates" / "codex-candidate-response.json"
            response_path.parent.mkdir(parents=True)
            response_path.write_text(
                json.dumps(
                    CodexCandidateResponse(
                        name="verification-candidate",
                        target_version="v2",
                        summary="fix verification failure",
                        expected_improvements={"task-fail"},
                        expected_regressions=set(),
                        smoke_ok=True,
                        request_id="controller:v1:task-fail:verification",
                    ).to_dict()
                ),
                encoding="utf-8",
            )
            controller = AgentExecutionController(
                root,
                harness_version="v1",
                model_version="model-v1",
                request_id_factory=lambda _execution, _failure_category: "controller:v1:task-fail:verification",
            )

            outcome = controller.record_execution(
                AgentExecution(
                    task_id="task-fail",
                    model_output="attempted fix",
                    tool_calls=[ToolCallResult("python3", 0, "ran tests")],
                    verification_results=[VerificationResult("python3 -m unittest", 1, "FAILED")],
                )
            )

            records = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())
            state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertFalse(outcome.solved)
        self.assertEqual(outcome.stage, "candidate_shipped")
        self.assertEqual(outcome.request_id, "controller:v1:task-fail:verification")
        self.assertTrue(outcome.evolution is not None and outcome.evolution.shipped)
        self.assertEqual(outcome.harness_version, "v2")
        self.assertEqual(state["harness_version"], "v2")
        self.assertEqual(state["phase"], "candidate_shipped")
        self.assertEqual(state["metadata"]["last_task"]["request_id"], "controller:v1:task-fail:verification")
        self.assertEqual(records[-1].metadata["record_type"], "evolution_decision")
        self.assertEqual(records[-1].metadata["decision"], "accepted")

    def test_rejects_stale_codex_response_for_different_request(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            response_path = root / ".harness" / "candidates" / "codex-candidate-response.json"
            response_path.parent.mkdir(parents=True)
            response_path.write_text(
                json.dumps({
                    "schema_version": "codex-agent-candidate-response/v1",
                    "llm_owner": "current-codex-agent",
                    "request_id": "controller:v1:old-task:verification",
                    "name": "stale-old-task",
                    "target_version": "v-stale",
                    "summary": "old response",
                    "expected_improvements": ["old-task"],
                    "expected_regressions": [],
                    "smoke_ok": True,
                }),
                encoding="utf-8",
            )
            controller = AgentExecutionController(root, harness_version="v1", model_version="model-v1")

            outcome = controller.record_execution(
                AgentExecution(
                    task_id="new-task",
                    model_output="attempted fix",
                    verification_results=[VerificationResult("python3 -m unittest", 1, "FAILED")],
                )
            )

            state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertEqual(outcome.stage, "candidate_requested")
        self.assertIsNone(outcome.evolution)
        self.assertEqual(outcome.harness_version, "v1")
        self.assertEqual(state["harness_version"], "v1")
        self.assertEqual(state["phase"], "candidate_requested")

    def test_caller_metadata_cannot_overwrite_controller_audit_fields(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            controller = AgentExecutionController(root, harness_version="v1", model_version="model-v1", variant_id="default")

            controller.record_execution(
                AgentExecution(
                    task_id="task-metadata",
                    model_output="attempted fix",
                    verification_results=[VerificationResult("python3 -m unittest", 1, "FAILED")],
                    metadata={
                        "record_type": "spoofed",
                        "variant_id": "spoofed",
                        "model_version": "spoofed",
                        "failure_category": "spoofed",
                        "note": "user supplied",
                    },
                )
            )

            [record] = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())

        self.assertEqual(record.metadata["record_type"], "agent_execution")
        self.assertEqual(record.metadata["variant_id"], "default")
        self.assertEqual(record.metadata["model_version"], "model-v1")
        self.assertEqual(record.metadata["failure_category"], "verification")
        self.assertEqual(record.metadata["execution_metadata"]["record_type"], "spoofed")
        self.assertEqual(record.metadata["execution_metadata"]["note"], "user supplied")

    def test_preserves_ordered_execution_events_when_provided(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            controller = AgentExecutionController(root, harness_version="v1", model_version="model-v1")

            controller.record_execution(
                AgentExecution(
                    task_id="task-ordered",
                    model_output="summary",
                    events=[
                        ExecutionEvent("model_output", {"content": "first"}, sequence=1, correlation_id="m1"),
                        ExecutionEvent("tool_call", {"name": "python3", "exit_code": 0}, sequence=2, correlation_id="t1"),
                        ExecutionEvent("model_output", {"content": "after tool"}, sequence=3, correlation_id="m2"),
                        ExecutionEvent("verification_result", {"command": "python3 -m unittest", "exit_code": 0}, sequence=4, correlation_id="v1"),
                    ],
                )
            )

            [record] = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())

        self.assertEqual([event["type"] for event in record.events], ["model_output", "tool_call", "model_output", "verification_result"])
        self.assertEqual([event["correlation_id"] for event in record.events], ["m1", "t1", "m2", "v1"])
        self.assertEqual(record.events[0]["payload"], {"content": "first"})

    def test_execution_event_payload_cannot_overwrite_replay_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            controller = AgentExecutionController(root, harness_version="v1", model_version="model-v1")

            controller.record_execution(
                AgentExecution(
                    task_id="task-envelope",
                    model_output="summary",
                    events=[
                        ExecutionEvent(
                            "model_output",
                            {"type": "spoofed", "content": "real"},
                            sequence=1,
                            correlation_id="m1",
                        ),
                        ExecutionEvent("verification_result", {"command": "python3 -m unittest", "exit_code": 0}, sequence=2),
                    ],
                )
            )

            [record] = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())

        self.assertEqual(record.events[0]["type"], "model_output")
        self.assertEqual(record.events[0]["payload"]["type"], "spoofed")

    def test_repeated_identical_failure_ignores_prior_matching_response_instance(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            response_path = root / ".harness" / "candidates" / "codex-candidate-response.json"
            response_path.parent.mkdir(parents=True)
            response_path.write_text(
                json.dumps({
                    "schema_version": "codex-agent-candidate-response/v1",
                    "llm_owner": "current-codex-agent",
                    "request_id": "old-request",
                    "name": "stale-repeat",
                    "target_version": "v-stale",
                    "summary": "old response for same task",
                    "expected_improvements": ["repeat-task"],
                    "expected_regressions": [],
                    "smoke_ok": True,
                }),
                encoding="utf-8",
            )
            controller = AgentExecutionController(
                root,
                harness_version="v1",
                model_version="model-v1",
                request_id_factory=lambda _execution, _failure_category: "new-request",
            )

            outcome = controller.record_execution(
                AgentExecution(
                    task_id="repeat-task",
                    model_output="attempted fix",
                    verification_results=[VerificationResult("python3 -m unittest", 1, "FAILED")],
                )
            )
            request = json.loads((root / ".harness" / "candidates" / "codex-candidate-request.json").read_text(encoding="utf-8"))

        self.assertEqual(outcome.stage, "candidate_requested")
        self.assertEqual(outcome.harness_version, "v1")
        self.assertEqual(request["request_id"], "new-request")

    def test_controller_state_preserves_launcher_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mark_harness_session_started(root, model="gpt-5.5", reasoning="high", yolo=True)
            controller = AgentExecutionController(root, harness_version="v1", model_version="model-v1")

            controller.record_execution(
                AgentExecution(
                    task_id="task-pass",
                    model_output="implemented fix",
                    verification_results=[VerificationResult("python3 -m unittest", 0, "OK")],
                )
            )
            state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertEqual(state["phase"], "task_complete")
        self.assertEqual(state["metadata"]["launch"]["model"], "gpt-5.5")
        self.assertEqual(state["metadata"]["status"]["command"], "harness-status")
        self.assertEqual(state["metadata"]["last_task"]["task_id"], "task-pass")

    def test_classifies_tool_missing_verification_and_model_failures(self):
        cases = [
            (
                "tool-task",
                AgentExecution(
                    task_id="tool-task",
                    model_output="attempted fix",
                    tool_calls=[ToolCallResult("python3", 2, "tool error")],
                    verification_results=[VerificationResult("python3 -m unittest", 0, "OK")],
                ),
                "tool",
            ),
            (
                "missing-verification-task",
                AgentExecution(
                    task_id="missing-verification-task",
                    model_output="attempted fix",
                    tool_calls=[ToolCallResult("apply_patch", 0, "patched")],
                ),
                "verification_missing",
            ),
            (
                "model-task",
                AgentExecution(
                    task_id="model-task",
                    model_output="summary did not satisfy task",
                    verification_results=[VerificationResult("python3 -m unittest", 0, "OK")],
                    accepted=False,
                ),
                "model",
            ),
        ]
        for _name, execution, expected_category in cases:
            with self.subTest(expected_category=expected_category):
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    controller = AgentExecutionController(root, harness_version="v1", model_version="model-v1")

                    outcome = controller.record_execution(execution)
                    [record] = list(ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all())

                self.assertFalse(outcome.solved)
                self.assertEqual(outcome.failure_category, expected_category)
                self.assertEqual(record.metadata["failure_category"], expected_category)


if __name__ == "__main__":
    unittest.main()
