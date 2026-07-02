"""Candidate evolution loop for the personal harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Sequence

from .core import HarnessConfig
from .eval import CandidateManifest, EvaluationGate, EvaluationResult
from .replay import TrajectoryRecord


@dataclass(frozen=True)
class CandidateEdit:
    name: str
    apply: Callable[[HarnessConfig], HarnessConfig]
    manifest: CandidateManifest
    smoke_ok: bool = True


@dataclass(frozen=True)
class CandidateRejection:
    candidate: str
    reasons: List[str]


@dataclass(frozen=True)
class EvolutionOutcome:
    harness: HarnessConfig
    shipped: bool
    shipped_candidate: str | None = None
    rejections: List[CandidateRejection] = field(default_factory=list)


class EvolutionEngine:
    def __init__(self, gate: EvaluationGate):
        self.gate = gate

    def _record_decision(
        self,
        *,
        candidate_name: str,
        base_version: str,
        candidate_version: str,
        decision: str,
        reasons: List[str],
    ) -> None:
        self.gate.replay_store.append(
            TrajectoryRecord(
                task_id=f"evolution:{candidate_name}",
                harness_version=base_version,
                reward=1.0 if decision == "accepted" else 0.0,
                solved=False,
                metadata={
                    "record_type": "evolution_decision",
                    "candidate": candidate_name,
                    "base_version": base_version,
                    "candidate_version": candidate_version,
                    "decision": decision,
                    "reasons": reasons,
                },
            )
        )

    def evolve(self, current: HarnessConfig, candidates: Sequence[CandidateEdit]) -> EvolutionOutcome:
        rejections: List[CandidateRejection] = []
        for candidate in candidates:
            try:
                next_harness = candidate.apply(current)
            except Exception as exc:  # noqa: BLE001 - candidate failures become auditable rejections
                reasons = [f"REJECT_APPLY_FAILED: {exc}"]
                self._record_decision(
                    candidate_name=candidate.name,
                    base_version=current.version,
                    candidate_version=current.version,
                    decision="rejected",
                    reasons=reasons,
                )
                rejections.append(CandidateRejection(candidate.name, reasons))
                continue
            result = self.gate.evaluate(current, next_harness, candidate.manifest, smoke_ok=candidate.smoke_ok)
            if result.accepted:
                self._record_decision(
                    candidate_name=candidate.name,
                    base_version=current.version,
                    candidate_version=next_harness.version,
                    decision="accepted",
                    reasons=result.reasons,
                )
                return EvolutionOutcome(next_harness, True, candidate.name, rejections)
            self._record_decision(
                candidate_name=candidate.name,
                base_version=current.version,
                candidate_version=next_harness.version,
                decision="rejected",
                reasons=result.reasons,
            )
            rejections.append(CandidateRejection(candidate.name, result.reasons))
        return EvolutionOutcome(current, False, None, rejections)


__all__ = ["CandidateEdit", "CandidateRejection", "EvolutionEngine", "EvolutionOutcome"]
