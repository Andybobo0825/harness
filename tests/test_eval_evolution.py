import tempfile
import unittest
from pathlib import Path

from personal_harness.core import HarnessConfig, Hook, Processor, ProcessorOutcome
from personal_harness.eval import CandidateManifest, EvaluationGate
from personal_harness.evolution import CandidateEdit, EvolutionEngine
from personal_harness.replay import ReplayStore, TrajectoryRecord


class MarkerProcessor(Processor):
    singleton_group = "marker"

    def __init__(self, marker):
        self.marker = marker

    def process(self, event):
        payload = {**event.payload, "marker": self.marker}
        return ProcessorOutcome.emit(event.with_payload(payload))


def make_store(path):
    store = ReplayStore(path)
    store.append(TrajectoryRecord(task_id="already-solved", harness_version="v1", reward=1.0, solved=True))
    return store


class TestEvalEvolution(unittest.TestCase):
    def test_gate_rejects_incomplete_manifest(self):
        gate = EvaluationGate(make_store(Path(tempfile.mkdtemp()) / "r.jsonl"))
        result = gate.evaluate(HarnessConfig("v1"), HarnessConfig("v2"), CandidateManifest(summary="", expected_improvements={"x"}, expected_regressions=set()))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons[0], "REJECT_MANIFEST_INCOMPLETE")

    def test_gate_rejects_smoke_failure(self):
        gate = EvaluationGate(make_store(Path(tempfile.mkdtemp()) / "r.jsonl"))
        result = gate.evaluate(HarnessConfig("v1"), HarnessConfig("v2"), CandidateManifest(summary="candidate", expected_improvements={"x"}, expected_regressions=set()), smoke_ok=False)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons[0], "REJECT_SMOKE_FAILED")

    def test_gate_rejects_previously_solved_regression(self):
        gate = EvaluationGate(make_store(Path(tempfile.mkdtemp()) / "r.jsonl"))
        result = gate.evaluate(HarnessConfig("v1"), HarnessConfig("v2"), CandidateManifest(summary="candidate", expected_improvements={"x"}, expected_regressions={"already-solved"}))
        self.assertFalse(result.accepted)
        self.assertTrue(result.reasons[0].startswith("REJECT_SEESAW_REGRESSION"))

    def test_evolution_accepts_first_passing_candidate_and_records_audit(self):
        with tempfile.TemporaryDirectory() as d:
            store = make_store(Path(d) / "r.jsonl")
            base = HarnessConfig("v1")
            candidate = CandidateEdit(
                name="add-marker",
                apply=lambda h: h.with_version("v2").with_processor(Hook.BEFORE_MODEL, MarkerProcessor("accepted")),
                manifest=CandidateManifest(summary="adds marker", expected_improvements={"new-task"}, expected_regressions=set()),
            )
            outcome = EvolutionEngine(EvaluationGate(store)).evolve(base, [candidate])
            records = list(store.read_all())
        self.assertTrue(outcome.shipped)
        self.assertEqual(outcome.harness.version, "v2")
        self.assertEqual(outcome.shipped_candidate, "add-marker")
        audit = records[-1]
        self.assertEqual(audit.task_id, "evolution:add-marker")
        self.assertEqual(audit.metadata["decision"], "accepted")
        self.assertEqual(audit.metadata["candidate_version"], "v2")

    def test_evolution_noops_when_all_candidates_reject_and_records_audit(self):
        with tempfile.TemporaryDirectory() as d:
            store = make_store(Path(d) / "r.jsonl")
            base = HarnessConfig("v1")
            candidate = CandidateEdit(
                name="bad",
                apply=lambda h: h.with_version("v2"),
                manifest=CandidateManifest(summary="", expected_improvements=set(), expected_regressions=set()),
            )
            outcome = EvolutionEngine(EvaluationGate(store)).evolve(base, [candidate])
            records = list(store.read_all())
        self.assertFalse(outcome.shipped)
        self.assertEqual(outcome.harness.version, "v1")
        self.assertEqual(outcome.rejections[0].candidate, "bad")
        audit = records[-1]
        self.assertEqual(audit.task_id, "evolution:bad")
        self.assertEqual(audit.metadata["decision"], "rejected")
        self.assertEqual(audit.metadata["reasons"], ["REJECT_MANIFEST_INCOMPLETE"])


if __name__ == "__main__":
    unittest.main()
