import tempfile
import unittest
from pathlib import Path

from personal_harness.aegis import AEGISPipeline, Digester, Planner, Evolver, Critic
from personal_harness.core import HarnessConfig
from personal_harness.eval import CandidateManifest, EvaluationGate
from personal_harness.evolution import CandidateEdit
from personal_harness.replay import ReplayStore, TrajectoryRecord


class TestAEGISPipeline(unittest.TestCase):
    def test_pipeline_uses_generator_candidate_and_ships_when_gate_passes(self):
        with tempfile.TemporaryDirectory() as d:
            store = ReplayStore(Path(d) / "replay.jsonl")
            store.append(TrajectoryRecord(task_id="task-a", harness_version="v1", reward=0.0, solved=False, metadata={"failure_category": "tool"}))

            def generator(current, landscape):
                return [CandidateEdit(
                    name="tool-fix",
                    apply=lambda h: h.with_version("v2"),
                    manifest=CandidateManifest(summary="fix tool failures", expected_improvements={"task-a"}, expected_regressions=set()),
                )]

            pipeline = AEGISPipeline(Digester(store), Planner(), Evolver(generator), Critic(), EvaluationGate(store))
            outcome = pipeline.run_round(HarnessConfig("v1"))
            records = list(store.read_all())

        self.assertTrue(outcome.evolution.shipped)
        self.assertEqual(outcome.evolution.shipped_candidate, "tool-fix")
        self.assertEqual(outcome.stage, "shipped")
        self.assertEqual(records[-1].metadata["record_type"], "evolution_decision")

    def test_pipeline_short_circuits_when_no_actionable_failures(self):
        with tempfile.TemporaryDirectory() as d:
            store = ReplayStore(Path(d) / "replay.jsonl")
            store.append(TrajectoryRecord(task_id="task-a", harness_version="v1", reward=1.0, solved=True))
            pipeline = AEGISPipeline(Digester(store), Planner(), Evolver(lambda _h, _l: []), Critic(), EvaluationGate(store))
            outcome = pipeline.run_round(HarnessConfig("v1"))

        self.assertFalse(outcome.evolution.shipped)
        self.assertEqual(outcome.stage, "digester_noop")
        self.assertEqual(outcome.harness.version, "v1")

    def test_critic_rejects_candidate_with_declared_exploit_risk(self):
        with tempfile.TemporaryDirectory() as d:
            store = ReplayStore(Path(d) / "replay.jsonl")
            store.append(TrajectoryRecord(task_id="task-a", harness_version="v1", reward=0.0, solved=False))

            def generator(current, landscape):
                return [CandidateEdit(
                    name="hacky",
                    apply=lambda h: h.with_version("v2"),
                    manifest=CandidateManifest(summary="exploit verifier answer format", expected_improvements={"task-a"}, expected_regressions=set()),
                )]

            pipeline = AEGISPipeline(Digester(store), Planner(), Evolver(generator), Critic(), EvaluationGate(store))
            outcome = pipeline.run_round(HarnessConfig("v1"))

        self.assertFalse(outcome.evolution.shipped)
        self.assertEqual(outcome.stage, "critic_noop")
        self.assertEqual(outcome.evolution.rejections[0].reasons, ["REJECT_CRITIC_EXPLOIT_RISK"])


if __name__ == "__main__":
    unittest.main()
