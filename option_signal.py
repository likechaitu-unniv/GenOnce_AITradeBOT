"""
Pure-Python technical signal logic. No network calls, no Kite/AI dependency -
every function here takes plain data in and returns plain data out, so it can
be unit tested with fake input (see test_option_signal.py).

engine.py is responsible for fetching the raw numbers (from the live tick
store / Kite historical API) and handing them to these functions.
"""

import math
from statistics import mean


def get_strike_interval(sorted_strikes):
    """
    Derive the exchange's current strike-spacing from a sorted list of
    available strikes for the nearest expiry, instead of hardcoding "50"
    (NSE revises strike intervals from time to time).
    """
    if len(sorted_strikes) < 2:
        raise ValueError("need at least 2 strikes to derive strike interval")
    diffs = [b - a for a, b in zip(sorted_strikes, sorted_strikes[1:]) if b > a]
    if not diffs:
        raise ValueError("could not derive a positive strike interval")
    return min(diffs)


def get_atm_strike(spot_price, strike_interval):
    """Round spot price to the nearest available strike."""
    return int(round(spot_price / strike_interval) * strike_interval)


def get_strikes_around_atm(atm_strike, strike_interval, n=5):
    """ATM +/- n strikes (2n+1 strikes total, default 11: ATM +/- 5)."""
    return [atm_strike + i * strike_interval for i in range(-n, n + 1)]


def compute_oi_bias(strike_oi_data, pcr_threshold, spot_price=None, strike_interval=None, window_strikes=5,
                     strike_oi_change_ratios=None, oi_change_clamp=0.5):
    """
    strike_oi_data: {strike: {"CE": call_oi, "PE": put_oi}, ...}
                     expected to cover ATM +/- 5 strikes (11 strikes).
    pcr_threshold:  settings.json "pcr_threshold", e.g. 0.15

    pcr = total_put_oi / total_call_oi   (the REAL, standard PCR - shown as-is on the
                                           dashboard so it always matches what you'd see
                                           cross-checking the real NSE/Zerodha option chain)
    pcr > 1 + threshold  -> BULLISH  (puts piling up -> put writers expect support -> bullish)
    pcr < 1 - threshold  -> BEARISH
    else                 -> NEUTRAL

    Phase 6 upgrade (consistency with compute_direction_probability's Phase
    2/3 upgrades): the plain, whole-window PCR treats a strike 5 positions
    away exactly the same as one sitting right next to spot - the same "PCR
    alone isn't enough, it has to be combined with WHERE the nearest strike
    actually is" reasoning already applied to the OI-wall base probability
    also applies to the BULLISH/BEARISH/NEUTRAL classification here, or it
    ends up looking artificially "stuck" on NEUTRAL relative to it even
    when the market's immediate vicinity is clearly skewed. When
    spot_price/strike_interval are given, a SEPARATE proximity-weighted PCR
    (`weighted_pcr` in the return value) is computed the same way
    compute_direction_probability does (full weight at spot, zero weight at
    the edge of the tracked ATM+/-window_strikes window) and is what
    actually decides oi_bias - the real, unweighted `pcr` above is left
    untouched for display/verification. Without those extra args (left as
    None), `weighted_pcr` just equals the plain `pcr` and behavior is
    unchanged - existing callers are unaffected.

    Phase 10 upgrade: strike_oi_change_ratios (optional {strike: {"CE": ratio|
    None, "PE": ratio|None}}) folds in the SAME fresh today-vs-yesterday
    OI-change numbers already used for the OI-wall base probability (3b)
    and ATM's own OI-change (Phase 7) - a leg showing heavy fresh build-up
    right now is stronger, more current evidence than its static size alone.
    Only strikes actually present in this dict get reweighted (typically
    just the identified walls + ATM, since that's all the extra API calls
    this project fetches) - every other strike keeps its plain proximity
    weight, clamped the same way as elsewhere (`oi_change_clamp`, default
    +/-50%) so one unusual day's swing can't dominate. Omitting this arg
    leaves behavior exactly as before (proximity-only).
    """
    total_call_oi = sum(v.get("CE", 0) or 0 for v in strike_oi_data.values())
    total_put_oi = sum(v.get("PE", 0) or 0 for v in strike_oi_data.values())

    if total_call_oi <= 0:
        pcr = float("inf") if total_put_oi > 0 else 0.0
    else:
        pcr = total_put_oi / total_call_oi

    def _proximity(strike):
        if spot_price is None or not strike_interval:
            return 1.0
        max_distance = window_strikes * strike_interval
        if max_distance <= 0:
            return 1.0
        return max(0.0, 1 - abs(strike - spot_price) / max_distance)

    def _change_multiplier(strike, opt_type):
        if not strike_oi_change_ratios:
            return 1.0
        ratio = strike_oi_change_ratios.get(strike, {}).get(opt_type)
        if ratio is None:
            return 1.0
        return min(1 + oi_change_clamp, max(1 - oi_change_clamp, ratio))

    weighted_call_oi = sum(
        (v.get("CE", 0) or 0) * _proximity(strike) * _change_multiplier(strike, "CE")
        for strike, v in strike_oi_data.items()
    )
    weighted_put_oi = sum(
        (v.get("PE", 0) or 0) * _proximity(strike) * _change_multiplier(strike, "PE")
        for strike, v in strike_oi_data.items()
    )
    if weighted_call_oi <= 0:
        weighted_pcr = float("inf") if weighted_put_oi > 0 else 0.0
    else:
        weighted_pcr = weighted_put_oi / weighted_call_oi

    if weighted_pcr > 1 + pcr_threshold:
        oi_bias = "BULLISH"
    elif weighted_pcr < 1 - pcr_threshold:
        oi_bias = "BEARISH"
    else:
        oi_bias = "NEUTRAL"

    return {
        "pcr": pcr,
        "weighted_pcr": weighted_pcr,
        "oi_bias": oi_bias,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
    }


def compute_vwap_from_candles(candles):
    """
    Fallback VWAP calculation from minute candles, used only if Kite's
    quote()/tick "average_price" field is unavailable for some reason.

    candles: list of {"high": float, "low": float, "close": float, "volume": int}
             cumulative from today's market open.
    """
    total_vol = 0
    total_tp_vol = 0.0
    for c in candles:
        typical_price = (c["high"] + c["low"] + c["close"]) / 3
        vol = c.get("volume", 0) or 0
        total_tp_vol += typical_price * vol
        total_vol += vol
    if total_vol <= 0:
        raise ValueError("cannot compute VWAP: zero cumulative volume")
    return total_tp_vol / total_vol


def compute_vwap_bias(current_price, vwap, neutral_band_pct=0.0005):
    """
    current_price > vwap -> BULLISH
    current_price < vwap -> BEARISH
    Within +/- neutral_band_pct of vwap -> NEUTRAL (avoids noise-flipping
    the bias when price is basically sitting on VWAP). Default band is
    +/-0.05%; set neutral_band_pct=0 to disable the band entirely.
    """
    if vwap <= 0:
        raise ValueError("vwap must be positive")

    band = vwap * neutral_band_pct
    if current_price > vwap + band:
        vwap_bias = "BULLISH"
    elif current_price < vwap - band:
        vwap_bias = "BEARISH"
    else:
        vwap_bias = "NEUTRAL"

    return {"vwap": vwap, "vwap_bias": vwap_bias}


def compute_orb_bias(current_price, orb_high, orb_low):
    """
    Phase 9: Opening Range Breakout - replaces the classic floor-trader
    Pivot R1/S1 breakout check this project used before. Both ask "has
    today broken out of a reference range", but Pivot's R1/S1 band spans
    close to a FULL prior day's typical range - a rare, late, "this is
    definitely a trend day" confirmation, correctly NEUTRAL on most
    ordinary days by design (not a calibration problem, verified against
    real logged numbers). ORB uses TODAY's own first `orb_minutes`
    (settings.json, default 15) of price action as the reference instead of
    yesterday's - a far narrower, well-established, widely-watched
    intraday level for NIFTY option buyers specifically - so it resolves
    much earlier in the session and triggers on more days.

    Breaking above the opening range high -> BULLISH.
    Breaking below the opening range low -> BEARISH.
    Still inside it -> NEUTRAL (range hasn't resolved a direction yet, or
    the opening range itself hasn't finished forming - see engine.get_orb_bias).
    """
    if current_price > orb_high:
        return "BULLISH"
    if current_price < orb_low:
        return "BEARISH"
    return "NEUTRAL"


def compute_cpr_width(prev_high, prev_low, prev_close):
    """
    Phase 9: Central Pivot Range (CPR) width - a NIFTY-specific "will today
    trend or chop" classifier, computed purely from YESTERDAY's High/Low/
    Close (known before today even opens, unlike ORB which needs today's
    own early price action):

        pivot = (prev_high + prev_low + prev_close) / 3
        bc (bottom central) = (prev_high + prev_low) / 2
        tc (top central)    = 2*pivot - bc
        width = |tc - bc|

    A narrow width means yesterday's close sat close to the middle of its
    own day's range; a wide width means it was skewed toward its high or
    low. Narrow-CPR-often-precedes-a-bigger-move is a well-established
    technical observation (the same "narrow range precedes wide range"
    idea that also shows up as NR7/inside-day patterns), not specific
    folklore invented for this project. Fed into
    compute_volatility_confidence as one more real, computed signal.

    Mathematically, width is always between 0 and (prev_high-prev_low)/3 -
    a real, derivable bound of this exact formula (reached when prev_close
    equals prev_high or prev_low), used as the normalization reference
    where this is consumed - not an arbitrary invented threshold.
    """
    pivot = (prev_high + prev_low + prev_close) / 3
    bc = (prev_high + prev_low) / 2
    tc = 2 * pivot - bc
    return abs(tc - bc)


def compute_momentum_bias(points_moved_from_open, expected_move, min_fraction=0.25, max_fraction=0.65):
    """
    Phase 1 (of the multi-signal upgrade requested): today's ALREADY-REALIZED
    move (points_moved_from_open) is real directional evidence that was
    previously computed but never fed into the probability at all - only
    shown on the dashboard. This turns it into a genuine signal, sized
    relative to how far VIX implied the market could move today
    (expected_move), not a fixed points number (the same realized move means
    something different on a high-VIX day vs a low-VIX day).

    used_fraction = |points_moved_from_open| / expected_move

      - Below min_fraction (default 25%): too little movement yet to be
        meaningful evidence over normal noise -> NEUTRAL.
      - Between min_fraction and max_fraction: real, still-developing
        directional evidence -> BULLISH (moved up) or BEARISH (moved down).
      - Above max_fraction (default 65%): deliberately EXCLUDED, not treated
        as extra-strong evidence - a move that's already used up most of
        today's expected range is more likely near-exhausted than about to
        extend further, so counting it as bullish/bearish here would risk
        buying near the top of a move that's about to stall or reverse
        (chasing), instead of catching it early.

    Returns NEUTRAL (no nudge) if expected_move is None/zero - momentum
    can't be sized without a VIX-implied baseline to compare against.
    """
    if expected_move is None or expected_move <= 0 or points_moved_from_open is None:
        return {"momentum_bias": "NEUTRAL", "used_fraction": None}

    used_fraction = abs(points_moved_from_open) / expected_move
    if min_fraction <= used_fraction <= max_fraction:
        momentum_bias = "BULLISH" if points_moved_from_open > 0 else "BEARISH"
    else:
        momentum_bias = "NEUTRAL"

    return {"momentum_bias": momentum_bias, "used_fraction": used_fraction}


def compute_volatility_confidence(expected_move, avg_range, orb_bias, momentum_used_fraction,
                                   resistance_oi_change_ratio=None, support_oi_change_ratio=None,
                                   oi_change_clamp=0.5, cpr_width_ratio=None,
                                   ratio_scale=50, orb_bonus=15, momentum_scale=20, oi_buildup_scale=15,
                                   cpr_narrow_scale=15):
    """
    Phase 4 (+ later OI/option-chain/ORB/CPR extensions): a SEPARATE 0-100
    "how likely is a genuinely big move today" gauge, independent of the
    Up/Down probability split (which answers a different question - WHICH
    WAY, not HOW MUCH). Combines five already-computed, real signals -
    never a fixed/guessed number on its own:

      1. expected_move / avg_range: today's (VIX+historical-blended, Phase 7)
         expected move relative to the recent typical daily range - the
         core "is today unusual" ratio. Mapped to confidence via
         `ratio_scale` (default 50): a ratio of 1.0 (today = typical) maps
         to 50%, a ratio of 2.0 (today implies double the usual range) maps
         to 100%.
      2. ORB breakout bonus (Phase 9): if today's price has ALREADY broken
         out past today's own opening-range high/low (orb_bias != NEUTRAL),
         that's real, confirmed evidence of an active move, not just an
         implied one - adds a flat `orb_bonus` (default 15 points). Replaces
         the old Pivot-R1/S1-breakout bonus (see compute_orb_bias).
      3. Momentum bonus: how much of today's expected move is ALREADY
         realized (momentum_used_fraction, from compute_momentum_bias) -
         scales up to `momentum_scale` (default 20 points) as more of the
         expected range gets used up, since real, already-happening movement
         is itself confidence-building regardless of the VIX ratio.
      4. OI build-up bonus: VIX is a statistical/implied number - it says
         nothing about whether real option-chain positioning is actually
         backing up a big move TODAY. resistance_oi_change_ratio/
         support_oi_change_ratio (today's OI at the two nearest walls vs
         yesterday's close - the exact same numbers already computed for
         the OI-wall probability, see get_oi_change/compute_direction_
         probability) are reused here by their MAGNITUDE, not their sign:
         heavy fresh OI build-up OR unwind near spot, in EITHER direction,
         means real conviction is actively being added or removed right
         now - that is evidence a big move is more likely today, regardless
         of which way. Averaged across whichever of the two walls have a
         ratio available, clamped the same way as the probability signal
         (`oi_change_clamp`, default +/-50%) so one outlier strike can't
         dominate, then scaled to `oi_buildup_scale` (default 15 points) at
         the clamp limit.
      5. CPR-narrowness bonus (Phase 9): cpr_width_ratio (compute_cpr_width's
         result divided by avg_range, see engine.py) is a PRE-MARKET "will
         today trend or chop" classifier from yesterday's H/L/C alone. A
         narrower CPR (ratio closer to 0) historically precedes a bigger
         move - scaled via `1 - cpr_width_ratio/(1/3)` (1/3 being the real,
         derived mathematical maximum of this ratio when using a day's OWN
         high-low as the denominator - see compute_cpr_width - used here as
         the normalization reference, not an invented cutoff), clamped to
         [0, 1], times `cpr_narrow_scale` (default 15 points). A wide CPR
         contributes nothing (never subtracts).

    All five weights (ratio_scale/orb_bonus/momentum_scale/
    oi_buildup_scale/cpr_narrow_scale) are deliberately exposed as tunable
    settings (see settings.json) rather than baked in - this is a reasoned
    combination of real signals, not a backtested/verified formula, so it
    should be adjustable rather than presented as more precise than it is.

    Returns None if expected_move/avg_range aren't both available - a
    confidence score can't be sized without that baseline ratio.
    """
    if expected_move is None or not avg_range or avg_range <= 0:
        return None

    ratio = expected_move / avg_range
    confidence = ratio * ratio_scale

    if orb_bias not in (None, "NEUTRAL"):
        confidence += orb_bonus

    if momentum_used_fraction is not None:
        confidence += min(momentum_scale, max(0.0, momentum_used_fraction) * momentum_scale)

    oi_magnitudes = [
        min(oi_change_clamp, abs(r - 1))
        for r in (resistance_oi_change_ratio, support_oi_change_ratio)
        if r is not None
    ]
    if oi_magnitudes and oi_change_clamp > 0:
        avg_oi_magnitude = sum(oi_magnitudes) / len(oi_magnitudes)
        confidence += (avg_oi_magnitude / oi_change_clamp) * oi_buildup_scale

    if cpr_width_ratio is not None:
        narrowness = max(0.0, 1 - (cpr_width_ratio / (1 / 3)))
        confidence += narrowness * cpr_narrow_scale

    return max(0.0, min(100.0, confidence))


def combine_bias(*biases):
    """
    Majority vote across 2 or more bias inputs (each "BULLISH"/"BEARISH"/
    "NEUTRAL"), e.g. combine_bias(oi_bias, vwap_bias, orb_bias):

        >=2 biases say BULLISH, and BULLISH count > BEARISH count -> BULLISH
        >=2 biases say BEARISH, and BEARISH count > BULLISH count -> BEARISH
        otherwise (no side has a clear majority)                 -> NEUTRAL

    With exactly 2 inputs this reduces to "both must agree" (the original
    2-signal rule). With 3 inputs it becomes "any 2 of 3 agree", which is
    intentionally less conservative - options bought outright lose value to
    time decay every day, so requiring unanimous agreement between only two
    signals was flagging NEUTRAL far too often to ever act on. A third,
    independent signal (opening-range breakout) means 2-of-3 agreement is still
    evidence-based, not just a loosened threshold on the same two inputs.
    """
    bullish = biases.count("BULLISH")
    bearish = biases.count("BEARISH")
    if bullish >= 2 and bullish > bearish:
        return "BULLISH"
    if bearish >= 2 and bearish > bullish:
        return "BEARISH"
    return "NEUTRAL"


def compute_gap_flag(daily_ranges, today_open, yesterday_close, gap_threshold_factor):
    """
    daily_ranges: list of (high - low) for the last `range_lookback_days`
                  COMPLETED trading days (today excluded).
    today_open, yesterday_close: from Kite quote()'s ohlc block.
    gap_threshold_factor: settings.json "gap_threshold_factor"

    NOTE: a large gap is flagged only as "extra caution" - whether it
    continues or reverses is NOT predictable from this alone, so it is used
    to trade BOTH legs (avoid picking a confident side), never to flip the
    direction that technical/AI bias already suggests.
    """
    if not daily_ranges:
        raise ValueError("daily_ranges must not be empty")

    avg_range = mean(daily_ranges)
    gap = today_open - yesterday_close
    large_gap = abs(gap) > avg_range * gap_threshold_factor

    return {"avg_range": avg_range, "gap": gap, "large_gap": large_gap}


def compute_vix_expected_move(spot_price, india_vix, trading_days_per_year=252):
    """
    Standard options-industry formula for the 1-day expected move implied by
    an annualized volatility number (India VIX):

        expected_move = spot_price * (india_vix / 100) * sqrt(1 / trading_days_per_year)

    This is the same sqrt(time) scaling used industry-wide to de-annualize
    an IV/VIX figure to a shorter horizon; 252 trading days/year is the
    standard annualization convention used across the industry for this
    calculation (not a custom guess). The result is the 1 standard
    deviation (~68% confidence) expected move in NIFTY points, either side
    of the current price, for the rest of today.
    """
    if spot_price <= 0 or india_vix < 0:
        raise ValueError("spot_price must be positive and india_vix must be non-negative")
    return spot_price * (india_vix / 100) * math.sqrt(1 / trading_days_per_year)


def compute_avg_open_to_close_range(daily_candles):
    """
    Phase 7: average |close - open| across the given recent COMPLETED daily
    candles - a DIFFERENT, narrower measure than compute_gap_flag's
    avg_range (which is high-low, including intraday wicks): this asks how
    far each day actually travelled from its open to its close by the end
    of the session - the same thing VIX's expected_move is meant to predict
    (a move by end of day), so it's a fair, real, non-invented number to
    compare it against.

    Returns None if daily_candles is empty (no history available).
    """
    if not daily_candles:
        return None
    diffs = [abs(c["close"] - c["open"]) for c in daily_candles]
    return sum(diffs) / len(diffs)


def compute_oi_walls(strike_oi_data, atm_strike):
    """
    Find the nearest strong resistance and support from the OPTION SELLERS'
    point of view, using OTM open interest:

      - For strikes ABOVE the current ATM (OTM calls from here), the strike
        with the highest Call OI is where call writers are most concentrated
        betting price won't go past -> resistance ceiling.
      - For strikes BELOW the current ATM (OTM puts from here), the strike
        with the highest Put OI is where put writers are most concentrated
        betting price won't fall past -> support floor.

    strike_oi_data: {strike: {"CE": call_oi, "PE": put_oi}, ...} - the same
                    dict option_signal.compute_oi_bias() already consumes.
    """
    strikes_above = {s: v for s, v in strike_oi_data.items() if s > atm_strike}
    strikes_below = {s: v for s, v in strike_oi_data.items() if s < atm_strike}

    resistance_strike, resistance_oi = None, 0
    for strike, oi in strikes_above.items():
        call_oi = oi.get("CE", 0) or 0
        if call_oi > resistance_oi:
            resistance_strike, resistance_oi = strike, call_oi

    support_strike, support_oi = None, 0
    for strike, oi in strikes_below.items():
        put_oi = oi.get("PE", 0) or 0
        if put_oi > support_oi:
            support_strike, support_oi = strike, put_oi

    return {
        "resistance_strike": resistance_strike,
        "resistance_oi": resistance_oi,
        "support_strike": support_strike,
        "support_oi": support_oi,
    }


def compute_direction_probability(support_oi, resistance_oi, support_strike=None, resistance_strike=None,
                                   spot_price=None, strike_interval=None, window_strikes=5,
                                   support_oi_change_ratio=None, resistance_oi_change_ratio=None,
                                   oi_change_clamp=0.5):
    """
    Probability split derived from the OI-wall imbalance above: heavier
    put-writing (support) OI relative to call-writing (resistance) OI means
    sellers are collectively leaning toward the market NOT falling - which
    tilts the odds toward the upside, and vice versa. This is the same
    put/call OI-imbalance idea as PCR, applied specifically to the nearest
    OTM walls instead of the whole ATM+/-5 range.

    Phase 2 upgrade: a wall's raw OI size alone doesn't say how RELEVANT it
    is right now - a heavy wall sitting right next to the current price is
    about to be tested and matters far more than an equally heavy wall
    sitting at the far edge of the tracked ATM+/-window_strikes range, which
    price would have to travel much further to even reach. When
    support_strike/resistance_strike/spot_price/strike_interval are ALL
    given, each wall's OI is weighted by its distance from spot, decaying
    linearly from full weight (wall exactly at spot) to zero weight (wall at
    the edge of the tracked window, window_strikes*strike_interval points
    away):

        proximity = max(0, 1 - |strike - spot_price| / (window_strikes * strike_interval))
        weighted_oi = raw_oi * proximity

    Phase 3 upgrade: a wall's OI size (even proximity-weighted) doesn't say
    whether that conviction is FRESH (being actively added to right now -
    real, current evidence) or STALE/UNWINDING (sellers are actually
    covering, i.e. the wall is getting weaker even if its absolute OI is
    still large). support_oi_change_ratio/resistance_oi_change_ratio -
    today's OI at that wall divided by YESTERDAY's closing OI for that same
    contract (engine.get_oi_change) - scale the weighted OI further:
    ratio > 1 (fresh build-up) strengthens that wall, ratio < 1 (unwinding)
    weakens it. Clamped to [1-oi_change_clamp, 1+oi_change_clamp] (default
    0.5-1.5x) so one unusually large single-day swing can't dominate the
    whole picture on its own.

        upside_probability = weighted_support / (weighted_support + weighted_resistance)

    Without the Phase 2/3 extra args (any left as None), falls back to the
    original pure OI-magnitude comparison - existing callers are unaffected:

        upside_probability = support_oi / (support_oi + resistance_oi)

    Falls back to a neutral 50/50 split if both (weighted) walls are
    zero/unknown - e.g. both walls being right at the edge of the tracked
    window, or the position isn't known.
    """
    def _weighted(strike, oi, oi_change_ratio):
        oi = oi or 0
        if strike is None or spot_price is None or not strike_interval:
            weighted = oi  # backward-compatible: no proximity data given, use raw OI as-is
        else:
            max_distance = window_strikes * strike_interval
            if max_distance <= 0:
                weighted = oi
            else:
                distance = abs(strike - spot_price)
                proximity = max(0.0, 1 - distance / max_distance)
                weighted = oi * proximity
        if oi_change_ratio is not None:
            multiplier = min(1 + oi_change_clamp, max(1 - oi_change_clamp, oi_change_ratio))
            weighted *= multiplier
        return weighted

    weighted_support = _weighted(support_strike, support_oi, support_oi_change_ratio)
    weighted_resistance = _weighted(resistance_strike, resistance_oi, resistance_oi_change_ratio)

    total = weighted_support + weighted_resistance
    if total <= 0:
        return {"upside_probability": 0.5, "downside_probability": 0.5}
    upside = weighted_support / total
    return {"upside_probability": upside, "downside_probability": 1 - upside}


def compute_atm_oi_change_bias(atm_ce_oi_change_ratio, atm_pe_oi_change_ratio, threshold=0.1):
    """
    Phase 7: fresh OI build-up/unwind (today vs yesterday's close) compared
    CALL vs PUT specifically AT the ATM strike - a distinct signal from the
    resistance/support walls above, which are by construction always OTM
    strikes away from ATM (see compute_oi_walls). This answers "what's
    happening right at the current price today", not just further out.

    Same interpretation convention already used across this codebase for
    OI (see compute_oi_walls/compute_oi_bias): relatively faster PUT OI
    build-up right at the money means more fresh conviction is backing the
    floor right under the current price -> BULLISH; relatively faster CALL
    OI build-up means more fresh conviction is capping it from directly
    above -> BEARISH.

    Returns "BULLISH"/"BEARISH"/"NEUTRAL". NEUTRAL when either ratio is
    unavailable, or the two are within `threshold` of each other (avoids
    reacting to noise-level differences).
    """
    if atm_ce_oi_change_ratio is None or atm_pe_oi_change_ratio is None:
        return "NEUTRAL"
    diff = atm_pe_oi_change_ratio - atm_ce_oi_change_ratio
    if diff > threshold:
        return "BULLISH"
    if diff < -threshold:
        return "BEARISH"
    return "NEUTRAL"


def combine_probability_signals(base_upside_probability, pcr, pcr_threshold, vwap_bias, orb_bias, gap,
                                 ai_sentiment, momentum_bias="NEUTRAL", atm_oi_bias="NEUTRAL",
                                 adjustment_per_signal=0.05):
    """
    Blends ALL of the requested inputs into one final upside/downside
    probability - not just the OI-wall ratio alone:

      - base_upside_probability: the OI-wall ratio from compute_direction_probability()
        (the option chain's most direct positioning data - used as the anchor).
      - pcr: the broader ATM+/-5 put/call OI ratio (option chain, distinct from
        the OI walls above) - >1+pcr_threshold nudges bullish, <1-pcr_threshold
        nudges bearish, otherwise no nudge (same threshold as compute_oi_bias).
      - vwap_bias: "BULLISH"/"BEARISH"/"NEUTRAL" - nudges accordingly.
      - orb_bias: "BULLISH"/"BEARISH"/"NEUTRAL" (from compute_orb_bias, Phase 9 -
        today's price vs today's own opening-range high/low) - nudges
        accordingly. This is the same signal used in combine_bias(), now
        also folded into the probability so it isn't a separate,
        disconnected number. Replaces the classic Pivot R1/S1 breakout
        this project used before (see compute_orb_bias's docstring for why).
      - gap: today's open-vs-yesterday's-close in points (from compute_gap_flag) -
        a gap up nudges bullish, a gap down nudges bearish, ~0 is neutral.
      - ai_sentiment: "POSITIVE"/"NEGATIVE"/"NEUTRAL" (news/global-cues read) -
        nudges accordingly.
      - momentum_bias: "BULLISH"/"BEARISH"/"NEUTRAL" (from compute_momentum_bias -
        today's own already-realized move, sized against VIX's expected move)
        - nudges accordingly. Phase 1 of the multi-signal upgrade: the market's
        own price action today is itself evidence, previously computed
        (points_moved_from_open) but never actually used here.
      - atm_oi_bias: "BULLISH"/"BEARISH"/"NEUTRAL" (from compute_atm_oi_change_bias -
        fresh OI build-up/unwind CALL vs PUT specifically at the ATM strike,
        today vs yesterday) - nudges accordingly. Phase 7: distinct from pcr
        above (which is the broader ATM+/-5 window's static ratio) - this is
        what's freshly changing right at the money today.

    Each of the 7 nudging signals shifts the base probability by
    +/-adjustment_per_signal (default 5 percentage points) if it agrees or
    disagrees with the upside direction; a signal that's neutral/unavailable
    contributes no nudge. The result is clamped to [5%, 95%] - this system
    never claims false certainty in either direction.

    India VIX is deliberately NOT a direct input here: it's a volatility/
    magnitude measure, not a directional one on its own, without a
    historical baseline to compare against - its role is sizing the expected
    MOVE (see compute_vix_expected_move) and, via momentum_bias, sizing how
    meaningful today's realized move is - not nudging the probability by
    itself.
    """
    net = 0
    if pcr is not None:
        if pcr > 1 + pcr_threshold:
            net += 1
        elif pcr < 1 - pcr_threshold:
            net -= 1
    if vwap_bias == "BULLISH":
        net += 1
    elif vwap_bias == "BEARISH":
        net -= 1
    if orb_bias == "BULLISH":
        net += 1
    elif orb_bias == "BEARISH":
        net -= 1
    if gap is not None:
        if gap > 0:
            net += 1
        elif gap < 0:
            net -= 1
    if atm_oi_bias == "BULLISH":
        net += 1
    elif atm_oi_bias == "BEARISH":
        net -= 1
    if ai_sentiment == "POSITIVE":
        net += 1
    elif ai_sentiment == "NEGATIVE":
        net -= 1
    if momentum_bias == "BULLISH":
        net += 1
    elif momentum_bias == "BEARISH":
        net -= 1

    upside = base_upside_probability + net * adjustment_per_signal
    upside = max(0.05, min(0.95, upside))
    return {"upside_probability": upside, "downside_probability": 1 - upside}
