import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from personal_harness.launcher import (
    build_harness_status_watch_command,
    build_codex_command,
    build_tmux_bootstrap_command,
    close_harness_session,
    mark_harness_session_started,
    render_harness_status,
    render_git_tree_status,
    run_harness_codex,
)
from personal_harness.replay import ReplayStore


def _iso_from_epoch(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write_codex_session(
    path: Path,
    *,
    cwd: Path,
    session_id: str = "sess-launch",
    timestamp: Optional[str] = None,
):
    if timestamp is None:
        timestamp = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {
                    "timestamp": timestamp,
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": str(cwd), "agent_role": "executor"},
                },
                {
                    "timestamp": "2026-07-01T02:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "verified"}],
                    },
                },
                {
                    "timestamp": "2026-07-01T02:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call-verify",
                        "name": "functions.exec_command",
                        "arguments": json.dumps({"cmd": "python3 -m unittest tests.test_launcher -v"}),
                    },
                },
                {
                    "timestamp": "2026-07-01T02:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-verify",
                        "output": "OK\nProcess exited with code 0",
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _agent_execution_records(root: Path):
    return [
        record
        for record in ReplayStore(root / ".harness" / "replay" / "replay.jsonl").read_all()
        if record.metadata.get("record_type") == "agent_execution"
    ]


class TestHarnessLauncher(unittest.TestCase):
    def test_build_codex_command_defaults_to_gpt56_sol_medium_yolo(self):
        command = build_codex_command(Path("/repo"), prompt_args=["修測試"])

        self.assertEqual(command[:3], ["env", "GIT_CEILING_DIRECTORIES=/", "codex"])
        self.assertIn("--model", command)
        self.assertIn("gpt-5.6-sol", command)
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("-C", command)
        self.assertIn("/repo", command)
        self.assertEqual(command[-1], "修測試")

    def test_session_state_starts_active_and_closes_inactive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            started_path = mark_harness_session_started(root, model="gpt-5.5", reasoning="high", yolo=True)
            started = json.loads(started_path.read_text(encoding="utf-8"))

            close_harness_session(root, exit_code=0)
            closed = json.loads(started_path.read_text(encoding="utf-8"))

        self.assertTrue(started["active"])
        self.assertEqual(started["phase"], "session")
        self.assertEqual(started["metadata"]["launch"]["model"], "gpt-5.5")
        self.assertEqual(started["metadata"]["launch"]["reasoning"], "high")
        self.assertTrue(started["metadata"]["launch"]["yolo"])
        self.assertRegex(started["metadata"]["launch"]["session_id"], r"^[0-9a-f]{32}$")
        self.assertFalse(closed["active"])
        self.assertEqual(closed["phase"], "closed")
        self.assertEqual(closed["metadata"]["exit_code"], 0)
        self.assertEqual(
            closed["metadata"]["launch"]["session_id"],
            started["metadata"]["launch"]["session_id"],
        )

    def test_session_lifecycle_preserves_malformed_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state_path = root / ".harness" / "state" / "personal-harness-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(json.JSONDecodeError):
                mark_harness_session_started(root, model="gpt-5.5", reasoning="high", yolo=True)
            with self.assertRaises(json.JSONDecodeError):
                close_harness_session(root, exit_code=0)
            state_text = state_path.read_text(encoding="utf-8")

        self.assertEqual(state_text, "{not-json")

    def test_status_mentions_harness_model_reasoning_and_yolo(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mark_harness_session_started(root, model="gpt-5.5", reasoning="high", yolo=True)
            status = render_harness_status(root)

        self.assertIn("[harness]", status)
        self.assertIn("gpt-5.5 high", status)
        self.assertIn("YOLO", status)
        self.assertIn("active", status)
        self.assertIn("git:", status)

    def test_colored_status_adds_ansi_sequences_when_requested(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mark_harness_session_started(root, model="gpt-5.5", reasoning="high", yolo=True)
            plain = render_harness_status(root, compact=True, color=False)
            colored = render_harness_status(root, compact=True, color=True)

        self.assertNotIn("\x1b[", plain)
        self.assertIn("\x1b[", colored)
        self.assertIn("[harness]", colored)
        self.assertIn("YOLO", colored)

    def test_git_tree_status_reports_no_git_repo(self):
        with tempfile.TemporaryDirectory() as d:
            status = render_git_tree_status(Path(d))

        self.assertEqual(status, "git:no-repo")

    def test_git_tree_status_does_not_walk_into_parent_home_repo(self):
        with tempfile.TemporaryDirectory() as d:
            home_repo = Path(d)
            root = home_repo / "Desktop" / "SideProject" / "harness"
            root.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=home_repo, check=True)
            (home_repo / "outside.txt").write_text("outside", encoding="utf-8")

            status = render_git_tree_status(root)

        self.assertEqual(status, "git:no-repo")

    def test_git_tree_status_reports_branch_clean_and_untracked_counts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)

            clean = render_git_tree_status(root)
            (root / "new.txt").write_text("hello", encoding="utf-8")
            dirty = render_git_tree_status(root)

        self.assertRegex(clean, r"^git:[^ ]+ clean$")
        self.assertRegex(dirty, r"^git:[^ ]+ untracked:1$")

    def test_status_marks_dead_pid_session_as_stale(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state_path = mark_harness_session_started(root, model="gpt-5.5", reasoning="high", yolo=True)
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw["metadata"]["launch"]["pid"] = 999999999
            state_path.write_text(json.dumps(raw), encoding="utf-8")

            status = render_harness_status(root)

        self.assertIn("[harness]", status)
        self.assertIn("stale", status)
        self.assertNotIn("active |", status)

    def test_script_wrappers_show_help(self):
        repo = Path(__file__).resolve().parents[1]
        for script in ["scripts/harness-codex", "scripts/harness-status"]:
            completed = subprocess.run(
                ["python3", script, "--help"],
                cwd=repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("harness", completed.stdout.lower())

    def test_harness_codex_starts_status_before_running_codex(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            seen = {}

            def fake_runner(command):
                seen["command"] = command
                state_path = root / ".harness" / "state" / "personal-harness-state.json"
                seen["state_during_run"] = json.loads(state_path.read_text(encoding="utf-8"))
                return subprocess.CompletedProcess(command, 0)

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = run_harness_codex(
                    ["--root", str(root), "--no-tmux-status", "--", "hello"],
                    runner=fake_runner,
                )

            final_state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertIn("[harness] active", stdout.getvalue())
        self.assertIn("gpt-5.6-sol medium", stdout.getvalue())
        self.assertTrue(seen["state_during_run"]["active"])
        self.assertEqual(seen["state_during_run"]["metadata"]["status"]["mode"], "inline")
        self.assertEqual(final_state["phase"], "closed")

    def test_harness_codex_prepares_fresh_repo_for_coding(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "new-repo"
            seen = {}

            def fake_runner(command):
                seen["command"] = command
                return subprocess.CompletedProcess(command, 0)

            exit_code = run_harness_codex(
                ["--root", str(root), "--no-tmux-status", "--quiet-status", "--no-capture-on-exit", "--", "hello"],
                runner=fake_runner,
            )

            git_root = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            ).stdout.strip()
            state_path = root / ".harness" / "state" / "personal-harness-state.json"
            root_exists = root.exists()
            agents_exists = (root / "AGENTS.md").exists()
            agents_text = (root / "AGENTS.md").read_text(encoding="utf-8")
            state_exists = state_path.exists()

        self.assertEqual(exit_code, 0)
        self.assertTrue(root_exists)
        self.assertEqual(Path(git_root).resolve(), root.resolve())
        self.assertTrue(agents_exists)
        self.assertTrue(state_exists)
        self.assertEqual(seen["command"][:3], ["env", f"GIT_CEILING_DIRECTORIES={root.parent.resolve()}", "codex"])
        self.assertIn("Repository Boundary", agents_text)
        self.assertIn(str(root.resolve()), agents_text)
        self.assertIn(".harness/state/personal-harness-state.json", agents_text)
        self.assertIn(".harness/replay/replay.jsonl", agents_text)
        self.assertIn(".harness/flow-checkpoints/checkpoints.jsonl", agents_text)
        self.assertIn(".harness/memory/hot.md", agents_text)
        self.assertIn(".harness/memory/warm.md", agents_text)
        self.assertIn(".harness/memory/archive.md", agents_text)
        self.assertIn(".harness/candidates/", agents_text)
        self.assertIn("Do not run destructive git commands", agents_text)
        self.assertIn("Let Codex and available skills choose the coding workflow dynamically", agents_text)
        self.assertIn("record evidence and verification with harness flow checkpoints", agents_text)
        self.assertIn("accepted decisions, corrections/lessons from failures, milestones, and verified facts", agents_text)
        self.assertIn("Users should not need to record checkpoints or rotate memory manually", agents_text)

    def test_harness_codex_preserves_existing_git_and_agents_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            agents_path = root / "AGENTS.md"
            agents_path.write_text("custom repo instructions\n", encoding="utf-8")

            exit_code = run_harness_codex(
                ["--root", str(root), "--no-tmux-status", "--quiet-status", "--no-capture-on-exit", "--", "hello"],
                runner=lambda command: subprocess.CompletedProcess(command, 0),
            )

            git_dir = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--git-dir"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            ).stdout.strip()
            agents_text = agents_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(git_dir, ".git")
        self.assertEqual(agents_text, "custom repo instructions\n")

    def test_harness_codex_rejects_malformed_state_without_overwriting(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state_path = root / ".harness" / "state" / "personal-harness-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{not-json", encoding="utf-8")
            stderr = StringIO()
            seen = {"runner_called": False}

            def fake_runner(command):
                seen["runner_called"] = True
                return subprocess.CompletedProcess(command, 0)

            with redirect_stderr(stderr):
                exit_code = run_harness_codex(
                    ["--root", str(root), "--no-tmux-status", "--quiet-status", "--no-capture-on-exit", "--", "hello"],
                    runner=fake_runner,
                )
            state_text = state_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 2)
        self.assertFalse(seen["runner_called"])
        self.assertEqual(state_text, "{not-json")
        self.assertIn("invalid existing harness state", stderr.getvalue())

    def test_harness_codex_records_automatic_success_checkpoints(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            exit_code = run_harness_codex(
                ["--root", str(root), "--no-tmux-status", "--quiet-status", "--no-capture-on-exit", "--", "hello"],
                runner=lambda command: subprocess.CompletedProcess(command, 0),
            )

            checkpoint_lines = (root / ".harness" / "flow-checkpoints" / "checkpoints.jsonl").read_text(encoding="utf-8").splitlines()
            checkpoints = [json.loads(line) for line in checkpoint_lines]
            flow_ids = [checkpoint["flow_id"] for checkpoint in checkpoints]
            final_state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(flow_ids, ["harness-codex-session-started", "harness-codex-session-complete"])
        self.assertEqual(checkpoints[-1]["status"], "complete")
        self.assertEqual(checkpoints[-1]["skill_context"]["orchestrator"], "harness-codex")
        self.assertRegex(checkpoints[0]["session_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(checkpoints[0]["session_id"], checkpoints[1]["session_id"])
        self.assertEqual(final_state["metadata"]["flow_checkpoints"][-1]["flow_id"], "harness-codex-session-complete")
        self.assertEqual(
            final_state["metadata"]["flow_checkpoints"][-1]["session_id"],
            checkpoints[-1]["session_id"],
        )

    def test_harness_codex_persists_capture_failure_in_lifecycle_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            empty_sessions = root / "empty-sessions"
            empty_sessions.mkdir()

            exit_code = run_harness_codex(
                [
                    "--root",
                    str(root),
                    "--no-tmux-status",
                    "--quiet-status",
                    "--capture-sessions-root",
                    str(empty_sessions),
                    "--",
                    "hello",
                ],
                runner=lambda command: subprocess.CompletedProcess(command, 0),
            )

            checkpoints = [
                json.loads(line)
                for line in (root / ".harness" / "flow-checkpoints" / "checkpoints.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            final_state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(checkpoints[-1]["details"]["capture_on_exit"], "failed")
        self.assertIn("FileNotFoundError", checkpoints[-1]["details"]["capture_error"])
        self.assertEqual(
            final_state["metadata"]["flow_checkpoints"][-1]["details"]["capture_error"],
            checkpoints[-1]["details"]["capture_error"],
        )

    def test_harness_codex_records_automatic_failed_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            exit_code = run_harness_codex(
                ["--root", str(root), "--no-tmux-status", "--quiet-status", "--no-capture-on-exit", "--", "hello"],
                runner=lambda command: subprocess.CompletedProcess(command, 7),
            )

            checkpoints = [
                json.loads(line)
                for line in (root / ".harness" / "flow-checkpoints" / "checkpoints.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(exit_code, 7)
        self.assertEqual(checkpoints[-1]["flow_id"], "harness-codex-session-failed")
        self.assertEqual(checkpoints[-1]["status"], "failed")
        self.assertIn("exit_code=7", checkpoints[-1]["evidence"])

    def test_harness_codex_can_disable_automatic_checkpoints(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            exit_code = run_harness_codex(
                [
                    "--root",
                    str(root),
                    "--no-tmux-status",
                    "--quiet-status",
                    "--no-capture-on-exit",
                    "--no-auto-checkpoint",
                    "--",
                    "hello",
                ],
                runner=lambda command: subprocess.CompletedProcess(command, 0),
            )

            checkpoint_path = root / ".harness" / "flow-checkpoints" / "checkpoints.jsonl"

        self.assertEqual(exit_code, 0)
        self.assertFalse(checkpoint_path.exists())

    def test_harness_codex_captures_latest_session_on_exit_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_dir = root / "codex-sessions" / "2026" / "07" / "01"
            session_path = session_dir / "rollout.jsonl"

            def fake_runner(command):
                _write_codex_session(session_path, cwd=root)
                return subprocess.CompletedProcess(command, 0)

            exit_code = run_harness_codex(
                [
                    "--root",
                    str(root),
                    "--no-tmux-status",
                    "--capture-sessions-root",
                    str(root / "codex-sessions"),
                    "--",
                    "hello",
                ],
                runner=fake_runner,
            )

            [record] = _agent_execution_records(root)
            final_state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(record.task_id, "sess-launch")
        self.assertTrue(record.solved)
        self.assertFalse(final_state["active"])
        self.assertEqual(final_state["phase"], "closed")
        self.assertEqual(final_state["metadata"]["last_task"]["task_id"], "sess-launch")
        self.assertEqual(final_state["metadata"]["capture_on_exit"]["status"], "recorded")

    def test_harness_codex_captures_current_launch_session_id_not_other_repo_or_later_session(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            other_root = root / "other-repo"
            other_root.mkdir()
            session_dir = root / "codex-sessions" / "2026" / "07" / "01"

            def fake_runner(command):
                state_path = root / ".harness" / "state" / "personal-harness-state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                started_at = state["metadata"]["launch"]["started_at"]
                old_iso = _iso_from_epoch(started_at - 10)
                target_iso = _iso_from_epoch(started_at + 0.001)
                later_iso = _iso_from_epoch(started_at + 10)
                self.assertGreater(started_at, 0)
                _write_codex_session(session_dir / "old-same.jsonl", cwd=root, session_id="sess-old", timestamp=old_iso)
                _write_codex_session(session_dir / "target.jsonl", cwd=root, session_id="sess-current-launch", timestamp=target_iso)
                _write_codex_session(session_dir / "later-same.jsonl", cwd=root, session_id="sess-later", timestamp=later_iso)
                _write_codex_session(session_dir / "other-project.jsonl", cwd=other_root, session_id="sess-other", timestamp=target_iso)
                return subprocess.CompletedProcess(command, 0)

            exit_code = run_harness_codex(
                [
                    "--root",
                    str(root),
                    "--no-tmux-status",
                    "--capture-sessions-root",
                    str(root / "codex-sessions"),
                    "--",
                    "hello",
                ],
                runner=fake_runner,
            )

            [record] = _agent_execution_records(root)
            final_state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(record.task_id, "sess-current-launch")
        self.assertEqual(final_state["metadata"]["capture_on_exit"]["session_id"], "sess-current-launch")
        self.assertEqual(final_state["metadata"]["captured_sessions"][0]["session_id"], "sess-current-launch")

    def test_harness_codex_can_disable_capture_on_exit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_dir = root / "codex-sessions" / "2026" / "07" / "01"
            session_path = session_dir / "rollout.jsonl"

            def fake_runner(command):
                _write_codex_session(session_path, cwd=root)
                return subprocess.CompletedProcess(command, 0)

            exit_code = run_harness_codex(
                [
                    "--root",
                    str(root),
                    "--no-tmux-status",
                    "--no-capture-on-exit",
                    "--capture-sessions-root",
                    str(root / "codex-sessions"),
                    "--",
                    "hello",
                ],
                runner=fake_runner,
            )

            replay_path = root / ".harness" / "replay" / "replay.jsonl"
            final_state = json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(replay_path.exists())
        self.assertNotIn("last_task", final_state["metadata"])
        self.assertNotIn("capture_on_exit", final_state["metadata"])

    def test_harness_codex_splits_tmux_hud_watch_pane_by_default_inside_tmux(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tmux_calls = []
            seen = {}

            def fake_tmux(args):
                tmux_calls.append(args)
                if args[:2] == ["show-option", "-qv"] and args[-1] == "status-left":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="old-left", stderr="")
                if args[:2] == ["show-option", "-qv"] and args[-1] == "mouse":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="off\n", stderr="")
                if args[:2] == ["display-message", "-p"] and args[-1] == "#W":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="old-window\n", stderr="")
                if args[:2] == ["display-message", "-p"] and args[-1] == "#{pane_id}":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="%1\n", stderr="")
                if args and args[0] == "split-window":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="%2\n", stderr="")
                return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

            with patch.dict("os.environ", {"TMUX": "/tmp/tmux,123,0"}, clear=False):
                with patch("personal_harness.launcher._tmux", fake_tmux):
                    exit_code = run_harness_codex(
                        ["--root", str(root), "--quiet-status", "--", "hello"],
                        runner=lambda command: (
                            seen.setdefault(
                                "state_during_run",
                                json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8")),
                            )
                            and subprocess.CompletedProcess(command, 0)
                        ),
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(seen["state_during_run"]["metadata"]["status"]["mode"], "tmux-hud-pane")
        split_command = next(call[-1] for call in tmux_calls if call and call[0] == "split-window")
        self.assertIn("HARNESS_TMUX_HUD_OWNER=1", split_command)
        self.assertIn("HARNESS_TMUX_HUD_LEADER_PANE=%1", split_command)
        self.assertIn("harness-status", split_command)
        self.assertIn("--watch", split_command)
        self.assertIn(["set-option", "mouse", "on"], tmux_calls)
        self.assertIn(["select-pane", "-t", "%2", "-P", "fg=colour81,bg=colour234"], tmux_calls)
        self.assertIn(["select-pane", "-t", "%1"], tmux_calls)
        self.assertIn(["kill-pane", "-t", "%2"], tmux_calls)
        self.assertIn(["set-option", "mouse", "off"], tmux_calls)
        self.assertNotIn(["show-option", "-qv", "status-left"], tmux_calls)
        self.assertFalse(any(call[:2] == ["set-option", "status-left"] for call in tmux_calls))

    def test_harness_codex_splits_tmux_hud_watch_pane_when_opted_in_and_kills_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tmux_calls = []
            seen = {}

            def fake_tmux(args):
                tmux_calls.append(args)
                if args[:2] == ["show-option", "-qv"] and args[-1] == "status-left":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="old-left", stderr="")
                if args[:2] == ["show-option", "-qv"] and args[-1] == "mouse":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="off\n", stderr="")
                if args[:2] == ["display-message", "-p"] and args[-1] == "#W":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="old-window\n", stderr="")
                if args[:2] == ["display-message", "-p"] and args[-1] == "#{pane_id}":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="%1\n", stderr="")
                if args and args[0] == "split-window":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="%2\n", stderr="")
                return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

            with patch.dict("os.environ", {"TMUX": "/tmp/tmux,123,0"}, clear=False):
                with patch("personal_harness.launcher._tmux", fake_tmux):
                    exit_code = run_harness_codex(
                        ["--root", str(root), "--tmux-hud", "--quiet-status", "--", "hello"],
                        runner=lambda command: (
                            seen.setdefault(
                                "state_during_run",
                                json.loads((root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8")),
                            )
                            and subprocess.CompletedProcess(command, 0)
                        ),
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(seen["state_during_run"]["metadata"]["status"]["mode"], "tmux-hud-pane")
        self.assertTrue(any(call and call[0] == "split-window" and "harness-status" in call[-1] and "--watch" in call[-1] for call in tmux_calls))
        self.assertIn(["select-pane", "-t", "%2", "-P", "fg=colour81,bg=colour234"], tmux_calls)
        self.assertIn(["kill-pane", "-t", "%2"], tmux_calls)

    def test_no_tmux_status_disables_hud_even_inside_tmux(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tmux_calls = []
            seen = {}

            def fake_runner(command):
                seen["command"] = command
                seen["state_during_run"] = json.loads(
                    (root / ".harness" / "state" / "personal-harness-state.json").read_text(encoding="utf-8")
                )
                return subprocess.CompletedProcess(command, 0)

            with patch.dict("os.environ", {"TMUX": "/tmp/tmux,123,0"}, clear=False):
                with patch("personal_harness.launcher._tmux", lambda args: tmux_calls.append(args)):
                    exit_code = run_harness_codex(
                        ["--root", str(root), "--no-tmux-status", "--quiet-status", "--no-capture-on-exit", "--", "hello"],
                        runner=fake_runner,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(seen["command"][:3], ["env", f"GIT_CEILING_DIRECTORIES={root.resolve().parent}", "codex"])
        self.assertEqual(seen["state_during_run"]["metadata"]["status"]["mode"], "inline")
        self.assertEqual(tmux_calls, [])

    def test_tmux_status_left_update_is_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tmux_calls = []

            def fake_tmux(args):
                tmux_calls.append(args)
                if args[:2] == ["show-option", "-qv"] and args[-1] == "status-left":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="old-left", stderr="")
                if args[:2] == ["show-option", "-qv"] and args[-1] == "mouse":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="off\n", stderr="")
                if args[:2] == ["display-message", "-p"] and args[-1] == "#W":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="old-window\n", stderr="")
                if args[:2] == ["display-message", "-p"] and args[-1] == "#{pane_id}":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="%1\n", stderr="")
                if args and args[0] == "split-window":
                    return subprocess.CompletedProcess(["tmux", *args], 0, stdout="%2\n", stderr="")
                return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

            with patch.dict("os.environ", {"TMUX": "/tmp/tmux,123,0", "HARNESS_TMUX_STATUS_LEFT": "1"}, clear=False):
                with patch("personal_harness.launcher._tmux", fake_tmux):
                    exit_code = run_harness_codex(
                        ["--root", str(root), "--quiet-status", "--no-capture-on-exit", "--", "hello"],
                        runner=lambda command: subprocess.CompletedProcess(command, 0),
                    )

        self.assertEqual(exit_code, 0)
        self.assertIn(["show-option", "-qv", "status-left"], tmux_calls)
        self.assertTrue(any(call[:2] == ["set-option", "status-left"] and "harness-status" in call[-1] for call in tmux_calls))
        self.assertFalse(
            any(call[:2] == ["set-option", "status-left"] and "/scripts/harness-status" in call[-1] for call in tmux_calls)
        )
        self.assertIn(["set-option", "status-left", "old-left"], tmux_calls)

    def test_harness_status_watch_command_uses_installed_status_command(self):
        command = build_harness_status_watch_command(Path("/repo root"))

        self.assertIn("GIT_CEILING_DIRECTORIES=/", command)
        self.assertIn("harness-status", command)
        self.assertNotIn("/scripts/harness-status", command)
        self.assertIn("--root", command)
        self.assertIn("--compact", command)
        self.assertIn("--watch", command)
        self.assertIn("--color always", command)

    def test_non_tmux_harness_codex_bootstraps_tmux_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            seen = {}

            def fake_runner(command):
                seen["command"] = command
                return subprocess.CompletedProcess(command, 0)

        with patch.dict("os.environ", {}, clear=True):
            exit_code = run_harness_codex(
                ["--root", str(root), "--no-capture-on-exit", "--quiet-status", "--", "hello"],
                runner=fake_runner,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(seen["command"][:2], ["tmux", "new-session"])
        self.assertIn("HARNESS_CODEX_TMUX_BOOTSTRAPPED=1", seen["command"][-1])
        self.assertIn(f"GIT_CEILING_DIRECTORIES={root.resolve().parent}", seen["command"][-1])
        self.assertIn("--tmux-hud", seen["command"][-1])
        self.assertIn("harness-codex", seen["command"][-1])
        self.assertNotIn("/scripts/harness-codex", seen["command"][-1])

    def test_no_auto_tmux_runs_codex_directly_outside_tmux(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            seen = {}

            def fake_runner(command):
                seen["command"] = command
                return subprocess.CompletedProcess(command, 0)

            with patch.dict("os.environ", {}, clear=True):
                exit_code = run_harness_codex(
                    ["--root", str(root), "--no-auto-tmux", "--no-capture-on-exit", "--quiet-status", "--", "hello"],
                    runner=fake_runner,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(seen["command"][:3], ["env", f"GIT_CEILING_DIRECTORIES={root.resolve().parent}", "codex"])
        self.assertNotIn("tmux", seen["command"])

    def test_dry_run_prints_codex_command_without_auto_tmux(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            def fail_runner(command):
                raise AssertionError(f"dry-run should not execute {command}")

            stdout = StringIO()
            with patch.dict("os.environ", {}, clear=True):
                with redirect_stdout(stdout):
                    exit_code = run_harness_codex(["--root", str(root), "--dry-run"], runner=fail_runner)

        self.assertEqual(exit_code, 0)
        self.assertIn("codex --model gpt-5.6-sol", stdout.getvalue())
        self.assertIn('model_reasoning_effort="medium"', stdout.getvalue())
        self.assertNotIn("tmux new-session", stdout.getvalue())

    def test_non_tmux_harness_codex_bootstraps_tmux_when_hud_opted_in(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            seen = {}

            def fake_runner(command):
                seen["command"] = command
                return subprocess.CompletedProcess(command, 0)

            with patch.dict("os.environ", {}, clear=True):
                exit_code = run_harness_codex(["--root", str(root), "--tmux-hud", "--", "hello"], runner=fake_runner)

        self.assertEqual(exit_code, 0)
        self.assertEqual(seen["command"][:2], ["tmux", "new-session"])
        self.assertIn("HARNESS_CODEX_TMUX_BOOTSTRAPPED=1", seen["command"][-1])
        self.assertIn(f"GIT_CEILING_DIRECTORIES={root.resolve().parent}", seen["command"][-1])
        self.assertIn("--tmux-hud", seen["command"][-1])
        self.assertIn("harness-codex", seen["command"][-1])
        self.assertNotIn("/scripts/harness-codex", seen["command"][-1])

    def test_tmux_bootstrap_command_forwards_model_reasoning_yolo_and_prompt(self):
        command = build_tmux_bootstrap_command(
            Path("/repo root"),
            model="gpt-5.5",
            reasoning="high",
            yolo=True,
            quiet_status=False,
            prompt_args=["hello"],
        )

        self.assertEqual(command[:2], ["tmux", "new-session"])
        self.assertIn("-c", command)
        self.assertIn("/repo root", command)
        self.assertIn("HARNESS_CODEX_TMUX_BOOTSTRAPPED=1", command[-1])
        self.assertIn("GIT_CEILING_DIRECTORIES=/", command[-1])
        self.assertIn("harness-codex", command[-1])
        self.assertNotIn("/scripts/harness-codex", command[-1])
        self.assertIn("--model gpt-5.5", command[-1])
        self.assertIn("--reasoning high", command[-1])
        self.assertIn("-- hello", command[-1])


if __name__ == "__main__":
    unittest.main()
