"""Standalone harness command: current Codex agent response -> gate -> replay audit."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping
import argparse
import json

from .codex_agent import CodexAgentEvolver, CodexCandidateRequest, CodexCandidateResponse
from .core import HarnessConfig
from .eval import EvaluationGate
from .evolution import EvolutionEngine
from .flow_checkpoint import record_flow_checkpoint
from .memory import ALLOWED_MEMORY_CATEGORIES, MemoryEntry, sync_checkpoint_memory
from .replay import ReplayStore

DEFAULT_REPLAY_PATH = Path(".harness") / "replay" / "replay.jsonl"
GATE_RESULT_PATH = Path(".harness") / "candidates" / "codex-gate-result.json"


@dataclass(frozen=True)
class CodexGateCommandResult:
    accepted: bool
    harness_version: str
    candidate: str | None
    result_path: Path


def _load_request(root: Path) -> CodexCandidateRequest:
    path = CodexAgentEvolver(root).request_path
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "codex-agent-candidate-request/v1":
        raise ValueError(f"Unsupported request schema: {data.get('schema_version')}")
    if data.get("llm_owner") != "current-codex-agent":
        raise ValueError(f"Unsupported llm_owner: {data.get('llm_owner')}")
    if "request_id" not in data:
        raise ValueError("Codex candidate request is missing request_id")
    return CodexCandidateRequest(
        current_version=str(data["current_version"]),
        target_tasks=set(map(str, data.get("target_tasks", []))),
        edit_buckets=set(map(str, data.get("edit_buckets", []))),
        failure_categories={str(k): str(v) for k, v in dict(data.get("failure_categories", {})).items()},
        request_id=str(data["request_id"]) if "request_id" in data else None,
    )


def _default_codex_agent_response(request: CodexCandidateRequest) -> CodexCandidateResponse:
    """Deterministic stand-in for the current Codex agent's candidate response.

    In an interactive run, the active Codex agent may overwrite the response file
    with a richer candidate before this command runs. If no response exists, this
    creates a conservative manifest-only candidate from the request.
    """

    bucket = sorted(request.edit_buckets)[0] if request.edit_buckets else "harness"
    target_version = f"{request.current_version}+codex-{bucket}"
    return CodexCandidateResponse(
        name=f"codex-{bucket}-candidate",
        target_version=target_version,
        summary=f"目前 Codex agent 針對 {bucket} failure 產生候選 harness edit",
        expected_improvements=set(request.target_tasks),
        expected_regressions=set(),
        smoke_ok=True,
        request_id=request.request_id,
    )


def _ensure_response(root: Path, request: CodexCandidateRequest, overwrite_response: bool = False) -> Path:
    evolver = CodexAgentEvolver(root)
    if overwrite_response or not evolver.response_path.exists():
        response = _default_codex_agent_response(request)
        evolver.response_path.parent.mkdir(parents=True, exist_ok=True)
        evolver.response_path.write_text(
            json.dumps(response.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return evolver.response_path


def _write_gate_result(root: Path, payload: Mapping[str, Any]) -> Path:
    path = root / GATE_RESULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def run_codex_candidate_gate(
    root: Path,
    *,
    replay_path: Path | None = None,
    overwrite_response: bool = False,
) -> CodexGateCommandResult:
    root = Path(root)
    request = _load_request(root)
    response_path = _ensure_response(root, request, overwrite_response=overwrite_response)

    replay = ReplayStore(replay_path if replay_path is not None else root / DEFAULT_REPLAY_PATH)
    candidates = CodexAgentEvolver(root).read_response_candidates(request_id=request.request_id)
    outcome = EvolutionEngine(EvaluationGate(replay)).evolve(HarnessConfig(request.current_version), candidates)

    payload = {
        "schema_version": "codex-agent-gate-result/v1",
        "decision": "accepted" if outcome.shipped else "rejected",
        "base_version": request.current_version,
        "harness_version": outcome.harness.version,
        "candidate": outcome.shipped_candidate,
        "request_id": request.request_id,
        "response_path": str(response_path.relative_to(root)),
        "rejections": [
            {"candidate": rejection.candidate, "reasons": rejection.reasons}
            for rejection in outcome.rejections
        ],
    }
    result_path = _write_gate_result(root, payload)
    return CodexGateCommandResult(outcome.shipped, outcome.harness.version, outcome.shipped_candidate, result_path)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run current-Codex-agent candidate response through standalone harness gate.")
    parser.add_argument("--root", default=".", help="Repository root containing .harness/candidates request/response files.")
    parser.add_argument("--replay", default=None, help="Replay JSONL path. Defaults to .harness/replay/replay.jsonl under --root.")
    parser.add_argument("--overwrite-response", action="store_true", help="Regenerate response from the current request before gating.")
    parser.add_argument("--flow-checkpoint", action="store_true", help="Record a major workflow checkpoint before Codex session exit.")
    parser.add_argument("--flow-id", default=None, help="Flow id for --flow-checkpoint.")
    parser.add_argument("--status", default="complete", help="Flow status for --flow-checkpoint.")
    parser.add_argument("--evidence", default=None, help="Human-readable evidence for --flow-checkpoint.")
    parser.add_argument("--skill-context-json", default="{}", help="JSON object recording requested/selected skill routing context.")
    parser.add_argument("--replay-ref", action="append", default=[], help="Replay reference attached to --flow-checkpoint. May be repeated.")
    parser.add_argument("--candidate-ref", action="append", default=[], help="Candidate artifact reference attached to --flow-checkpoint. May be repeated.")
    parser.add_argument("--memory-sync", action="store_true", help="Rotate harness memory and optionally write a selective memory entry.")
    parser.add_argument("--no-memory-sync", action="store_true", help="Disable checkpoint-driven memory sync for this flow checkpoint.")
    parser.add_argument("--memory-category", choices=sorted(ALLOWED_MEMORY_CATEGORIES), default=None, help="Selective memory category.")
    parser.add_argument("--memory-text", default=None, help="Concise durable memory text.")
    parser.add_argument("--memory-source", default=None, help="Memory source, for example flow:<id> or user:<topic>.")
    parser.add_argument("--memory-reason", default="", help="Optional verification or supersession reason for memory.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result.")
    args = parser.parse_args(argv)

    root = Path(args.root)
    memory_entry = _memory_entry_from_args(args, parser)
    if args.no_memory_sync and (args.memory_sync or memory_entry is not None):
        parser.error("--no-memory-sync cannot be combined with memory entry arguments or --memory-sync")
    if args.memory_sync and not args.flow_checkpoint:
        memory_result = sync_checkpoint_memory(root, entry=memory_entry)
        if args.json:
            print(json.dumps({"memory": memory_result.to_dict()}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"memory-sync: accepted={memory_result.accepted} reason={memory_result.reason} hot={memory_result.hot_path}")
        return 0

    if args.flow_checkpoint:
        if not args.flow_id:
            parser.error("--flow-id is required with --flow-checkpoint")
        if args.evidence is None:
            parser.error("--evidence is required with --flow-checkpoint")
        try:
            skill_context = json.loads(args.skill_context_json)
        except json.JSONDecodeError:
            parser.error("--skill-context-json must be a JSON object")
        if not isinstance(skill_context, dict):
            parser.error("--skill-context-json must be a JSON object")
        checkpoint_path = record_flow_checkpoint(
            root,
            flow_id=args.flow_id,
            status=args.status,
            evidence=args.evidence,
            skill_context=skill_context,
            replay_refs=args.replay_ref,
            candidate_refs=args.candidate_ref,
            memory_entry=memory_entry,
            sync_memory=(args.memory_sync or memory_entry is not None) and not args.no_memory_sync,
        )
        memory_payload = _checkpoint_memory_payload(checkpoint_path)
        if args.json:
            print(json.dumps({
                "checkpoint_path": str(checkpoint_path),
                "flow_id": args.flow_id,
                "status": args.status,
                "memory": memory_payload,
            }, ensure_ascii=False, sort_keys=True))
        else:
            print(
                f"flow-checkpoint: {args.flow_id} status={args.status} "
                f"path={checkpoint_path} memory={memory_payload.get('reason', 'unknown')}"
            )
        return 0

    replay_path = Path(args.replay) if args.replay else None
    result = run_codex_candidate_gate(root, replay_path=replay_path, overwrite_response=args.overwrite_response)
    if args.json:
        print(json.dumps({
            "accepted": result.accepted,
            "harness_version": result.harness_version,
            "candidate": result.candidate,
            "result_path": str(result.result_path),
        }, ensure_ascii=False, sort_keys=True))
    else:
        decision = "accepted" if result.accepted else "rejected"
        print(f"{decision}: harness_version={result.harness_version} result={result.result_path}")
    return 0 if result.accepted else 2


def _memory_entry_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> MemoryEntry | None:
    supplied = [args.memory_category is not None, args.memory_text is not None, args.memory_source is not None]
    if any(supplied) and not all(supplied):
        parser.error("--memory-category, --memory-text, and --memory-source must be provided together")
    if not any(supplied):
        return None
    return MemoryEntry(
        date="",
        category=args.memory_category,
        text=args.memory_text,
        source=args.memory_source,
        reason=args.memory_reason,
    )


def _checkpoint_memory_payload(checkpoint_path: Path) -> Mapping[str, Any]:
    try:
        lines = [line for line in checkpoint_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return {"accepted": False, "reason": "missing-checkpoint"}
        record = json.loads(lines[-1])
    except (OSError, json.JSONDecodeError):
        return {"accepted": False, "reason": "unreadable-checkpoint"}
    memory = record.get("memory")
    return memory if isinstance(memory, dict) else {"accepted": False, "reason": "disabled"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
