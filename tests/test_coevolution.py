import tempfile
import unittest
from pathlib import Path

from personal_harness.coevolution import CrossHarnessReplayBuffer, GRPOTrainer, CoEvolutionEngine
from personal_harness.core import HarnessConfig
from personal_harness.eval import EvaluationGate
from personal_harness.evolution import EvolutionEngine, CandidateEdit
from personal_harness.eval import CandidateManifest
from personal_harness.replay import ReplayStore, TrajectoryRecord


class TestCoEvolution(unittest.TestCase):
    def test_buffer_keeps_recent_records_and_computes_task_relative_advantages(self):
        buffer = CrossHarnessReplayBuffer(capacity=3)
        buffer.add(TrajectoryRecord("t1", "h1", 0.0, False))
        buffer.add(TrajectoryRecord("t1", "h2", 1.0, True))
        buffer.add(TrajectoryRecord("t2", "h1", 0.5, False))
        buffer.add(TrajectoryRecord("t1", "h3", 0.5, False))
        self.assertEqual([record.harness_version for record in buffer.records], ["h2", "h1", "h3"])
        advantages = buffer.group_relative_advantages("t1")
        self.assertAlmostEqual(sum(advantages.values()), 0.0, places=6)
        self.assertGreater(advantages["h2"], advantages["h3"])

    def test_coevolution_runs_harness_evolution_and_model_update(self):
        with tempfile.TemporaryDirectory() as d:
            store = ReplayStore(Path(d) / "replay.jsonl")
            store.append(TrajectoryRecord("task-a", "v1", 0.0, False))
            candidate = CandidateEdit(
                name="fix-a",
                apply=lambda h: h.with_version("v2"),
                manifest=CandidateManifest("fix failing task", {"task-a"}, set()),
            )
            engine = CoEvolutionEngine(EvolutionEngine(EvaluationGate(store)), GRPOTrainer(), CrossHarnessReplayBuffer(capacity=10))
            outcome = engine.step("model-v1", HarnessConfig("v1"), [candidate], list(store.read_all()))
        self.assertEqual(outcome.model_version, "model-v1+grpo1")
        self.assertEqual(outcome.harness.version, "v2")
        self.assertEqual(outcome.training_metadata["updated_records"], 1)


if __name__ == "__main__":
    unittest.main()
