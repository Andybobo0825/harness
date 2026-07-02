"""Typed lifecycle processor core for the standalone harness-coding agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


class HarnessContractError(RuntimeError):
    """Raised when a processor violates hook/event contracts."""


class Hook(str, Enum):
    TASK_START = "task_start"
    STEP_START = "step_start"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    STEP_END = "step_end"
    TASK_END = "task_end"


READ_ONLY_HOOKS = {Hook.STEP_END, Hook.TASK_END}


@dataclass(frozen=True)
class Event:
    hook: Hook
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def with_payload(self, payload: Mapping[str, Any]) -> "Event":
        return Event(self.hook, dict(payload), dict(self.metadata))

    def with_metadata(self, metadata: Mapping[str, Any]) -> "Event":
        return Event(self.hook, dict(self.payload), dict(metadata))


@dataclass(frozen=True)
class ProcessorOutcome:
    events: Tuple[Event, ...]

    @classmethod
    def emit(cls, *events: Event) -> "ProcessorOutcome":
        return cls(tuple(events))

    @classmethod
    def intercept(cls) -> "ProcessorOutcome":
        return cls(())


class Processor:
    """Base processor.

    Subclasses consume one event and return zero or more events. A zero-event
    outcome is an intercept. Multiple events represent split semantics.
    """

    singleton_group = "processor"
    order = 10
    after: Sequence[str] = ()

    def process(self, event: Event) -> ProcessorOutcome:  # pragma: no cover - abstract by convention
        return ProcessorOutcome.emit(event)


ProcessorMap = Mapping[Hook, Tuple[Processor, ...]]


@dataclass(frozen=True)
class HarnessConfig:
    version: str
    processors: ProcessorMap = field(default_factory=dict)
    slots: Mapping[str, Any] = field(default_factory=dict)

    def with_version(self, version: str) -> "HarnessConfig":
        return HarnessConfig(version=version, processors=dict(self.processors), slots=dict(self.slots))

    def with_processor(self, hook: Hook, processor: Processor) -> "HarnessConfig":
        current = list(self.processors.get(hook, ()))
        group = processor.singleton_group
        current = [existing for existing in current if existing.singleton_group != group]
        current.append(processor)
        current.sort(key=lambda item: (item.order, item.singleton_group, item.__class__.__name__))
        next_processors: Dict[Hook, Tuple[Processor, ...]] = dict(self.processors)
        next_processors[hook] = tuple(current)
        return HarnessConfig(version=self.version, processors=next_processors, slots=dict(self.slots))

    def processors_for(self, hook: Hook) -> Tuple[Processor, ...]:
        return tuple(self.processors.get(hook, ()))


def _validate_event_for_hook(hook: Hook, event: Event) -> None:
    if event.hook != hook:
        raise HarnessContractError(f"event hook {event.hook.value} does not match pipeline hook {hook.value}")


def _validate_processor_output(hook: Hook, before: Event, after: Event) -> None:
    _validate_event_for_hook(hook, after)
    if hook in READ_ONLY_HOOKS and after != before:
        raise HarnessContractError(f"processor mutated read-only hook {hook.value}")


def run_hook(config: HarnessConfig, hook: Hook, event: Event) -> List[Event]:
    """Run one hook pipeline and return resulting events.

    The function is intentionally deterministic: processors are ordered during
    composition, intercepts stop propagation by returning no events, and hook
    contracts are validated after every processor invocation.
    """

    _validate_event_for_hook(hook, event)
    events: List[Event] = [event]
    for processor in config.processors_for(hook):
        next_events: List[Event] = []
        for current in events:
            outcome = processor.process(current)
            if not isinstance(outcome, ProcessorOutcome):
                raise HarnessContractError(f"{processor.__class__.__name__}.process must return ProcessorOutcome")
            for produced in outcome.events:
                _validate_processor_output(hook, current, produced)
                next_events.append(produced)
        events = next_events
        if not events:
            break
    return events


__all__ = [
    "Event",
    "HarnessConfig",
    "HarnessContractError",
    "Hook",
    "Processor",
    "ProcessorOutcome",
    "run_hook",
]
