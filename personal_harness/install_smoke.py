"""Post-install smoke tests that gate Harness deployment commits."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Mapping, Protocol, Sequence

from .codex_capture import agent_execution_from_codex_session
from .launcher import run_harness_codex


@dataclass(frozen=True)
class SmokeResult:
    name: str
    passed: bool
    details: Mapping[str, Any]
    started_at: float
    finished_at: float
    duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OmxContext(Protocol):
    omx_package_root: Path


def run_custom_capture_smoke() -> SmokeResult:
    started_at = time.time()
    started_monotonic = time.monotonic()
    with tempfile.TemporaryDirectory() as d:
        session_path = Path(d) / "session.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": "install-smoke-custom"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-smoke",
                    "name": "exec",
                    "input": 'const r = await tools.exec_command({cmd:"python3 -m unittest -v"}); text(r.output)',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-smoke",
                    "output": [{"type": "input_text", "text": "Script completed\nOutput:\nRan 1 test\nOK\n"}],
                },
            },
        ]
        session_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        execution = agent_execution_from_codex_session(session_path)
    details = {
        "tool_call_count": len(execution.tool_calls),
        "verification_result_count": len(execution.verification_results),
        "exit_code": execution.verification_results[0].exit_code if execution.verification_results else None,
    }
    passed = details == {"tool_call_count": 1, "verification_result_count": 1, "exit_code": 0}
    return _smoke_result("custom_capture", passed, details, started_at, started_monotonic)


def run_tmux_oversized_image_smoke(
    hook_path: Path | str,
    *,
    cwd: Path | str | None = None,
    node_executable: str | None = None,
    timeout_seconds: float = 30.0,
) -> SmokeResult:
    started_at = time.time()
    started_monotonic = time.monotonic()
    hook = Path(hook_path).resolve()
    node = node_executable or shutil.which("node") or "node"
    temporary_cwd = tempfile.TemporaryDirectory(prefix="harness-hook-smoke-") if cwd is None else None
    working_directory = Path(cwd) if cwd is not None else Path(temporary_cwd.name)
    image_payload = json.dumps(
        {
            "attachments": [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + "A" * (6 * 1024 * 1024),
                }
            ],
            "cwd": str(working_directory),
            "session_id": "harness-install-smoke-image",
            "prompt": "image attachment smoke",
            "hook_event_name": "UserPromptSubmit",
        }
    ).encode()
    environment = dict(os.environ)
    environment["TMUX"] = environment.get("TMUX") or "/tmp/harness-install-smoke-tmux,1,0"
    image = _run_hook_payload(
        hook,
        image_payload,
        cwd=working_directory,
        node=node,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )

    stop_session_id = "harness-install-smoke-stop"
    stop_state = working_directory / ".omx" / "state" / "sessions" / stop_session_id
    stop_state.mkdir(parents=True, exist_ok=True)
    (stop_state / "autopilot-state.json").write_text(
        json.dumps(
            {
                "active": True,
                "mode": "autopilot",
                "current_phase": "ultragoal",
                "iteration": 1,
                "max_iterations": 50,
            }
        ),
        encoding="utf-8",
    )
    stop_environment = dict(environment)
    for key in ("OMX_STATE_ROOT", "OMX_TEAM_STATE_ROOT"):
        stop_environment.pop(key, None)
    stop_environment.update(
        {
            "OMX_ROOT": str(working_directory),
            "OMX_SESSION_ID": stop_session_id,
        }
    )
    stop_payload = json.dumps(
        {
            "attachment": "A" * (6 * 1024 * 1024),
            "cwd": str(working_directory),
            "session_id": stop_session_id,
            "hook_event_name": "Stop",
            "metadata": {"name": "UserPromptSubmit", "event": "UserPromptSubmit"},
        }
    ).encode()
    stop = _run_hook_payload(
        hook,
        stop_payload,
        cwd=working_directory,
        node=node,
        environment=stop_environment,
        timeout_seconds=timeout_seconds,
    )
    try:
        stop_output = json.loads(str(stop["stdout"]))
    except json.JSONDecodeError:
        stop_output = {}
    if temporary_cwd is not None:
        temporary_cwd.cleanup()

    image_passed = (
        image["producer_error"] is None
        and image["timed_out"] is False
        and image["hook_exit_code"] == 0
        and image["stdout"] == "{}"
        and not image["stderr"]
    )
    stop_passed = (
        stop["producer_error"] is None
        and stop["timed_out"] is False
        and stop["hook_exit_code"] == 0
        and stop_output.get("decision") == "block"
        and stop_output.get("stopReason") == "native_stop_stdin_oversized_active_workflow"
    )
    details = {
        **image,
        "payload_bytes": len(image_payload),
        "payload_shape": "image_attachment_before_event_name",
        "stop_gate_passed": stop_passed,
        "stop_hook_exit_code": stop["hook_exit_code"],
        "stop_producer_error": stop["producer_error"],
        "stop_stdout": stop["stdout"],
        "stop_stderr": stop["stderr"],
    }
    return _smoke_result(
        "tmux_oversized_image",
        image_passed and stop_passed,
        details,
        started_at,
        started_monotonic,
    )


def _run_hook_payload(
    hook: Path,
    payload: bytes,
    *,
    cwd: Path,
    node: str,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    process = subprocess.Popen(
        [node, str(hook)],
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    producer_errors: list[str] = []
    assert process.stdin is not None

    def produce() -> None:
        try:
            for offset in range(0, len(payload), 64 * 1024):
                process.stdin.write(payload[offset : offset + 64 * 1024])
                process.stdin.flush()
        except BrokenPipeError as exc:
            producer_errors.append(f"{type(exc).__name__}: errno={exc.errno}")
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError as exc:
                producer_errors.append(f"{type(exc).__name__}: errno={exc.errno}")

    producer = threading.Thread(target=produce, name="harness-smoke-payload-producer", daemon=True)
    producer.start()
    timed_out = False
    try:
        exit_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        exit_code = process.wait(timeout=5)
    producer.join(timeout=5)
    if producer.is_alive():
        producer_errors.append("producer thread did not terminate")
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = process.stdout.read().decode("utf-8", errors="replace").strip()
    stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
    process.stdout.close()
    process.stderr.close()
    producer_error = producer_errors[0] if producer_errors else None
    return {
        "producer_error": producer_error,
        "hook_exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
    }


def run_lifecycle_smoke() -> SmokeResult:
    started_at = time.time()
    started_monotonic = time.monotonic()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "project"
        empty_sessions = Path(d) / "empty-sessions"
        empty_sessions.mkdir()
        state_path = root / ".harness" / "state" / "personal-harness-state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": "personal-harness-state/v1",
                    "active": False,
                    "harness_version": "v1",
                    "model_version": "gpt-5.5",
                    "variant_id": "default",
                    "phase": "closed",
                    "metadata": {},
                    "updated_at": 1.0,
                }
            ),
            encoding="utf-8",
        )
        exit_code = run_harness_codex(
            [
                "--root",
                str(root),
                "--no-tmux-status",
                "--quiet-status",
                "--capture-sessions-root",
                str(empty_sessions),
                "--",
                "install smoke",
            ],
            runner=lambda command: subprocess.CompletedProcess(command, 0),
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        checkpoint_path = root / ".harness" / "flow-checkpoints" / "checkpoints.jsonl"
        checkpoints = [json.loads(line) for line in checkpoint_path.read_text(encoding="utf-8").splitlines() if line]
    session_ids = [str(checkpoint.get("session_id", "")) for checkpoint in checkpoints]
    capture_error = str(checkpoints[-1].get("details", {}).get("capture_error", "")) if checkpoints else ""
    details = {
        "exit_code": exit_code,
        "state_schema": state.get("schema_version"),
        "checkpoint_count": len(checkpoints),
        "session_ids_match": len(session_ids) == 2 and bool(session_ids[0]) and len(set(session_ids)) == 1,
        "capture_error": capture_error,
    }
    passed = (
        exit_code == 0
        and details["state_schema"] == "personal-harness-state/v2"
        and details["checkpoint_count"] == 2
        and details["session_ids_match"] is True
        and "FileNotFoundError" in capture_error
    )
    return _smoke_result("lifecycle", passed, details, started_at, started_monotonic)


def _smoke_result(
    name: str,
    passed: bool,
    details: Mapping[str, Any],
    started_at: float,
    started_monotonic: float,
) -> SmokeResult:
    finished_at = time.time()
    return SmokeResult(
        name=name,
        passed=passed,
        details=details,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=max(0.0, time.monotonic() - started_monotonic),
    )


def run_install_smoke_tests(context: OmxContext) -> Sequence[Mapping[str, Any]]:
    results = (
        run_custom_capture_smoke(),
        run_tmux_oversized_image_smoke(
            Path(context.omx_package_root) / "dist" / "scripts" / "codex-native-hook.js"
        ),
        run_lifecycle_smoke(),
    )
    return [result.to_dict() for result in results]


__all__ = [
    "SmokeResult",
    "run_custom_capture_smoke",
    "run_install_smoke_tests",
    "run_lifecycle_smoke",
    "run_tmux_oversized_image_smoke",
]
