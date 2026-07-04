"""Checkpoint-driven hot/warm/archive memory for harness coding repos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
import re

from .harness_state import PersonalHarnessRuntimeState, read_personal_harness_state, write_personal_harness_state

MEMORY_ROOT_RELATIVE_PATH = Path(".harness") / "memory"
HOT_MEMORY_RELATIVE_PATH = MEMORY_ROOT_RELATIVE_PATH / "hot.md"
WARM_MEMORY_RELATIVE_PATH = MEMORY_ROOT_RELATIVE_PATH / "warm.md"
ARCHIVE_MEMORY_RELATIVE_PATH = MEMORY_ROOT_RELATIVE_PATH / "archive.md"
ALLOWED_MEMORY_CATEGORIES = frozenset({"decision", "correction", "milestone", "verified-fact"})
HOT_MEMORY_MAX_AGE_DAYS = 7
WARM_MEMORY_MAX_AGE_DAYS = 30
HOT_MEMORY_MAX_ENTRIES = 20

_FORBIDDEN_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(password|passwd|secret|token|api[_-]?key)\s*=", re.IGNORECASE),
    re.compile(r"\.env\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class MemoryEntry:
    date: str
    category: str
    text: str
    source: str
    reason: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, default_date: str) -> "MemoryEntry":
        return cls(
            date=str(payload.get("date") or default_date),
            category=str(payload.get("category", "")),
            text=str(payload.get("text", "")),
            source=str(payload.get("source", "")),
            reason=str(payload.get("reason", "")),
        )


@dataclass(frozen=True)
class MemorySyncResult:
    accepted: bool
    reason: str
    hot_path: Path
    warm_path: Path
    archive_path: Path
    hot_count: int
    warm_count: int
    archive_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "hot_path": str(self.hot_path),
            "warm_path": str(self.warm_path),
            "archive_path": str(self.archive_path),
            "hot_count": self.hot_count,
            "warm_count": self.warm_count,
            "archive_count": self.archive_count,
        }


def sync_checkpoint_memory(
    root: Path,
    *,
    entry: MemoryEntry | Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> MemorySyncResult:
    root = Path(root)
    current = now or datetime.now(timezone.utc)
    default_date = current.date().isoformat()
    candidate = _coerce_entry(entry, default_date=default_date)
    accepted = False
    reason = "rotation-only"

    hot_entries = _read_entries(root / HOT_MEMORY_RELATIVE_PATH)
    warm_entries = _read_entries(root / WARM_MEMORY_RELATIVE_PATH)
    archive_entries = _read_entries(root / ARCHIVE_MEMORY_RELATIVE_PATH)

    if candidate is not None:
        validation_error = _validate_entry(candidate)
        if validation_error:
            reason = validation_error
        else:
            hot_entries.append(_sanitize_entry(candidate))
            accepted = True
            reason = "accepted"

    hot_entries, warm_entries, archive_entries = _rotate_entries(
        hot_entries,
        warm_entries,
        archive_entries,
        now=current,
    )
    hot_path = _write_entries(root / HOT_MEMORY_RELATIVE_PATH, "Hot Harness Memory", hot_entries)
    warm_path = _write_entries(root / WARM_MEMORY_RELATIVE_PATH, "Warm Harness Memory", warm_entries)
    archive_path = _write_entries(root / ARCHIVE_MEMORY_RELATIVE_PATH, "Archive Harness Memory", archive_entries)

    result = MemorySyncResult(
        accepted=accepted,
        reason=reason,
        hot_path=hot_path,
        warm_path=warm_path,
        archive_path=archive_path,
        hot_count=len(hot_entries),
        warm_count=len(warm_entries),
        archive_count=len(archive_entries),
    )
    _write_memory_state(root, result)
    return result


def _coerce_entry(entry: MemoryEntry | Mapping[str, Any] | None, *, default_date: str) -> MemoryEntry | None:
    if entry is None:
        return None
    if isinstance(entry, MemoryEntry):
        if entry.date:
            return entry
        return MemoryEntry(
            date=default_date,
            category=entry.category,
            text=entry.text,
            source=entry.source,
            reason=entry.reason,
        )
    return MemoryEntry.from_mapping(entry, default_date=default_date)


def _validate_entry(entry: MemoryEntry) -> str | None:
    if entry.category not in ALLOWED_MEMORY_CATEGORIES:
        return f"unsupported category: {entry.category}"
    if not entry.text.strip():
        return "memory text is required"
    if not entry.source.strip():
        return "memory source is required"
    haystack = "\n".join([entry.text, entry.source, entry.reason])
    if any(pattern.search(haystack) for pattern in _FORBIDDEN_PATTERNS):
        return "forbidden sensitive content"
    try:
        datetime.fromisoformat(entry.date)
    except ValueError:
        return f"invalid date: {entry.date}"
    return None


def _sanitize_entry(entry: MemoryEntry) -> MemoryEntry:
    return MemoryEntry(
        date=entry.date,
        category=entry.category,
        text=_clean_field(entry.text),
        source=_clean_field(entry.source),
        reason=_clean_field(entry.reason),
    )


def _clean_field(value: str) -> str:
    return " ".join(value.replace("|", "/").split())


def _rotate_entries(
    hot_entries: Sequence[MemoryEntry],
    warm_entries: Sequence[MemoryEntry],
    archive_entries: Sequence[MemoryEntry],
    *,
    now: datetime,
) -> tuple[list[MemoryEntry], list[MemoryEntry], list[MemoryEntry]]:
    hot_candidates: list[MemoryEntry] = []
    warm_candidates: list[MemoryEntry] = list(warm_entries)
    archive_candidates: list[MemoryEntry] = list(archive_entries)

    for entry in hot_entries:
        age = _entry_age_days(entry, now)
        if age <= HOT_MEMORY_MAX_AGE_DAYS:
            hot_candidates.append(entry)
        elif age <= WARM_MEMORY_MAX_AGE_DAYS:
            warm_candidates.append(entry)
        else:
            archive_candidates.append(entry)

    hot_sorted = _dedupe_entries(_sort_entries(hot_candidates))
    warm_candidates.extend(hot_sorted[HOT_MEMORY_MAX_ENTRIES:])
    hot_kept = hot_sorted[:HOT_MEMORY_MAX_ENTRIES]

    warm_kept: list[MemoryEntry] = []
    for entry in _dedupe_entries(_sort_entries(warm_candidates)):
        if _entry_age_days(entry, now) <= WARM_MEMORY_MAX_AGE_DAYS:
            warm_kept.append(entry)
        else:
            archive_candidates.append(entry)

    archive_kept = _dedupe_entries(_sort_entries(archive_candidates))
    return hot_kept, warm_kept, archive_kept


def _entry_age_days(entry: MemoryEntry, now: datetime) -> int:
    entry_date = datetime.fromisoformat(entry.date).date()
    return (now.date() - entry_date).days


def _sort_entries(entries: Sequence[MemoryEntry]) -> list[MemoryEntry]:
    return sorted(entries, key=lambda item: (item.date, item.category, item.text, item.source), reverse=True)


def _dedupe_entries(entries: Sequence[MemoryEntry]) -> list[MemoryEntry]:
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[MemoryEntry] = []
    for entry in entries:
        key = (entry.date, entry.category, entry.text, entry.source)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _read_entries(path: Path) -> list[MemoryEntry]:
    if not path.exists():
        return []
    entries: list[MemoryEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        parsed = _parse_entry_line(line)
        if parsed is not None:
            entries.append(parsed)
    return entries


def _parse_entry_line(line: str) -> MemoryEntry | None:
    parts = line[2:].split(" | ")
    if len(parts) < 4:
        return None
    date, category, text = parts[:3]
    source = ""
    reason = ""
    for part in parts[3:]:
        if part.startswith("source: "):
            source = part.removeprefix("source: ")
        elif part.startswith("reason: "):
            reason = part.removeprefix("reason: ")
    if not source:
        return None
    return MemoryEntry(date=date, category=category, text=text, source=source, reason=reason)


def _write_entries(path: Path, title: str, entries: Sequence[MemoryEntry]) -> Path:
    if not entries and not path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {title}",
        "",
        "Selective checkpoint-driven memory. Do not store secrets, raw logs, or full transcripts.",
        "",
    ]
    for entry in entries:
        line = f"- {entry.date} | {entry.category} | {entry.text} | source: {entry.source}"
        if entry.reason:
            line = f"{line} | reason: {entry.reason}"
        lines.append(line)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def _write_memory_state(root: Path, result: MemorySyncResult) -> None:
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
        phase = "memory_sync"
        harness_version = "uninitialized"
        model_version = "unknown"
        variant_id = "default"

    metadata["memory"] = {
        "hot_path": str(HOT_MEMORY_RELATIVE_PATH),
        "warm_path": str(WARM_MEMORY_RELATIVE_PATH),
        "archive_path": str(ARCHIVE_MEMORY_RELATIVE_PATH),
        "hot_count": result.hot_count,
        "warm_count": result.warm_count,
        "archive_count": result.archive_count,
        "last_sync": {
            "accepted": result.accepted,
            "reason": result.reason,
        },
    }
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


__all__ = [
    "ALLOWED_MEMORY_CATEGORIES",
    "ARCHIVE_MEMORY_RELATIVE_PATH",
    "HOT_MEMORY_MAX_AGE_DAYS",
    "HOT_MEMORY_MAX_ENTRIES",
    "HOT_MEMORY_RELATIVE_PATH",
    "MEMORY_ROOT_RELATIVE_PATH",
    "WARM_MEMORY_MAX_AGE_DAYS",
    "WARM_MEMORY_RELATIVE_PATH",
    "MemoryEntry",
    "MemorySyncResult",
    "sync_checkpoint_memory",
]
