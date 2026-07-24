import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from personal_harness.omx_overlay import OmxOverlayError, apply_omx_overlay


SOURCE_OLD = """async function readStdinJson(): Promise<NativeHookCliReadResult> {
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
    chunks.push(buffer);
  }
  const raw = Buffer.concat(chunks).toString("utf-8").trim();
  const rawHookEventName = extractRawCodexHookEventName(raw);
}

async function buildOversizedStdinHookOutput(rawHookEventName: CodexHookEventName | null, cwd: string) {
  if (rawHookEventName === "Stop") {
    return await buildOversizedStopActiveWorkflowOutput(cwd) ?? buildOversizedStopInactiveWorkflowOutput();
  }
  return {
    continue: false,
    stopReason: "native_hook_stdin_oversized",
  };
}
"""

DIST_OLD = """const MAX_NATIVE_STDIN_JSON_BYTES = 1024;
function extractRawCodexHookEventName(raw) {
    const match = raw.match(/\"hook_event_name\"\\s*:\\s*\"(UserPromptSubmit|Stop)\"/);
    return match ? match[1] : null;
}
async function readStdinJson() {
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
        chunks.push(buffer);
    }
    const raw = Buffer.concat(chunks).toString("utf-8").trim();
    const rawHookEventName = extractRawCodexHookEventName(raw);
    return { oversized, rawHookEventName };
}
async function buildOversizedStdinHookOutput(rawHookEventName, cwd) {
    if (rawHookEventName === "Stop") {
        return await buildOversizedStopActiveWorkflowOutput(cwd) ?? buildOversizedStopInactiveWorkflowOutput();
    }
    return {
        continue: false,
        stopReason: "native_hook_stdin_oversized",
    };
}
process.stdout.write(JSON.stringify(await readStdinJson()) + "\\n");
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _make_package(root: Path, *, version: str = "0.20.2") -> dict[str, str]:
    (root / "dist" / "scripts").mkdir(parents=True)
    (root / "src" / "scripts").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "oh-my-codex", "version": version}))
    (root / "dist" / "scripts" / "codex-native-hook.js").write_text(DIST_OLD)
    (root / "src" / "scripts" / "codex-native-hook.ts").write_text(SOURCE_OLD)
    return {
        "dist/scripts/codex-native-hook.js": _sha(DIST_OLD),
        "src/scripts/codex-native-hook.ts": _sha(SOURCE_OLD),
    }


class TestOmxOverlay(unittest.TestCase):
    def test_applies_exact_overlay_to_source_and_dist_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            checksums = _make_package(root)

            result = apply_omx_overlay(root, accepted_preimage_checksums=checksums)
            source = (root / "src" / "scripts" / "codex-native-hook.ts").read_text()
            dist = (root / "dist" / "scripts" / "codex-native-hook.js").read_text()
            repeated = apply_omx_overlay(
                root,
                accepted_preimage_checksums=checksums,
                accepted_postimage_checksums=result.after,
            )

        self.assertEqual(result.status, "applied")
        self.assertEqual(result.version, "0.20.2")
        self.assertNotIn("process.stdin.destroy()", source + dist)
        self.assertIn("if (oversized) continue;", source)
        self.assertIn("if (oversized)\n            continue;", dist)
        self.assertIn("hookEventScanDepth", source + dist)
        self.assertIn("hookEventScanTokenOverflow", source + dist)
        self.assertIn("hookEventScanConflict", source + dist)
        self.assertIn("extractRawCodexHookEventName(raw) ?? scannedHookEventName", source + dist)
        self.assertNotIn("|event|name", source + dist)
        self.assertNotIn('rawHookEventName === "UserPromptSubmit" || rawHookEventName === null', source + dist)
        self.assertIn('rawHookEventName === "UserPromptSubmit"', source + dist)
        self.assertEqual(repeated.status, "already_applied")
        self.assertEqual(repeated.after, result.after)

    def test_rejects_drifted_already_applied_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            checksums = _make_package(root)
            applied = apply_omx_overlay(root, accepted_preimage_checksums=checksums)
            dist = root / "dist" / "scripts" / "codex-native-hook.js"
            dist.write_text(dist.read_text() + "// unverified drift\n")

            with self.assertRaisesRegex(OmxOverlayError, "postimage checksum"):
                apply_omx_overlay(
                    root,
                    accepted_preimage_checksums=checksums,
                    accepted_postimage_checksums=applied.after,
                )

    def test_streaming_scan_recognizes_stop_after_oversized_field(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            checksums = _make_package(root)
            apply_omx_overlay(root, accepted_preimage_checksums=checksums)
            hook = root / "dist" / "scripts" / "codex-native-hook.js"
            payload = json.dumps({"attachment": "A" * 4096, "hook_event_name": "Stop"})

            completed = subprocess.run(
                ["node", str(hook)],
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"oversized": True, "rawHookEventName": "Stop"})

    def test_streaming_scan_keeps_canonical_event_across_chunk_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            checksums = _make_package(root)
            apply_omx_overlay(root, accepted_preimage_checksums=checksums)
            hook = root / "dist" / "scripts" / "codex-native-hook.js"
            payload = json.dumps({"attachment": "A" * 4096, "hook_event_name": "Stop"}).encode()
            boundary = payload.index(b"hook_event_name") + len(b"hook_event_")
            process = subprocess.Popen(
                ["node", str(hook)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdin is not None
            process.stdin.write(payload[:boundary])
            process.stdin.flush()
            time.sleep(0.05)
            process.stdin.write(payload[boundary:])
            process.stdin.close()
            exit_code = process.wait(timeout=5)
            assert process.stdout is not None
            assert process.stderr is not None
            stdout = process.stdout.read().decode()
            stderr = process.stderr.read().decode()
            process.stdout.close()
            process.stderr.close()

        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout), {"oversized": True, "rawHookEventName": "Stop"})

    def test_conflicting_canonical_event_names_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            checksums = _make_package(root)
            apply_omx_overlay(root, accepted_preimage_checksums=checksums)
            hook = root / "dist" / "scripts" / "codex-native-hook.js"
            payload = (
                '{"attachment":"'
                + "A" * 4096
                + '","hook_event_name":"Stop","hookEventName":"UserPromptSubmit"}'
            )

            completed = subprocess.run(
                ["node", str(hook)],
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"oversized": True, "rawHookEventName": None})

    def test_nested_canonical_event_does_not_override_top_level_stop(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            checksums = _make_package(root)
            apply_omx_overlay(root, accepted_preimage_checksums=checksums)
            hook = root / "dist" / "scripts" / "codex-native-hook.js"
            payload = json.dumps(
                {
                    "attachment": "A" * 4096,
                    "hook_event_name": "Stop",
                    "metadata": {"hook_event_name": "UserPromptSubmit", "name": "UserPromptSubmit"},
                }
            )

            completed = subprocess.run(
                ["node", str(hook)],
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"oversized": True, "rawHookEventName": "Stop"})

    def test_rejects_wrong_version_unknown_checksum_and_mixed_state_without_partial_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            checksums = _make_package(root, version="0.20.1")
            with self.assertRaisesRegex(OmxOverlayError, "0.20.2"):
                apply_omx_overlay(root, accepted_preimage_checksums=checksums)

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            checksums = _make_package(root)
            source_path = root / "src" / "scripts" / "codex-native-hook.ts"
            source_path.write_text(SOURCE_OLD + "// unexpected upstream edit\n")
            before_source = source_path.read_text()
            before_dist = (root / "dist" / "scripts" / "codex-native-hook.js").read_text()

            with self.assertRaisesRegex(OmxOverlayError, "checksum"):
                apply_omx_overlay(root, accepted_preimage_checksums=checksums)

            self.assertEqual(source_path.read_text(), before_source)
            self.assertEqual((root / "dist" / "scripts" / "codex-native-hook.js").read_text(), before_dist)

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            checksums = _make_package(root)
            first = apply_omx_overlay(root, accepted_preimage_checksums=checksums)
            source_path = root / "src" / "scripts" / "codex-native-hook.ts"
            patched_source = source_path.read_text()
            dist_path = root / "dist" / "scripts" / "codex-native-hook.js"
            dist_path.write_text(DIST_OLD)

            with self.assertRaisesRegex(OmxOverlayError, "mixed"):
                apply_omx_overlay(root, accepted_preimage_checksums=checksums)

            self.assertEqual(source_path.read_text(), patched_source)
            self.assertEqual(dist_path.read_text(), DIST_OLD)
            self.assertNotEqual(first.after["dist/scripts/codex-native-hook.js"], _sha(DIST_OLD))


if __name__ == "__main__":
    unittest.main()
