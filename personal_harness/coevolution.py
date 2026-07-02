"""Harness-model co-evolution seams.

This module does not fine-tune a real model. It provides deterministic replay
buffering and a GRPO-like trainer seam so later work can plug in actual training.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Dict, Iterable, List, Mapping, Sequence

from .core import HarnessConfig
from .evolution import CandidateEdit, EvolutionEngine, EvolutionOutcome
from .replay import TrajectoryRecord


class CrossHarnessReplayBuffer:
    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.records: List[TrajectoryRecord] = []

    def add(self, record: TrajectoryRecord) -> None:
        self.records.append(record)
        overflow = len(self.records) - self.capacity
        if overflow > 0:
            self.records = self.records[overflow:]

    def extend(self, records: Iterable[TrajectoryRecord]) -> None:
        for record in records:
            self.add(record)

    def group(self, task_id: str) -> List[TrajectoryRecord]:
        return [record for record in self.records if record.task_id == task_id]

    def group_relative_advantages(self, task_id: str, epsilon: float = 1e-8) -> Dict[str, float]:
        group = self.group(task_id)
        if not group:
            return {}
        rewards = [record.reward for record in group]
        mean = sum(rewards) / len(rewards)
        variance = sum((reward - mean) ** 2 for reward in rewards) / len(rewards)
        std = sqrt(variance)
        advantages: Dict[str, float] = {}
        for index, record in enumerate(group):
            key = record.harness_version
            if key in advantages:
                key = f"{key}#{index}"
            advantages[key] = (record.reward - mean) / (std + epsilon)
        return advantages


@dataclass(frozen=True)
class TrainingUpdate:
    model_version: str
    metadata: Mapping[str, object]


class GRPOTrainer:
    """Deterministic stand-in for cross-harness GRPO."""

    def update(self, model_version: str, buffer: CrossHarnessReplayBuffer) -> TrainingUpdate:
        task_ids = sorted({record.task_id for record in buffer.records})
        metadata = {
            "algorithm": "mock-cross-harness-grpo",
            "updated_records": len(buffer.records),
            "task_groups": len(task_ids),
            "task_ids": task_ids,
        }
        return TrainingUpdate(f"{model_version}+grpo1", metadata)


@dataclass(frozen=True)
class CoEvolutionOutcome:
    model_version: str
    harness: HarnessConfig
    evolution: EvolutionOutcome
    training_metadata: Mapping[str, object]


class CoEvolutionEngine:
    def __init__(self, evolution_engine: EvolutionEngine, trainer: GRPOTrainer, replay_buffer: CrossHarnessReplayBuffer):
        self.evolution_engine = evolution_engine
        self.trainer = trainer
        self.replay_buffer = replay_buffer

    def step(
        self,
        model_version: str,
        harness: HarnessConfig,
        candidates: Sequence[CandidateEdit],
        new_records: Iterable[TrajectoryRecord],
    ) -> CoEvolutionOutcome:
        self.replay_buffer.extend(new_records)
        evolution = self.evolution_engine.evolve(harness, candidates)
        update = self.trainer.update(model_version, self.replay_buffer)
        return CoEvolutionOutcome(update.model_version, evolution.harness, evolution, update.metadata)


__all__ = [
    "CoEvolutionEngine",
    "CoEvolutionOutcome",
    "CrossHarnessReplayBuffer",
    "GRPOTrainer",
    "TrainingUpdate",
]
