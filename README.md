# Harness Coding Agent

Start Codex as a repo-scoped coding agent with automatic checkpoints, selective memory, HUD status, and replayable execution evidence.

[![Version](https://img.shields.io/badge/version-1.0.0-blue)](./pyproject.toml)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-brightgreen)](https://www.python.org/)
[![Runtime](https://img.shields.io/badge/runtime-.harness%2F-6f42c1)](#what-harness-writes)

Harness Coding Agent does not replace Codex. It keeps Codex as the execution engine and adds a local runtime around it so each repository can carry its own agent boundary, checkpoints, memory, replay evidence, and recovery state.

## What It Is

Harness is a thin product layer for day-to-day Codex coding sessions:

- `harness-codex` launches Codex with repo preparation, lifecycle checkpoints, optional tmux HUD, and capture-on-exit.
- `harness-agent` records major workflow checkpoints and writes selective hot/warm/archive memory.
- `harness-status` prints the current harness session state for humans, scripts, or HUD panes.
- `harness-capture-codex` converts Codex session JSONL into `.harness` replay/state records.

OMX can still be useful while developing this repo, but it is not the product runtime. Harness-owned state lives under `.harness/` in the target repository.

## Why Use It

Use Harness if you already use Codex CLI and want each coding repo to have:

- a clear repo-local `AGENTS.md` boundary
- automatic git initialization for fresh coding repos
- session start / complete / failed lifecycle checkpoints
- checkpoint-driven memory that records only durable decisions, corrections, milestones, and verified facts
- capture-on-exit as a fallback transcript-to-replay path
- a one-line status surface for long sessions
- non-destructive evidence collection for later debugging or iteration

If you only want plain Codex with no persistent repo memory or replay trail, you probably do not need Harness.

## Install

Requirements:

- Python 3.11+
- OpenAI Codex CLI installed and authenticated
- `tmux` if you want the recommended HUD pane experience

Install from this repository:

```bash
python3 -m pip install git+https://github.com/Andybobo0825/harness.git
```

For local development from a clone:

```bash
python3 -m pip install -e .
```

Verify the installed commands:

```bash
harness-codex --help
harness-agent --help
harness-status --help
harness-capture-codex --help
```

## Quick Start

Create or enter a coding repo, then start Harness:

```bash
mkdir my-coding-repo
harness-codex --root my-coding-repo
```

Before Codex starts, Harness will:

- create the target directory if needed
- run `git init -q` if the target is not already a git repo
- create a repo-local `AGENTS.md` if one does not exist
- write active session state under `.harness/state/`
- record a `harness-codex-session-started` checkpoint

Codex then runs with the target repo as its working directory.

## Recommended First Smoke Test

Use a tiny task so you can inspect the full loop:

```bash
harness-codex --root /tmp/harness-smoke --no-auto-tmux --no-tmux-status -- \
  exec "Create stats_cli.py with a sum command, add unittest coverage, run python3 -m unittest -v, then record a harness-agent flow checkpoint with milestone memory."
```

After it exits, inspect:

```bash
harness-status --root /tmp/harness-smoke
cat /tmp/harness-smoke/.harness/flow-checkpoints/checkpoints.jsonl
cat /tmp/harness-smoke/.harness/memory/hot.md
```

## Daily Workflow

The default path is intentionally simple:

1. Start from the repo you want Codex to edit.
2. Launch with `harness-codex --root .`.
3. Let Codex choose the coding workflow from the request, repo context, and `AGENTS.md`.
4. At each major workflow boundary, record a checkpoint through `harness-agent`.
5. Let capture-on-exit record the final transcript as a fallback.

Manual checkpoint example:

```bash
harness-agent --root . \
  --flow-checkpoint \
  --flow-id fix-tests \
  --status complete \
  --evidence "python3 -m unittest -v passed" \
  --memory-category milestone \
  --memory-text "Unit test fix completed and verified." \
  --memory-source "flow:fix-tests" \
  --json
```

## Checkpoint-Driven Memory

Harness memory is selective. It is for durable repo knowledge, not transcripts.

Allowed categories:

| Category | Use it for |
| --- | --- |
| `decision` | Accepted project or workflow decisions |
| `correction` | Mistakes, failed assumptions, and verified fixes |
| `milestone` | Delivered or verified progress |
| `verified-fact` | Facts established by inspection, tests, or official sources |

Memory layers:

```text
.harness/memory/hot.md      # recent memory, default layer
.harness/memory/warm.md     # older memory for manual retrieval
.harness/memory/archive.md  # cold historical memory
```

Harness rejects unsupported categories and secret-like memory text. Do not store raw transcripts, long logs, API keys, tokens, `.env` contents, personal data, speculation, or unresolved discussion.

## What Harness Writes

Harness writes runtime state only inside the target repo:

```text
.harness/state/personal-harness-state.json
.harness/replay/replay.jsonl
.harness/flow-checkpoints/checkpoints.jsonl
.harness/memory/hot.md
.harness/memory/warm.md
.harness/memory/archive.md
.harness/candidates/
```

The source repo is not required at runtime after installation. Installed CLI commands call package entrypoints, not local `scripts/...` paths.

## Command Reference

| Command | Purpose |
| --- | --- |
| `harness-codex --root .` | Launch Codex inside the Harness runtime |
| `harness-codex --root . --dry-run` | Print the Codex launch command without running it |
| `harness-codex --root . --no-capture-on-exit` | Disable final transcript capture |
| `harness-codex --root . --no-auto-checkpoint` | Disable lifecycle checkpoints |
| `harness-agent --flow-checkpoint ...` | Record a major workflow checkpoint |
| `harness-agent --memory-sync ...` | Rotate/write selective memory without a checkpoint |
| `harness-status --root . --compact` | Print one-line status |
| `harness-capture-codex --latest --cwd . ...` | Capture a Codex session JSONL manually |

## Safety Model

Harness is designed to observe and record before it changes strategy:

- no destructive git commands in checkpoint recording
- no overwrite of existing `AGENTS.md`
- no `.omx/` product runtime ownership
- no memory writes unless the entry passes category and sensitive-content checks
- no cross-repo session capture when `cwd` and launch time do not match

`harness-codex` can launch Codex in YOLO mode because that is the intended coding-agent path here. Use it only in repositories and environments where that level of local authority is acceptable.

## Architecture

The product surface is small, but the runtime keeps extension points for later harness evolution:

- `personal_harness.launcher` owns `harness-codex` and `harness-status`
- `personal_harness.flow_checkpoint` records non-destructive major-flow evidence
- `personal_harness.memory` owns hot/warm/archive memory validation and rotation
- `personal_harness.codex_capture` converts Codex session JSONL into execution records
- `personal_harness.execution_controller` turns executions into replay, state, and candidate requests
- `personal_harness.aegis`, `evolution`, `eval`, and `variants` provide the current adaptation/gating seams

The current LLM/coding backend is Codex itself. Candidate handoff uses files under `.harness/candidates/`; there is no separate model service hidden behind the CLI.

## Development

Run the local verification suite:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall personal_harness tests scripts
python3 -m pip install --dry-run .
git diff --check
```

## Current Boundaries

Version `1.0.0` is a local/global CLI release, not a hosted service.

Current intentional limits:

- no real GRPO or model fine-tuning yet
- no PyPI release workflow yet
- no automatic interactive candidate rewrite loop beyond the file-backed handoff
- variant routing and AEGIS candidate forking are present as seams, not a complete autonomous product loop

These are roadmap surfaces, not required setup steps for using `harness-codex`.
