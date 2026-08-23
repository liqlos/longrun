from longrun.cli import (_CHAIN_OWNER_STOP_STATUSES, _chain_error_was_before_execution,
                         _chain_failure_continuation, _chain_outcome_allows_advance)


def test_chain_advances_freely_only_after_evaluator_accepted_status():
    assert _chain_outcome_allows_advance("PASSED")
    assert _chain_outcome_allows_advance("PARTIAL_PASS")
    for status in ("RESET_RECOMMENDED", "BLOCKED", "BUDGET_EXHAUSTED", "FAILED", "STOPPED"):
        assert not _chain_outcome_allows_advance(status)


def test_owner_stop_statuses_are_never_skipped_past():
    assert set(_CHAIN_OWNER_STOP_STATUSES) == {"STOPPED", "INTERRUPTED", "OWNER_JUDGMENT_REQUIRED"}


def test_exactly_one_strategy_change_is_bought_by_a_failure():
    # first bounded failure: one continuation allowed
    assert _chain_failure_continuation(0, "no workspace delta", None) is None
    # a second consecutive failure stops, whatever its signature
    stop = _chain_failure_continuation(1, "different signature", "no workspace delta")
    assert stop and "single allowed strategy change" in stop


def test_repeating_the_same_failure_signature_stops_immediately():
    stop = _chain_failure_continuation(0, "no workspace delta", "no workspace delta")
    assert stop and "same failure signature" in stop
    assert _chain_failure_continuation(0, "", "anything") is None  # empty signature cannot match


def test_post_freeze_controller_error_is_not_mislabeled_start_failure():
    assert _chain_error_was_before_execution({"counters": {"rounds": 0}})
    assert not _chain_error_was_before_execution({"contract_hash": "abc", "counters": {"rounds": 0}})
    assert not _chain_error_was_before_execution({"counters": {"rounds": 1}})


def _chain_episode_sequence(statuses, signatures):
    """Mirror cmd_go's exact bookkeeping across a chain and return the stop decision
    for the final failure (True = chain stopped)."""
    failed_outcomes = 0
    last_fail_sig = None
    decisions = []
    for status, sig in zip(statuses, signatures):
        if status in ("PASSED", "PARTIAL_PASS"):
            failed_outcomes = 0          # the reset under test: progress starts a new episode
            last_fail_sig = None
            continue
        if status in ("STOPPED", "INTERRUPTED", "OWNER_JUDGMENT_REQUIRED"):
            return True
        stop = _chain_failure_continuation(failed_outcomes, sig, last_fail_sig)
        if stop:
            return True
        failed_outcomes += 1
        last_fail_sig = sig
    return False


def test_second_consecutive_failure_stops_the_chain():
    assert _chain_episode_sequence(["FAILED", "FAILED"], ["sig-a", "sig-b"]) is True


def test_failure_after_accepted_progress_gets_a_fresh_continuation():
    # FAILED -> PASSED -> FAILED: the second failure is a new episode, not consecutive.
    assert _chain_episode_sequence(
        ["FAILED", "PASSED", "FAILED"], ["sig-a", None, "sig-b"]) is False
    assert _chain_episode_sequence(
        ["FAILED", "PARTIAL_PASS", "FAILED"], ["sig-a", None, "sig-b"]) is False


def test_repeated_signature_without_intermediate_progress_stops():
    assert _chain_episode_sequence(["FAILED", "FAILED"], ["sig-a", "sig-a"]) is True


def test_longer_mixed_chain_stays_bounded_and_recovers():
    # Two healthy outcomes between failures: each episode again buys one continuation,
    # then the third consecutive-ish pair stops as before.
    assert _chain_episode_sequence(
        ["FAILED", "PASSED", "FAILED", "PARTIAL_PASS", "FAILED"],
        ["sig-a", None, "sig-b", None, "sig-c"]) is False
    # a fresh episode still allows only one failure: its second consecutive one stops
    assert _chain_episode_sequence(
        ["FAILED", "PASSED", "FAILED", "FAILED"],
        ["sig-a", None, "sig-b", "sig-c"]) is True
