"""Standalone harness-coding-agent runtime state persisted under .harness/state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping
import json
import os
import time

SCHEMA_VERSION = "personal-harness-state/v1"
STATE_RELATIVE_PATH = Path(".harness") / "state" / "personal-harness-state.json"


@dataclass(frozen=True)
class PersonalHarnessRuntimeState:
    active: bool
    harness_version: str
    model_version: str
    variant_id: str
    phase: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "active": self.active,
            "harness_version": self.harness_version,
            "model_version": self.model_version,
            "variant_id": self.variant_id,
            "phase": self.phase,
            "metadata": dict(self.metadata),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PersonalHarnessRuntimeState":
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported personal harness state schema: {data.get('schema_version')}")
        return cls(
            active=bool(data["active"]),
            harness_version=str(data["harness_version"]),
            model_version=str(data["model_version"]),
            variant_id=str(data["variant_id"]),
            phase=str(data["phase"]),
            metadata=dict(data.get("metadata", {})),
            updated_at=float(data.get("updated_at", time.time())),
        )


def _state_path(root: Path) -> Path:
    return Path(root) / STATE_RELATIVE_PATH


def write_personal_harness_state(root: Path, state: PersonalHarnessRuntimeState) -> Path:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_personal_harness_state(root: Path) -> PersonalHarnessRuntimeState:
    path = _state_path(root)
    return PersonalHarnessRuntimeState.from_dict(json.loads(path.read_text(encoding="utf-8")))


__all__ = [
    "PersonalHarnessRuntimeState",
    "SCHEMA_VERSION",
    "STATE_RELATIVE_PATH",
    "read_personal_harness_state",
    "write_personal_harness_state",
]
