import unittest
from unittest.mock import patch


class PartialPassTest(unittest.TestCase):
    """A bounded failure that still verified something must not be reported as a total loss.

    Measured case: one night's run ended with four of five criteria PASS and a working traffic system, and the
    whole branch was discarded ($43.68) because the fifth criterion was a composition judgement.
    """

    def _partial(self, statuses, contract_ids):
        from longrun import controller
        contract = {"criteria": [{"id": i} for i in contract_ids]}

        class FakeStore:
            def read(self, verify=True):
                return {"criteria": {k: {"status": v} for k, v in statuses.items()}}

        return controller._partial(FakeStore(), contract)

    def test_some_verified_some_open_is_partial(self):
        status, passed, open_ = self._partial(
            {"C1": "PASS", "C2": "FAIL", "C3": "PASS", "C4": "PASS", "C5": "PASS"},
            ["C1", "C2", "C3", "C4", "C5"])
        self.assertEqual(status, "PARTIAL_PASS")
        self.assertEqual(sorted(passed), ["C1", "C3", "C4", "C5"])
        self.assertEqual(open_, ["C2"])

    def test_nothing_verified_stays_a_reset(self):
        status, passed, _ = self._partial({"C1": "FAIL", "C2": "FAIL"}, ["C1", "C2"])
        self.assertEqual(status, "RESET_RECOMMENDED")
        self.assertEqual(passed, [])

    def test_everything_verified_is_not_partial(self):
        # the all-pass path is the normal PASSED exit; _partial must not claim it
        status, _, open_ = self._partial({"C1": "PASS", "C2": "PASS"}, ["C1", "C2"])
        self.assertEqual(status, "RESET_RECOMMENDED")
        self.assertEqual(open_, [])

    def test_partial_pass_is_a_terminal_state(self):
        from longrun.store import TERMINAL_STATES, ACTIVE_STATES
        self.assertIn("PARTIAL_PASS", TERMINAL_STATES)
        self.assertNotIn("PARTIAL_PASS", ACTIVE_STATES)


if __name__ == "__main__":
    unittest.main()
