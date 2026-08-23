import unittest

from longrun.controller import _LIMIT_RE, _RESET_RE


class LimitDetectionTest(unittest.TestCase):
    """A provider limit must be recognised as infrastructure, not as the builder's failure: it schedules a
    strategic fallback instead of charging a round and telling the next builder to change hypothesis.

    Per-model limits are phrased differently from account limits and were missed entirely. The message that
    actually ends a Fable session names the model and points at /usage-credits — it contains neither the words
    "usage limit" nor a reset time, so the strategic fallback did not fire for the one case it was built for.
    This was found when a live Fable agent died with exactly this text."""

    REAL_LIMITS = [
        "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model.",
        "You've reached your Opus 5 limit. Run /usage-credits to continue.",
        "Claude usage limit reached. Your limit will reset at 3pm.",
        "5-hour limit reached ∙ resets 4am",
        "API Error: 429 rate limit exceeded",
        "API Error: 529 overloaded_error",
        "fetch failed",
    ]

    NOT_LIMITS = [
        "error CS0128: a local variable named 'n' is already defined",
        "AssertionError: expected 3 rivets, found 2",
        "the girder limit switch prefab is missing",     # 'limit' as a domain word, not a provider message
        "Traceback (most recent call last): ZeroDivisionError",
    ]

    def test_every_real_limit_message_is_recognised(self):
        for m in self.REAL_LIMITS:
            with self.subTest(m=m):
                self.assertTrue(_LIMIT_RE.search(m), f"not detected as a provider limit: {m!r}")

    def test_ordinary_failures_are_not_mistaken_for_limits(self):
        for m in self.NOT_LIMITS:
            with self.subTest(m=m):
                self.assertFalse(_LIMIT_RE.search(m), f"wrongly detected as a provider limit: {m!r}")

    def test_a_reset_time_is_still_parsed_when_one_is_given(self):
        m = _RESET_RE.search("Claude usage limit reached. Your limit will reset at 3pm.")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "3")

    def test_a_per_model_limit_without_a_reset_time_parses_no_time(self):
        self.assertIsNone(_RESET_RE.search("You've reached your Fable 5 limit. Run /usage-credits to continue."))


if __name__ == "__main__":
    unittest.main()
