"""Capture Codex session JSONL into harness execution transcripts."""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Dict, Iterable, List, Mapping

from .execution_controller import (
    AgentExecution,
    AgentExecutionController,
    AgentExecutionOutcome,
    ExecutionEvent,
    ToolCallResult,
    VerificationResult,
)


CODEX_SESSIONS_PATH = Path.home() / ".codex" / "sessions"
_EXIT_CODE_PATTERNS = (
    re.compile(r"Process exited with code\s+(-?\d+)"),
    re.compile(r"Exit code:\s*(-?\d+)"),
)
_DIRECT_VERIFICATION_TOOLS = {
    "pytest",
    "unittest",
    "compileall",
    "py_compile",
    "ruff",
    "mypy",
    "pyright",
    "tsc",
    "eslint",
}
_PYTHON_MODULE_VERIFIERS = {"pytest", "unittest", "compileall", "py_compile", "ruff", "mypy"}
_RUN_WRAPPERS = {"uv", "poetry", "pipenv", "rye"}


def agent_execution_from_codex_session(
    session_path: Path | str,
    *,
    task_id: str | None = None,
    accepted: bool = True,
) -> AgentExecution:
    """Convert a Codex session JSONL file into an AgentExecution transcript."""

    path = Path(session_path).expanduser()
    metadata: Dict[str, Any] = {
        "source": "codex_session_jsonl",
        "source_path": str(path),
    }
    events: List[ExecutionEvent] = []
    tool_calls: List[ToolCallResult] = []
    verification_results: List[VerificationResult] = []
    assistant_messages: List[str] = []
    pending_calls: Dict[str, Mapping[str, Any]] = {}
    sequence = 0

    for line_number, record in _read_jsonl(path):
        timestamp = record.get("timestamp")
        record_type = record.get("type")
        payload = _mapping_or_empty(record.get("payload"))

        if record_type == "session_meta":
            _merge_session_metadata(metadata, payload)
            continue

        if record_type != "response_item":
            continue

        payload_type = payload.get("type")
        if payload_type == "message":
            if payload.get("role") != "assistant":
                continue
            content = _content_text(payload.get("content"))
            if not content:
                continue
            assistant_messages.append(content)
            sequence += 1
            events.append(
                ExecutionEvent(
                    "model_output",
                    {"role": "assistant", "content": content},
                    sequence=sequence,
                    correlation_id=str(payload.get("id")) if payload.get("id") is not None else None,
                    metadata=_event_metadata(timestamp, line_number),
                )
            )
            continue

        if payload_type == "function_call":
            call_id = str(payload.get("call_id") or payload.get("id") or f"call-{line_number}")
            arguments = _parse_arguments(payload.get("arguments"))
            name = str(payload.get("name") or "unknown")
            command = _command_from_arguments(arguments)
            pending_calls[call_id] = {
                "name": name,
                "arguments": arguments,
                "command": command,
                "timestamp": timestamp,
                "line_number": line_number,
            }
            event_payload: Dict[str, Any] = {"name": name, "arguments": arguments}
            if command is not None:
                event_payload["command"] = command
            sequence += 1
            events.append(
                ExecutionEvent(
                    "tool_call",
                    event_payload,
                    sequence=sequence,
                    correlation_id=call_id,
                    metadata=_event_metadata(timestamp, line_number),
                )
            )
            continue

        if payload_type == "function_call_output":
            call_id = str(payload.get("call_id") or f"call-{line_number}")
            output = _string_or_empty(payload.get("output"))
            call = pending_calls.get(call_id, {})
            name = str(call.get("name") or "unknown")
            command = call.get("command")
            exit_code, exit_code_source = _parse_exit_code(output)
            is_verification = command is not None and _looks_like_verification(str(command))
            tool_metadata = {
                "call_id": call_id,
                "exit_code_source": exit_code_source,
                "line_number": line_number,
            }
            if command is not None:
                tool_metadata["command"] = str(command)
            if timestamp is not None:
                tool_metadata["timestamp"] = timestamp
            tool_calls.append(ToolCallResult(name, exit_code, output, metadata=tool_metadata))

            result_payload: Dict[str, Any] = {
                "name": name,
                "exit_code": exit_code,
                "output": output,
            }
            if command is not None:
                result_payload["command"] = command
            sequence += 1
            events.append(
                ExecutionEvent(
                    "tool_result",
                    result_payload,
                    sequence=sequence,
                    correlation_id=call_id,
                    metadata=_event_metadata(timestamp, line_number),
                )
            )

            if is_verification:
                verification_exit_code = exit_code
                verification_metadata = dict(tool_metadata)
                if exit_code_source != "tool_output":
                    verification_exit_code = 1
                    verification_metadata["exit_code_source"] = "missing_verification_exit_code"
                verification_results.append(
                    VerificationResult(str(command), verification_exit_code, output, metadata=verification_metadata)
                )
                sequence += 1
                events.append(
                    ExecutionEvent(
                        "verification_result",
                        {"command": str(command), "exit_code": verification_exit_code, "output": output},
                        sequence=sequence,
                        correlation_id=call_id,
                        metadata=_event_metadata(timestamp, line_number),
                    )
                )

    session_id = metadata.get("session_id")
    metadata.update(
        {
            "event_count": len(events),
            "tool_call_count": len(tool_calls),
            "verification_result_count": len(verification_results),
        }
    )
    return AgentExecution(
        task_id=task_id or str(session_id or path.stem),
        model_output="\n\n".join(assistant_messages),
        tool_calls=tuple(tool_calls),
        verification_results=tuple(verification_results),
        events=tuple(events),
        accepted=accepted,
        metadata=metadata,
    )


def record_codex_session(
    root: Path | str,
    session_path: Path | str,
    *,
    harness_version: str,
    model_version: str,
    task_id: str | None = None,
    accepted: bool = True,
    variant_id: str = "default",
) -> AgentExecutionOutcome:
    """Parse a Codex session JSONL file and record it through the controller."""

    execution = agent_execution_from_codex_session(session_path, task_id=task_id, accepted=accepted)
    controller = AgentExecutionController(
        Path(root).expanduser(),
        harness_version=harness_version,
        model_version=model_version,
        variant_id=variant_id,
    )
    return controller.record_execution(execution)


def find_latest_codex_session(
    base: Path | str | None = None,
    *,
    cwd: Path | str | None = None,
    started_after: float | str | None = None,
    exclude_session_ids: Collection[str] | None = None,
) -> Path:
    """Return a Codex session JSONL, optionally bounded to the current harness launch."""

    root = Path(base).expanduser() if base is not None else CODEX_SESSIONS_PATH
    paths = list(root.rglob("*.jsonl"))
    if not paths:
        raise FileNotFoundError(f"No Codex session JSONL files found under {root}")

    started_after_epoch = _timestamp_to_epoch(started_after) if started_after is not None else None
    excluded = {str(session_id) for session_id in (exclude_session_ids or ())}
    expected_cwd = str(Path(cwd).expanduser().resolve()) if cwd is not None else None
    candidates = []
    for path in paths:
        stat = path.stat()
        summary = _session_summary(path)
        if expected_cwd is not None and summary.get("cwd") != expected_cwd:
            continue
        session_id = summary.get("session_id")
        if session_id is not None and str(session_id) in excluded:
            continue
        session_epoch = summary.get("first_timestamp_epoch")
        candidate_epoch = session_epoch if isinstance(session_epoch, float) else stat.st_mtime
        if started_after_epoch is not None and candidate_epoch < started_after_epoch:
            continue
        candidates.append((path, candidate_epoch, stat.st_mtime))

    if not candidates:
        if expected_cwd is None:
            raise FileNotFoundError(f"No Codex session JSONL files found under {root}")
        raise FileNotFoundError(f"No Codex session JSONL files found under {root} for cwd {expected_cwd}")

    if started_after_epoch is None:
        candidates.sort(key=lambda item: item[2], reverse=True)
    else:
        candidates.sort(key=lambda item: (item[1], item[2]))
    return candidates[0][0]


def _read_jsonl(path: Path) -> Iterable[tuple[int, Mapping[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Malformed Codex session JSONL at line {line_number}: {exc.msg}") from exc
            if not isinstance(record, Mapping):
                raise ValueError(f"Malformed Codex session JSONL at line {line_number}: expected object")
            yield line_number, record


def _merge_session_metadata(metadata: Dict[str, Any], payload: Mapping[str, Any]) -> None:
    field_map = {
        "id": "session_id",
        "thread_id": "thread_id",
        "cwd": "cwd",
        "agent_role": "agent_role",
        "agent_nickname": "agent_nickname",
        "cli_version": "cli_version",
    }
    for source, target in field_map.items():
        value = payload.get(source)
        if value is not None:
            metadata[target] = value


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, Mapping):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(part for part in parts if part)


def _parse_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return arguments


def _command_from_arguments(arguments: Any) -> str | None:
    if not isinstance(arguments, Mapping):
        return None
    for key in ("cmd", "command"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_exit_code(output: str) -> tuple[int, str]:
    for pattern in _EXIT_CODE_PATTERNS:
        match = pattern.search(output)
        if match is not None:
            return int(match.group(1)), "tool_output"
    return 0, "output_event_completed"


def _looks_like_verification(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return _tokens_are_verification(_strip_env_prefix(tokens))


def _tokens_are_verification(tokens: list[str]) -> bool:
    if not tokens:
        return False
    executable = Path(tokens[0]).name.lower()
    args = tokens[1:]

    if executable in _RUN_WRAPPERS and args[:1] == ["run"]:
        return _tokens_are_verification(args[1:])
    if executable in {"npx", "bun", "deno"} and args[:1] == ["test"]:
        return True
    if executable == "npx" and args:
        return Path(args[0]).name.lower() in _DIRECT_VERIFICATION_TOOLS
    if executable.startswith("python") and len(args) >= 2 and args[0] == "-m":
        return args[1].lower() in _PYTHON_MODULE_VERIFIERS
    if executable in _DIRECT_VERIFICATION_TOOLS:
        return True
    if executable in {"npm", "pnpm"}:
        return args[:1] == ["test"] or args[:2] == ["run", "test"]
    if executable in {"yarn", "cargo", "go", "mix", "swift"}:
        return args[:1] == ["test"]
    if executable == "xcodebuild":
        return "test" in args
    return False


def _strip_env_prefix(tokens: list[str]) -> list[str]:
    index = 0
    if tokens and Path(tokens[0]).name.lower() == "env":
        index = 1
    while index < len(tokens) and _looks_like_assignment(tokens[index]):
        index += 1
    return tokens[index:]


def _looks_like_assignment(token: str) -> bool:
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token) is not None


def _session_summary(path: Path) -> Mapping[str, Any]:
    summary: Dict[str, Any] = {}
    for _line_number, record in _read_jsonl(path):
        if "first_timestamp_epoch" not in summary:
            timestamp_epoch = _timestamp_to_epoch(record.get("timestamp"))
            if timestamp_epoch is not None:
                summary["first_timestamp_epoch"] = timestamp_epoch
        if record.get("type") == "session_meta":
            payload = _mapping_or_empty(record.get("payload"))
            cwd = payload.get("cwd")
            session_id = payload.get("id")
            if cwd is not None:
                summary["cwd"] = str(Path(str(cwd)).expanduser().resolve())
            if session_id is not None:
                summary["session_id"] = str(session_id)
            if "first_timestamp_epoch" in summary:
                break
    return summary


def _timestamp_to_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _event_metadata(timestamp: Any, line_number: int) -> Mapping[str, Any]:
    metadata: Dict[str, Any] = {"line_number": line_number}
    if timestamp is not None:
        metadata["timestamp"] = timestamp
    return metadata


__all__ = [
    "CODEX_SESSIONS_PATH",
    "agent_execution_from_codex_session",
    "find_latest_codex_session",
    "record_codex_session",
]
