import tempfile
import unittest
from pathlib import Path

from personal_harness.core import HarnessConfig
from personal_harness.replay import ReplayStore, TrajectoryRecord
from personal_harness.variants import HarnessVariant, VariantRouter


class TestVariantRouter(unittest.TestCase):
    def test_routes_task_to_variant_with_highest_prior_success_rate(self):
        with tempfile.TemporaryDirectory() as d:
            store = ReplayStore(Path(d) / "replay.jsonl")
            store.append(TrajectoryRecord(task_id="gaia-1", harness_version="v1", reward=1.0, solved=True, metadata={"variant_id": "search"}))
            store.append(TrajectoryRecord(task_id="gaia-1", harness_version="v2", reward=0.0, solved=False, metadata={"variant_id": "code"}))
            router = VariantRouter([
                HarnessVariant("search", HarnessConfig("v1")),
                HarnessVariant("code", HarnessConfig("v2")),
            ], store)
            chosen = router.route("gaia-1")
        self.assertEqual(chosen.variant_id, "search")

    def test_forks_variant_and_retires_lowest_when_pool_is_full(self):
        with tempfile.TemporaryDirectory() as d:
            store = ReplayStore(Path(d) / "replay.jsonl")
            store.append(TrajectoryRecord(task_id="a", harness_version="old", reward=0.0, solved=False, metadata={"variant_id": "weak"}))
            store.append(TrajectoryRecord(task_id="a", harness_version="strong", reward=1.0, solved=True, metadata={"variant_id": "strong"}))
            router = VariantRouter([
                HarnessVariant("weak", HarnessConfig("old")),
                HarnessVariant("strong", HarnessConfig("strong")),
            ], store, max_variants=2)
            router.fork("new", HarnessConfig("new"), parent_id="weak")
            ids = [variant.variant_id for variant in router.variants]
        self.assertEqual(ids, ["strong", "new"])


if __name__ == "__main__":
    unittest.main()
