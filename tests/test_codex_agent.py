import json
import tempfile
import unittest
from pathlib import Path

from personal_harness.aegis import AdaptationLandscape
from personal_harness.codex_agent import CodexAgentEvolver, CodexCandidateRequest, CodexCandidateResponse
from personal_harness.core import HarnessConfig


class TestCodexAgentEvolver(unittest.TestCase):
    def test_writes_candidate_request_for_current_codex_agent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            request = CodexCandidateRequest(
                current_version="v1",
                target_tasks={"task-a"},
                edit_buckets={"tool"},
                failure_categories={"task-a": "tool"},
            )
            path = CodexAgentEvolver(root).write_request(request)
            raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path, root / ".harness" / "candidates" / "codex-candidate-request.json")
        self.assertEqual(raw["schema_version"], "codex-agent-candidate-request/v1")
        self.assertEqual(raw["llm_owner"], "current-codex-agent")
        self.assertEqual(raw["current_version"], "v1")
        self.assertEqual(raw["target_tasks"], ["task-a"])
        self.assertTrue(raw["request_id"].startswith("codex-agent:"))

    def test_reads_codex_agent_response_into_candidate_edit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            response = CodexCandidateResponse(
                name="codex-tool-fix",
                target_version="v2",
                summary="目前 Codex agent 產生 tool fix",
                expected_improvements={"task-a"},
                expected_regressions=set(),
                smoke_ok=True,
                request_id="request-1",
            )
            response_path = root / ".harness" / "candidates" / "codex-candidate-response.json"
            response_path.parent.mkdir(parents=True)
            response_path.write_text(json.dumps(response.to_dict(), ensure_ascii=False), encoding="utf-8")

            [candidate] = CodexAgentEvolver(root).read_response_candidates(request_id="request-1")
            stale_candidates = CodexAgentEvolver(root).read_response_candidates(request_id="request-2")
            uncorrelated_candidates = CodexAgentEvolver(root).read_response_candidates(allow_uncorrelated=True)
            next_harness = candidate.apply(HarnessConfig("v1"))
        self.assertEqual(candidate.name, "codex-tool-fix")
        self.assertEqual(candidate.manifest.summary, "目前 Codex agent 產生 tool fix")
        self.assertEqual(next_harness.version, "v2")
        self.assertEqual(stale_candidates, [])
        self.assertEqual(len(uncorrelated_candidates), 1)

    def test_response_consumption_requires_request_id_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            with self.assertRaisesRegex(ValueError, "requires request_id"):
                CodexAgentEvolver(root).read_response_candidates()

    def test_builds_request_from_aegis_landscape(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            landscape = AdaptationLandscape(target_tasks={"task-a"}, edit_buckets={"tool"}, failure_categories={"task-a": "tool"})
            path = CodexAgentEvolver(root).request_from_landscape(HarnessConfig("v1"), landscape)
            raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["current_version"], "v1")
        self.assertEqual(raw["edit_buckets"], ["tool"])


if __name__ == "__main__":
    unittest.main()
