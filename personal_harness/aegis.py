"""AEGIS-style four-stage harness adaptation pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Sequence, Set

from .core import HarnessConfig
from .eval import CandidateManifest, EvaluationGate
from .evolution import CandidateEdit, CandidateRejection, EvolutionEngine, EvolutionOutcome
from .replay import ReplayStore, TrajectoryRecord


@dataclass(frozen=True)
class DigestEvidence:
    failing_tasks: Set[str]
    failure_categories: Dict[str, str]

    @property
    def actionable(self) -> bool:
        return bool(self.failing_tasks)


@dataclass(frozen=True)
class AdaptationLandscape:
    target_tasks: Set[str]
    edit_buckets: Set[str]
    failure_categories: Dict[str, str]

    @property
    def empty(self) -> bool:
        return not self.target_tasks


@dataclass(frozen=True)
class AEGISRoundOutcome:
    harness: HarnessConfig
    evolution: EvolutionOutcome
    stage: str
    evidence: DigestEvidence | None = None
    landscape: AdaptationLandscape | None = None


class Digester:
    """Compress replay records into task-level failure evidence."""

    def __init__(self, replay_store: ReplayStore):
        self.replay_store = replay_store

    def digest(self) -> DigestEvidence:
        latest_by_task: Dict[str, TrajectoryRecord] = {}
        for record in self.replay_store.read_all():
            if record.metadata.get("record_type") == "evolution_decision":
                continue
            latest_by_task[record.task_id] = record
        failing = {task_id for task_id, record in latest_by_task.items() if not record.solved}
        categories = {
            task_id: str(record.metadata.get("failure_category", "unknown"))
            for task_id, record in latest_by_task.items()
            if not record.solved
        }
        return DigestEvidence(failing, categories)


class Planner:
    """Builds an adaptation landscape from digested failures."""

    def plan(self, evidence: DigestEvidence) -> AdaptationLandscape:
        if not evidence.actionable:
            return AdaptationLandscape(set(), set(), {})
        buckets = {category for category in evidence.failure_categories.values() if category != "unknown"}
        if not buckets:
            buckets = {"prompt", "processor", "tool"}
        return AdaptationLandscape(set(evidence.failing_tasks), buckets, dict(evidence.failure_categories))


CandidateGenerator = Callable[[HarnessConfig, AdaptationLandscape], Sequence[CandidateEdit]]


class Evolver:
    """Turns a landscape into typed candidate edits.

    In production this seam can call an LLM. In tests and local use it accepts a
    deterministic generator, keeping the pipeline auditable.
    """

    def __init__(self, generator: CandidateGenerator):
        self.generator = generator

    def evolve(self, current: HarnessConfig, landscape: AdaptationLandscape) -> List[CandidateEdit]:
        return list(self.generator(current, landscape))


class Critic:
    """Ranks or rejects candidates before deterministic gating."""

    EXPLOIT_TERMS = ("exploit", "hardcode", "answer leak", "verifier hack")

    def review(self, candidates: Sequence[CandidateEdit]) -> tuple[List[CandidateEdit], List[CandidateRejection]]:
        accepted: List[CandidateEdit] = []
        rejected: List[CandidateRejection] = []
        for candidate in candidates:
            summary = candidate.manifest.summary.lower()
            if any(term in summary for term in self.EXPLOIT_TERMS):
                rejected.append(CandidateRejection(candidate.name, ["REJECT_CRITIC_EXPLOIT_RISK"]))
            else:
                accepted.append(candidate)
        return accepted, rejected


class AEGISPipeline:
    def __init__(
        self,
        digester: Digester,
        planner: Planner,
        evolver: Evolver,
        critic: Critic,
        gate: EvaluationGate,
    ):
        self.digester = digester
        self.planner = planner
        self.evolver = evolver
        self.critic = critic
        self.gate = gate

    def run_round(self, current: HarnessConfig) -> AEGISRoundOutcome:
        evidence = self.digester.digest()
        if not evidence.actionable:
            noop = EvolutionOutcome(current, False, None, [])
            return AEGISRoundOutcome(current, noop, "digester_noop", evidence, None)

        landscape = self.planner.plan(evidence)
        if landscape.empty:
            noop = EvolutionOutcome(current, False, None, [])
            return AEGISRoundOutcome(current, noop, "planner_noop", evidence, landscape)

        candidates = self.evolver.evolve(current, landscape)
        if not candidates:
            noop = EvolutionOutcome(current, False, None, [])
            return AEGISRoundOutcome(current, noop, "evolver_noop", evidence, landscape)

        reviewed, critic_rejections = self.critic.review(candidates)
        if not reviewed:
            noop = EvolutionOutcome(current, False, None, critic_rejections)
            return AEGISRoundOutcome(current, noop, "critic_noop", evidence, landscape)

        outcome = EvolutionEngine(self.gate).evolve(current, reviewed)
        combined = EvolutionOutcome(outcome.harness, outcome.shipped, outcome.shipped_candidate, critic_rejections + outcome.rejections)
        return AEGISRoundOutcome(combined.harness, combined, "shipped" if combined.shipped else "gate_noop", evidence, landscape)


__all__ = [
    "AEGISPipeline",
    "AEGISRoundOutcome",
    "AdaptationLandscape",
    "CandidateGenerator",
    "Critic",
    "DigestEvidence",
    "Digester",
    "Evolver",
    "Planner",
]
