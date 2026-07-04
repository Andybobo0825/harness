# Global CLI Auto Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make harness installable as global CLI commands and make `harness-codex` record automatic lifecycle checkpoints without manual user action.

**Architecture:** Add Python package metadata with console scripts that call the existing module entrypoints. Extend `run_harness_codex()` with default-on lifecycle checkpointing while preserving manual `harness-agent --flow-checkpoint` as a primitive for hooks and tests.

**Tech Stack:** Python stdlib, setuptools console scripts, unittest, existing `.harness` JSON/JSONL runtime.

---

### Task 1: Package Console Scripts

**Files:**
- Create: `pyproject.toml`
- Test: `tests/test_packaging.py`

- [ ] **Step 1: Write the failing packaging test**

```python
import pathlib
import tomllib
import unittest


class TestPackaging(unittest.TestCase):
    def test_console_scripts_are_declared(self):
        data = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
        scripts = data["project"]["scripts"]
        self.assertEqual(scripts["harness-codex"], "personal_harness.launcher:run_harness_codex")
        self.assertEqual(scripts["harness-status"], "personal_harness.launcher:status_main")
        self.assertEqual(scripts["harness-agent"], "personal_harness.harness_command:main")
        self.assertEqual(scripts["harness-capture-codex"], "personal_harness.codex_capture_command:main")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_packaging -v`
Expected: FAIL because `pyproject.toml` does not exist.

- [ ] **Step 3: Add minimal `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "personal-harness"
version = "0.1.0"
requires-python = ">=3.11"

[project.scripts]
harness-codex = "personal_harness.launcher:run_harness_codex"
harness-status = "personal_harness.launcher:status_main"
harness-agent = "personal_harness.harness_command:main"
harness-capture-codex = "personal_harness.codex_capture_command:main"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_packaging -v`
Expected: OK.

### Task 2: Auto Checkpoint Orchestration

**Files:**
- Modify: `personal_harness/launcher.py`
- Test: `tests/test_launcher.py`

- [ ] **Step 1: Write failing tests**

Add launcher tests asserting:
- successful `run_harness_codex()` writes automatic `harness-codex-session-started` and `harness-codex-session-complete` flow checkpoint records.
- nonzero runner return writes `harness-codex-session-failed`.
- `--no-auto-checkpoint` disables lifecycle checkpoint writes.

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_launcher -v`
Expected: FAIL because auto checkpoint flags and records are missing.

- [ ] **Step 3: Add minimal launcher implementation**

Add `--no-auto-checkpoint`; call `record_flow_checkpoint()` after session start and in `finally` after capture attempt. Use only `.harness` paths and preserve malformed-state behavior.

- [ ] **Step 4: Run targeted tests**

Run: `python3 -m unittest tests.test_launcher -v`
Expected: OK.

### Task 3: Documentation and Full Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document global install and auto checkpoint behavior**

Add short docs that installed users call `harness-codex --root .`, not source-repo scripts, and that lifecycle checkpoints are automatic by default.

- [ ] **Step 2: Run full verification**

Run:
- `python3 -m unittest discover -s tests -p 'test_*.py' -v`
- `python3 -m compileall personal_harness tests scripts`
- `git diff --check`

Expected: all pass.

## Self-Review

- Spec coverage: global CLI scripts, auto checkpoint orchestration, disable flag, target-root `.harness` ownership, and docs are covered.
- Placeholder scan: no TBD/TODO placeholders.
- Type consistency: entrypoint targets match existing module functions.
