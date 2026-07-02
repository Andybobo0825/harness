"""Agent execution controller for closing the personal harness feedback loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Mapping, Sequence
import time
from uuid import uuid4

from .codex_agent import CodexAgentEvolver, CodexCandidateRequest
from .core import HarnessConfig
from .eval import EvaluationGate
from .evolution import EvolutionEngine, EvolutionOutcome
from .harness_state import PersonalHarnessRuntimeState, read_personal_harness_state, write_personal_harness_state
from .replay import ReplayStore, TrajectoryRecord


DEFAULT_REPLAY_PATH = Path(".harness") / "replay" / "replay.jsonl"


@dataclass(frozen=True)
class ExecutionEvent:
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    sequence: int | None = None
    correlation_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> Mapping[str, Any]:
        event = {"type": self.event_type, "payload": dict(self.payload)}
        if self.sequence is not None:
            event["sequence"] = self.sequence
        if self.correlation_id is not None:
            event["correlation_id"] = self.correlation_id
        if self.metadata:
            event["metadata"] = dict(self.metadata)
        return event


@dataclass(frozen=True)
class ToolCallResult:
    name: str
    exit_code: int
    output: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_event(self) -> Mapping[str, Any]:
        return {
            "type": "tool_call",
            "name": self.name,
            "exit_code": self.exit_code,
            "output": self.output,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class VerificationResult:
    command: str
    exit_code: int
    output: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_event(self) -> Mapping[str, Any]:
        return {
            "type": "verification_result",
            "command": self.command,
            "exit_code": self.exit_code,
            "output": self.output,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AgentExecution:
    task_id: str
    model_output: str
    tool_calls: Sequence[ToolCallResult] = ()
    verification_results: Sequence[VerificationResult] = ()
    events: Sequence[ExecutionEvent] = ()
    accepted: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentExecutionOutcome:
    task_id: str
    solved: bool
    reward: float
    failure_category: str | None
    stage: str
    harness_version: str
    candidate_request_path: Path | None = None
    request_id: str | None = None
    evolution: EvolutionOutcome | None = None


class AgentExecutionController:
    """Coordinates execution evidence into replay, candidate, gate, and state updates.

    The controller consumes an explicit execution transcript instead of pretending
    to intercept private Codex internals. Codex or another frontend can later
    populate AgentExecution from real model/tool/test events.
    """

    def __init__(
        self,
        root: Path,
        *,
        harness_version: str,
        model_version: str,
        variant_id: str = "default",
        replay_path: Path | None = None,
        request_id_factory: Callable[[AgentExecution, str], str] | None = None,
    ):
        self.root = Path(root)
        self.harness_version = harness_version
        self.model_version = model_version
        self.variant_id = variant_id
        self.request_id_factory = request_id_factory
        self.replay = ReplayStore(replay_path if replay_path is not None else self.root / DEFAULT_REPLAY_PATH)
        self.codex_evolver = CodexAgentEvolver(self.root)

    def record_execution(self, execution: AgentExecution) -> AgentExecutionOutcome:
        solved = self._is_solved(execution)
        reward = 1.0 if solved else 0.0
        failure_category = None if solved else self._classify_failure(execution)
        request_id = None if solved else self._request_id(execution, failure_category)
        events = self._events_for(execution)
        metadata = self._metadata_for(execution, failure_category, request_id)
        self.replay.append(
            TrajectoryRecord(
                task_id=execution.task_id,
                harness_version=self.harness_version,
                reward=reward,
                solved=solved,
                events=events,
                metadata=metadata,
            )
        )

        if solved:
            self._write_state("task_complete", execution, self.harness_version, failure_category, request_id)
            return AgentExecutionOutcome(
                task_id=execution.task_id,
                solved=True,
                reward=reward,
                failure_category=None,
                stage="task_complete",
                harness_version=self.harness_version,
            )

        request_path = self._write_candidate_request(execution, failure_category, request_id)
        candidates = self.codex_evolver.read_response_candidates(request_id=request_id)
        if not candidates:
            self._write_state("candidate_requested", execution, self.harness_version, failure_category, request_id)
            return AgentExecutionOutcome(
                task_id=execution.task_id,
                solved=False,
                reward=reward,
                failure_category=failure_category,
                stage="candidate_requested",
                harness_version=self.harness_version,
                candidate_request_path=request_path,
                request_id=request_id,
            )

        evolution = EvolutionEngine(EvaluationGate(self.replay)).evolve(HarnessConfig(self.harness_version), candidates)
        stage = "candidate_shipped" if evolution.shipped else "candidate_rejected"
        next_version = evolution.harness.version
        self._write_state(stage, execution, next_version, failure_category, request_id)
        return AgentExecutionOutcome(
            task_id=execution.task_id,
            solved=False,
            reward=reward,
            failure_category=failure_category,
            stage=stage,
            harness_version=next_version,
            candidate_request_path=request_path,
            request_id=request_id,
            evolution=evolution,
        )

    def _is_solved(self, execution: AgentExecution) -> bool:
        tool_exit_codes = self._tool_exit_codes(execution)
        verification_exit_codes = self._verification_exit_codes(execution)
        if not verification_exit_codes:
            return False
        tools_ok = all(exit_code == 0 for exit_code in tool_exit_codes)
        verification_ok = all(exit_code == 0 for exit_code in verification_exit_codes)
        return tools_ok and verification_ok and execution.accepted

    def _classify_failure(self, execution: AgentExecution) -> str:
        tool_exit_codes = self._tool_exit_codes(execution)
        verification_exit_codes = self._verification_exit_codes(execution)
        if any(exit_code != 0 for exit_code in tool_exit_codes):
            return "tool"
        if not verification_exit_codes:
            return "verification_missing"
        if any(exit_code != 0 for exit_code in verification_exit_codes):
            return "verification"
        return "model"

    def _events_for(self, execution: AgentExecution) -> List[Mapping[str, Any]]:
        if execution.events:
            return [event.to_record() for event in execution.events]
        events: List[Mapping[str, Any]] = [{"type": "model_output", "content": execution.model_output}]
        events.extend(result.to_event() for result in execution.tool_calls)
        events.extend(result.to_event() for result in execution.verification_results)
        return events

    def _metadata_for(self, execution: AgentExecution, failure_category: str | None, request_id: str | None) -> Mapping[str, Any]:
        metadata = {
            "record_type": "agent_execution",
            "variant_id": self.variant_id,
            "model_version": self.model_version,
        }
        if failure_category is not None:
            metadata["failure_category"] = failure_category
        if request_id is not None:
            metadata["request_id"] = request_id
        if execution.metadata:
            metadata["execution_metadata"] = dict(execution.metadata)
        return metadata

    def _write_candidate_request(self, execution: AgentExecution, failure_category: str, request_id: str) -> Path:
        return self.codex_evolver.write_request(
            CodexCandidateRequest(
                current_version=self.harness_version,
                target_tasks={execution.task_id},
                edit_buckets={failure_category},
                failure_categories={execution.task_id: failure_category},
                request_id=request_id,
            )
        )

    def _request_id(self, execution: AgentExecution, failure_category: str) -> str:
        if self.request_id_factory is not None:
            return self.request_id_factory(execution, failure_category)
        return f"controller:{self.harness_version}:{execution.task_id}:{failure_category}:{uuid4().hex}"

    def _tool_exit_codes(self, execution: AgentExecution) -> List[int]:
        exit_codes = [result.exit_code for result in execution.tool_calls]
        for event in execution.events:
            if event.event_type == "tool_call" and "exit_code" in event.payload:
                exit_codes.append(int(event.payload["exit_code"]))
        return exit_codes

    def _verification_exit_codes(self, execution: AgentExecution) -> List[int]:
        exit_codes = [result.exit_code for result in execution.verification_results]
        for event in execution.events:
            if event.event_type == "verification_result" and "exit_code" in event.payload:
                exit_codes.append(int(event.payload["exit_code"]))
        return exit_codes

    def _write_state(
        self,
        phase: str,
        execution: AgentExecution,
        harness_version: str,
        failure_category: str | None,
        request_id: str | None,
    ) -> Path:
        try:
            metadata = dict(read_personal_harness_state(self.root).metadata)
        except (FileNotFoundError, ValueError, KeyError):
            metadata = {}
        metadata.update(
            {
                "runtime_owner": "standalone-.harness",
                "llm_backend": "current-codex-agent",
                "last_task": {
                    "task_id": execution.task_id,
                    "solved": failure_category is None,
                    "failure_category": failure_category,
                    "request_id": request_id,
                    "recorded_at": time.time(),
                },
            }
        )
        return write_personal_harness_state(
            self.root,
            PersonalHarnessRuntimeState(
                active=True,
                harness_version=harness_version,
                model_version=self.model_version,
                variant_id=self.variant_id,
                phase=phase,
                metadata=metadata,
            ),
        )


__all__ = [
    "AgentExecution",
    "AgentExecutionController",
    "AgentExecutionOutcome",
    "ExecutionEvent",
    "ToolCallResult",
    "VerificationResult",
]
