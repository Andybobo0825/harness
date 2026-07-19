"""Verified, idempotent overlay for the pinned oh-my-codex native hook."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping

from .release_contract import sha256_file

PINNED_OMX_VERSION = "0.20.2"
OMX_TARBALL_URL = "https://registry.npmjs.org/oh-my-codex/-/oh-my-codex-0.20.2.tgz"
OMX_TARBALL_INTEGRITY = "sha512-f48bqkK3UX4D2VfKimiqVpbYV+rqim7jJM6KDI/+gzpKzLtnwNTyc06whrkWBqlah0Tg87rX5rG8mkPcGzoZGQ=="
OVERLAY_REVISION = "stdin-drain-canonical-event-scan-v4"
OVERLAY_FILES = (
    "dist/scripts/codex-native-hook.js",
    "src/scripts/codex-native-hook.ts",
)
OFFICIAL_PREIMAGE_CHECKSUMS = {
    "dist/scripts/codex-native-hook.js": "ea5798cc14ffa60e05d378943df2d3c8c2ef8f7bfd03720517e6f24ad422a36f",
    "src/scripts/codex-native-hook.ts": "9af70248539b8a4c9d44cb3c1f253b4f7143d5c8b455a24e892d86cf2aeeb3cd",
}
OFFICIAL_POSTIMAGE_CHECKSUMS = {
    # Recomputed from the verified 0.20.2 preimages whenever the overlay changes.
    "dist/scripts/codex-native-hook.js": "6537df1b07dedbe6ba7583fe90774d8d5c70e86d0c57b84d9550026646228a35",
    "src/scripts/codex-native-hook.ts": "d4bbe30ccf79325fa7e93690cab81b7644694c60101a165e936e589dab843575",
}

_TS_DRAIN_OLD = """async function readStdinJson(): Promise<NativeHookCliReadResult> {
  const chunks: Buffer[] = [];
  let totalBytes = 0;
  let oversized = false;
  for await (const chunk of process.stdin) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
    totalBytes += buffer.byteLength;
    if (totalBytes > MAX_NATIVE_STDIN_JSON_BYTES) {
      const remaining = Math.max(0, MAX_NATIVE_STDIN_JSON_BYTES - (totalBytes - buffer.byteLength));
      if (remaining > 0) chunks.push(Buffer.from(buffer.subarray(0, remaining)));
      oversized = true;
      process.stdin.destroy();
      break;
    }
"""
_TS_DRAIN_NEW = """async function readStdinJson(): Promise<NativeHookCliReadResult> {
  const chunks: Buffer[] = [];
  let totalBytes = 0;
  let oversized = false;
  let hookEventScanDepth = 0;
  let hookEventScanInString = false;
  let hookEventScanEscape = false;
  let hookEventScanToken = "";
  let hookEventScanTokenOverflow = false;
  let hookEventScanPendingKey: string | null = null;
  let hookEventScanStringRole: "key" | "value" | "ignored" = "ignored";
  let hookEventScanStage: "root" | "key" | "colon" | "value" | "afterValue" = "root";
  let scannedHookEventName: CodexHookEventName | null = null;
  let hookEventScanConflict = false;
  for await (const chunk of process.stdin) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
    for (const char of buffer.toString("utf-8")) {
      if (hookEventScanInString) {
        if (hookEventScanEscape) {
          hookEventScanEscape = false;
          if (hookEventScanStringRole !== "ignored") hookEventScanTokenOverflow = true;
          continue;
        }
        if (char === "\\\\") {
          hookEventScanEscape = true;
          continue;
        }
        if (char !== '"') {
          if (hookEventScanStringRole !== "ignored") {
            if (hookEventScanToken.length < 64) hookEventScanToken += char;
            else hookEventScanTokenOverflow = true;
          }
          continue;
        }
        hookEventScanInString = false;
        if (hookEventScanStringRole === "key") {
          hookEventScanPendingKey = hookEventScanTokenOverflow ? null : hookEventScanToken;
          hookEventScanStage = "colon";
        } else if (hookEventScanStringRole === "value") {
          const scanned = !hookEventScanTokenOverflow
            && (hookEventScanPendingKey === "hook_event_name" || hookEventScanPendingKey === "hookEventName")
            && (hookEventScanToken === "Stop" || hookEventScanToken === "UserPromptSubmit")
            ? hookEventScanToken as CodexHookEventName
            : null;
          if (scanned !== null) {
            if (scannedHookEventName !== null && scannedHookEventName !== scanned) hookEventScanConflict = true;
            else scannedHookEventName = scanned;
          }
          hookEventScanStage = "afterValue";
        }
        hookEventScanStringRole = "ignored";
        continue;
      }
      if (char === '"') {
        hookEventScanInString = true;
        hookEventScanEscape = false;
        hookEventScanToken = "";
        hookEventScanTokenOverflow = false;
        hookEventScanStringRole = hookEventScanDepth === 1 && hookEventScanStage === "key"
          ? "key"
          : hookEventScanDepth === 1 && hookEventScanStage === "value"
            ? "value"
            : "ignored";
        continue;
      }
      if (char === "{" || char === "[") {
        hookEventScanDepth += 1;
        if (hookEventScanDepth === 1 && char === "{") hookEventScanStage = "key";
        else if (hookEventScanDepth === 2 && hookEventScanStage === "value") hookEventScanStage = "afterValue";
        continue;
      }
      if (char === "}" || char === "]") {
        if (hookEventScanDepth > 0) hookEventScanDepth -= 1;
        if (hookEventScanDepth === 1) hookEventScanStage = "afterValue";
        continue;
      }
      if (hookEventScanDepth !== 1) continue;
      if (hookEventScanStage === "colon" && char === ":") hookEventScanStage = "value";
      else if (hookEventScanStage === "afterValue" && char === ",") {
        hookEventScanPendingKey = null;
        hookEventScanStage = "key";
      } else if (hookEventScanStage === "value" && !/\\s/.test(char)) hookEventScanStage = "afterValue";
    }
    if (oversized) continue;
    totalBytes += buffer.byteLength;
    if (totalBytes > MAX_NATIVE_STDIN_JSON_BYTES) {
      const remaining = Math.max(0, MAX_NATIVE_STDIN_JSON_BYTES - (totalBytes - buffer.byteLength));
      if (remaining > 0) chunks.push(Buffer.from(buffer.subarray(0, remaining)));
      oversized = true;
      continue;
    }
"""
_TS_RAW_EVENT_OLD = """  const rawHookEventName = extractRawCodexHookEventName(raw);
"""
_TS_RAW_EVENT_NEW = """  const rawHookEventName = hookEventScanConflict
    ? null
    : extractRawCodexHookEventName(raw) ?? scannedHookEventName;
"""
_TS_PROMPT_OLD = """  if (rawHookEventName === "Stop") {
    return await buildOversizedStopActiveWorkflowOutput(cwd) ?? buildOversizedStopInactiveWorkflowOutput();
  }
  return {
"""
_TS_PROMPT_NEW = """  if (rawHookEventName === "Stop") {
    return await buildOversizedStopActiveWorkflowOutput(cwd) ?? buildOversizedStopInactiveWorkflowOutput();
  }
  if (rawHookEventName === "UserPromptSubmit") {
    return {};
  }
  return {
"""
_JS_DRAIN_OLD = """async function readStdinJson() {
    const chunks = [];
    let totalBytes = 0;
    let oversized = false;
    for await (const chunk of process.stdin) {
        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
        totalBytes += buffer.byteLength;
        if (totalBytes > MAX_NATIVE_STDIN_JSON_BYTES) {
            const remaining = Math.max(0, MAX_NATIVE_STDIN_JSON_BYTES - (totalBytes - buffer.byteLength));
            if (remaining > 0)
                chunks.push(Buffer.from(buffer.subarray(0, remaining)));
            oversized = true;
            process.stdin.destroy();
            break;
        }
"""
_JS_DRAIN_NEW = """async function readStdinJson() {
    const chunks = [];
    let totalBytes = 0;
    let oversized = false;
    let hookEventScanDepth = 0;
    let hookEventScanInString = false;
    let hookEventScanEscape = false;
    let hookEventScanToken = "";
    let hookEventScanTokenOverflow = false;
    let hookEventScanPendingKey = null;
    let hookEventScanStringRole = "ignored";
    let hookEventScanStage = "root";
    let scannedHookEventName = null;
    let hookEventScanConflict = false;
    for await (const chunk of process.stdin) {
        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
        for (const char of buffer.toString("utf-8")) {
            if (hookEventScanInString) {
                if (hookEventScanEscape) {
                    hookEventScanEscape = false;
                    if (hookEventScanStringRole !== "ignored")
                        hookEventScanTokenOverflow = true;
                    continue;
                }
                if (char === "\\\\") {
                    hookEventScanEscape = true;
                    continue;
                }
                if (char !== '"') {
                    if (hookEventScanStringRole !== "ignored") {
                        if (hookEventScanToken.length < 64)
                            hookEventScanToken += char;
                        else
                            hookEventScanTokenOverflow = true;
                    }
                    continue;
                }
                hookEventScanInString = false;
                if (hookEventScanStringRole === "key") {
                    hookEventScanPendingKey = hookEventScanTokenOverflow ? null : hookEventScanToken;
                    hookEventScanStage = "colon";
                }
                else if (hookEventScanStringRole === "value") {
                    const scanned = !hookEventScanTokenOverflow
                        && (hookEventScanPendingKey === "hook_event_name" || hookEventScanPendingKey === "hookEventName")
                        && (hookEventScanToken === "Stop" || hookEventScanToken === "UserPromptSubmit")
                        ? hookEventScanToken
                        : null;
                    if (scanned !== null) {
                        if (scannedHookEventName !== null && scannedHookEventName !== scanned)
                            hookEventScanConflict = true;
                        else
                            scannedHookEventName = scanned;
                    }
                    hookEventScanStage = "afterValue";
                }
                hookEventScanStringRole = "ignored";
                continue;
            }
            if (char === '"') {
                hookEventScanInString = true;
                hookEventScanEscape = false;
                hookEventScanToken = "";
                hookEventScanTokenOverflow = false;
                hookEventScanStringRole = hookEventScanDepth === 1 && hookEventScanStage === "key"
                    ? "key"
                    : hookEventScanDepth === 1 && hookEventScanStage === "value"
                        ? "value"
                        : "ignored";
                continue;
            }
            if (char === "{" || char === "[") {
                hookEventScanDepth += 1;
                if (hookEventScanDepth === 1 && char === "{")
                    hookEventScanStage = "key";
                else if (hookEventScanDepth === 2 && hookEventScanStage === "value")
                    hookEventScanStage = "afterValue";
                continue;
            }
            if (char === "}" || char === "]") {
                if (hookEventScanDepth > 0)
                    hookEventScanDepth -= 1;
                if (hookEventScanDepth === 1)
                    hookEventScanStage = "afterValue";
                continue;
            }
            if (hookEventScanDepth !== 1)
                continue;
            if (hookEventScanStage === "colon" && char === ":")
                hookEventScanStage = "value";
            else if (hookEventScanStage === "afterValue" && char === ",") {
                hookEventScanPendingKey = null;
                hookEventScanStage = "key";
            }
            else if (hookEventScanStage === "value" && !/\\s/.test(char))
                hookEventScanStage = "afterValue";
        }
        if (oversized)
            continue;
        totalBytes += buffer.byteLength;
        if (totalBytes > MAX_NATIVE_STDIN_JSON_BYTES) {
            const remaining = Math.max(0, MAX_NATIVE_STDIN_JSON_BYTES - (totalBytes - buffer.byteLength));
            if (remaining > 0)
                chunks.push(Buffer.from(buffer.subarray(0, remaining)));
            oversized = true;
            continue;
        }
"""
_JS_RAW_EVENT_OLD = """    const rawHookEventName = extractRawCodexHookEventName(raw);
"""
_JS_RAW_EVENT_NEW = """    const rawHookEventName = hookEventScanConflict
        ? null
        : extractRawCodexHookEventName(raw) ?? scannedHookEventName;
"""
_JS_PROMPT_OLD = """    if (rawHookEventName === "Stop") {
        return await buildOversizedStopActiveWorkflowOutput(cwd) ?? buildOversizedStopInactiveWorkflowOutput();
    }
    return {
"""
_JS_PROMPT_NEW = """    if (rawHookEventName === "Stop") {
        return await buildOversizedStopActiveWorkflowOutput(cwd) ?? buildOversizedStopInactiveWorkflowOutput();
    }
    if (rawHookEventName === "UserPromptSubmit") {
        return {};
    }
    return {
"""


class OmxOverlayError(RuntimeError):
    """Raised when the installed OMX package is not a verified patch target."""


@dataclass(frozen=True)
class OmxOverlayResult:
    version: str
    revision: str
    status: str
    before: Mapping[str, str]
    after: Mapping[str, str]


def apply_omx_overlay(
    package_root: Path | str,
    *,
    expected_version: str = PINNED_OMX_VERSION,
    accepted_preimage_checksums: Mapping[str, str] | None = None,
    accepted_postimage_checksums: Mapping[str, str] | None = None,
) -> OmxOverlayResult:
    root = Path(package_root)
    version = _package_version(root)
    if version != expected_version:
        raise OmxOverlayError(f"OMX overlay requires {expected_version}, found {version}")

    paths = {relative: root / relative for relative in OVERLAY_FILES}
    try:
        originals = {relative: path.read_text(encoding="utf-8") for relative, path in paths.items()}
    except OSError as exc:
        raise OmxOverlayError(f"Missing OMX overlay target: {exc}") from exc
    before = {relative: sha256_file(path) for relative, path in paths.items()}
    states = {
        "dist/scripts/codex-native-hook.js": _overlay_state(
            originals["dist/scripts/codex-native-hook.js"],
            _JS_DRAIN_OLD,
            _JS_DRAIN_NEW,
            _JS_RAW_EVENT_OLD,
            _JS_RAW_EVENT_NEW,
            _JS_PROMPT_OLD,
            _JS_PROMPT_NEW,
        ),
        "src/scripts/codex-native-hook.ts": _overlay_state(
            originals["src/scripts/codex-native-hook.ts"],
            _TS_DRAIN_OLD,
            _TS_DRAIN_NEW,
            _TS_RAW_EVENT_OLD,
            _TS_RAW_EVENT_NEW,
            _TS_PROMPT_OLD,
            _TS_PROMPT_NEW,
        ),
    }
    if set(states.values()) == {"new"}:
        accepted_postimage = dict(accepted_postimage_checksums or OFFICIAL_POSTIMAGE_CHECKSUMS)
        _validate_checksums("postimage", before, accepted_postimage)
        return OmxOverlayResult(version, OVERLAY_REVISION, "already_applied", before, before)
    if set(states.values()) != {"old"}:
        raise OmxOverlayError(f"OMX overlay targets are in a mixed or unknown state: {states}")

    accepted = dict(accepted_preimage_checksums or OFFICIAL_PREIMAGE_CHECKSUMS)
    _validate_checksums("preimage", before, accepted)

    updated = {
        "dist/scripts/codex-native-hook.js": originals["dist/scripts/codex-native-hook.js"]
        .replace(_JS_DRAIN_OLD, _JS_DRAIN_NEW, 1)
        .replace(_JS_RAW_EVENT_OLD, _JS_RAW_EVENT_NEW, 1)
        .replace(_JS_PROMPT_OLD, _JS_PROMPT_NEW, 1),
        "src/scripts/codex-native-hook.ts": originals["src/scripts/codex-native-hook.ts"]
        .replace(_TS_DRAIN_OLD, _TS_DRAIN_NEW, 1)
        .replace(_TS_RAW_EVENT_OLD, _TS_RAW_EVENT_NEW, 1)
        .replace(_TS_PROMPT_OLD, _TS_PROMPT_NEW, 1),
    }
    try:
        for relative, path in paths.items():
            temporary = path.with_suffix(path.suffix + ".harness-overlay.tmp")
            temporary.write_text(updated[relative], encoding="utf-8")
        for path in paths.values():
            os.replace(path.with_suffix(path.suffix + ".harness-overlay.tmp"), path)
    except Exception as exc:
        for relative, path in paths.items():
            path.write_text(originals[relative], encoding="utf-8")
            path.with_suffix(path.suffix + ".harness-overlay.tmp").unlink(missing_ok=True)
        raise OmxOverlayError(f"Failed to apply OMX overlay: {exc}") from exc

    after = {relative: sha256_file(path) for relative, path in paths.items()}
    if accepted_postimage_checksums is not None or accepted_preimage_checksums is None:
        _validate_checksums(
            "postimage",
            after,
            dict(accepted_postimage_checksums or OFFICIAL_POSTIMAGE_CHECKSUMS),
        )
    return OmxOverlayResult(version, OVERLAY_REVISION, "applied", before, after)


def _validate_checksums(label: str, actual: Mapping[str, str], expected: Mapping[str, str]) -> None:
    for relative, checksum in actual.items():
        if expected.get(relative) != checksum:
            raise OmxOverlayError(f"OMX {label} checksum mismatch for {relative}: {checksum}")


def _overlay_state(
    text: str,
    old_drain: str,
    new_drain: str,
    old_raw_event: str,
    new_raw_event: str,
    old_prompt: str,
    new_prompt: str,
) -> str:
    old = all(fragment in text for fragment in (old_drain, old_raw_event, old_prompt)) and not any(
        fragment in text for fragment in (new_drain, new_raw_event, new_prompt)
    )
    new = all(fragment in text for fragment in (new_drain, new_raw_event, new_prompt)) and not any(
        fragment in text for fragment in (old_drain, old_raw_event, old_prompt)
    )
    if old:
        return "old"
    if new:
        return "new"
    return "unknown"


def _package_version(root: Path) -> str:
    try:
        payload = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OmxOverlayError(f"Cannot read OMX package.json: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("name") != "oh-my-codex":
        raise OmxOverlayError("Target is not the oh-my-codex package")
    return str(payload.get("version", ""))


__all__ = [
    "OFFICIAL_PREIMAGE_CHECKSUMS",
    "OFFICIAL_POSTIMAGE_CHECKSUMS",
    "OMX_TARBALL_INTEGRITY",
    "OMX_TARBALL_URL",
    "OVERLAY_FILES",
    "OVERLAY_REVISION",
    "PINNED_OMX_VERSION",
    "OmxOverlayError",
    "OmxOverlayResult",
    "apply_omx_overlay",
]
