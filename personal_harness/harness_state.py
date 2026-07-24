"""Standalone harness-coding-agent runtime state persisted under .harness/state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping
import json
import os
import time

LEGACY_SCHEMA_VERSION = "personal-harness-state/v1"
SCHEMA_VERSION = "personal-harness-state/v2"
STATE_REVISION = 2
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
    installation_id: str = field(default_factory=lambda: _default_installation_id())
    state_revision: int = STATE_REVISION
    migrated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "installation_id": self.installation_id,
            "state_revision": self.state_revision,
            "migrated_at": self.migrated_at,
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
            installation_id=str(data["installation_id"]),
            state_revision=int(data["state_revision"]),
            migrated_at=float(data["migrated_at"]),
        )


@dataclass(frozen=True)
class StateMigrationResult:
    path: Path
    from_schema: str
    to_schema: str
    migrated: bool


def _state_path(root: Path) -> Path:
    return Path(root) / STATE_RELATIVE_PATH


def write_personal_harness_state(root: Path, state: PersonalHarnessRuntimeState) -> Path:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.to_dict()
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(previous, Mapping) and previous.get("schema_version") == SCHEMA_VERSION:
            payload["migrated_at"] = float(previous.get("migrated_at", payload["migrated_at"]))
            if payload["installation_id"] == "unmanaged":
                payload["installation_id"] = str(previous.get("installation_id", "unmanaged"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_personal_harness_state(root: Path) -> PersonalHarnessRuntimeState:
    path = _state_path(root)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, Mapping) and raw.get("schema_version") == LEGACY_SCHEMA_VERSION:
        migrate_personal_harness_state(root, installation_id=_default_installation_id())
        raw = json.loads(path.read_text(encoding="utf-8"))
    return PersonalHarnessRuntimeState.from_dict(raw)


def migrate_personal_harness_state(
    root: Path,
    *,
    installation_id: str,
    now: float | None = None,
) -> StateMigrationResult:
    path = _state_path(root)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("Personal harness state must be a JSON object")
    source_schema = str(raw.get("schema_version"))
    if source_schema == SCHEMA_VERSION:
        PersonalHarnessRuntimeState.from_dict(raw)
        return StateMigrationResult(path, source_schema, SCHEMA_VERSION, False)
    if source_schema != LEGACY_SCHEMA_VERSION:
        raise ValueError(f"Unsupported personal harness state schema: {source_schema}")

    migrated = dict(raw)
    migrated.update(
        {
            "schema_version": SCHEMA_VERSION,
            "installation_id": installation_id,
            "state_revision": STATE_REVISION,
            "migrated_at": time.time() if now is None else float(now),
        }
    )
    PersonalHarnessRuntimeState.from_dict(migrated)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(migrated, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return StateMigrationResult(path, source_schema, SCHEMA_VERSION, True)


def _default_installation_id() -> str:
    configured = os.environ.get("HARNESS_INSTALLATION_ID", "").strip()
    if configured:
        return configured
    harness_home = Path(os.environ.get("HARNESS_HOME", "~/.local/share/harness-codex")).expanduser()
    manifest_path = harness_home / "install" / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unmanaged"
    installation_id = payload.get("installation_id") if isinstance(payload, Mapping) else None
    return str(installation_id).strip() if installation_id else "unmanaged"


__all__ = [
    "PersonalHarnessRuntimeState",
    "StateMigrationResult",
    "LEGACY_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "STATE_REVISION",
    "STATE_RELATIVE_PATH",
    "read_personal_harness_state",
    "migrate_personal_harness_state",
    "write_personal_harness_state",
]
