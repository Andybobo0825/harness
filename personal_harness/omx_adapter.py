"""Optional read-only compatibility adapter for observing existing OMX files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass(frozen=True)
class OmxCompatibilitySnapshot:
    root: Path
    present: bool
    state_files: List[str]
    log_files: List[str]


def _list_relative_files(base: Path, prefix: str) -> List[str]:
    if not base.exists():
        return []
    files = [f"{prefix}/{path.relative_to(base).as_posix()}" for path in base.rglob("*") if path.is_file()]
    return sorted(files)


def snapshot_omx_compatibility(root: Path) -> OmxCompatibilitySnapshot:
    root = Path(root)
    omx_root = root / ".omx"
    if not omx_root.exists():
        return OmxCompatibilitySnapshot(root=root, present=False, state_files=[], log_files=[])
    return OmxCompatibilitySnapshot(
        root=root,
        present=True,
        state_files=_list_relative_files(omx_root / "state", "state"),
        log_files=_list_relative_files(omx_root / "logs", "logs"),
    )


__all__ = ["OmxCompatibilitySnapshot", "snapshot_omx_compatibility"]
