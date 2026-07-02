"""JSONL replay store for reward-annotated personal harness trajectories."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Set
import json
import time


@dataclass(frozen=True)
class TrajectoryRecord:
    task_id: str
    harness_version: str
    reward: float
    solved: bool
    events: List[Mapping[str, Any]] = field(default_factory=list)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "harness_version": self.harness_version,
            "reward": self.reward,
            "solved": self.solved,
            "events": list(self.events),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TrajectoryRecord":
        return cls(
            task_id=str(data["task_id"]),
            harness_version=str(data["harness_version"]),
            reward=float(data["reward"]),
            solved=bool(data["solved"]),
            events=list(data.get("events", [])),
            metadata=dict(data.get("metadata", {})),
            created_at=float(data.get("created_at", time.time())),
        )


class ReplayStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, record: TrajectoryRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    def read_all(self) -> Iterator[TrajectoryRecord]:
        if not self.path.exists():
            return iter(())
        return self._read_existing()

    def _read_existing(self) -> Iterator[TrajectoryRecord]:
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                    yield TrajectoryRecord.from_dict(data)
                except Exception as exc:  # noqa: BLE001 - convert to stable public error
                    raise ValueError(f"Malformed replay JSONL at line {line_no}: {exc}") from exc

    def solved_task_ids(self) -> Set[str]:
        return {record.task_id for record in self.read_all() if record.solved}


__all__ = ["ReplayStore", "TrajectoryRecord"]
