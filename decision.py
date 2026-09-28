"""
Final BUY-direction decision.

Pure, standalone, deterministic. No I/O, no Kite, no AI calls here - only the
truth table below. Kept in its own file on purpose so it can be tuned/edited
independently of everything else.
"""

VALID_AI_SENTIMENT = ("POSITIVE", "NEGATIVE", "NEUTRAL")
VALID_DECISIONS = ("CALL", "PUT", "BOTH", "NO_TRADE")


def decide_direction(upside_probability: float, ai_sentiment: str, large_gap: bool,
                      range_too_tight: bool = False,
                      expected_move: float = None,
                      required_move_call: float = None,
                      required_move_put: float = None,
                      required_move_both: float = None,
                      call_threshold: float = 0.55, put_threshold: float = 0.45) -> str:
    """
    upside_probability: the FINAL combined probability from
                         option_signal.combine_probability_signals() - the one
                         number that already blends option chain (PCR + OI
                         walls), VWAP, pivot, gap, and AI sentiment together.
                         This is the actual decision driver: the same number
                         shown on the dashboard as "Probability of Move" is
                         the number this function acts on - there is no
                         separate, disconnected bias vote behind the scenes.
    ai_sentiment:        "POSITIVE" | "NEGATIVE" | "NEUTRAL". Used here only
                         as a conflict check: a strong AI view opposing the
                         probability-implied direction downgrades a
                         single-sided CALL/PUT to BOTH, rather than betting
                         one side while a genuinely contrary signal exists.
    large_gap:           bool (from option_signal.compute_gap_flag) - whether
                         a large gap continues or reverses isn't predictable
                         from this alone, so it always trades BOTH legs
                         regardless of what the probability says.
    range_too_tight:     bool (from comparing option_signal.compute_vix_expected_move
                         against settings["min_expected_move_points"]) - True
                         when India VIX implies today's likely move is too
                         small to be worth paying for an option's time decay,
                         regardless of direction or probability. Checked
                         FIRST, so it overrides everything below too.
    expected_move:       India-VIX-implied 1-day expected move in points
                         (option_signal.compute_vix_expected_move) - the
                         market's own estimate of how far NIFTY may move
                         today, either direction. This is compared against
                         the required_move_* figures below to judge whether
                         a trade is actually likely to be PROFITABLE, not
                         just directionally correct.
    required_move_call/required_move_put/required_move_both:
                         the real points NIFTY needs to move (in the
                         relevant direction) for that specific trade to
                         break even TODAY plus a safety margin - computed by
                         the caller (engine.py) from today's REAL, LIVE ATM
                         option premiums (CALL's own breakeven = its own
                         premium, PUT's own breakeven = its own premium,
                         BOTH/straddle breakeven = the two premiums
                         combined). This is never a fixed or guessed
                         constant: it changes every single day with the
                         option's own price, which itself already reflects
                         that day's implied volatility and time decay - a
                         cheap option needs less move to profit, an
                         expensive one needs more, regardless of how
                         "volatile" the day looks by any other measure.
                         None (premium data unavailable) means "cannot
                         verify profitability", and the corresponding
                         trade is skipped rather than assumed safe.
    call_threshold/put_threshold: the probability band outside which there is
                         judged to be a real, tradeable directional edge.
                         Inside the band (default 45%-55%) is treated as too
                         close to a coin flip to bet a single side on - see
                         settings.json "call_probability_threshold" /
                         "put_probability_threshold" to tune.

    Returns one of: "CALL", "PUT", "BOTH", "NO_TRADE"
    """
    if ai_sentiment not in VALID_AI_SENTIMENT:
        raise ValueError(f"invalid ai_sentiment: {ai_sentiment!r}")
    if not 0.0 <= upside_probability <= 1.0:
        raise ValueError(f"upside_probability must be within [0.0, 1.0]: {upside_probability!r}")
    if not 0.0 <= put_threshold < call_threshold <= 1.0:
        raise ValueError(f"invalid thresholds: put_threshold={put_threshold!r} must be < "
                          f"call_threshold={call_threshold!r}, both within [0.0, 1.0]")

    if range_too_tight:
        return "NO_TRADE"  # VIX implies too small a move today to be worth the time-decay cost

    if large_gap:
        return "BOTH"  # unconfirmed / volatile day -> straddle both legs, unconditionally

    call_can_profit = expected_move is not None and required_move_call is not None and expected_move >= required_move_call
    put_can_profit = expected_move is not None and required_move_put is not None and expected_move >= required_move_put
    both_can_profit = expected_move is not None and required_move_both is not None and expected_move >= required_move_both

    if upside_probability >= call_threshold:
        if ai_sentiment == "NEGATIVE":
            # real edge up, but AI actively disagrees -> hedge instead of
            # picking a side, but only if the straddle's own (larger) combined
            # breakeven is actually achievable too.
            return "BOTH" if both_can_profit else "NO_TRADE"
        return "CALL" if call_can_profit else "NO_TRADE"

    if upside_probability <= put_threshold:
        if ai_sentiment == "POSITIVE":
            return "BOTH" if both_can_profit else "NO_TRADE"
        return "PUT" if put_can_profit else "NO_TRADE"

    # Direction unclear (inside the coin-flip band) - still worth a straddle
    # if the market's own expected move is big enough to clear BOTH
    # premiums combined, i.e. profitable regardless of which way it goes.
    if both_can_profit:
        return "BOTH"

    return "NO_TRADE"  # too close to a coin flip AND not expected to move enough to profit either way
