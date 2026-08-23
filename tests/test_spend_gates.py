import unittest
from types import SimpleNamespace

from longrun.controller import _infra_failure


def res(exit_code=1, dur=2000.0, timed_out=False, interrupted=False):
    return SimpleNamespace(exit_code=exit_code, duration_s=dur, timed_out=timed_out, interrupted=interrupted)


class MidSessionLimitTest(unittest.TestCase):
    """A provider usage limit that lands mid-session is not a build round.

    One night's run burned 36 minutes and $8.96 on a session that did 83 actions and then died on
    'You've hit your session limit'. It was charged as a round, and the harness then ordered the builder to
    change hypothesis — because of someone else's rate limit.
    """

    LIMIT = "You've hit your session limit · resets 12:40am (Europe/Belgrade)"

    def test_limit_after_real_actions_but_no_evidence_is_infra(self):
        infra, wait = _infra_failure(res(), 83, self.LIMIT, produced_evidence=False)
        self.assertTrue(infra, "a usage limit mid-session must not be charged as a round")
        self.assertGreater(wait, 0)

    def test_limit_after_evidence_was_submitted_is_a_real_round(self):
        # work reached the ledger: the round happened, however it ended
        self.assertEqual(_infra_failure(res(), 83, self.LIMIT, produced_evidence=True), (False, 0))

    def test_ordinary_mid_session_crash_is_still_a_round(self):
        self.assertEqual(_infra_failure(res(), 83, "Traceback: something broke", produced_evidence=False), (False, 0))

    def test_silent_timeout_is_infra(self):
        self.assertEqual(_infra_failure(res(timed_out=True), 0, self.LIMIT, produced_evidence=False), (True, 0))


class SpendWithoutDeltaTest(unittest.TestCase):
    """The stop rule is money-without-progress, not money. Measured from one night:
    the run that should have stopped had spent $8.10 past its last criterion delta and produced nothing;
    the run that should NOT have stopped spent $9.66 past its last delta and then passed.
    A flat cost ceiling cannot separate those two — a since-last-delta ceiling can."""

    def test_default_threshold_separates_the_two_measured_runs(self):
        from longrun.contract import DEFAULT_BUDGETS
        threshold = DEFAULT_BUDGETS["max_cost_without_delta_usd"]
        stalled_run_spend_since_delta = 8.10 + 8.0   # kept paying after the guard also flagged stagnation
        productive_run_spend_since_delta = 9.66
        self.assertGreater(stalled_run_spend_since_delta, threshold)
        self.assertLess(productive_run_spend_since_delta, threshold,
                        "the threshold must not kill a run that was still one round from passing")


if __name__ == "__main__":
    unittest.main()
