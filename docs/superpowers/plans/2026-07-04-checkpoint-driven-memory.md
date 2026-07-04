# Checkpoint-Driven Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add harness-agent driven hot/warm/archive memory that is updated from major flow checkpoints.

**Architecture:** `personal_harness.memory` owns deterministic memory entry validation, secret rejection, markdown persistence, and hot/warm/archive rotation. `record_flow_checkpoint()` can optionally pass an accepted memory entry to that module, while `harness-agent` exposes the CLI surface used by Codex workflows and hooks.

**Tech Stack:** Python standard library, existing `.harness` runtime files, `unittest`.

---

### Task 1: Memory Core

**Files:**
- Create: `personal_harness/memory.py`
- Test: `tests/test_memory.py`

- [ ] Write failing tests for allowed categories, forbidden secret rejection, hot 7-day/20-entry cap, warm 8-30-day layer, and archive older-than-30-day layer.
- [ ] Implement `MemoryEntry`, `MemorySyncResult`, `sync_checkpoint_memory()`, and markdown read/write helpers.
- [ ] Run `python3 -m unittest tests.test_memory -v`.

### Task 2: Checkpoint Integration

**Files:**
- Modify: `personal_harness/flow_checkpoint.py`
- Test: `tests/test_flow_checkpoint.py`

- [ ] Write failing tests showing checkpoint memory entry writes `.harness/memory/hot.md` and updates state metadata.
- [ ] Add optional `memory_entry` and `sync_memory` parameters to `record_flow_checkpoint()`.
- [ ] Run `python3 -m unittest tests.test_flow_checkpoint -v`.

### Task 3: harness-agent CLI

**Files:**
- Modify: `personal_harness/harness_command.py`
- Test: `tests/test_harness_command.py`

- [ ] Write failing tests for `--memory-category`, `--memory-text`, `--memory-source`, `--memory-reason`, `--memory-sync`, and `--no-memory-sync`.
- [ ] Implement CLI validation and JSON output with memory sync status.
- [ ] Run `python3 -m unittest tests.test_harness_command -v`.

### Task 4: Repo Guidance And Docs

**Files:**
- Modify: `personal_harness/launcher.py`
- Modify: `README.md`
- Test: `tests/test_launcher.py`

- [ ] Update generated `AGENTS.md` to document hot/warm/archive memory and checkpoint-driven memory rules.
- [ ] Update README with command examples and allowed memory categories.
- [ ] Run launcher and packaging tests.

### Task 5: Verification

- [ ] Run targeted tests: `python3 -m unittest tests.test_memory tests.test_flow_checkpoint tests.test_harness_command tests.test_launcher tests.test_packaging -v`.
- [ ] Run full tests: `python3 -m unittest discover -s tests -p 'test_*.py' -v`.
- [ ] Run compile check: `python3 -m compileall personal_harness tests scripts`.
- [ ] Run packaging dry-run: `python3 -m pip install --dry-run .`.
- [ ] Run `git diff --check`.
