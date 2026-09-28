"""Unit tests for option_signal.py - all pure functions, no Kite/network needed."""

import pytest

import option_signal as os_


def test_get_strike_interval():
    assert os_.get_strike_interval([24700, 24750, 24800, 24850]) == 50


def test_get_strike_interval_too_few():
    with pytest.raises(ValueError):
        os_.get_strike_interval([24700])


def test_get_atm_strike_rounds_to_nearest():
    assert os_.get_atm_strike(24732, 50) == 24750
    assert os_.get_atm_strike(24724, 50) == 24700
    # exact tie (24725 is exactly halfway between 24700 and 24750) uses
    # Python's round() banker's-rounding (round-half-to-even) -> 24700
    assert os_.get_atm_strike(24725, 50) == 24700


def test_get_strikes_around_atm():
    strikes = os_.get_strikes_around_atm(24700, 50, n=5)
    assert strikes == [24450, 24500, 24550, 24600, 24650, 24700, 24750, 24800, 24850, 24900, 24950]
    assert len(strikes) == 11


def test_compute_oi_bias_bullish():
    # more puts than calls -> bullish
    data = {
        24700: {"CE": 100000, "PE": 150000},
        24750: {"CE": 90000, "PE": 140000},
    }
    result = os_.compute_oi_bias(data, pcr_threshold=0.15)
    assert result["oi_bias"] == "BULLISH"
    assert result["pcr"] > 1.15


def test_compute_oi_bias_bearish():
    data = {
        24700: {"CE": 150000, "PE": 90000},
        24750: {"CE": 140000, "PE": 80000},
    }
    result = os_.compute_oi_bias(data, pcr_threshold=0.15)
    assert result["oi_bias"] == "BEARISH"
    assert result["pcr"] < 0.85


def test_compute_oi_bias_neutral():
    data = {
        24700: {"CE": 100000, "PE": 100000},
        24750: {"CE": 100000, "PE": 105000},
    }
    result = os_.compute_oi_bias(data, pcr_threshold=0.15)
    assert result["oi_bias"] == "NEUTRAL"


def test_compute_oi_bias_zero_call_oi():
    data = {24700: {"CE": 0, "PE": 100}}
    result = os_.compute_oi_bias(data, pcr_threshold=0.15)
    assert result["oi_bias"] == "BULLISH"
    assert result["pcr"] == float("inf")


# ---------------------------------------------------------------- proximity-weighted compute_oi_bias (Phase 6)

def test_compute_oi_bias_without_proximity_args_weighted_pcr_equals_plain_pcr():
    # omitting spot_price/strike_interval must behave EXACTLY like before -
    # existing callers (and the "pcr" shown on the dashboard) are unaffected
    data = {24700: {"CE": 100000, "PE": 100000}, 24750: {"CE": 100000, "PE": 105000}}
    result = os_.compute_oi_bias(data, pcr_threshold=0.15)
    assert result["weighted_pcr"] == pytest.approx(result["pcr"])
    assert result["oi_bias"] == "NEUTRAL"


def test_compute_oi_bias_proximity_can_flip_neutral_to_bullish():
    # whole-window PCR is a mild, NEUTRAL 1.11 (matches the real report that
    # prompted this upgrade) - but the OTM strike immediately next to spot
    # is heavily put-skewed, while a call-heavy strike sits far away. The
    # real, unweighted "pcr" shown on the dashboard must NOT change (still
    # verifiable against the real option chain) - only the classification.
    data = {
        23350: {"CE": 10000, "PE": 90000},   # 50pts from spot - heavily put-skewed, right next door
        23650: {"CE": 90000, "PE": 10000},   # 250pts from spot (tracked window edge) - heavily call-skewed, far away
    }
    plain = os_.compute_oi_bias(data, pcr_threshold=0.15)
    assert plain["oi_bias"] == "NEUTRAL"  # whole-window totals happen to cancel out to ~1.0

    weighted = os_.compute_oi_bias(
        data, pcr_threshold=0.15, spot_price=23400, strike_interval=50, window_strikes=5,
    )
    assert weighted["pcr"] == pytest.approx(plain["pcr"])  # real PCR for display is untouched
    assert weighted["oi_bias"] == "BULLISH"  # but classification now reflects the nearby skew
    assert weighted["weighted_pcr"] > weighted["pcr"]


def test_compute_oi_bias_proximity_defaults_to_no_effect_when_partial_args_missing():
    # spot_price given but strike_interval missing (or vice versa) -> no
    # proximity weighting applied, same as giving neither
    data = {23350: {"CE": 10000, "PE": 90000}, 23650: {"CE": 90000, "PE": 10000}}
    result = os_.compute_oi_bias(data, pcr_threshold=0.15, spot_price=23400)
    assert result["weighted_pcr"] == pytest.approx(result["pcr"])


# ---------------------------------------------------------------- OI-change-weighted compute_oi_bias (Phase 10)

def test_compute_oi_bias_oi_change_can_flip_neutral_to_bullish():
    # Symmetric static OI (so proximity-only weighting stays exactly
    # neutral) at a support and a resistance strike equidistant from spot -
    # but the support (PUT) side shows heavy fresh build-up vs yesterday
    # while the resistance (CALL) side is unchanged. This reproduces the
    # user's real reported scenario (heavy PUT build-up at support/ATM
    # while OI Bias stayed NEUTRAL).
    data = {23350: {"CE": 10000, "PE": 50000}, 23450: {"CE": 50000, "PE": 10000}}
    without_change = os_.compute_oi_bias(
        data, pcr_threshold=0.15, spot_price=23400, strike_interval=50, window_strikes=5,
    )
    assert without_change["oi_bias"] == "NEUTRAL"  # symmetric proximity -> exactly balanced

    with_change = os_.compute_oi_bias(
        data, pcr_threshold=0.15, spot_price=23400, strike_interval=50, window_strikes=5,
        strike_oi_change_ratios={23350: {"PE": 1.5}}, oi_change_clamp=0.5,  # support PE +50% fresh build-up
    )
    assert with_change["weighted_pcr"] > without_change["weighted_pcr"]
    assert with_change["oi_bias"] == "BULLISH"
    assert with_change["pcr"] == pytest.approx(without_change["pcr"])  # real/display PCR untouched


def test_compute_oi_bias_oi_change_clamped():
    data = {23350: {"CE": 10000, "PE": 50000}, 23450: {"CE": 50000, "PE": 10000}}
    at_clamp = os_.compute_oi_bias(
        data, pcr_threshold=0.15, spot_price=23400, strike_interval=50,
        strike_oi_change_ratios={23350: {"PE": 1.5}}, oi_change_clamp=0.5,
    )
    beyond_clamp = os_.compute_oi_bias(
        data, pcr_threshold=0.15, spot_price=23400, strike_interval=50,
        strike_oi_change_ratios={23350: {"PE": 3.0}}, oi_change_clamp=0.5,  # way past the clamp
    )
    assert beyond_clamp["weighted_pcr"] == pytest.approx(at_clamp["weighted_pcr"])  # clamped, not the raw ratio


def test_compute_oi_bias_oi_change_only_applies_to_listed_strikes():
    # a strike with no entry in strike_oi_change_ratios keeps its plain
    # proximity weight untouched
    data = {23350: {"CE": 10000, "PE": 50000}, 23450: {"CE": 50000, "PE": 10000}}
    baseline = os_.compute_oi_bias(data, pcr_threshold=0.15, spot_price=23400, strike_interval=50)
    with_unrelated_strike = os_.compute_oi_bias(
        data, pcr_threshold=0.15, spot_price=23400, strike_interval=50,
        strike_oi_change_ratios={99999: {"CE": 1.5}},  # a strike not even in strike_oi_data
    )
    assert with_unrelated_strike["weighted_pcr"] == pytest.approx(baseline["weighted_pcr"])


def test_compute_oi_bias_oi_change_none_ratio_has_no_effect():
    data = {23350: {"CE": 10000, "PE": 50000}, 23450: {"CE": 50000, "PE": 10000}}
    baseline = os_.compute_oi_bias(data, pcr_threshold=0.15, spot_price=23400, strike_interval=50)
    with_none = os_.compute_oi_bias(
        data, pcr_threshold=0.15, spot_price=23400, strike_interval=50,
        strike_oi_change_ratios={23350: {"PE": None}},
    )
    assert with_none["weighted_pcr"] == pytest.approx(baseline["weighted_pcr"])


def test_compute_vwap_from_candles():
    candles = [
        {"high": 100, "low": 98, "close": 99, "volume": 1000},
        {"high": 102, "low": 99, "close": 101, "volume": 2000},
    ]
    vwap = os_.compute_vwap_from_candles(candles)
    tp1 = (100 + 98 + 99) / 3
    tp2 = (102 + 99 + 101) / 3
    expected = (tp1 * 1000 + tp2 * 2000) / 3000
    assert vwap == pytest.approx(expected)


def test_compute_vwap_from_candles_zero_volume():
    with pytest.raises(ValueError):
        os_.compute_vwap_from_candles([{"high": 1, "low": 1, "close": 1, "volume": 0}])


def test_compute_vwap_bias_bullish():
    result = os_.compute_vwap_bias(current_price=25000, vwap=24800)
    assert result["vwap_bias"] == "BULLISH"


def test_compute_vwap_bias_bearish():
    result = os_.compute_vwap_bias(current_price=24600, vwap=24800)
    assert result["vwap_bias"] == "BEARISH"


def test_compute_vwap_bias_neutral_band():
    # within default +/-0.05% band around vwap=24800 -> neutral
    result = os_.compute_vwap_bias(current_price=24805, vwap=24800)
    assert result["vwap_bias"] == "NEUTRAL"


def test_combine_bias_agree_bullish():
    assert os_.combine_bias("BULLISH", "BULLISH") == "BULLISH"


def test_combine_bias_agree_bearish():
    assert os_.combine_bias("BEARISH", "BEARISH") == "BEARISH"


def test_combine_bias_disagree():
    assert os_.combine_bias("BULLISH", "BEARISH") == "NEUTRAL"
    assert os_.combine_bias("BULLISH", "NEUTRAL") == "NEUTRAL"
    assert os_.combine_bias("NEUTRAL", "NEUTRAL") == "NEUTRAL"


def test_combine_bias_three_signals_two_of_three_wins():
    # 2 of 3 agreeing bullish is enough, even if the third is neutral or opposed
    assert os_.combine_bias("BULLISH", "BULLISH", "NEUTRAL") == "BULLISH"
    assert os_.combine_bias("BULLISH", "NEUTRAL", "BULLISH") == "BULLISH"
    assert os_.combine_bias("BULLISH", "BULLISH", "BEARISH") == "BULLISH"
    assert os_.combine_bias("BEARISH", "BEARISH", "NEUTRAL") == "BEARISH"
    assert os_.combine_bias("BEARISH", "BULLISH", "BEARISH") == "BEARISH"


def test_combine_bias_three_signals_no_majority_is_neutral():
    assert os_.combine_bias("BULLISH", "BEARISH", "NEUTRAL") == "NEUTRAL"
    assert os_.combine_bias("NEUTRAL", "NEUTRAL", "NEUTRAL") == "NEUTRAL"
    assert os_.combine_bias("BULLISH", "NEUTRAL", "NEUTRAL") == "NEUTRAL"


def test_compute_orb_bias_bullish_breakout():
    result = os_.compute_orb_bias(current_price=23470, orb_high=23450, orb_low=23400)
    assert result == "BULLISH"


def test_compute_orb_bias_bearish_breakdown():
    result = os_.compute_orb_bias(current_price=23380, orb_high=23450, orb_low=23400)
    assert result == "BEARISH"


def test_compute_orb_bias_neutral_inside_range():
    result = os_.compute_orb_bias(current_price=23420, orb_high=23450, orb_low=23400)
    assert result == "NEUTRAL"
    # exactly on the boundary is still "inside" (not yet broken)
    assert os_.compute_orb_bias(current_price=23450, orb_high=23450, orb_low=23400) == "NEUTRAL"
    assert os_.compute_orb_bias(current_price=23400, orb_high=23450, orb_low=23400) == "NEUTRAL"


def test_compute_cpr_width_zero_when_close_at_midpoint():
    # close exactly at (high+low)/2 -> narrowest possible CPR (width 0)
    width = os_.compute_cpr_width(prev_high=23500, prev_low=23300, prev_close=23400)
    assert width == pytest.approx(0)


def test_compute_cpr_width_maximal_when_close_at_high_or_low():
    # close at the day's high (or low) -> CPR width hits its real
    # mathematical maximum of (high-low)/3 for that same day's own range
    width_at_high = os_.compute_cpr_width(prev_high=23500, prev_low=23300, prev_close=23500)
    assert width_at_high == pytest.approx((23500 - 23300) / 3)
    width_at_low = os_.compute_cpr_width(prev_high=23500, prev_low=23300, prev_close=23300)
    assert width_at_low == pytest.approx((23500 - 23300) / 3)


def test_compute_cpr_width_is_symmetric_around_midpoint():
    # close equidistant above vs below the midpoint gives the same width
    above = os_.compute_cpr_width(prev_high=23500, prev_low=23300, prev_close=23450)
    below = os_.compute_cpr_width(prev_high=23500, prev_low=23300, prev_close=23350)
    assert above == pytest.approx(below)


def test_compute_gap_flag_large_gap():
    result = os_.compute_gap_flag(
        daily_ranges=[100, 120, 80, 90, 110],
        today_open=25100,
        yesterday_close=25000,
        gap_threshold_factor=0.5,
    )
    # avg_range = 100, gap = 100, threshold = 50 -> large gap
    assert result["large_gap"] is True
    assert result["avg_range"] == 100
    assert result["gap"] == 100


def test_compute_gap_flag_small_gap():
    result = os_.compute_gap_flag(
        daily_ranges=[100, 120, 80, 90, 110],
        today_open=25020,
        yesterday_close=25000,
        gap_threshold_factor=0.5,
    )
    # gap = 20, threshold = 50 -> not a large gap
    assert result["large_gap"] is False


def test_compute_gap_flag_empty_ranges():
    with pytest.raises(ValueError):
        os_.compute_gap_flag([], today_open=100, yesterday_close=100, gap_threshold_factor=0.5)


def test_compute_vix_expected_move():
    # spot=24700, vix=13 -> 24700 * 0.13 * sqrt(1/252)
    import math
    expected = 24700 * 0.13 * math.sqrt(1 / 252)
    result = os_.compute_vix_expected_move(24700, 13)
    assert result == pytest.approx(expected)


def test_compute_vix_expected_move_scales_with_vix():
    low_vix_move = os_.compute_vix_expected_move(24700, 10)
    high_vix_move = os_.compute_vix_expected_move(24700, 20)
    assert high_vix_move == pytest.approx(low_vix_move * 2)


def test_compute_vix_expected_move_invalid_inputs():
    with pytest.raises(ValueError):
        os_.compute_vix_expected_move(0, 13)
    with pytest.raises(ValueError):
        os_.compute_vix_expected_move(24700, -1)


def test_compute_oi_walls_finds_max_otm_oi_each_side():
    atm = 24700
    strike_oi_data = {
        24600: {"CE": 50000, "PE": 80000},
        24650: {"CE": 60000, "PE": 200000},   # strongest PE below ATM -> support
        24700: {"CE": 100000, "PE": 100000},  # ATM itself excluded from walls
        24750: {"CE": 300000, "PE": 40000},   # strongest CE above ATM -> resistance
        24800: {"CE": 90000, "PE": 20000},
    }
    result = os_.compute_oi_walls(strike_oi_data, atm)
    assert result["resistance_strike"] == 24750
    assert result["resistance_oi"] == 300000
    assert result["support_strike"] == 24650
    assert result["support_oi"] == 200000


def test_compute_oi_walls_no_strikes_either_side():
    result = os_.compute_oi_walls({24700: {"CE": 100, "PE": 100}}, 24700)
    assert result["resistance_strike"] is None
    assert result["support_strike"] is None


def test_compute_direction_probability_support_heavy_is_bullish():
    result = os_.compute_direction_probability(support_oi=300000, resistance_oi=100000)
    assert result["upside_probability"] == pytest.approx(0.75)
    assert result["downside_probability"] == pytest.approx(0.25)


def test_compute_direction_probability_resistance_heavy_is_bearish():
    result = os_.compute_direction_probability(support_oi=100000, resistance_oi=300000)
    assert result["upside_probability"] == pytest.approx(0.25)


def test_compute_direction_probability_no_data_is_neutral_split():
    result = os_.compute_direction_probability(support_oi=0, resistance_oi=0)
    assert result["upside_probability"] == pytest.approx(0.5)
    assert result["downside_probability"] == pytest.approx(0.5)


# ---------------------------------------------------------------- proximity-weighted compute_direction_probability (Phase 2)

def test_compute_direction_probability_without_proximity_args_is_unchanged():
    # omitting strike/spot_price/strike_interval must behave EXACTLY like
    # before Phase 2 - existing callers are unaffected
    result = os_.compute_direction_probability(support_oi=300000, resistance_oi=100000)
    assert result["upside_probability"] == pytest.approx(0.75)


def test_compute_direction_probability_equal_oi_closer_wall_wins():
    # EQUAL OI on both sides, but support is much closer to spot than
    # resistance - proximity alone should now tilt it bullish, proving the
    # wall's distance actually matters, not just its raw OI size.
    result = os_.compute_direction_probability(
        support_oi=100000, resistance_oi=100000,
        support_strike=23350, resistance_strike=23600,  # support 50pts away, resistance 200pts away
        spot_price=23400, strike_interval=50, window_strikes=5,  # window = 250pts
    )
    assert result["upside_probability"] > 0.5


def test_compute_direction_probability_wall_at_window_edge_contributes_nothing():
    # resistance sitting EXACTLY at the edge of the tracked window (5*50=250pts
    # away) should get zero weight, regardless of its OI size - support
    # right at spot should completely dominate.
    result = os_.compute_direction_probability(
        support_oi=50000, resistance_oi=999999,
        support_strike=23400, resistance_strike=23650,  # support 0pts away, resistance exactly 250pts away
        spot_price=23400, strike_interval=50, window_strikes=5,
    )
    assert result["upside_probability"] == pytest.approx(1.0)


def test_compute_direction_probability_heavier_but_farther_wall_can_still_lose():
    # a MUCH heavier resistance wall that's far away can be outweighed by a
    # lighter but much closer support wall - proximity isn't just a tiebreaker,
    # it can flip the overall lean vs raw-OI-only comparison.
    raw_only = os_.compute_direction_probability(support_oi=50000, resistance_oi=500000)
    assert raw_only["upside_probability"] < 0.5  # raw OI alone says bearish

    weighted = os_.compute_direction_probability(
        support_oi=50000, resistance_oi=500000,
        support_strike=23410, resistance_strike=23650,  # support 10pts away, resistance 250pts away (edge, zero weight)
        spot_price=23400, strike_interval=50, window_strikes=5,
    )
    assert weighted["upside_probability"] == pytest.approx(1.0)  # proximity flips it fully bullish


# ---------------------------------------------------------------- OI-change-weighted compute_direction_probability (Phase 3)

def test_compute_direction_probability_fresh_buildup_strengthens_wall():
    # equal OI, equal proximity (both directly at spot, no distance
    # component) - but support has FRESH build-up (ratio 1.4x yesterday)
    # while resistance is flat (ratio 1.0) -> should tilt bullish.
    result = os_.compute_direction_probability(
        support_oi=100000, resistance_oi=100000,
        support_oi_change_ratio=1.4, resistance_oi_change_ratio=1.0,
    )
    assert result["upside_probability"] > 0.5


def test_compute_direction_probability_unwinding_weakens_wall():
    # equal OI, but resistance is UNWINDING (ratio 0.6, sellers covering)
    # while support is flat -> should tilt bullish (resistance getting
    # weaker even though its raw OI number hasn't visibly dropped much yet).
    result = os_.compute_direction_probability(
        support_oi=100000, resistance_oi=100000,
        support_oi_change_ratio=1.0, resistance_oi_change_ratio=0.6,
    )
    assert result["upside_probability"] > 0.5


def test_compute_direction_probability_oi_change_ratio_is_clamped():
    # an extreme, single-day 10x build-up must be clamped (default
    # oi_change_clamp=0.5 -> multiplier capped at 1.5x), not allowed to
    # completely dominate the result on its own.
    clamped = os_.compute_direction_probability(
        support_oi=100000, resistance_oi=100000,
        support_oi_change_ratio=10.0, resistance_oi_change_ratio=1.0,
    )
    capped_at_1_5x = os_.compute_direction_probability(
        support_oi=100000, resistance_oi=100000,
        support_oi_change_ratio=1.5, resistance_oi_change_ratio=1.0,
    )
    assert clamped["upside_probability"] == pytest.approx(capped_at_1_5x["upside_probability"])


def test_compute_direction_probability_oi_change_ratio_defaults_to_no_effect():
    # omitting oi_change_ratio entirely must not change existing (Phase 1/2) behavior
    result = os_.compute_direction_probability(support_oi=100000, resistance_oi=100000)
    assert result["upside_probability"] == pytest.approx(0.5)


def test_combine_probability_signals_all_bullish_nudges_up():
    result = os_.combine_probability_signals(
        base_upside_probability=0.5, pcr=1.3, pcr_threshold=0.15,
        vwap_bias="BULLISH", orb_bias="BULLISH", gap=20, ai_sentiment="POSITIVE",
    )
    # base 0.5 + 5 agreeing signals * 0.05 = 0.75
    assert result["upside_probability"] == pytest.approx(0.75)
    assert result["downside_probability"] == pytest.approx(0.25)


def test_combine_probability_signals_all_bearish_nudges_down():
    result = os_.combine_probability_signals(
        base_upside_probability=0.5, pcr=0.7, pcr_threshold=0.15,
        vwap_bias="BEARISH", orb_bias="BEARISH", gap=-20, ai_sentiment="NEGATIVE",
    )
    assert result["upside_probability"] == pytest.approx(0.25)


def test_combine_probability_signals_orb_alone_nudges():
    # only ORB disagrees with an otherwise-neutral read - proves ORB is
    # actually wired into the probability, not just the separate technical
    # bias vote (same gap that was previously reported for pivot - the old
    # signal this replaces - showed BULLISH on the dashboard but never
    # moved the probability number).
    result = os_.combine_probability_signals(
        base_upside_probability=0.5, pcr=None, pcr_threshold=0.15,
        vwap_bias="NEUTRAL", orb_bias="BULLISH", gap=None, ai_sentiment="NEUTRAL",
    )
    assert result["upside_probability"] == pytest.approx(0.55)


def test_combine_probability_signals_mixed_partially_cancels():
    # pcr bullish, vwap bearish -> cancel out; orb/gap neutral; ai neutral
    result = os_.combine_probability_signals(
        base_upside_probability=0.6, pcr=1.3, pcr_threshold=0.15,
        vwap_bias="BEARISH", orb_bias="NEUTRAL", gap=0, ai_sentiment="NEUTRAL",
    )
    assert result["upside_probability"] == pytest.approx(0.6)


def test_combine_probability_signals_neutral_inputs_dont_move_base():
    result = os_.combine_probability_signals(
        base_upside_probability=0.55, pcr=None, pcr_threshold=0.15,
        vwap_bias="NEUTRAL", orb_bias="NEUTRAL", gap=None, ai_sentiment="NEUTRAL",
    )
    assert result["upside_probability"] == pytest.approx(0.55)


def test_combine_probability_signals_clamped_to_5_95_range():
    result = os_.combine_probability_signals(
        base_upside_probability=0.9, pcr=1.5, pcr_threshold=0.15,
        vwap_bias="BULLISH", orb_bias="BULLISH", gap=50, ai_sentiment="POSITIVE",
    )
    assert result["upside_probability"] <= 0.95

    result2 = os_.combine_probability_signals(
        base_upside_probability=0.1, pcr=0.5, pcr_threshold=0.15,
        vwap_bias="BEARISH", orb_bias="BEARISH", gap=-50, ai_sentiment="NEGATIVE",
    )
    assert result2["upside_probability"] >= 0.05


def test_combine_probability_signals_momentum_alone_nudges():
    # only momentum disagrees with an otherwise-neutral read - proves
    # momentum is actually wired into the probability (Phase 1 of the
    # multi-signal upgrade: today's own already-realized move, previously
    # computed only for display, never fed into the probability at all).
    result = os_.combine_probability_signals(
        base_upside_probability=0.5, pcr=None, pcr_threshold=0.15,
        vwap_bias="NEUTRAL", orb_bias="NEUTRAL", gap=None, ai_sentiment="NEUTRAL",
        momentum_bias="BULLISH",
    )
    assert result["upside_probability"] == pytest.approx(0.55)

    result2 = os_.combine_probability_signals(
        base_upside_probability=0.5, pcr=None, pcr_threshold=0.15,
        vwap_bias="NEUTRAL", orb_bias="NEUTRAL", gap=None, ai_sentiment="NEUTRAL",
        momentum_bias="BEARISH",
    )
    assert result2["upside_probability"] == pytest.approx(0.45)


def test_combine_probability_signals_momentum_defaults_to_neutral():
    # omitting momentum_bias entirely must not change existing behavior
    result = os_.combine_probability_signals(
        base_upside_probability=0.5, pcr=None, pcr_threshold=0.15,
        vwap_bias="NEUTRAL", orb_bias="NEUTRAL", gap=None, ai_sentiment="NEUTRAL",
    )
    assert result["upside_probability"] == pytest.approx(0.5)


# ---------------------------------------------------------------- compute_momentum_bias

def test_compute_momentum_bias_below_min_fraction_is_neutral():
    # moved only 10% of the expected move - too little to be meaningful evidence
    result = os_.compute_momentum_bias(points_moved_from_open=15, expected_move=150,
                                        min_fraction=0.25, max_fraction=0.65)
    assert result["momentum_bias"] == "NEUTRAL"
    assert result["used_fraction"] == pytest.approx(0.10)


def test_compute_momentum_bias_within_band_is_directional():
    # moved 40% of the expected move upward - real, still-developing evidence
    result = os_.compute_momentum_bias(points_moved_from_open=60, expected_move=150,
                                        min_fraction=0.25, max_fraction=0.65)
    assert result["momentum_bias"] == "BULLISH"
    assert result["used_fraction"] == pytest.approx(0.40)

    result2 = os_.compute_momentum_bias(points_moved_from_open=-60, expected_move=150,
                                         min_fraction=0.25, max_fraction=0.65)
    assert result2["momentum_bias"] == "BEARISH"


def test_compute_momentum_bias_above_max_fraction_is_excluded():
    # moved 90% of the expected move already - deliberately excluded (near-
    # exhausted move, not extra-strong evidence) to avoid chasing
    result = os_.compute_momentum_bias(points_moved_from_open=135, expected_move=150,
                                        min_fraction=0.25, max_fraction=0.65)
    assert result["momentum_bias"] == "NEUTRAL"
    assert result["used_fraction"] == pytest.approx(0.90)


def test_compute_momentum_bias_handles_missing_data_safely():
    assert os_.compute_momentum_bias(points_moved_from_open=50, expected_move=None) == {
        "momentum_bias": "NEUTRAL", "used_fraction": None,
    }
    assert os_.compute_momentum_bias(points_moved_from_open=50, expected_move=0) == {
        "momentum_bias": "NEUTRAL", "used_fraction": None,
    }
    assert os_.compute_momentum_bias(points_moved_from_open=None, expected_move=150) == {
        "momentum_bias": "NEUTRAL", "used_fraction": None,
    }


# ---------------------------------------------------------------- compute_volatility_confidence (Phase 4)

def test_compute_volatility_confidence_typical_day_is_50():
    # expected_move == avg_range (ratio 1.0), no breakout, no momentum -> 50%
    result = os_.compute_volatility_confidence(
        expected_move=150, avg_range=150, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    assert result == pytest.approx(50.0)


def test_compute_volatility_confidence_double_expected_move_maxes_out():
    # expected_move == 2x avg_range (ratio 2.0) -> 100%, clamped
    result = os_.compute_volatility_confidence(
        expected_move=300, avg_range=150, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    assert result == pytest.approx(100.0)


def test_compute_volatility_confidence_calm_day_is_low():
    # expected_move == half avg_range (ratio 0.5) -> 25%
    result = os_.compute_volatility_confidence(
        expected_move=75, avg_range=150, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    assert result == pytest.approx(25.0)


def test_compute_volatility_confidence_orb_breakout_adds_bonus():
    base = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    with_breakout = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="BULLISH", momentum_used_fraction=None,
    )
    assert with_breakout == pytest.approx(base + 15)


def test_compute_volatility_confidence_momentum_adds_scaled_bonus():
    base = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    half_used = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=0.5,
    )
    fully_used = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=1.0,
    )
    assert half_used == pytest.approx(base + 10)   # 0.5 * momentum_scale(20) = 10
    assert fully_used == pytest.approx(base + 20)  # capped at momentum_scale(20)


def test_compute_volatility_confidence_missing_data_returns_none():
    assert os_.compute_volatility_confidence(None, 150, "NEUTRAL", None) is None
    assert os_.compute_volatility_confidence(150, None, "NEUTRAL", None) is None
    assert os_.compute_volatility_confidence(150, 0, "NEUTRAL", None) is None


def test_compute_volatility_confidence_oi_buildup_adds_bonus_regardless_of_direction():
    # Real option-chain positioning (OI build-up OR unwind at the nearest
    # walls) should raise confidence by its MAGNITUDE, not its direction -
    # this is what the user asked for: VIX alone only tells you the
    # statistical range, not whether real chain activity backs up a big
    # move today.
    base = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    resistance_built_up = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
        resistance_oi_change_ratio=1.5, oi_change_clamp=0.5,  # +50%, clamped at the limit
    )
    support_unwound = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
        support_oi_change_ratio=0.5, oi_change_clamp=0.5,  # -50%, same magnitude, opposite direction
    )
    assert resistance_built_up == pytest.approx(base + 15)  # at clamp limit -> full oi_buildup_scale (15)
    assert support_unwound == pytest.approx(base + 15)      # magnitude-based: unwind counts the same as build-up


def test_compute_volatility_confidence_oi_buildup_averages_both_walls_and_clamps():
    base = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    both_walls_moderate = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
        resistance_oi_change_ratio=1.25, support_oi_change_ratio=1.25, oi_change_clamp=0.5,
    )
    # avg magnitude = 0.25, clamp = 0.5 -> half the max bonus (15/2 = 7.5)
    assert both_walls_moderate == pytest.approx(base + 7.5)

    beyond_clamp = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
        resistance_oi_change_ratio=3.0, oi_change_clamp=0.5,  # +200%, way past the clamp
    )
    assert beyond_clamp == pytest.approx(base + 15)  # clamped, not the raw (unbounded) ratio


def test_compute_volatility_confidence_oi_buildup_unavailable_has_no_effect():
    base = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    with_none_ratios = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
        resistance_oi_change_ratio=None, support_oi_change_ratio=None,
    )
    assert with_none_ratios == pytest.approx(base)


def test_compute_volatility_confidence_cpr_narrow_adds_full_bonus():
    base = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    narrowest = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
        cpr_width_ratio=0.0,
    )
    assert narrowest == pytest.approx(base + 15)  # narrowest possible CPR -> full cpr_narrow_scale (15)


def test_compute_volatility_confidence_cpr_wide_adds_nothing():
    base = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    at_theoretical_max = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
        cpr_width_ratio=1 / 3,
    )
    assert at_theoretical_max == pytest.approx(base)

    beyond_max = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
        cpr_width_ratio=0.9,
    )
    assert beyond_max == pytest.approx(base)  # never goes negative, just contributes nothing


def test_compute_volatility_confidence_cpr_half_narrow_adds_half_bonus():
    base = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    half = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
        cpr_width_ratio=1 / 6,  # halfway to the theoretical max (1/3)
    )
    assert half == pytest.approx(base + 7.5)


def test_compute_volatility_confidence_cpr_unavailable_has_no_effect():
    base = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
    )
    with_none = os_.compute_volatility_confidence(
        expected_move=100, avg_range=200, orb_bias="NEUTRAL", momentum_used_fraction=None,
        cpr_width_ratio=None,
    )
    assert with_none == pytest.approx(base)


# ---------------------------------------------------------------- compute_avg_open_to_close_range (Phase 7)

def test_compute_avg_open_to_close_range_basic_average():
    candles = [
        {"open": 23400, "close": 23450},  # 50
        {"open": 23400, "close": 23350},  # 50 (abs)
        {"open": 23400, "close": 23500},  # 100
    ]
    assert os_.compute_avg_open_to_close_range(candles) == pytest.approx((50 + 50 + 100) / 3)


def test_compute_avg_open_to_close_range_empty_returns_none():
    assert os_.compute_avg_open_to_close_range([]) is None
    assert os_.compute_avg_open_to_close_range(None) is None


# ---------------------------------------------------------------- compute_atm_oi_change_bias (Phase 7)

def test_compute_atm_oi_change_bias_put_buildup_faster_is_bullish():
    result = os_.compute_atm_oi_change_bias(atm_ce_oi_change_ratio=1.0, atm_pe_oi_change_ratio=1.3)
    assert result == "BULLISH"


def test_compute_atm_oi_change_bias_call_buildup_faster_is_bearish():
    result = os_.compute_atm_oi_change_bias(atm_ce_oi_change_ratio=1.3, atm_pe_oi_change_ratio=1.0)
    assert result == "BEARISH"


def test_compute_atm_oi_change_bias_within_threshold_is_neutral():
    result = os_.compute_atm_oi_change_bias(atm_ce_oi_change_ratio=1.05, atm_pe_oi_change_ratio=1.1, threshold=0.1)
    assert result == "NEUTRAL"


def test_compute_atm_oi_change_bias_missing_data_is_neutral():
    assert os_.compute_atm_oi_change_bias(None, 1.2) == "NEUTRAL"
    assert os_.compute_atm_oi_change_bias(1.2, None) == "NEUTRAL"
    assert os_.compute_atm_oi_change_bias(None, None) == "NEUTRAL"


# ---------------------------------------------------------------- combine_probability_signals: atm_oi_bias (Phase 7)

def test_combine_probability_signals_atm_oi_bias_nudges_bullish():
    base = os_.combine_probability_signals(0.5, None, 0.15, "NEUTRAL", "NEUTRAL", None, "NEUTRAL")
    with_atm = os_.combine_probability_signals(0.5, None, 0.15, "NEUTRAL", "NEUTRAL", None, "NEUTRAL",
                                                atm_oi_bias="BULLISH")
    assert with_atm["upside_probability"] == pytest.approx(base["upside_probability"] + 0.05)


def test_combine_probability_signals_atm_oi_bias_nudges_bearish():
    base = os_.combine_probability_signals(0.5, None, 0.15, "NEUTRAL", "NEUTRAL", None, "NEUTRAL")
    with_atm = os_.combine_probability_signals(0.5, None, 0.15, "NEUTRAL", "NEUTRAL", None, "NEUTRAL",
                                                atm_oi_bias="BEARISH")
    assert with_atm["upside_probability"] == pytest.approx(base["upside_probability"] - 0.05)
