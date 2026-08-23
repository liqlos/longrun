import unittest

from longrun.adapters import load_adapter
from longrun.controller import _budgets_with_adapter_floor


class ChildTimeoutFloorTest(unittest.TestCase):
    """A builder session must outlast one verification step. Measured: a round doing a clean Unity rebuild
    plus a simulator capture was killed by the 2700 s default mid-build — $11.80 and 45 minutes, no evidence."""

    def test_vr_visual_raises_a_short_default(self):
        b = _budgets_with_adapter_floor({"child_timeout_seconds": 2700, "wall_time_seconds": 14400},
                                        load_adapter("vr_visual", None))
        self.assertEqual(b["child_timeout_seconds"], 5400)

    def test_the_floor_never_exceeds_the_wall_budget(self):
        b = _budgets_with_adapter_floor({"child_timeout_seconds": 600, "wall_time_seconds": 1800},
                                        load_adapter("vr_visual", None))
        self.assertEqual(b["child_timeout_seconds"], 1800)

    def test_a_generous_contract_value_is_left_alone(self):
        b = _budgets_with_adapter_floor({"child_timeout_seconds": 7200, "wall_time_seconds": 14400},
                                        load_adapter("vr_visual", None))
        self.assertEqual(b["child_timeout_seconds"], 7200)

    def test_adapters_without_a_floor_are_untouched(self):
        b = _budgets_with_adapter_floor({"child_timeout_seconds": 600, "wall_time_seconds": 14400},
                                        load_adapter("software", None))
        self.assertEqual(b["child_timeout_seconds"], 600)


if __name__ == "__main__":
    unittest.main()
