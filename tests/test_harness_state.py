import json
import tempfile
import unittest
from pathlib import Path

from personal_harness.harness_state import PersonalHarnessRuntimeState, write_personal_harness_state, read_personal_harness_state


class TestHarnessState(unittest.TestCase):
    def test_writes_and_reads_personal_harness_runtime_state_under_harness_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = PersonalHarnessRuntimeState(active=True, harness_version="v2", model_version="model-v1", variant_id="default", phase="aegis")
            path = write_personal_harness_state(root, state)
            loaded = read_personal_harness_state(root)
            raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path, root / ".harness" / "state" / "personal-harness-state.json")
        self.assertEqual(raw["schema_version"], "personal-harness-state/v1")
        self.assertEqual(loaded.harness_version, "v2")
        self.assertEqual(loaded.phase, "aegis")


if __name__ == "__main__":
    unittest.main()
