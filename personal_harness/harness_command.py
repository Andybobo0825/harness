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
    parser.add_argument("--json", action="store_true", help="Print machine-readable result.")
    args = parser.parse_args(argv)

    root = Path(args.root)
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
