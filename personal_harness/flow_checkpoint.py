"""Flow-level checkpoint records for active harness coding sessions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
import json
import subprocess
import time

from .harness_state import (
    PersonalHarnessRuntimeState,
    read_personal_harness_state,
    write_personal_harness_state,
)
from .launcher import render_git_tree_status
from .memory import MemoryEntry, sync_checkpoint_memory
from .replay import ReplayStore, TrajectoryRecord

CHECKPOINT_RELATIVE_PATH = Path(".harness") / "flow-checkpoints" / "checkpoints.jsonl"


def record_flow_checkpoint(
    root: Path,
    *,
    flow_id: str,
    status: str,
    evidence: str,
    skill_context: Mapping[str, Any] | None = None,
    replay_refs: Sequence[str] = (),
    candidate_refs: Sequence[str] = (),
    memory_entry: MemoryEntry | Mapping[str, Any] | None = None,
    sync_memory: bool = False,
    include_diff_stat: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    """Append a non-destructive major-flow checkpoint under `.harness`.

    The checkpoint is intentionally observational: it gathers `git status` and
    optional `git diff --stat` metadata, then records evidence for later harness
    iteration. It never resets, checks out, cleans, or otherwise rewrites user
    work.
    """

    root = Path(root)
    created_at = time.time()
    record = {
        "schema_version": "harness-flow-checkpoint/v1",
        "flow_id": flow_id,
        "status": status,
        "evidence": evidence,
        "created_at": created_at,
        "git": _git_metadata(root, include_diff_stat=include_diff_stat, runner=runner),
        "skill_context": dict(skill_context or {}),
        "replay_refs": list(replay_refs),
        "candidate_refs": list(candidate_refs),
    }
    if sync_memory:
        memory_result = sync_checkpoint_memory(root, entry=memory_entry)
        record["memory"] = {
            "accepted": memory_result.accepted,
            "reason": memory_result.reason,
            "hot_count": memory_result.hot_count,
            "warm_count": memory_result.warm_count,
            "archive_count": memory_result.archive_count,
        }
    path = root / CHECKPOINT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        handle.write("\n")
    _append_checkpoint_to_replay(root, record)
    _append_checkpoint_to_state(root, record)
    return path


def _git_metadata(
    root: Path,
    *,
    include_diff_stat: bool,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> Mapping[str, Any]:
    metadata: dict[str, Any] = {"summary": render_git_tree_status(root)}
    if not include_diff_stat or metadata["summary"] == "git:no-repo":
        return metadata
    completed = runner(
        ["git", "-C", str(root), "diff", "--stat"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    metadata["diff_stat"] = completed.stdout.strip()
    if completed.returncode != 0:
        metadata["diff_stat_error"] = completed.stderr.strip()
    return metadata


def _append_checkpoint_to_state(root: Path, record: Mapping[str, Any]) -> None:
    try:
        previous = read_personal_harness_state(root)
        metadata = dict(previous.metadata)
        active = previous.active
        phase = previous.phase
        harness_version = previous.harness_version
        model_version = previous.model_version
        variant_id = previous.variant_id
    except FileNotFoundError:
        metadata = {"runtime_owner": "standalone-.harness"}
        active = False
        phase = "flow_checkpoint"
        harness_version = "uninitialized"
        model_version = "unknown"
        variant_id = "default"

    checkpoints = list(metadata.get("flow_checkpoints", [])) if isinstance(metadata.get("flow_checkpoints"), list) else []
    checkpoints.append(
        {
            "flow_id": record["flow_id"],
            "status": record["status"],
            "created_at": record["created_at"],
            "path": str(CHECKPOINT_RELATIVE_PATH),
        }
    )
    metadata["flow_checkpoints"] = checkpoints
    write_personal_harness_state(
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


def _append_checkpoint_to_replay(root: Path, record: Mapping[str, Any]) -> None:
    status = str(record["status"])
    solved = status.lower() in {"complete", "completed", "success", "passed"}
    ReplayStore(root / ".harness" / "replay" / "replay.jsonl").append(
        TrajectoryRecord(
            task_id=f"flow:{record['flow_id']}",
            harness_version=_current_harness_version(root),
            reward=1.0 if solved else 0.0,
            solved=solved,
            events=[
                {
                    "type": "flow_checkpoint",
                    "payload": {
                        "flow_id": record["flow_id"],
                        "status": status,
                        "evidence": record["evidence"],
                        "git": dict(record["git"]),
                        "skill_context": dict(record["skill_context"]),
                    },
                }
            ],
            metadata={
                "record_type": "flow_checkpoint",
                "status": status,
                "replay_refs": list(record["replay_refs"]),
                "candidate_refs": list(record["candidate_refs"]),
            },
        )
    )


def _current_harness_version(root: Path) -> str:
    try:
        return read_personal_harness_state(root).harness_version
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        return "uninitialized"


__all__ = ["CHECKPOINT_RELATIVE_PATH", "record_flow_checkpoint"]
