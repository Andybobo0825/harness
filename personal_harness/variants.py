"""Variant isolation and ensemble routing for heterogeneous task sets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

from .core import HarnessConfig
from .replay import ReplayStore, TrajectoryRecord


@dataclass(frozen=True)
class HarnessVariant:
    variant_id: str
    harness: HarnessConfig
    parent_id: str | None = None


class VariantRouter:
    def __init__(self, variants: Sequence[HarnessVariant], replay_store: ReplayStore, max_variants: int = 4):
        if not variants:
            raise ValueError("VariantRouter requires at least one variant")
        self.variants: List[HarnessVariant] = list(variants)
        self.replay_store = replay_store
        self.max_variants = max_variants

    def _success_rate(self, variant_id: str, task_id: str | None = None) -> float:
        total = 0
        solved = 0
        for record in self.replay_store.read_all():
            if record.metadata.get("variant_id") != variant_id:
                continue
            if task_id is not None and record.task_id != task_id:
                continue
            if record.metadata.get("record_type") == "evolution_decision":
                continue
            total += 1
            solved += 1 if record.solved else 0
        if total == 0:
            return 0.0
        return solved / total

    def route(self, task_id: str) -> HarnessVariant:
        return max(
            self.variants,
            key=lambda variant: (self._success_rate(variant.variant_id, task_id), self._success_rate(variant.variant_id), -self.variants.index(variant)),
        )

    def fork(self, variant_id: str, harness: HarnessConfig, parent_id: str | None = None) -> HarnessVariant:
        new_variant = HarnessVariant(variant_id, harness, parent_id)
        kept = list(self.variants)
        if len(kept) >= self.max_variants:
            weakest = min(kept, key=lambda variant: (self._success_rate(variant.variant_id), -kept.index(variant)))
            kept = [variant for variant in kept if variant.variant_id != weakest.variant_id]
        kept.append(new_variant)
        self.variants = kept
        return new_variant


__all__ = ["HarnessVariant", "VariantRouter"]
