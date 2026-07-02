import unittest

from personal_harness.core import (
    Event,
    HarnessConfig,
    HarnessContractError,
    Hook,
    Processor,
    ProcessorOutcome,
    run_hook,
)


class TransformProcessor(Processor):
    singleton_group = "transform"
    order = 10

    def process(self, event):
        next_event = event.with_payload({**event.payload, "value": event.payload.get("value", 0) + 1})
        return ProcessorOutcome.emit(next_event)


class EarlierProcessor(Processor):
    singleton_group = "earlier"
    order = 0

    def process(self, event):
        return ProcessorOutcome.emit(event.with_payload({**event.payload, "order": event.payload.get("order", []) + ["earlier"]}))


class LaterProcessor(Processor):
    singleton_group = "later"
    order = 20

    def process(self, event):
        return ProcessorOutcome.emit(event.with_payload({**event.payload, "order": event.payload.get("order", []) + ["later"]}))


class InterceptProcessor(Processor):
    singleton_group = "intercept"
    order = 5

    def process(self, event):
        return ProcessorOutcome.intercept()


class TestCore(unittest.TestCase):
    def test_processors_run_in_order(self):
        config = HarnessConfig(version="v1").with_processor(Hook.BEFORE_MODEL, LaterProcessor()).with_processor(Hook.BEFORE_MODEL, EarlierProcessor())
        [result] = run_hook(config, Hook.BEFORE_MODEL, Event(Hook.BEFORE_MODEL, {"order": []}))
        self.assertEqual(result.payload["order"], ["earlier", "later"])

    def test_singleton_group_replaces_existing_processor(self):
        class AddTen(TransformProcessor):
            def process(self, event):
                return ProcessorOutcome.emit(event.with_payload({"value": event.payload.get("value", 0) + 10}))

        config = HarnessConfig(version="v1").with_processor(Hook.BEFORE_MODEL, TransformProcessor()).with_processor(Hook.BEFORE_MODEL, AddTen())
        [result] = run_hook(config, Hook.BEFORE_MODEL, Event(Hook.BEFORE_MODEL, {"value": 1}))
        self.assertEqual(result.payload["value"], 11)

    def test_intercept_stops_downstream_processing(self):
        config = HarnessConfig(version="v1").with_processor(Hook.BEFORE_MODEL, InterceptProcessor()).with_processor(Hook.BEFORE_MODEL, TransformProcessor())
        results = run_hook(config, Hook.BEFORE_MODEL, Event(Hook.BEFORE_MODEL, {"value": 1}))
        self.assertEqual(results, [])

    def test_read_only_hook_rejects_mutation(self):
        config = HarnessConfig(version="v1").with_processor(Hook.STEP_END, TransformProcessor())
        with self.assertRaises(HarnessContractError):
            run_hook(config, Hook.STEP_END, Event(Hook.STEP_END, {"value": 1}))


if __name__ == "__main__":
    unittest.main()
