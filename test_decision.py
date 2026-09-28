"""Unit tests for decision.py - the final BUY-direction truth table.

decide_direction() is driven by TWO independent things that must both check
out before any single-sided or straddle trade is taken:
  1. upside_probability (the SAME combined probability shown on the
     dashboard - see option_signal.combine_probability_signals) for WHICH
     WAY the market is more likely to go.
  2. Whether the market's own VIX-implied expected_move is big enough to
     clear that trade's real, live-premium-derived breakeven
     (required_move_call/put/both, computed by engine.py from today's
     actual ATM option prices) - i.e. is it actually likely to be
     PROFITABLE, not just directionally plausible.

Default thresholds: call_threshold=0.55, put_threshold=0.45.
"""

import pytest

import decision


def test_large_gap_always_both():
    # large_gap fires BOTH unconditionally, before the profitability check -
    # no expected_move/required_move data needed.
    assert decision.decide_direction(0.80, "POSITIVE", large_gap=True) == "BOTH"
    assert decision.decide_direction(0.50, "NEUTRAL", large_gap=True) == "BOTH"
    assert decision.decide_direction(0.20, "NEGATIVE", large_gap=True) == "BOTH"


def test_range_too_tight_always_no_trade():
    # overrides everything else, including a clear high/low probability,
    # profitable move data, and even large_gap
    assert decision.decide_direction(
        0.90, "POSITIVE", large_gap=False, range_too_tight=True,
        expected_move=500, required_move_call=50, required_move_both=100,
    ) == "NO_TRADE"
    assert decision.decide_direction(0.10, "NEGATIVE", large_gap=True, range_too_tight=True) == "NO_TRADE"


def test_range_too_tight_defaults_to_false():
    # omitting range_too_tight doesn't itself block trading - given
    # sufficient profitability data, a clear edge still resolves to CALL.
    assert decision.decide_direction(
        0.80, "POSITIVE", large_gap=False, expected_move=200, required_move_call=100,
    ) == "CALL"


def test_no_profitability_data_defaults_to_no_trade():
    # a clear directional edge alone is NOT enough - without expected_move/
    # required_move data, profitability can't be verified, so nothing is
    # assumed safe.
    assert decision.decide_direction(0.80, "POSITIVE", large_gap=False) == "NO_TRADE"
    assert decision.decide_direction(0.20, "NEGATIVE", large_gap=False) == "NO_TRADE"


def test_coin_flip_probability_without_enough_move_is_no_trade():
    assert decision.decide_direction(
        0.50, "NEUTRAL", large_gap=False, expected_move=50, required_move_both=200,
    ) == "NO_TRADE"


def test_directional_edge_but_move_too_small_is_no_trade():
    # probability clearly favors CALL, but today's ATM premium is too rich
    # (or the expected move too small) for the trade to plausibly profit -
    # skip it rather than bet on direction alone.
    assert decision.decide_direction(
        0.90, "NEUTRAL", large_gap=False, expected_move=80, required_move_call=150,
    ) == "NO_TRADE"
    assert decision.decide_direction(
        0.10, "NEUTRAL", large_gap=False, expected_move=80, required_move_put=150,
    ) == "NO_TRADE"


def test_upside_edge_with_agreeing_or_neutral_ai_is_call_when_profitable():
    assert decision.decide_direction(
        0.55, "POSITIVE", large_gap=False, expected_move=150, required_move_call=100,
    ) == "CALL"
    assert decision.decide_direction(
        0.55, "NEUTRAL", large_gap=False, expected_move=150, required_move_call=100,
    ) == "CALL"
    assert decision.decide_direction(
        0.90, "NEUTRAL", large_gap=False, expected_move=150, required_move_call=100,
    ) == "CALL"


def test_downside_edge_with_agreeing_or_neutral_ai_is_put_when_profitable():
    assert decision.decide_direction(
        0.45, "NEGATIVE", large_gap=False, expected_move=150, required_move_put=100,
    ) == "PUT"
    assert decision.decide_direction(
        0.45, "NEUTRAL", large_gap=False, expected_move=150, required_move_put=100,
    ) == "PUT"
    assert decision.decide_direction(
        0.10, "NEUTRAL", large_gap=False, expected_move=150, required_move_put=100,
    ) == "PUT"


def test_ai_conflict_with_probability_is_both_when_straddle_profitable():
    assert decision.decide_direction(
        0.70, "NEGATIVE", large_gap=False, expected_move=250, required_move_call=100,
        required_move_both=200,
    ) == "BOTH"
    assert decision.decide_direction(
        0.30, "POSITIVE", large_gap=False, expected_move=250, required_move_put=100,
        required_move_both=200,
    ) == "BOTH"


def test_ai_conflict_but_straddle_not_profitable_is_no_trade():
    # real edge up, AI disagrees -> would hedge with BOTH, but the
    # straddle's own (larger, two-premium) breakeven isn't reachable either
    assert decision.decide_direction(
        0.70, "NEGATIVE", large_gap=False, expected_move=120, required_move_call=100,
        required_move_both=200,
    ) == "NO_TRADE"


def test_invalid_probability_raises():
    with pytest.raises(ValueError):
        decision.decide_direction(1.5, "NEUTRAL", large_gap=False)
    with pytest.raises(ValueError):
        decision.decide_direction(-0.1, "NEUTRAL", large_gap=False)


def test_invalid_ai_sentiment_raises():
    with pytest.raises(ValueError):
        decision.decide_direction(0.80, "MAYBE", large_gap=False)


def test_invalid_thresholds_raise():
    with pytest.raises(ValueError):
        decision.decide_direction(0.80, "NEUTRAL", large_gap=False, call_threshold=0.4, put_threshold=0.5)


def test_custom_thresholds_are_respected():
    # a wider, stricter band (0.4-0.6) - 0.58 no longer clears the call bar
    assert decision.decide_direction(
        0.58, "NEUTRAL", large_gap=False, call_threshold=0.6, put_threshold=0.4,
        expected_move=150, required_move_call=100,
    ) == "NO_TRADE"
    assert decision.decide_direction(
        0.62, "NEUTRAL", large_gap=False, call_threshold=0.6, put_threshold=0.4,
        expected_move=150, required_move_call=100,
    ) == "CALL"


def test_expected_move_clearing_straddle_breakeven_turns_coin_flip_into_both():
    # direction unclear (inside the 45-55% band), but the market's expected
    # move is big enough to clear the ACTUAL combined CE+PE premium cost
    assert decision.decide_direction(
        0.50, "NEUTRAL", large_gap=False, expected_move=230, required_move_both=200,
    ) == "BOTH"
    assert decision.decide_direction(
        0.52, "NEUTRAL", large_gap=False, expected_move=230, required_move_both=200,
    ) == "BOTH"


def test_required_move_has_no_effect_once_a_directional_edge_exists():
    # call/put thresholds are checked first - the coin-flip BOTH profitability
    # check only matters in the coin-flip band, never overrides a real
    # directional edge that's itself already profitable
    assert decision.decide_direction(
        0.80, "NEUTRAL", large_gap=False, expected_move=150, required_move_call=100, required_move_both=1000,
    ) == "CALL"
    assert decision.decide_direction(
        0.20, "NEUTRAL", large_gap=False, expected_move=150, required_move_put=100, required_move_both=1000,
    ) == "PUT"


def test_range_too_tight_overrides_profitable_straddle():
    # the two checks should never both fire in practice, but if they somehow
    # did, "too tight to trade at all" wins
    assert decision.decide_direction(
        0.50, "NEUTRAL", large_gap=False, range_too_tight=True,
        expected_move=230, required_move_both=200,
    ) == "NO_TRADE"


@pytest.mark.parametrize(
    "upside_probability,ai_sentiment,large_gap,expected",
    [
        (0.80, "POSITIVE", False, "CALL"),
        (0.80, "NEUTRAL", False, "CALL"),
        (0.80, "NEGATIVE", False, "BOTH"),
        (0.20, "NEGATIVE", False, "PUT"),
        (0.20, "NEUTRAL", False, "PUT"),
        (0.20, "POSITIVE", False, "BOTH"),
        # Coin-flip band (45%-55%): with ample profitable move data supplied
        # below (expected_move=300 clears required_move_both=200), this now
        # correctly resolves to BOTH rather than NO_TRADE - a straddle IS
        # worth taking here since direction is unclear but a big, profitable
        # move is expected either way. See test_coin_flip_probability_
        # without_enough_move_is_no_trade for the case where it isn't.
        (0.50, "NEUTRAL", False, "BOTH"),
        (0.50, "POSITIVE", False, "BOTH"),
        (0.50, "NEGATIVE", False, "BOTH"),
    ],
)
def test_full_truth_table(upside_probability, ai_sentiment, large_gap, expected):
    # ample, uniformly profitable move data throughout, so this table tests
    # purely the probability/AI-conflict truth table, independent of the
    # profitability gate (which has its own dedicated tests above).
    assert decision.decide_direction(
        upside_probability, ai_sentiment, large_gap,
        expected_move=300, required_move_call=100, required_move_put=100, required_move_both=200,
    ) == expected
