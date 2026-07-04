"""Launcher helpers for running Codex under the standalone harness runtime."""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Sequence
import argparse
import errno
import json
import os
import shlex
import subprocess
import sys
import time

from .codex_capture_command import capture_codex_session_command
from .harness_state import (
    PersonalHarnessRuntimeState,
    STATE_RELATIVE_PATH,
    read_personal_harness_state,
    write_personal_harness_state,
)

DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_REASONING = "high"
DEFAULT_HUD_HEIGHT_LINES = 1
DEFAULT_HUD_PANE_STYLE = "fg=colour81,bg=colour234"
HARNESS_CODEX_COMMAND = "harness-codex"
HARNESS_STATUS_COMMAND = "harness-status"
AUTO_CHECKPOINT_STARTED_FLOW_ID = "harness-codex-session-started"
AUTO_CHECKPOINT_COMPLETE_FLOW_ID = "harness-codex-session-complete"
AUTO_CHECKPOINT_FAILED_FLOW_ID = "harness-codex-session-failed"
TMUX_BOOTSTRAP_ENV = "HARNESS_CODEX_TMUX_BOOTSTRAPPED"
HARNESS_TMUX_HUD_OWNER_ENV = "HARNESS_TMUX_HUD_OWNER"
HARNESS_TMUX_HUD_LEADER_PANE_ENV = "HARNESS_TMUX_HUD_LEADER_PANE"
HARNESS_TMUX_STATUS_LEFT_ENV = "HARNESS_TMUX_STATUS_LEFT"
AGENTS_FILE_NAME = "AGENTS.md"
ANSI_RESET = "\033[0m"
ANSI_PALETTE = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "magenta": "\033[35m",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _git_ceiling_directory(root: Path | str) -> str:
    return str(Path(root).expanduser().resolve().parent)


def _git_ceiling_assignment(root: Path | str) -> str:
    return f"GIT_CEILING_DIRECTORIES={_git_ceiling_directory(root)}"


def _git_ceiling_env(root: Path | str) -> dict[str, str]:
    env = dict(os.environ)
    ceiling = _git_ceiling_directory(root)
    current = env.get("GIT_CEILING_DIRECTORIES")
    env["GIT_CEILING_DIRECTORIES"] = f"{ceiling}{os.pathsep}{current}" if current else ceiling
    return env


def _default_agents_md(root: Path) -> str:
    return f"""# AGENTS.md

This repository is prepared for `harness-codex`.

## Repository Boundary

- Treat this directory as the project root: `{Path(root).resolve()}`.
- Keep runtime state under `.harness/`; do not use `.omx/` as product runtime state.
- Preserve user work. Do not run destructive git commands such as `git reset`, `git checkout`, or `git clean` unless the user explicitly asks.

## Harness Memory

- `.harness/state/personal-harness-state.json` stores the active/closed harness session state.
- `.harness/replay/replay.jsonl` stores execution and verification evidence.
- `.harness/flow-checkpoints/checkpoints.jsonl` stores major workflow checkpoints during long Codex sessions.
- `.harness/memory/hot.md` stores recent selective memory and is the only memory layer to consult by default.
- `.harness/memory/warm.md` stores older memory for manual retrieval.
- `.harness/memory/archive.md` stores cold monthly/topic history for manual retrieval.
- `.harness/candidates/` stores candidate request/response/gate artifacts when iteration is needed.
- Memory is selective: record only accepted decisions, corrections/lessons from failures, milestones, and verified facts.
- Do not store secrets, raw logs, full transcripts, speculation, or unresolved discussion in memory.

## Coding Workflow

- Use the smallest workflow that fits the user's request.
- Let Codex and available skills choose the coding workflow dynamically from the request, repo context, and AGENTS.md.
- After each major workflow, record evidence and verification with harness flow checkpoints instead of waiting only for final Codex exit.
- When a major workflow finishes or fails, call `harness-agent --flow-checkpoint ...` from the agent workflow or hook; include `--memory-category`, `--memory-text`, and `--memory-source` only when the outcome is durable repo memory.
- Users should not need to record checkpoints or rotate memory manually.
- Prefer tests before behavior changes and verify before claiming completion.
"""


def _has_local_git_repo(root: Path) -> bool:
    if not root.exists():
        return False
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=_git_ceiling_env(root),
    )
    return completed.returncode == 0 and Path(completed.stdout.strip()).resolve() == root.resolve()


def prepare_harness_coding_root(root: Path) -> None:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not _has_local_git_repo(root):
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=_git_ceiling_env(root))
    agents_path = root / AGENTS_FILE_NAME
    if not agents_path.exists():
        agents_path.write_text(_default_agents_md(root), encoding="utf-8")


def _validate_existing_harness_state(root: Path) -> None:
    state_path = Path(root) / STATE_RELATIVE_PATH
    if state_path.exists():
        read_personal_harness_state(root)


def _current_harness_version(root: Path) -> str:
    gate_result = root / ".harness" / "candidates" / "codex-gate-result.json"
    if gate_result.exists():
        try:
            return str(json.loads(gate_result.read_text(encoding="utf-8")).get("harness_version", "unknown"))
        except (OSError, json.JSONDecodeError):
            return "unknown"
    try:
        return read_personal_harness_state(root).harness_version
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        return "uninitialized"


def build_codex_command(
    root: Path,
    *,
    model: str = DEFAULT_CODEX_MODEL,
    reasoning: str = DEFAULT_REASONING,
    yolo: bool = True,
    prompt_args: Sequence[str] = (),
) -> List[str]:
    command = [
        "env",
        _git_ceiling_assignment(root),
        "codex",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning}"',
        "-C",
        str(Path(root)),
    ]
    if yolo:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    command.extend(prompt_args)
    return command


def build_harness_status_watch_command(root: Path, *, leader_pane_id: str | None = None) -> str:
    command = [
        "env",
        _git_ceiling_assignment(root),
        f"{HARNESS_TMUX_HUD_OWNER_ENV}=1",
    ]
    if leader_pane_id:
        command.append(f"{HARNESS_TMUX_HUD_LEADER_PANE_ENV}={leader_pane_id}")
    command.append(HARNESS_STATUS_COMMAND)
    return (
        f"exec {shlex.join(command)} "
        f"--root {shlex.quote(str(Path(root)))} --compact --watch --color always"
    )


def build_tmux_bootstrap_command(
    root: Path,
    *,
    model: str,
    reasoning: str,
    yolo: bool,
    quiet_status: bool,
    capture_on_exit: bool = True,
    capture_sessions_root: Path | str | None = None,
    capture_task_id: str | None = None,
    tmux_hud: bool = False,
    prompt_args: Sequence[str] = (),
) -> List[str]:
    inner = [
        HARNESS_CODEX_COMMAND,
        "--root",
        str(Path(root)),
        "--model",
        model,
        "--reasoning",
        reasoning,
    ]
    if not yolo:
        inner.append("--no-yolo")
    if quiet_status:
        inner.append("--quiet-status")
    if not capture_on_exit:
        inner.append("--no-capture-on-exit")
    if capture_sessions_root is not None:
        inner.extend(["--capture-sessions-root", str(capture_sessions_root)])
    if capture_task_id is not None:
        inner.extend(["--capture-task-id", capture_task_id])
    if tmux_hud:
        inner.append("--tmux-hud")
    if prompt_args:
        inner.append("--")
        inner.extend(prompt_args)
    session_name = f"harness-codex-{os.getpid()}"
    inner_command = f"env {_git_ceiling_assignment(root)} {TMUX_BOOTSTRAP_ENV}=1 {shlex.join(inner)}"
    return [
        "tmux",
        "new-session",
        "-s",
        session_name,
        "-c",
        str(Path(root)),
        inner_command,
    ]


def mark_harness_session_started(
    root: Path,
    *,
    model: str,
    reasoning: str,
    yolo: bool,
    status_mode: str = "inline",
) -> Path:
    root = Path(root)
    _validate_existing_harness_state(root)
    metadata = {
        "runtime_owner": "standalone-.harness",
        "llm_backend": "current-codex-agent",
        "launch": {
            "model": model,
            "reasoning": reasoning,
            "yolo": yolo,
            "pid": os.getpid(),
            "started_at": time.time(),
        },
        "status": {
            "mode": status_mode,
            "command": "harness-status",
            "started_at": time.time(),
        },
    }
    return write_personal_harness_state(
        root,
        PersonalHarnessRuntimeState(
            active=True,
            harness_version=_current_harness_version(root),
            model_version=model,
            variant_id="default",
            phase="session",
            metadata=metadata,
        ),
    )


def close_harness_session(root: Path, *, exit_code: int) -> Path:
    root = Path(root)
    try:
        previous = read_personal_harness_state(root)
        metadata = dict(previous.metadata)
        harness_version = previous.harness_version
        model_version = previous.model_version
        variant_id = previous.variant_id
    except FileNotFoundError:
        metadata = {}
        harness_version = _current_harness_version(root)
        model_version = DEFAULT_CODEX_MODEL
        variant_id = "default"

    metadata.update({"exit_code": exit_code, "closed_at": time.time()})
    return write_personal_harness_state(
        root,
        PersonalHarnessRuntimeState(
            active=False,
            harness_version=harness_version,
            model_version=model_version,
            variant_id=variant_id,
            phase="closed",
            metadata=metadata,
        ),
    )


def capture_harness_session_exit(
    root: Path,
    *,
    model: str,
    sessions_root: Path | str | None = None,
    task_id: str | None = None,
) -> None:
    root = Path(root)
    metadata = _current_session_metadata(root)
    launch = _mapping_or_empty(metadata.get("launch"))
    started_after = launch.get("started_at")
    excluded_session_ids = _captured_session_ids(metadata)
    try:
        result = capture_codex_session_command(
            root,
            sessions_root=sessions_root,
            cwd=root,
            started_after=started_after,
            exclude_session_ids=excluded_session_ids,
            task_id=task_id,
            harness_version=_current_harness_version(root),
            model_version=model,
        )
        captured_sessions = _append_captured_session(
            metadata.get("captured_sessions"),
            {
                "session_id": result.session_id,
                "session_path": str(result.session_path),
                "task_id": result.task_id,
                "captured_at": time.time(),
            },
        )
        _merge_session_metadata(
            root,
            {
                "capture_on_exit": {
                    "status": "recorded",
                    "session_id": result.session_id,
                    "session_path": str(result.session_path),
                    "task_id": result.task_id,
                    "event_count": result.event_count,
                    "tool_call_count": result.tool_call_count,
                    "verification_result_count": result.verification_result_count,
                    "stage": result.outcome.stage if result.outcome is not None else None,
                    "solved": result.outcome.solved if result.outcome is not None else None,
                    "selection": {
                        "cwd": str(root),
                        "started_after": started_after,
                        "excluded_session_ids": sorted(excluded_session_ids),
                    },
                },
                "captured_sessions": captured_sessions,
            },
        )
    except Exception as exc:  # capture must not mask the Codex process exit code
        _merge_session_metadata(
            root,
            {
                "capture_on_exit": {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            },
        )


def _current_session_metadata(root: Path) -> dict:
    try:
        return dict(read_personal_harness_state(root).metadata)
    except FileNotFoundError:
        return {}


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _captured_session_ids(metadata: Mapping[str, Any]) -> set[str]:
    captured: set[str] = set()
    capture_on_exit = _mapping_or_empty(metadata.get("capture_on_exit"))
    session_id = capture_on_exit.get("session_id")
    if session_id is not None:
        captured.add(str(session_id))
    captured_sessions = metadata.get("captured_sessions")
    if isinstance(captured_sessions, list):
        for item in captured_sessions:
            item_session_id = _mapping_or_empty(item).get("session_id")
            if item_session_id is not None:
                captured.add(str(item_session_id))
    return captured


def _append_captured_session(existing: Any, entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    entries = list(existing) if isinstance(existing, list) else []
    session_id = entry.get("session_id")
    if session_id is not None:
        entries = [
            item
            for item in entries
            if _mapping_or_empty(item).get("session_id") != session_id
        ]
    entries.append(dict(entry))
    return entries


def _merge_session_metadata(root: Path, additions: dict) -> Path:
    try:
        previous = read_personal_harness_state(root)
        metadata = dict(previous.metadata)
        harness_version = previous.harness_version
        model_version = previous.model_version
        variant_id = previous.variant_id
        active = previous.active
        phase = previous.phase
    except FileNotFoundError:
        metadata = {}
        harness_version = _current_harness_version(root)
        model_version = DEFAULT_CODEX_MODEL
        variant_id = "default"
        active = False
        phase = "capture"
    metadata.update(additions)
    return write_personal_harness_state(
        root,
        PersonalHarnessRuntimeState(
            active=active,
            harness_version=harness_version,
            model_version=model_version,
            variant_id=variant_id,
            phase=phase,
            metadata=metadata,
        ),
    )


def _record_auto_checkpoint(root: Path, *, flow_id: str, status: str, evidence: str) -> None:
    from .flow_checkpoint import record_flow_checkpoint

    metadata = _current_session_metadata(root)
    capture_on_exit = _mapping_or_empty(metadata.get("capture_on_exit"))
    skill_context = {
        "orchestrator": "harness-codex",
        "event": flow_id,
    }
    if capture_on_exit:
        skill_context["capture_on_exit"] = str(capture_on_exit.get("status", "unknown"))
    try:
        record_flow_checkpoint(root, flow_id=flow_id, status=status, evidence=evidence, skill_context=skill_context)
    except Exception as exc:  # auto checkpoint must not mask the Codex process exit code
        _merge_session_metadata(
            root,
            {
                "auto_checkpoint": {
                    "status": "failed",
                    "flow_id": flow_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "recorded_at": time.time(),
                }
            },
        )


def render_git_tree_status(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(root)), "status", "--short", "--branch", "--untracked-files=normal"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
            env=_git_ceiling_env(root),
        )
    except (OSError, subprocess.SubprocessError):
        return "git:unavailable"
    if completed.returncode != 0:
        return "git:no-repo"

    lines = [line.rstrip("\n") for line in completed.stdout.splitlines() if line.strip()]
    branch = "unknown"
    changes = 0
    untracked = 0
    if lines and lines[0].startswith("## "):
        branch = _parse_git_branch(lines[0][3:].strip())
        status_lines = lines[1:]
    else:
        status_lines = lines
    for line in status_lines:
        if line.startswith("??"):
            untracked += 1
        else:
            changes += 1
    if changes == 0 and untracked == 0:
        return f"git:{branch} clean"
    parts = []
    if changes:
        parts.append(f"dirty:{changes}")
    if untracked:
        parts.append(f"untracked:{untracked}")
    return f"git:{branch} {' '.join(parts)}"


def _parse_git_branch(raw_branch_line: str) -> str:
    if raw_branch_line.startswith("No commits yet on "):
        return raw_branch_line.rsplit(" ", 1)[-1] or "unknown"
    branch = raw_branch_line.split("...", 1)[0].split(" ", 1)[0].strip()
    return branch or "unknown"


def render_harness_status(root: Path, *, compact: bool = False, color: bool = False) -> str:
    root = Path(root)
    git_status = render_git_tree_status(root)
    harness_label = _paint("[harness]", "cyan", color)
    git_status = _color_git_status(git_status, color)
    try:
        state = read_personal_harness_state(root)
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        return f"{harness_label} {_paint('inactive', 'dim', color)} | {git_status}"

    launch = dict(state.metadata.get("launch", {}))
    reasoning = str(launch.get("reasoning", "?"))
    yolo = _paint("YOLO", "magenta", color) if launch.get("yolo") else _paint("guarded", "dim", color)
    active = _runtime_status_label(state, launch)
    active = _color_runtime_status(active, color)
    if compact:
        return f"{harness_label} {active} {state.model_version} {reasoning} {yolo} {state.phase} | {git_status}"
    return (
        f"{harness_label} {active} | model:{state.model_version} {reasoning} | "
        f"{yolo} | variant:{state.variant_id} | phase:{state.phase} | version:{state.harness_version} | {git_status}"
    )


def _paint(text: str, color_name: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{ANSI_PALETTE[color_name]}{text}{ANSI_RESET}"


def _color_runtime_status(status: str, enabled: bool) -> str:
    if status == "active":
        return _paint(status, "green", enabled)
    if status == "stale":
        return _paint(status, "yellow", enabled)
    return _paint(status, "dim", enabled)


def _color_git_status(status: str, enabled: bool) -> str:
    if " clean" in status:
        return _paint(status, "green", enabled)
    if "dirty:" in status or "untracked:" in status:
        return _paint(status, "yellow", enabled)
    return _paint(status, "dim", enabled)


def _pid_is_running(pid: object) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _runtime_status_label(state: PersonalHarnessRuntimeState, launch: dict) -> str:
    if state.active and state.phase == "session":
        return "active" if _pid_is_running(launch.get("pid")) else "stale"
    return state.phase


def _tmux(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


@dataclass(frozen=True)
class TmuxHarnessStatusInstall:
    previous_status_left: str | None
    previous_window_name: str | None
    previous_mouse: str | None
    hud_pane_id: str | None


def _parse_tmux_pane_id(raw: str) -> str | None:
    pane_id = raw.splitlines()[0].strip() if raw.strip() else ""
    return pane_id if pane_id.startswith("%") else None


def _env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _install_tmux_status(root: Path, *, enabled: bool) -> TmuxHarnessStatusInstall:
    if not enabled or "TMUX" not in os.environ:
        return TmuxHarnessStatusInstall(None, None, None, None)
    update_status_left = _env_flag_enabled(HARNESS_TMUX_STATUS_LEFT_ENV)
    current_status = _tmux(["show-option", "-qv", "status-left"]).stdout.rstrip("\n") if update_status_left else None
    current_mouse = _tmux(["show-option", "-qv", "mouse"]).stdout.strip() or None
    current_window = _tmux(["display-message", "-p", "#W"]).stdout.strip() or None
    current_pane = _parse_tmux_pane_id(_tmux(["display-message", "-p", "#{pane_id}"]).stdout)
    _tmux(["set-option", "mouse", "on"])
    status_command = build_harness_status_watch_command(root, leader_pane_id=current_pane)
    split_args = [
        "split-window",
        "-v",
        "-l",
        str(DEFAULT_HUD_HEIGHT_LINES),
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "-c",
        str(root),
    ]
    if current_pane:
        split_args.extend(["-t", current_pane])
    split_args.append(status_command)
    hud_pane_id = _parse_tmux_pane_id(_tmux(split_args).stdout)
    hud_pane_style = os.environ.get("HARNESS_HUD_PANE_STYLE", DEFAULT_HUD_PANE_STYLE).strip()
    if hud_pane_id and hud_pane_style and hud_pane_style.lower() != "none":
        _tmux(["select-pane", "-t", hud_pane_id, "-P", hud_pane_style])
    if current_pane:
        _tmux(["select-pane", "-t", current_pane])
    if update_status_left:
        status_left = (
            f"#({HARNESS_STATUS_COMMAND} "
            f"--root {shlex.quote(str(root))} --compact --color never) "
        )
        _tmux(["set-option", "status-left", status_left])
    _tmux(["rename-window", "harness-codex"])
    return TmuxHarnessStatusInstall(current_status, current_window, current_mouse, hud_pane_id)


def _restore_tmux_status(install: TmuxHarnessStatusInstall) -> None:
    if install.hud_pane_id:
        _tmux(["kill-pane", "-t", install.hud_pane_id])
    if install.previous_mouse is not None:
        _tmux(["set-option", "mouse", install.previous_mouse])
    if install.previous_status_left is not None:
        _tmux(["set-option", "status-left", install.previous_status_left])
    if install.previous_window_name:
        _tmux(["rename-window", install.previous_window_name])


def run_harness_codex(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[int]] = subprocess.run,
) -> int:
    parser = argparse.ArgumentParser(description="Launch Codex inside the standalone harness runtime.")
    parser.add_argument("--root", default=".", help="Project root that owns the .harness runtime.")
    parser.add_argument("--model", default=DEFAULT_CODEX_MODEL, help="Codex model to launch. Default: gpt-5.5.")
    parser.add_argument("--reasoning", default=DEFAULT_REASONING, help="Codex reasoning effort. Default: high.")
    parser.add_argument("--no-yolo", action="store_true", help="Disable YOLO mode; omit bypass-approvals/sandbox flag.")
    parser.add_argument("--tmux-hud", action="store_true", help="Enable tmux HUD pane/status integration. Enabled by default.")
    parser.add_argument("--no-tmux-status", action="store_true", help="Disable tmux HUD pane/status integration.")
    parser.add_argument("--no-auto-tmux", action="store_true", help="Do not auto-start tmux when HUD is enabled outside tmux.")
    parser.add_argument("--quiet-status", action="store_true", help="Do not print the launch-time harness status line.")
    parser.add_argument("--no-capture-on-exit", action="store_true", help="Disable automatic Codex session capture after Codex exits.")
    parser.add_argument("--no-auto-checkpoint", action="store_true", help="Disable automatic harness-codex lifecycle flow checkpoints.")
    parser.add_argument("--capture-sessions-root", default=None, help="Codex sessions root for capture-on-exit. Defaults to ~/.codex/sessions.")
    parser.add_argument("--capture-task-id", default=None, help="Optional task id for capture-on-exit. Defaults to session id or filename.")
    parser.add_argument("--status-only", action="store_true", help="Print current harness status instead of launching Codex.")
    parser.add_argument("--dry-run", action="store_true", help="Print the Codex command and write no session state.")
    parser.add_argument("prompt", nargs=argparse.REMAINDER, help="Optional prompt forwarded to Codex.")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    prompt_args = list(args.prompt)
    if prompt_args[:1] == ["--"]:
        prompt_args = prompt_args[1:]

    if args.status_only:
        print(render_harness_status(root, color=sys.stdout.isatty()))
        return 0

    yolo = not args.no_yolo
    command = build_codex_command(
        root,
        model=args.model,
        reasoning=args.reasoning,
        yolo=yolo,
        prompt_args=prompt_args,
    )
    if args.dry_run:
        print(shlex.join(command))
        return 0

    try:
        _validate_existing_harness_state(root)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(
            f"harness-codex: invalid existing harness state at {root / STATE_RELATIVE_PATH}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    prepare_harness_coding_root(root)

    tmux_hud_requested = not args.no_tmux_status
    tmux_hud_enabled = (tmux_hud_requested or "TMUX" in os.environ) and not args.no_tmux_status
    if (
        tmux_hud_requested
        and "TMUX" not in os.environ
        and os.environ.get(TMUX_BOOTSTRAP_ENV) != "1"
        and not args.no_auto_tmux
    ):
        completed = runner(
            build_tmux_bootstrap_command(
                root,
                model=args.model,
                reasoning=args.reasoning,
                yolo=yolo,
                quiet_status=args.quiet_status,
                capture_on_exit=not args.no_capture_on_exit,
                capture_sessions_root=args.capture_sessions_root,
                capture_task_id=args.capture_task_id,
                tmux_hud=True,
                prompt_args=prompt_args,
            )
        )
        return int(completed.returncode)

    status_mode = "tmux-hud-pane" if tmux_hud_enabled and "TMUX" in os.environ else "inline"
    mark_harness_session_started(root, model=args.model, reasoning=args.reasoning, yolo=yolo, status_mode=status_mode)
    if not args.no_auto_checkpoint:
        _record_auto_checkpoint(
            root,
            flow_id=AUTO_CHECKPOINT_STARTED_FLOW_ID,
            status="started",
            evidence=f"harness-codex session started model={args.model} reasoning={args.reasoning} yolo={str(yolo).lower()}",
        )
    if not args.quiet_status:
        print(render_harness_status(root, compact=True, color=sys.stdout.isatty()), flush=True)
    tmux_status_install = _install_tmux_status(root, enabled=tmux_hud_enabled)
    exit_code = 1
    try:
        completed = runner(command)
        exit_code = int(completed.returncode)
        return exit_code
    finally:
        if not args.no_capture_on_exit and not args.dry_run:
            capture_harness_session_exit(
                root,
                model=args.model,
                sessions_root=args.capture_sessions_root,
                task_id=args.capture_task_id,
            )
        if not args.no_auto_checkpoint:
            flow_id = AUTO_CHECKPOINT_COMPLETE_FLOW_ID if exit_code == 0 else AUTO_CHECKPOINT_FAILED_FLOW_ID
            status = "complete" if exit_code == 0 else "failed"
            capture_status = _mapping_or_empty(_current_session_metadata(root).get("capture_on_exit")).get("status")
            capture_fragment = f" capture_on_exit={capture_status}" if capture_status else " capture_on_exit=disabled"
            _record_auto_checkpoint(
                root,
                flow_id=flow_id,
                status=status,
                evidence=f"harness-codex session exited exit_code={exit_code}{capture_fragment}",
            )
        close_harness_session(root, exit_code=exit_code)
        _restore_tmux_status(tmux_status_install)


def status_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print standalone harness runtime status.")
    parser.add_argument("--root", default=".", help="Project root that owns the .harness runtime.")
    parser.add_argument("--compact", action="store_true", help="Print compact one-line status for tmux footers.")
    parser.add_argument("--watch", action="store_true", help="Continuously refresh one-line status.")
    parser.add_argument("--interval", type=float, default=1.0, help="Watch refresh interval in seconds.")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Colorize status output. Default: auto.",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    color = args.color == "always" or (args.color == "auto" and sys.stdout.isatty())
    if args.watch:
        interval = args.interval if args.interval > 0 else 1.0
        try:
            while True:
                print(f"\r\033[2K{render_harness_status(root, compact=args.compact, color=color)}", end="", flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            print()
            return 0
    print(render_harness_status(root, compact=args.compact, color=color))
    return 0


__all__ = [
    "DEFAULT_CODEX_MODEL",
    "DEFAULT_HUD_PANE_STYLE",
    "DEFAULT_HUD_HEIGHT_LINES",
    "DEFAULT_REASONING",
    "HARNESS_CODEX_COMMAND",
    "HARNESS_STATUS_COMMAND",
    "AUTO_CHECKPOINT_STARTED_FLOW_ID",
    "AUTO_CHECKPOINT_COMPLETE_FLOW_ID",
    "AUTO_CHECKPOINT_FAILED_FLOW_ID",
    "HARNESS_TMUX_HUD_LEADER_PANE_ENV",
    "HARNESS_TMUX_HUD_OWNER_ENV",
    "HARNESS_TMUX_STATUS_LEFT_ENV",
    "TMUX_BOOTSTRAP_ENV",
    "prepare_harness_coding_root",
    "build_harness_status_watch_command",
    "build_tmux_bootstrap_command",
    "build_codex_command",
    "capture_harness_session_exit",
    "close_harness_session",
    "mark_harness_session_started",
    "render_harness_status",
    "render_git_tree_status",
    "run_harness_codex",
    "status_main",
]
