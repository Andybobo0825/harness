# AGENTS.md

This repository is prepared for `harness-codex`.

## Repository Boundary

- Treat this checkout directory as the project root.
- Keep runtime state under `.harness/`; do not use `.omx/` as product runtime state.
- Preserve user work. Do not run destructive git commands such as `git reset`, `git checkout`, or `git clean` unless the user explicitly asks.

## Harness Memory

- `.harness/state/personal-harness-state.json` stores the active/closed harness session state.
- `.harness/replay/replay.jsonl` stores execution and verification evidence.
- `.harness/flow-checkpoints/checkpoints.jsonl` stores major workflow checkpoints during long Codex sessions.
- `.harness/candidates/` stores candidate request/response/gate artifacts when iteration is needed.

## Coding Workflow

- Use the smallest workflow that fits the user's request.
- Let Codex and available skills choose the coding workflow dynamically from the request, repo context, and AGENTS.md.
- After each major workflow, record evidence and verification with harness flow checkpoints instead of waiting only for final Codex exit.
- When a major workflow finishes or fails, call `harness-agent --flow-checkpoint ...` from the agent workflow or hook; users should not need to record checkpoints manually.
- Prefer tests before behavior changes and verify before claiming completion.
