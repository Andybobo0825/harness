"""Deterministic evaluation gate for candidate harness edits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Iterable, List, Optional, Set

from .core import HarnessConfig
from .replay import ReplayStore


@dataclass(frozen=True)
class CandidateManifest:
    summary: str
    expected_improvements: Set[str]
    expected_regressions: Set[str]


@dataclass(frozen=True)
class EvaluationResult:
    accepted: bool
    reasons: List[str]


class EvaluationGate:
    """Accepts only complete, smoke-clean, non-regressing candidates."""

    ACCEPT_GATE_PASSED = "ACCEPT_GATE_PASSED"
    REJECT_MANIFEST_INCOMPLETE = "REJECT_MANIFEST_INCOMPLETE"
    REJECT_SMOKE_FAILED = "REJECT_SMOKE_FAILED"
    REJECT_SEESAW_REGRESSION = "REJECT_SEESAW_REGRESSION"

    def __init__(self, replay_store: ReplayStore):
        self.replay_store = replay_store

    def evaluate(
        self,
        current: HarnessConfig,
        candidate: HarnessConfig,
        manifest: CandidateManifest,
        *,
        smoke_ok: bool = True,
    ) -> EvaluationResult:
        del current, candidate  # reserved for richer structural checks
        if not manifest.summary.strip() or not manifest.expected_improvements:
            return EvaluationResult(False, [self.REJECT_MANIFEST_INCOMPLETE])
        if not smoke_ok:
            return EvaluationResult(False, [self.REJECT_SMOKE_FAILED])
        solved_regressions = sorted(self.replay_store.solved_task_ids().intersection(manifest.expected_regressions))
        if solved_regressions:
            return EvaluationResult(False, [f"{self.REJECT_SEESAW_REGRESSION}: {', '.join(solved_regressions)}"])
        return EvaluationResult(True, [self.ACCEPT_GATE_PASSED])


__all__ = ["CandidateManifest", "EvaluationGate", "EvaluationResult"]
