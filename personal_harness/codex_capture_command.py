"""Command-line entrypoint for recording Codex session captures into harness."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, List, Mapping
import argparse
import json

from .codex_capture import agent_execution_from_codex_session, find_latest_codex_session
from .execution_controller import AgentExecutionController, AgentExecutionOutcome


@dataclass(frozen=True)
class CodexCaptureCommandResult:
    session_path: Path
    session_id: str | None
    task_id: str
    event_count: int
    tool_call_count: int
    verification_result_count: int
    dry_run: bool
    outcome: AgentExecutionOutcome | None = None

    def to_dict(self) -> Mapping[str, Any]:
        payload = {
            "session_path": str(self.session_path),
            "session_id": self.session_id,
            "task_id": self.task_id,
            "event_count": self.event_count,
            "tool_call_count": self.tool_call_count,
            "verification_result_count": self.verification_result_count,
            "dry_run": self.dry_run,
        }
        if self.outcome is not None:
            payload.update(
                {
                    "solved": self.outcome.solved,
                    "reward": self.outcome.reward,
                    "failure_category": self.outcome.failure_category,
                    "stage": self.outcome.stage,
                    "harness_version": self.outcome.harness_version,
                    "request_id": self.outcome.request_id,
                    "candidate_request_path": (
                        str(self.outcome.candidate_request_path)
                        if self.outcome.candidate_request_path is not None
                        else None
                    ),
                }
            )
        return payload


def capture_codex_session_command(
    root: Path | str,
    *,
    session_path: Path | str | None = None,
    sessions_root: Path | str | None = None,
    cwd: Path | str | None = None,
    started_after: float | str | None = None,
    exclude_session_ids: Collection[str] | None = None,
    task_id: str | None = None,
    harness_version: str,
    model_version: str,
    variant_id: str = "default",
    dry_run: bool = False,
) -> CodexCaptureCommandResult:
    root_path = Path(root).expanduser().resolve()
    selected_session = _resolve_session_path(
        session_path=session_path,
        sessions_root=sessions_root,
        cwd=cwd or root_path,
        started_after=started_after,
        exclude_session_ids=exclude_session_ids,
    )
    execution = agent_execution_from_codex_session(selected_session, task_id=task_id)
    session_id = execution.metadata.get("session_id")
    session_id = str(session_id) if session_id is not None else None
    if dry_run:
        return CodexCaptureCommandResult(
            session_path=selected_session,
            session_id=session_id,
            task_id=execution.task_id,
            event_count=len(execution.events),
            tool_call_count=len(execution.tool_calls),
            verification_result_count=len(execution.verification_results),
            dry_run=True,
        )

    outcome = AgentExecutionController(
        root_path,
        harness_version=harness_version,
        model_version=model_version,
        variant_id=variant_id,
    ).record_execution(execution)
    return CodexCaptureCommandResult(
        session_path=selected_session,
        session_id=session_id,
        task_id=execution.task_id,
        event_count=len(execution.events),
        tool_call_count=len(execution.tool_calls),
        verification_result_count=len(execution.verification_results),
        dry_run=False,
        outcome=outcome,
    )


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture Codex session JSONL into the standalone harness runtime.")
    parser.add_argument("--root", default=".", help="Project root that owns the .harness runtime.")
    parser.add_argument("--session", default=None, help="Explicit Codex session JSONL path to capture.")
    parser.add_argument("--latest", action="store_true", help="Capture the latest Codex session under --sessions-root.")
    parser.add_argument("--sessions-root", default=None, help="Codex sessions root. Defaults to ~/.codex/sessions.")
    parser.add_argument("--cwd", default=None, help="When using --latest, select the newest session whose session cwd matches this path. Defaults to --root.")
    parser.add_argument("--started-after", default=None, help="When using --latest, select the first matching session after this epoch or ISO timestamp.")
    parser.add_argument("--exclude-session-id", action="append", default=[], help="Session id to skip when using --latest. Can be repeated.")
    parser.add_argument("--task-id", default=None, help="Task id to record. Defaults to session id or filename.")
    parser.add_argument("--harness-version", required=True, help="Harness version to attach to replay/state records.")
    parser.add_argument("--model-version", required=True, help="Model version to attach to replay/state records.")
    parser.add_argument("--variant-id", default="default", help="Harness variant id. Default: default.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and summarize the session without writing .harness state.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result.")
    args = parser.parse_args(argv)

    session_path = Path(args.session) if args.session else None
    if args.latest and session_path is not None:
        parser.error("--latest cannot be combined with --session")
    if not args.latest and session_path is None:
        parser.error("either --session or --latest is required")
    result = capture_codex_session_command(
        args.root,
        session_path=session_path,
        sessions_root=args.sessions_root,
        cwd=args.cwd,
        started_after=args.started_after,
        exclude_session_ids=args.exclude_session_id,
        task_id=args.task_id,
        harness_version=args.harness_version,
        model_version=args.model_version,
        variant_id=args.variant_id,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        _print_human_result(result)
    return 0


def _resolve_session_path(
    *,
    session_path: Path | str | None,
    sessions_root: Path | str | None,
    cwd: Path | str | None,
    started_after: float | str | None,
    exclude_session_ids: Collection[str] | None,
) -> Path:
    if session_path is not None:
        return Path(session_path).expanduser()
    return find_latest_codex_session(
        base=sessions_root,
        cwd=cwd,
        started_after=started_after,
        exclude_session_ids=exclude_session_ids,
    )


def _print_human_result(result: CodexCaptureCommandResult) -> None:
    prefix = "dry-run" if result.dry_run else "recorded"
    parts = [
        f"{prefix}: task_id={result.task_id}",
        f"session_id={result.session_id}",
        f"events={result.event_count}",
        f"tools={result.tool_call_count}",
        f"verification={result.verification_result_count}",
        f"session={result.session_path}",
    ]
    if result.outcome is not None:
        parts.extend(
            [
                f"stage={result.outcome.stage}",
                f"solved={str(result.outcome.solved).lower()}",
            ]
        )
        if result.outcome.failure_category:
            parts.append(f"failure={result.outcome.failure_category}")
    print(" ".join(parts))


__all__ = [
    "CodexCaptureCommandResult",
    "capture_codex_session_command",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
