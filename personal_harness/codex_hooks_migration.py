"""Conservative migration for legacy OMX-owned Codex hook trust fences."""

from __future__ import annotations

from dataclasses import dataclass


START_MARKER = "# OMX-owned Codex hook trust state"
END_MARKER = "# End OMX-owned Codex hook trust state"
CONFIG_START_MARKER = "# oh-my-codex (OMX) Configuration"
CONFIG_END_MARKER = "# End oh-my-codex"


class HookTrustMigrationError(RuntimeError):
    """Raised when legacy ownership markers cannot be changed unambiguously."""


@dataclass(frozen=True)
class HookTrustMigrationResult:
    content: str
    migrated: bool
    removed_legacy_keys: tuple[str, ...] = ()


def unfence_legacy_omx_hook_trust_state(config: str) -> HookTrustMigrationResult:
    """Prepare legacy OMX config ownership for an upstream verified refresh.

    The exact inner fence owns only setup-generated hook trust tables, so that block
    is removed before setup regenerates hashes for the patched hook.  The outer OMX
    fence is removed without deleting unrelated settings.  Three deprecated defaults
    are removed only when that outer fence proves old setup authored them.
    """

    lines = config.splitlines(keepends=True)
    marker_pairs = (
        (START_MARKER, END_MARKER),
        (CONFIG_START_MARKER, CONFIG_END_MARKER),
    )
    matched: list[tuple[int, int]] = []
    outer_range: tuple[int, int] | None = None
    for start_marker, end_marker in marker_pairs:
        starts = [index for index, line in enumerate(lines) if line.strip() == start_marker]
        ends = [index for index, line in enumerate(lines) if line.strip() == end_marker]
        if not starts and not ends:
            continue
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise HookTrustMigrationError(
                f"Ambiguous or unpaired OMX ownership markers: {start_marker} / {end_marker}"
            )
        pair = (starts[0], ends[0])
        matched.append(pair)
        if start_marker == CONFIG_START_MARKER:
            outer_range = pair
    if not matched:
        return HookTrustMigrationResult(config, False)
    if '"""' in config or "'''" in config:
        raise HookTrustMigrationError("Refusing to edit hook trust markers in a config with multiline TOML strings")
    removed = {index for pair in matched for index in pair}
    trust_pairs = [pair for pair in matched if lines[pair[0]].strip() == START_MARKER]
    for start, end in trust_pairs:
        for index in range(start + 1, end):
            stripped = lines[index].strip()
            if (
                not stripped
                or stripped.startswith("#")
                or (stripped.startswith('[hooks.state."') and stripped.endswith('"]'))
                or stripped.startswith('trusted_hash = "sha256:')
            ):
                continue
            raise HookTrustMigrationError(
                f"Refusing to remove non-trust TOML from OMX hook ownership fence: {stripped}"
            )
        removed.update(range(start, end + 1))
    removed_legacy_keys: list[str] = []
    if outer_range is not None:
        table = ""
        legacy_assignments = {
            ("[features]", "multi_agent = true"): "features.multi_agent",
            ("[agents]", "max_threads = 6"): "agents.max_threads",
            ("[agents]", "max_depth = 2"): "agents.max_depth",
        }
        for index in range(outer_range[0] + 1, outer_range[1]):
            stripped = lines[index].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                table = stripped
                continue
            legacy_key = legacy_assignments.get((table, stripped))
            if legacy_key is not None:
                removed.add(index)
                removed_legacy_keys.append(legacy_key)
    return HookTrustMigrationResult(
        "".join(line for index, line in enumerate(lines) if index not in removed),
        True,
        tuple(sorted(removed_legacy_keys)),
    )


__all__ = [
    "END_MARKER",
    "CONFIG_END_MARKER",
    "CONFIG_START_MARKER",
    "HookTrustMigrationError",
    "HookTrustMigrationResult",
    "START_MARKER",
    "unfence_legacy_omx_hook_trust_state",
]
