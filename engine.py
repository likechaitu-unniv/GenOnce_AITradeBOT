"""
Ties kite_client + option_signal + ai_sentiment + decision together into one
"Execute" run.

Safety rules enforced here (see README for the full list):
  - AI/analysis is called exactly once, at the start, before the monitoring
    loop. The monitoring loop NEVER calls AI or does network "analysis" -
    only plain arithmetic on live tick data already sitting in memory.
  - force_exit_time is checked on every single loop iteration, unconditionally,
    regardless of what the user has enabled/disabled.
  - stop_event is checked on every loop iteration; when set, open positions
    are squared off immediately.
  - Quantity is always settings["lots"] x the LIVE lot_size from Kite's own
    instrument dump - never a hardcoded number, never anything from AI output.
"""

import datetime
import json
import threading
import time

import option_signal
import ai_sentiment
import decision
from kite_client import KiteClient

# OpenAlgo is an optional alternative execution/data layer.
# Imported lazily inside make_client() so kiteconnect is not required when
# running in openalgo mode, and openalgo is not required in kite mode.


def make_client(settings, log=print):
    """
    Factory: return an OpenAlgoClient when settings["execution_provider"]
    is "openalgo", otherwise return a KiteClient (the default).

    Both clients expose the same interface so the rest of engine.py is
    completely unchanged regardless of which one is returned.
    """
    provider = settings.get("execution_provider", "kite").lower()
    if provider == "openalgo":
        from openalgo_client import OpenAlgoClient  # noqa: PLC0415
        log("[engine] execution_provider=openalgo — using OpenAlgoClient")
        return OpenAlgoClient(settings, log=log)
    log("[engine] execution_provider=kite — using KiteClient")
    return KiteClient(settings, log=log)

INDEX_SPOT_TRADINGSYMBOL = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MID SELECT",
}

# OpenAlgo uses shorter index symbol names (no space/suffix).
INDEX_SPOT_TRADINGSYMBOL_OA = {
    "NIFTY":      "NIFTY",
    "BANKNIFTY":  "NIFTY BANK",
    "FINNIFTY":   "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MID SELECT",
}

INDIA_VIX_TRADINGSYMBOL = "INDIAVIX"  # NSE's volatility index - one for the whole market,
                                        # not per underlying-index, so this name is fixed.

MARKET_OPEN_TIME = datetime.time(9, 15)  # NSE equity/F&O session open (IST) - used to bound
                                          # today's opening-range candles (see get_orb_bias).


def load_settings(path="settings.json"):
    with open(path, "r") as f:
        return json.load(f)


def _emit(on_event, payload):
    """Fire-and-forget structured event for a UI (e.g. the Phase 2 web
    dashboard). Never allowed to raise into the trading logic - a broken or
    slow UI hook must never affect order placement or the safety loop."""
    if on_event is None:
        return
    try:
        on_event(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"[engine] on_event callback raised {exc!r} (ignored)")


def _resolve_spot_tradingsymbol(index, is_openalgo=False):
    if is_openalgo:
        return INDEX_SPOT_TRADINGSYMBOL_OA.get(index, index)
    return INDEX_SPOT_TRADINGSYMBOL.get(index, f"{index} 50")


def _parse_hhmm(hhmm_str):
    hour, minute = hhmm_str.split(":")
    return datetime.time(int(hour), int(minute))


def _wait_for_ticks(kite_client, tokens, timeout=5, poll_interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ticks = kite_client.get_all_ticks()
        if all(t in ticks for t in tokens):
            return True
        time.sleep(poll_interval)
    return False


def _setup_universe_and_ticker(kite_client, settings, log, on_event, on_tick):
    """Shared by run() and manual_run(): build the option universe and get
    the live tick feed connected. Both entry paths need this identically -
    the only thing that differs between them is what happens next (a full
    analysis pass, or going straight to entering the chosen direction)."""
    universe = build_option_universe(kite_client, settings["index"], log)
    _emit(on_event, {
        "type": "universe_ready",
        "spot_symbol": universe["spot_symbol"],
        "spot_token": universe["spot_token"],
        "atm_strike": universe["atm_strike"],
        "nearest_expiry": str(universe["nearest_expiry"]),
        "strike_interval": universe["strike_interval"],
    })

    all_tokens = [universe["spot_token"]]
    if universe.get("futures_token") is not None:
        all_tokens.append(universe["futures_token"])
    for s in universe["target_strikes"]:
        all_tokens.append(universe["instruments_by_strike"][s]["CE"]["instrument_token"])
        all_tokens.append(universe["instruments_by_strike"][s]["PE"]["instrument_token"])

    kite_client.start_ticker(all_tokens, on_tick=on_tick)
    _emit(on_event, {"type": "progress", "stage": "ticker", "message": "Connecting live market data feed..."})
    if not kite_client.wait_for_ticker(timeout=10):
        log("[engine] WARNING: ticker did not confirm connection within 10s, continuing with REST fallbacks")

    return universe


def _safety_net_exit(kite_client, positions, log, on_event):
    """Shared cleanup for run() and manual_run(): square off anything still
    open on any unexpected exit path, then always stop the ticker."""
    if positions:
        log("[engine] unexpected exit path - squaring off any remaining open positions as a safety measure")
        snapshot = _positions_snapshot(positions)
        square_off_all(kite_client, positions, log, reason="SAFETY_NET_EXIT")
        _emit(on_event, {"type": "exit", "reason": "SAFETY_NET_EXIT", "positions": snapshot})
    kite_client.stop_ticker()


def build_option_universe(kite_client, index, log):
    """
    One-time (per run) setup: figure out nearest expiry, ATM strike, and the
    11 strikes (ATM +/- 5) we need to track. Returns everything engine needs
    to subscribe the ticker and place orders.
    """
    is_openalgo = hasattr(kite_client, '_oa')
    spot_symbol = _resolve_spot_tradingsymbol(index, is_openalgo=is_openalgo)
    spot_exch = "NSE_INDEX" if is_openalgo else "NSE"
    spot_instrument = kite_client.get_index_instrument(spot_symbol, exchange=spot_exch)
    spot_token = spot_instrument["instrument_token"]

    initial_quote = kite_client.get_quote([f"{spot_exch}:{spot_symbol}"])[f"{spot_exch}:{spot_symbol}"]
    spot_price = initial_quote["last_price"]
    log(f"[engine] {spot_symbol} spot price = {spot_price}")

    nearest_expiry, chain = kite_client.get_nfo_option_chain(index)
    log(f"[engine] nearest expiry = {nearest_expiry}, {len(chain)} CE/PE instruments")

    sorted_strikes = sorted({i["strike"] for i in chain})
    strike_interval = option_signal.get_strike_interval(sorted_strikes)
    atm_strike = option_signal.get_atm_strike(spot_price, strike_interval)
    target_strikes = option_signal.get_strikes_around_atm(atm_strike, strike_interval, n=5)
    log(f"[engine] strike_interval={strike_interval}, atm_strike={atm_strike}, tracking strikes={target_strikes}")

    instruments_by_strike = {s: {} for s in target_strikes}
    for i in chain:
        if i["strike"] in instruments_by_strike:
            instruments_by_strike[i["strike"]][i["instrument_type"]] = i

    for s in target_strikes:
        for opt_type in ("CE", "PE"):
            if opt_type not in instruments_by_strike[s]:
                raise ValueError(f"missing {opt_type} instrument for strike {s} in option chain")

    # Phase 8: the nearest-expiry NIFTY FUTURES contract, used as a real,
    # traded-volume proxy for VWAP (see engine.get_vwap) - the index itself
    # never carries average_price/volume in its ticks (confirmed via Kite's
    # docs: indices aren't traded instruments). A lookup failure here is
    # non-fatal - VWAP just becomes unavailable (NEUTRAL fallback), same as
    # any other optional signal.
    try:
        futures_instrument = kite_client.get_nfo_futures_instrument(index)
        futures_token = futures_instrument["instrument_token"]
        futures_tradingsymbol = futures_instrument["tradingsymbol"]
        log(f"[engine] VWAP proxy: nearest NIFTY futures = {futures_tradingsymbol} "
            f"(expiry {futures_instrument['expiry']})")
    except Exception as exc:  # noqa: BLE001
        log(f"[engine] futures lookup for VWAP failed ({exc!r}), VWAP will be unavailable")
        futures_token = None
        futures_tradingsymbol = None

    return {
        "spot_symbol": spot_symbol,
        "spot_instrument": spot_instrument,
        "spot_token": spot_token,
        "initial_quote": initial_quote,
        "nearest_expiry": nearest_expiry,
        "strike_interval": strike_interval,
        "atm_strike": atm_strike,
        "target_strikes": target_strikes,
        "instruments_by_strike": instruments_by_strike,
        "futures_token": futures_token,
        "futures_tradingsymbol": futures_tradingsymbol,
    }


def gather_strike_oi(kite_client, universe, log):
    """
    Returns (strike_oi_data, strike_volume_data) — both {strike: {"CE": x, "PE": x}}.

    For KiteConnect: reads OI from FULL-mode WebSocket ticks (carries oi field).
    For OpenAlgo: OpenAlgo's WebSocket quote feed does NOT carry OI (always 0).
    It uses a single batch REST multiquotes() call instead — one request for all
    22 option instruments, which is faster and more reliable than 22 individual
    quote() calls and still costs only one API call.
    """
    instruments_by_strike = universe["instruments_by_strike"]
    target_strikes = universe["target_strikes"]

    tokens = []
    token_meta = {}
    for strike in target_strikes:
        for opt_type in ("CE", "PE"):
            instr = instruments_by_strike[strike][opt_type]
            tokens.append(instr["instrument_token"])
            token_meta[instr["instrument_token"]] = (strike, opt_type, instr["tradingsymbol"])

    strike_oi_data = {s: {"CE": 0, "PE": 0} for s in target_strikes}
    strike_volume_data = {s: {"CE": 0, "PE": 0} for s in target_strikes}

    # Detect OpenAlgo: it stores ticks by tradingsymbol string (no numeric token).
    # OpenAlgo's WS quote feed never populates oi — use REST multiquotes() instead.
    is_openalgo = hasattr(kite_client, '_oa')

    if is_openalgo:
        # Single batch REST call for all 22 option instruments.
        symbols = [f"NFO:{token_meta[t][2]}" for t in tokens]
        try:
            # OpenAlgo multiquotes: list of {symbol, exchange}
            oa_symbols = [{"symbol": token_meta[t][2], "exchange": "NFO"} for t in tokens]
            resp = kite_client._oa.multiquotes(symbols=oa_symbols)
            if resp.get("status") == "success":
                for r in resp.get("results", []):
                    sym = r.get("symbol", "")
                    d   = r.get("data", {})
                    # Find which token this symbol belongs to
                    for token in tokens:
                        if token_meta[token][2] == sym:
                            strike, opt_type, _ = token_meta[token]
                            strike_oi_data[strike][opt_type]   = int(d.get("oi", 0) or 0)
                            strike_volume_data[strike][opt_type] = int(d.get("volume", 0) or 0)
                            break
                log(f"[engine] OI fetched via REST multiquotes for {len(tokens)} options "
                    f"(OpenAlgo WS does not carry OI)")
            else:
                log(f"[engine] multiquotes() failed ({resp}), falling back to individual quote() calls")
                raise RuntimeError("multiquotes failed")
        except Exception as exc:  # noqa: BLE001
            log(f"[engine] OpenAlgo batch OI fetch failed ({exc!r}), trying individual quote() fallback")
            fallback_quotes = kite_client.get_quote(symbols)
            for token in tokens:
                strike, opt_type, tradingsymbol = token_meta[token]
                q = fallback_quotes.get(f"NFO:{tradingsymbol}", {})
                strike_oi_data[strike][opt_type]   = int(q.get("oi", 0) or 0)
                strike_volume_data[strike][opt_type] = int(q.get("volume", 0) or 0)
    else:
        # KiteConnect path: read OI from FULL-mode WebSocket ticks.
        _wait_for_ticks(kite_client, tokens, timeout=5)
        ticks = kite_client.get_all_ticks()

        missing_symbols = [
            f"NFO:{token_meta[t][2]}" for t in tokens
            if t not in ticks or ticks[t].get("oi") is None
        ]
        fallback_quotes = {}
        if missing_symbols:
            log(f"[engine] {len(missing_symbols)} option(s) missing OI from ticker, "
                f"one-time REST quote() fallback")
            fallback_quotes = kite_client.get_quote(missing_symbols)

        for token in tokens:
            strike, opt_type, tradingsymbol = token_meta[token]
            tick = ticks.get(token)
            if tick and tick.get("oi") is not None:
                strike_oi_data[strike][opt_type]   = tick["oi"]
                strike_volume_data[strike][opt_type] = tick.get("volume_traded", 0) or 0
            else:
                q = fallback_quotes.get(f"NFO:{tradingsymbol}")
                strike_oi_data[strike][opt_type]   = (q or {}).get("oi", 0) or 0
                strike_volume_data[strike][opt_type] = (q or {}).get("volume", 0) or 0

    return strike_oi_data, strike_volume_data


def get_oi_change(kite_client, universe, oi_walls, log, atm_strike=None, atm_oi=None):
    """
    Phase 3: yesterday's closing OI (Kite historical_data with oi=True,
    confirmed supported by the installed SDK) for the two identified OI
    walls, compared against their CURRENT OI, to tell fresh build-up (more
    conviction actively being added right now) apart from stale/unwinding OI
    (sellers covering - the wall is getting weaker even if its absolute OI
    number still looks large). Only queries the 2 specific instruments that
    were actually identified as the nearest walls - not all ATM+/-5 - since
    that's all this needs and keeps the extra API calls to a minimum.

    Phase 7: when atm_strike/atm_oi are given, ALSO does the same
    today-vs-yesterday OI-change check for the ATM strike's own CALL and
    PUT (separately from the resistance/support walls, which by
    construction are always OTM strikes away from ATM - see
    compute_oi_walls). This answers "what's happening RIGHT AT the current
    price today", not just at the nearest walls further out.

    Returns {"resistance_oi_change_ratio", "support_oi_change_ratio",
    "atm_ce_oi_change_ratio", "atm_pe_oi_change_ratio"} (all float|None) -
    ratio = today's OI / yesterday's closing OI (1.0 = unchanged, >1 = built
    up, <1 = unwound). None when unavailable (new contract, no prior
    session, or a lookup failure) - a missing ratio has NO effect on
    anything downstream (see option_signal.compute_direction_probability /
    compute_atm_oi_change_bias).
    """
    result = {
        "resistance_oi_change_ratio": None, "support_oi_change_ratio": None,
        "atm_ce_oi_change_ratio": None, "atm_pe_oi_change_ratio": None,
    }
    today = datetime.date.today()
    from_date = today - datetime.timedelta(days=7)  # buffer for weekends/holidays
    to_date = today - datetime.timedelta(days=1)

    if oi_walls["resistance_strike"] is not None:
        try:
            instr = universe["instruments_by_strike"][oi_walls["resistance_strike"]]["CE"]
            candles = kite_client.get_daily_history(instr["instrument_token"], from_date, to_date, oi=True)
            prev_oi = candles[-1].get("oi") if candles else None
            if prev_oi:
                result["resistance_oi_change_ratio"] = oi_walls["resistance_oi"] / prev_oi
                log(f"[engine] resistance OI change: {instr['tradingsymbol']} yesterday_close_oi={prev_oi} "
                    f"today_oi={oi_walls['resistance_oi']} ratio={result['resistance_oi_change_ratio']:.2f}")
        except Exception as exc:  # noqa: BLE001 - one bad lookup must not kill the run
            log(f"[engine] resistance OI-change check failed ({exc!r}), skipping (no effect on probability)")

    if oi_walls["support_strike"] is not None:
        try:
            instr = universe["instruments_by_strike"][oi_walls["support_strike"]]["PE"]
            candles = kite_client.get_daily_history(instr["instrument_token"], from_date, to_date, oi=True)
            prev_oi = candles[-1].get("oi") if candles else None
            if prev_oi:
                result["support_oi_change_ratio"] = oi_walls["support_oi"] / prev_oi
                log(f"[engine] support OI change: {instr['tradingsymbol']} yesterday_close_oi={prev_oi} "
                    f"today_oi={oi_walls['support_oi']} ratio={result['support_oi_change_ratio']:.2f}")
        except Exception as exc:  # noqa: BLE001
            log(f"[engine] support OI-change check failed ({exc!r}), skipping (no effect on probability)")

    if atm_strike is not None and atm_oi is not None:
        try:
            instr = universe["instruments_by_strike"][atm_strike]["CE"]
            candles = kite_client.get_daily_history(instr["instrument_token"], from_date, to_date, oi=True)
            prev_oi = candles[-1].get("oi") if candles else None
            if prev_oi:
                result["atm_ce_oi_change_ratio"] = atm_oi.get("CE", 0) / prev_oi
                log(f"[engine] ATM CALL OI change: {instr['tradingsymbol']} yesterday_close_oi={prev_oi} "
                    f"today_oi={atm_oi.get('CE', 0)} ratio={result['atm_ce_oi_change_ratio']:.2f}")
        except Exception as exc:  # noqa: BLE001
            log(f"[engine] ATM CALL OI-change check failed ({exc!r}), skipping (no effect on probability)")

        try:
            instr = universe["instruments_by_strike"][atm_strike]["PE"]
            candles = kite_client.get_daily_history(instr["instrument_token"], from_date, to_date, oi=True)
            prev_oi = candles[-1].get("oi") if candles else None
            if prev_oi:
                result["atm_pe_oi_change_ratio"] = atm_oi.get("PE", 0) / prev_oi
                log(f"[engine] ATM PUT OI change: {instr['tradingsymbol']} yesterday_close_oi={prev_oi} "
                    f"today_oi={atm_oi.get('PE', 0)} ratio={result['atm_pe_oi_change_ratio']:.2f}")
        except Exception as exc:  # noqa: BLE001
            log(f"[engine] ATM PUT OI-change check failed ({exc!r}), skipping (no effect on probability)")

    return result


def get_atm_premiums(kite_client, universe, log):
    """
    Live CALL/PUT premium at the ATM strike right now - the REAL, current
    cost of the trade this run might make. Used to compute how many points
    NIFTY actually needs to move today for that specific trade to be
    profitable (see decision.decide_direction's required_move_* args) -
    instead of comparing against some fixed/guessed points number that has
    nothing to do with what the option itself actually costs today.

    Ticks are tried first (already streaming from _setup_universe_and_ticker's
    subscribe of every ATM+/-5 strike); a one-time REST quote() fallback
    covers the rare case a tick for these exact two instruments hasn't
    arrived yet - same pattern as gather_strike_oi's OI fallback.
    """
    atm = universe["atm_strike"]
    ce_instr = universe["instruments_by_strike"][atm]["CE"]
    pe_instr = universe["instruments_by_strike"][atm]["PE"]

    ce_tick = kite_client.get_tick(ce_instr["instrument_token"])
    pe_tick = kite_client.get_tick(pe_instr["instrument_token"])
    ce_premium = ce_tick["last_price"] if ce_tick and ce_tick.get("last_price") is not None else None
    pe_premium = pe_tick["last_price"] if pe_tick and pe_tick.get("last_price") is not None else None

    missing = []
    if ce_premium is None:
        missing.append(f"NFO:{ce_instr['tradingsymbol']}")
    if pe_premium is None:
        missing.append(f"NFO:{pe_instr['tradingsymbol']}")
    if missing:
        log(f"[engine] {len(missing)} ATM option(s) missing live tick for premium check, "
            f"one-time REST quote() fallback")
        q = kite_client.get_quote(missing)
        if ce_premium is None:
            ce_premium = q[f"NFO:{ce_instr['tradingsymbol']}"]["last_price"]
        if pe_premium is None:
            pe_premium = q[f"NFO:{pe_instr['tradingsymbol']}"]["last_price"]

    return {
        "ce_premium": ce_premium, "pe_premium": pe_premium,
        "ce_tradingsymbol": ce_instr["tradingsymbol"], "pe_tradingsymbol": pe_instr["tradingsymbol"],
    }


def get_india_vix(kite_client, log):
    """India VIX - a one-time REST quote, same pattern as the initial spot
    quote. Not per-underlying (NIFTY/BANKNIFTY/etc. all share one VIX)."""
    quote = kite_client.get_quote([f"NSE_INDEX:{INDIA_VIX_TRADINGSYMBOL}"])[f"NSE_INDEX:{INDIA_VIX_TRADINGSYMBOL}"]
    vix = quote["last_price"]
    if vix is None:
        raise ValueError(
            f"India VIX quote returned last_price=None "
            f"(symbol=NSE_INDEX:{INDIA_VIX_TRADINGSYMBOL}) — "
            f"check that your broker plugin supports this index symbol"
        )
    return vix


def get_vwap(kite_client, universe, log):
    """
    Phase 8: VWAP needs real traded volume, which the NIFTY 50 INDEX itself
    never carries - confirmed via Kite's own docs, index tick packets omit
    average_price/volume entirely (they aren't tradable instruments), and
    the index's own historical candles report zero volume for the same
    reason, so a minute-candle fallback on the index can never work either.
    Uses the nearest NIFTY FUTURES contract instead (`universe["futures_
    token"]`, looked up once in build_option_universe) - a genuinely traded
    instrument with real volume and average_price, tracking the index
    closely intraday (small, well-understood basis). Still compared against
    the INDEX's own current price in compute_vwap_bias - only the VWAP
    number's SOURCE changes, not what it's being compared against.
    """
    futures_token = universe.get("futures_token")
    if futures_token is None:
        raise ValueError("no futures instrument available for VWAP (lookup failed at startup)")

    tick = kite_client.get_tick(futures_token)
    if tick and tick.get("average_price"):
        return tick["average_price"]

    futures_symbol = universe.get("futures_tradingsymbol")
    if futures_symbol:
        q = kite_client.get_quote([f"NFO:{futures_symbol}"]).get(f"NFO:{futures_symbol}")
        if q and q.get("average_price"):
            return q["average_price"]

    log("[engine] futures average_price unavailable from tick/quote, falling back to minute-candle VWAP")
    today = datetime.date.today()
    market_open_dt = datetime.datetime.combine(today, MARKET_OPEN_TIME)
    now_dt = datetime.datetime.now()
    candles = kite_client.get_intraday_history(futures_token, market_open_dt, now_dt, interval="minute")
    if not candles:
        raise ValueError("no intraday candles available for VWAP calculation")
    formatted = [{"high": c["high"], "low": c["low"], "close": c["close"], "volume": c["volume"]} for c in candles]
    return option_signal.compute_vwap_from_candles(formatted)


def fetch_daily_history(kite_client, universe, settings):
    """One shared fetch of recent completed-day candles (today excluded),
    used by the gap check, the CPR check, and (Phase 7) the historical
    open-to-close range check below, so they don't each hit Kite's
    historical-data API separately. Fetches enough calendar days to cover
    whichever of the two lookback settings is larger."""
    lookback = max(settings["range_lookback_days"], settings.get("historical_range_lookback_days", 15))
    today = datetime.date.today()
    from_date = today - datetime.timedelta(days=lookback * 3 + 5)  # buffer for weekends/holidays
    to_date = today - datetime.timedelta(days=1)
    return kite_client.get_daily_history(universe["spot_token"], from_date, to_date)


def get_gap_flag(daily_candles, universe, settings, log):
    """Returns {"large_gap": bool, "gap": float, "avg_range": float} - the
    full numbers, not just the flag, so the UI can show *why* (rule 5's
    README note still applies: large_gap is only ever used as "trade both
    legs", never to flip a direction)."""
    lookback = settings["range_lookback_days"]
    recent = daily_candles[-lookback:]
    daily_ranges = [c["high"] - c["low"] for c in recent]

    if not daily_ranges:
        # Not enough completed-day history in the lookback window (e.g. a
        # long holiday cluster, or a brand new instrument) - this is a real,
        # expected edge case, not a failure, so just skip the gap check
        # rather than crashing the whole Execute run over it.
        log("[engine] gap check: no daily history available in lookback window, assuming no large gap")
        return {"large_gap": False, "gap": None, "avg_range": None}

    ohlc = universe["initial_quote"]["ohlc"]
    today_open = ohlc.get("open")
    yesterday_close = ohlc.get("close")

    if not today_open or not yesterday_close:
        log("[engine] gap check: today's open or yesterday's close unavailable from quote, assuming no large gap")
        return {"large_gap": False, "gap": None, "avg_range": mean(daily_ranges) if daily_ranges else None}

    result = option_signal.compute_gap_flag(
        daily_ranges, today_open, yesterday_close, settings["gap_threshold_factor"]
    )
    log(f"[engine] gap check: today_open={today_open} yesterday_close={yesterday_close} "
        f"avg_range={result['avg_range']:.2f} gap={result['gap']:.2f} large_gap={result['large_gap']}")
    return result


def get_orb_bias(kite_client, universe, current_price, settings, log):
    """
    Phase 9: Opening Range Breakout (see option_signal.compute_orb_bias) -
    replaces the old Pivot R1/S1 breakout check. Uses TODAY's own first
    `orb_minutes` (settings.json, default 15) of 1-minute candles on the
    INDEX itself - valid Open/High/Low/Close is available for an index even
    though volume isn't (that's what broke VWAP - see get_vwap - not this),
    so no futures proxy is needed here.
    """
    orb_minutes = settings.get("orb_minutes", 15)
    today = datetime.date.today()
    market_open = datetime.datetime.combine(today, MARKET_OPEN_TIME)
    orb_end = market_open + datetime.timedelta(minutes=orb_minutes)
    now = datetime.datetime.now()

    if now < orb_end:
        log(f"[engine] ORB check: today's opening range ({orb_minutes}min) hasn't finished forming yet "
            f"(now={now.time()}, range ends {orb_end.time()}), falling back to NEUTRAL")
        return {"orb_bias": "NEUTRAL", "orb_high": None, "orb_low": None, "orb_minutes": orb_minutes}

    candles = kite_client.get_intraday_history(universe["spot_token"], market_open, orb_end, interval="minute")
    if not candles:
        log("[engine] ORB check: no opening-range candles available, falling back to NEUTRAL")
        return {"orb_bias": "NEUTRAL", "orb_high": None, "orb_low": None, "orb_minutes": orb_minutes}

    orb_high = max(c["high"] for c in candles)
    orb_low = min(c["low"] for c in candles)
    orb_bias = option_signal.compute_orb_bias(current_price, orb_high, orb_low)
    log(f"[engine] ORB check: opening range ({orb_minutes}min)={orb_low:.2f}-{orb_high:.2f} "
        f"current={current_price} -> orb_bias={orb_bias}")
    return {"orb_bias": orb_bias, "orb_high": orb_high, "orb_low": orb_low, "orb_minutes": orb_minutes}


def get_cpr_width_ratio(daily_candles, avg_range, log):
    """
    Phase 9: Central Pivot Range width (see option_signal.compute_cpr_width),
    from the most recent completed day's High/Low/Close, expressed as a
    ratio against the recent avg_range (see get_gap_flag) so it's on the
    same scale wherever it's consumed (compute_volatility_confidence).
    """
    if not daily_candles or not avg_range or avg_range <= 0:
        return None

    prev_day = daily_candles[-1]
    cpr_width = option_signal.compute_cpr_width(prev_day["high"], prev_day["low"], prev_day["close"])
    ratio = cpr_width / avg_range
    log(f"[engine] CPR check: prev_day H/L/C={prev_day['high']}/{prev_day['low']}/{prev_day['close']} "
        f"cpr_width={cpr_width:.2f} avg_range={avg_range:.2f} ratio={ratio:.3f}")
    return ratio


def compute_positions_pnl(kite_client, positions):
    is_openalgo = hasattr(kite_client, '_oa')

    if is_openalgo and positions:
        # OpenAlgo: batch-fetch LTP for all open positions in one REST call
        # instead of relying solely on WS ticks, which may lag or miss the
        # spot index entirely.  WS tick is tried first (fastest); REST is
        # the fallback only when the tick is absent or stale.
        symbols_needed = []
        for p in positions:
            tick = kite_client.get_tick(p["token"]) or kite_client.get_tick(p["tradingsymbol"])
            if tick is None or tick.get("last_price") is None:
                symbols_needed.append(f"NFO:{p['tradingsymbol']}")

        rest_prices = {}
        if symbols_needed:
            try:
                q = kite_client.get_quote(symbols_needed)
                for key, val in q.items():
                    rest_prices[key] = val.get("last_price")
            except Exception:  # noqa: BLE001
                pass

        for p in positions:
            tick = kite_client.get_tick(p["token"]) or kite_client.get_tick(p["tradingsymbol"])
            ltp = tick["last_price"] if tick and tick.get("last_price") is not None else None
            if ltp is None:
                ltp = rest_prices.get(f"NFO:{p['tradingsymbol']}")
            if ltp is None:
                ltp = p["entry_price"]
            p["ltp"] = ltp
            p["pnl"] = (ltp - p["entry_price"]) * p["qty"] if p["entry_price"] is not None else 0.0
    else:
        # KiteConnect path: read from WS tick store (always populated for NFO options)
        for p in positions:
            tick = kite_client.get_tick(p["token"])
            if tick is None or tick.get("last_price") is None:
                tick = kite_client.get_tick(p["tradingsymbol"])
            ltp = tick["last_price"] if tick and tick.get("last_price") is not None else p["entry_price"]
            p["ltp"] = ltp
            p["pnl"] = (ltp - p["entry_price"]) * p["qty"] if p["entry_price"] is not None else 0.0
    return positions


def _square_off_one(kite_client, p, log, reason=""):
    """
    Places the real SELL for one open leg - but only after confirming with
    Zerodha's own live position data (kite_client.get_actual_position_qty)
    that there's actually still something open to sell. Protects against
    placing a SELL for a position the user already manually exited outside
    this app (e.g. via the Zerodha app/website) - blindly trusting this
    project's in-memory qty in that case would open a brand-new, unintended
    SHORT position instead of a harmless no-op. Also handles a manual
    PARTIAL exit correctly - always sells exactly the real remaining
    quantity, never a stale in-memory number.

    Returns True if a SELL was actually placed, False if skipped (nothing
    left to square off).
    """
    actual_qty = kite_client.get_actual_position_qty(
        p["tradingsymbol"], exchange="NFO", product="MIS", expected_qty=p["qty"]
    )
    if not actual_qty or actual_qty <= 0:
        log(f"[engine] {p['tradingsymbol']}: Zerodha shows no long quantity left - looks like this "
            f"was already exited manually outside this app - skipping square-off, NOT placing a SELL "
            f"(would otherwise open an unintended reversed/short position) [reason={reason}]")
        return False
    if actual_qty != p["qty"]:
        log(f"[engine] {p['tradingsymbol']}: expected qty {p['qty']} but Zerodha shows {actual_qty} "
            f"actually still open (partially exited manually?) - squaring off the real remaining "
            f"{actual_qty} instead")
    kite_client.place_market_order(p["tradingsymbol"], "NFO", "SELL", actual_qty)
    log(f"[engine] squared off {p['tradingsymbol']} qty={actual_qty} entry={p['entry_price']} "
        f"exit_ltp={p.get('ltp')} pnl={p.get('pnl')} reason={reason}")
    return True


def square_off_all(kite_client, positions, log, reason=""):
    for p in positions:
        _square_off_one(kite_client, p, log, reason=reason)
    positions.clear()


def enter_positions(kite_client, universe, direction, settings, log):
    atm = universe["atm_strike"]
    instruments_by_strike = universe["instruments_by_strike"]

    opt_types = {"CALL": ["CE"], "PUT": ["PE"], "BOTH": ["CE", "PE"]}[direction]

    # ---- OpenAlgo path: use high-level optionsorder / optionsmultiorder ----
    if settings.get("execution_provider", "kite").lower() == "openalgo":
        return _enter_positions_openalgo(kite_client, universe, direction, settings, log)

    # ---- KiteConnect path (original logic, unchanged) ----------------------
    positions = []
    for opt_type in opt_types:
        instr = instruments_by_strike[atm][opt_type]
        lot_size = instr["lot_size"]  # LIVE from Kite instrument dump, never hardcoded
        qty = settings["lots"] * lot_size

        kite_client.place_market_order(
            instr["tradingsymbol"], "NFO", kite_client.kite.TRANSACTION_TYPE_BUY, qty
        )

        tick = kite_client.get_tick(instr["instrument_token"])
        entry_price = tick["last_price"] if tick and tick.get("last_price") is not None else None
        if entry_price is None:
            q = kite_client.get_quote([f"NFO:{instr['tradingsymbol']}"])
            entry_price = q[f"NFO:{instr['tradingsymbol']}"]["last_price"]

        positions.append({
            "token": instr["instrument_token"],
            "tradingsymbol": instr["tradingsymbol"],
            "type": opt_type,
            "qty": qty,
            "entry_price": entry_price,
            "ltp": entry_price,
            "pnl": 0.0,
        })
        log(f"[engine] entered BUY {instr['tradingsymbol']} qty={qty} entry_price={entry_price}")

    return positions


def _enter_positions_openalgo(oa_client, universe, direction, settings, log):
    """
    OpenAlgo entry path: uses the high-level optionsorder / optionsmultiorder
    API so the tradingsymbol is resolved by OpenAlgo itself (never hardcoded).
    Falls back to place_market_order with the already-resolved tradingsymbol
    from the option chain if the high-level call isn't available.

    Returns the same positions list shape as the Kite path so the rest of
    engine.py (PnL, square-off, monitoring) works identically.
    """
    from openalgo_client import _OA_INDEX_EXCHANGE, _expiry_to_oa_str  # noqa: PLC0415

    index = settings["index"]
    exchange_oa = _OA_INDEX_EXCHANGE.get(index, "NSE_INDEX")
    nearest_expiry = universe["nearest_expiry"]
    expiry_str = _expiry_to_oa_str(nearest_expiry)
    atm = universe["atm_strike"]
    instruments_by_strike = universe["instruments_by_strike"]

    # Lot size and qty — always from the live instrument dump, never hardcoded.
    sample_instr = instruments_by_strike[atm].get("CE") or instruments_by_strike[atm].get("PE")
    lot_size = sample_instr["lot_size"] if sample_instr else 75
    qty = settings["lots"] * lot_size

    positions = []

    if direction == "BOTH":
        resp = oa_client.place_options_straddle(index, exchange_oa, expiry_str, qty)
        for result in resp.get("results", []):
            opt_type_str = "CE" if result.get("option_type") == "CE" else "PE"
            traded_symbol = result.get("symbol", f"{index}{expiry_str}ATM{opt_type_str}")
            # Get entry price from tick or quote
            entry_price = _get_entry_price(oa_client, traded_symbol)
            positions.append({
                "token": traded_symbol,
                "tradingsymbol": traded_symbol,
                "type": opt_type_str,
                "qty": qty,
                "entry_price": entry_price,
                "ltp": entry_price,
                "pnl": 0.0,
            })
            log(f"[engine/openalgo] entered BUY {traded_symbol} qty={qty} entry_price={entry_price}")
        # Dry-run fallback — resp has no results list
        if not positions and resp.get("dry_run"):
            for opt_type_str in ("CE", "PE"):
                instr = instruments_by_strike[atm].get(opt_type_str)
                sym = instr["tradingsymbol"] if instr else f"{index}ATM{opt_type_str}(dry)"
                positions.append({
                    "token": sym, "tradingsymbol": sym, "type": opt_type_str,
                    "qty": qty, "entry_price": None, "ltp": None, "pnl": 0.0,
                })
    else:
        opt_type_str = "CE" if direction == "CALL" else "PE"
        resp = oa_client.place_options_order(index, exchange_oa, expiry_str, opt_type_str, qty)
        traded_symbol = resp.get("symbol", f"{index}{expiry_str}ATM{opt_type_str}")
        entry_price = _get_entry_price(oa_client, traded_symbol)
        positions.append({
            "token": traded_symbol,
            "tradingsymbol": traded_symbol,
            "type": opt_type_str,
            "qty": qty,
            "entry_price": entry_price,
            "ltp": entry_price,
            "pnl": 0.0,
        })
        log(f"[engine/openalgo] entered BUY {traded_symbol} qty={qty} entry_price={entry_price}")

    return positions


def _get_entry_price(client, tradingsymbol):
    """Read the entry price from tick store; fall back to a REST quote."""
    tick = client.get_tick(tradingsymbol)
    if tick and tick.get("last_price") is not None:
        return tick["last_price"]
    try:
        q = client.get_quote([f"NFO:{tradingsymbol}"])
        return q[f"NFO:{tradingsymbol}"]["last_price"]
    except Exception:  # noqa: BLE001
        return None


def reconcile_open_positions(kite_client, settings, log):
    """
    Startup/login-time recovery: this project's own tracking of an open
    position (entry price, qty, SL/target progress) lives ONLY in the
    `positions` list inside the running monitor_and_exit() thread - nothing
    is ever written to disk. If this process restarts (or crashes) while a
    trade is live, that list is gone completely, even though the real
    position on Zerodha is untouched and still open. This rebuilds the
    in-memory list from Zerodha's own positions() data - real entry price
    (average_price), real quantity, real tradingsymbol - so PnL is
    guaranteed to exactly match the broker (never re-derived/guessed), and
    monitoring can resume automatically. Returns [] if dry_run (no real
    Zerodha position ever exists in dry_run) or nothing matching is open -
    the normal case on a clean start.
    """
    if settings.get("dry_run", True):
        return []

    try:
        net = kite_client.kite.positions().get("net", [])
    except Exception as exc:  # noqa: BLE001 - a transient API hiccup here must not crash startup/login
        log(f"[engine] could not fetch Zerodha positions for reconciliation ({exc!r}) - assuming none open")
        return []

    index = settings["index"]
    try:
        _, chain = kite_client.get_nfo_option_chain(index)
    except Exception as exc:  # noqa: BLE001
        log(f"[engine] could not fetch option chain for reconciliation ({exc!r}) - assuming none open")
        return []
    chain_by_symbol = {i["tradingsymbol"]: i for i in chain}

    positions = []
    for p in net:
        if p.get("exchange") != "NFO" or p.get("product") != "MIS":
            continue
        qty = p.get("quantity", 0)
        if qty <= 0:
            continue  # this project only ever BUYS options - a long qty is the only thing it can own
        symbol = p.get("tradingsymbol")
        instr = chain_by_symbol.get(symbol)
        if instr is None:
            continue  # not a current-expiry option of this index - not this project's to manage

        opt_type = "CALL" if instr["instrument_type"] == "CE" else "PUT"
        entry_price = p.get("average_price")
        positions.append({
            "token": instr["instrument_token"],
            "tradingsymbol": symbol,
            "type": opt_type,
            "qty": qty,
            "entry_price": entry_price,
            "ltp": p.get("last_price") if p.get("last_price") is not None else entry_price,
            "pnl": p.get("pnl", 0.0),
        })
        log(f"[engine] RECONCILED existing Zerodha position: {symbol} qty={qty} "
            f"avg_price={entry_price} - recovered after restart, resuming monitoring")

    return positions


def resume_existing_position(stop_event=None, settings_path="settings.json", log=print,
                              kite_client=None, on_event=None, on_tick=None):
    """
    Called right after every successful login (see app.py). Never analyzes
    or enters anything new - only checks whether a position from BEFORE this
    process started is still open on Zerodha (e.g. the app was restarted or
    crashed while a trade was live) via reconcile_open_positions(), and if
    so, resumes the EXACT same monitor_and_exit() SL/target/time-exit safety
    net that run()/manual_run() use, automatically. A no-op (returns
    {"positions": []} immediately) when nothing matching is open, which is
    the normal case on every ordinary login.
    """
    settings = load_settings(settings_path)
    stop_event = stop_event or threading.Event()

    if kite_client is None:
        kite_client = make_client(settings, log=log)
        kite_client.login()
    else:
        kite_client.settings = settings

    positions = reconcile_open_positions(kite_client, settings, log)
    if not positions:
        return {"positions": []}

    # Rebuild the same "universe" a normal run() would have (spot/futures
    # tokens, ATM strike, expiry) - not just the two option legs being
    # monitored - so the dashboard's live chart/LTP/header come back exactly
    # as they would after a fresh Execute, not stuck on STALE for the rest
    # of this session. Purely read-only (never re-decides or re-enters
    # anything); a failure here must never stop the actual SL/target
    # monitoring below, only degrade the chart/LTP display.
    all_tokens = []
    try:
        universe = build_option_universe(kite_client, settings["index"], log)
        _emit(on_event, {
            "type": "universe_ready",
            "spot_symbol": universe["spot_symbol"],
            "spot_token": universe["spot_token"],
            "atm_strike": universe["atm_strike"],
            "nearest_expiry": str(universe["nearest_expiry"]),
            "strike_interval": universe["strike_interval"],
        })
        all_tokens.append(universe["spot_token"])
        if universe.get("futures_token") is not None:
            all_tokens.append(universe["futures_token"])
        for s in universe["target_strikes"]:
            all_tokens.append(universe["instruments_by_strike"][s]["CE"]["instrument_token"])
            all_tokens.append(universe["instruments_by_strike"][s]["PE"]["instrument_token"])
    except Exception as exc:  # noqa: BLE001
        log(f"[engine] could not rebuild full universe on resume ({exc!r}) - "
            f"live chart/LTP may stay stale, but SL/target monitoring below is unaffected")

    for p in positions:
        if p["token"] not in all_tokens:
            all_tokens.append(p["token"])

    kite_client.start_ticker(all_tokens, on_tick=on_tick)
    if not kite_client.wait_for_ticker(timeout=10):
        log("[engine] WARNING: ticker did not confirm connection within 10s, continuing with REST fallbacks")

    _emit(on_event, {"type": "positions_entered", "positions": _positions_snapshot(positions), "resumed": True})

    try:
        monitor_and_exit(kite_client, positions, settings, stop_event, log, on_event=on_event, settings_path=settings_path)
        return {"positions": positions}
    finally:
        _safety_net_exit(kite_client, positions, log, on_event)


def _positions_snapshot(positions):
    return [
        {"tradingsymbol": p["tradingsymbol"], "type": p["type"], "qty": p["qty"],
         "entry_price": p["entry_price"], "ltp": p.get("ltp"), "pnl": p.get("pnl")}
        for p in positions
    ]


STALE_TICK_WARNING_SECONDS = 20  # no tick at all for this long -> the live feed may have stalled
STALE_TICK_RE_WARN_SECONDS = 30  # don't spam the log/UI - re-warn at most this often while still stale


_EXIT_SETTING_KEYS = ("sl_enabled", "max_loss", "target_enabled", "target_profit",
                      "time_exit_enabled", "time_exit", "pnl_mode", "force_exit_time")


def _read_live_exit_settings(settings, settings_path, log):
    """
    Re-read ONLY the SL/Target/time-exit/force-exit-time/pnl_mode fields from
    settings.json fresh on every monitoring-loop tick, so changing them in
    the UI (Settings -> Save) while a position is already open takes effect
    immediately - no more Stop+re-enter needed just to tighten/loosen a
    stop-loss mid-trade. Everything else (lots, index, API keys, ...) is
    deliberately NOT re-read here - those are already "spent" the moment
    enter_positions() ran and can't retroactively change what was bought.

    Falls back to the last-known-good `settings` dict (the caller's running
    copy) on any read/parse failure (e.g. settings.json briefly mid-write
    from a concurrent /api/settings save) - a transient read glitch must
    never crash the safety-net loop or be treated as "everything disabled".
    """
    try:
        with open(settings_path, "r") as f:
            fresh = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[engine] could not re-read {settings_path} for live SL/Target update ({exc!r}), "
            f"using last-known values this tick")
        return {k: settings[k] for k in _EXIT_SETTING_KEYS if k in settings}
    return {k: fresh[k] for k in _EXIT_SETTING_KEYS if k in fresh}


def monitor_and_exit(kite_client, positions, settings, stop_event, log, on_event=None,
                      settings_path="settings.json"):
    live = dict(settings)  # seed with the entry-time snapshot, then live-refreshed every tick below
    last_stale_warning_at = 0.0
    last_logged_exit_settings = None

    while positions:
        if stop_event.is_set():
            log("[engine] STOP signal received - squaring off immediately")
            snapshot = _positions_snapshot(positions)
            square_off_all(kite_client, positions, log, reason="MANUAL_STOP")
            _emit(on_event, {"type": "exit", "reason": "MANUAL_STOP", "positions": snapshot})
            break

        live.update(_read_live_exit_settings(settings, settings_path, log))
        exit_settings_now = {k: live.get(k) for k in _EXIT_SETTING_KEYS}
        if last_logged_exit_settings is not None and exit_settings_now != last_logged_exit_settings:
            log(f"[engine] SL/Target/time-exit settings changed while position is open - now using: "
                f"sl_enabled={live.get('sl_enabled')} max_loss={live.get('max_loss')} "
                f"target_enabled={live.get('target_enabled')} target_profit={live.get('target_profit')} "
                f"time_exit_enabled={live.get('time_exit_enabled')} time_exit={live.get('time_exit')} "
                f"pnl_mode={live.get('pnl_mode', 'COMBINED')}")
        last_logged_exit_settings = exit_settings_now

        force_exit_t = _parse_hhmm(live["force_exit_time"])
        time_exit_t = _parse_hhmm(live["time_exit"]) if live.get("time_exit_enabled") else None
        pnl_mode = live.get("pnl_mode", "COMBINED")

        # Watchdog: SL/target only protect you if prices are actually
        # updating. If the live feed stalls (network blip, Kite disconnect),
        # make that visible instead of silently trusting a frozen price -
        # the ticker's own auto-reconnect (see kite_client.py) keeps trying
        # in the background regardless.
        stale_seconds = kite_client.seconds_since_last_tick()
        if stale_seconds is not None and stale_seconds > STALE_TICK_WARNING_SECONDS:
            now_wall = time.time()
            if now_wall - last_stale_warning_at > STALE_TICK_RE_WARN_SECONDS:
                log(f"[engine] WARNING: no live tick received for {stale_seconds:.0f}s - "
                    f"price data may be stale (ticker auto-reconnect is still trying in the background)")
                _emit(on_event, {"type": "tick_stale", "seconds_since_last_tick": stale_seconds})
                last_stale_warning_at = now_wall

        compute_positions_pnl(kite_client, positions)
        _emit(on_event, {"type": "pnl_update", "positions": _positions_snapshot(positions),
                          "total_pnl": sum(p["pnl"] for p in positions)})
        now_t = datetime.datetime.now().time()

        # Rule 5: day-end safety exit - always active, never skippable.
        if now_t >= force_exit_t:
            log(f"[engine] force_exit_time {live['force_exit_time']} reached - squaring off (safety net)")
            snapshot = _positions_snapshot(positions)
            square_off_all(kite_client, positions, log, reason="FORCE_EXIT_TIME")
            _emit(on_event, {"type": "exit", "reason": "FORCE_EXIT_TIME", "positions": snapshot})
            break

        if time_exit_t is not None and now_t >= time_exit_t:
            log(f"[engine] time_exit {live['time_exit']} reached - squaring off")
            snapshot = _positions_snapshot(positions)
            square_off_all(kite_client, positions, log, reason="TIME_EXIT")
            _emit(on_event, {"type": "exit", "reason": "TIME_EXIT", "positions": snapshot})
            break

        if pnl_mode == "COMBINED":
            total_pnl = sum(p["pnl"] for p in positions)
            if live.get("sl_enabled") and total_pnl <= -abs(live["max_loss"]):
                log(f"[engine] combined SL hit: pnl={total_pnl:.2f} <= -{live['max_loss']}")
                snapshot = _positions_snapshot(positions)
                square_off_all(kite_client, positions, log, reason="STOP_LOSS")
                _emit(on_event, {"type": "exit", "reason": "STOP_LOSS", "positions": snapshot})
                break
            if live.get("target_enabled") and total_pnl >= abs(live["target_profit"]):
                log(f"[engine] combined target hit: pnl={total_pnl:.2f} >= {live['target_profit']}")
                snapshot = _positions_snapshot(positions)
                square_off_all(kite_client, positions, log, reason="TARGET")
                _emit(on_event, {"type": "exit", "reason": "TARGET", "positions": snapshot})
                break
        else:  # per-leg / individual pnl mode
            for p in list(positions):
                if live.get("sl_enabled") and p["pnl"] <= -abs(live["max_loss"]):
                    log(f"[engine] leg SL hit: {p['tradingsymbol']} pnl={p['pnl']:.2f}")
                    _square_off_one(kite_client, p, log, reason="STOP_LOSS")
                    _emit(on_event, {"type": "leg_exit", "reason": "STOP_LOSS", "tradingsymbol": p["tradingsymbol"], "pnl": p["pnl"]})
                    positions.remove(p)
                elif live.get("target_enabled") and p["pnl"] >= abs(live["target_profit"]):
                    log(f"[engine] leg target hit: {p['tradingsymbol']} pnl={p['pnl']:.2f}")
                    _square_off_one(kite_client, p, log, reason="TARGET")
                    _emit(on_event, {"type": "leg_exit", "reason": "TARGET", "tradingsymbol": p["tradingsymbol"], "pnl": p["pnl"]})
                    positions.remove(p)

        # Event-driven-ish tick: instantly interruptible by stop_event.set(),
        # otherwise acts as a short 1s poll for the time-based checks above
        # (ticks themselves update the in-memory store asynchronously via
        # the KiteTicker callback, not via this wait).
        stop_event.wait(timeout=1)


def _run_analysis(kite_client, universe, settings, log, on_event):
    """
    The full one-shot analysis pipeline (rule 4: AI/analysis called only
    once, here) - OI/VWAP/history/gap/ORB/CPR/probability/live-ATM-premium/AI,
    combined into decision.decide_direction(). Emits the "decision" event
    with every intermediate number the dashboard shows, and returns just the
    final direction string.

    Shared by run() (which enters a position if the result isn't NO_TRADE)
    and analyze_only() (which reports the result but never enters anything,
    in any mode) - so both paths are GUARANTEED to make the exact same
    decision from the exact same inputs; there is no separate, cheaper
    "preview" calculation that could ever drift from what a real Execute
    would actually decide.

    Every step below is independently defensive: a single flaky Kite API
    call (empty history, a missing field, a network blip) must degrade that
    ONE input to a safe/neutral value, never silently kill the whole run
    with no explanation.
    """
    _emit(on_event, {"type": "progress", "stage": "oi", "message": "Reading option chain OI (ATM +/- 5 strikes)..."})
    try:
        strike_oi_data, strike_volume_data = gather_strike_oi(kite_client, universe, log)

        # Phase 10: the OI walls + their fresh OI-change (today vs
        # yesterday) are computed HERE (moved up from the probability
        # section below, where they used to be computed only for the base
        # probability) so oi_bias's classification can reuse the exact same
        # numbers, not just proximity - see compute_oi_bias's Phase 10 note.
        # No extra API calls: this is the same fetch that used to happen
        # later, just reused in both places now.
        oi_walls = option_signal.compute_oi_walls(strike_oi_data, universe["atm_strike"])
        _emit(on_event, {"type": "progress", "stage": "oi_change",
                          "message": "Checking OI build-up vs yesterday's close at the nearest walls and ATM..."})
        oi_change = get_oi_change(
            kite_client, universe, oi_walls, log,
            atm_strike=universe["atm_strike"], atm_oi=strike_oi_data.get(universe["atm_strike"]),
        )
        strike_oi_change_ratios = {}
        if oi_walls["resistance_strike"] is not None:
            strike_oi_change_ratios.setdefault(oi_walls["resistance_strike"], {})["CE"] = oi_change["resistance_oi_change_ratio"]
        if oi_walls["support_strike"] is not None:
            strike_oi_change_ratios.setdefault(oi_walls["support_strike"], {})["PE"] = oi_change["support_oi_change_ratio"]
        strike_oi_change_ratios.setdefault(universe["atm_strike"], {})["CE"] = oi_change["atm_ce_oi_change_ratio"]
        strike_oi_change_ratios.setdefault(universe["atm_strike"], {})["PE"] = oi_change["atm_pe_oi_change_ratio"]

        # Phase 6: PCR alone (whole ATM+/-5 window, unweighted) can sit at a
        # mild, NEUTRAL-looking ratio even when the strike immediately next
        # to spot is heavily skewed - weight by proximity to spot for the
        # actual BULLISH/BEARISH/NEUTRAL call, same reasoning already
        # applied to the OI-wall base probability (see
        # compute_direction_probability). The real, unweighted "pcr" is
        # still returned as-is for the dashboard, so it always matches what
        # you'd see cross-checking the real option chain.
        oi_result = option_signal.compute_oi_bias(
            strike_oi_data, settings["pcr_threshold"],
            spot_price=universe["initial_quote"]["last_price"], strike_interval=universe["strike_interval"],
            strike_oi_change_ratios=strike_oi_change_ratios, oi_change_clamp=settings.get("oi_change_clamp", 0.5),
        )
        log(f"[engine] OI bias: real PCR={oi_result['pcr']:.3f} (whole ATM+/-5 window), "
            f"proximity+OI-change-weighted PCR={oi_result['weighted_pcr']:.3f} -> oi_bias={oi_result['oi_bias']}")
    except Exception as exc:  # noqa: BLE001 - one bad data point must not kill the run
        log(f"[engine] OI-bias check failed ({exc!r}), falling back to NEUTRAL")
        # `pcr`/`vwap` stay None (not NaN) below - NaN isn't valid JSON and
        # would break the socket event on the browser side.
        strike_oi_data = {}
        strike_volume_data = {}
        oi_walls = {"resistance_strike": None, "resistance_oi": 0, "support_strike": None, "support_oi": 0}
        oi_change = {
            "resistance_oi_change_ratio": None, "support_oi_change_ratio": None,
            "atm_ce_oi_change_ratio": None, "atm_pe_oi_change_ratio": None,
        }
        oi_result = {"oi_bias": "NEUTRAL", "pcr": None, "weighted_pcr": None, "total_call_oi": 0, "total_put_oi": 0}

    _emit(on_event, {"type": "progress", "stage": "vwap", "message": "Checking VWAP..."})
    try:
        vwap = get_vwap(kite_client, universe, log)
        vwap_result = option_signal.compute_vwap_bias(universe["initial_quote"]["last_price"], vwap)
    except Exception as exc:  # noqa: BLE001
        log(f"[engine] VWAP-bias check failed ({exc!r}), falling back to NEUTRAL")
        vwap_result = {"vwap_bias": "NEUTRAL", "vwap": None}

    _emit(on_event, {"type": "progress", "stage": "history",
                      "message": "Fetching yesterday's range for gap/CPR checks..."})
    try:
        daily_candles = fetch_daily_history(kite_client, universe, settings)
    except Exception as exc:  # noqa: BLE001
        log(f"[engine] fetching daily history failed ({exc!r}), gap/CPR checks will be skipped")
        daily_candles = []

    _emit(on_event, {"type": "progress", "stage": "gap", "message": "Checking today's gap vs average range..."})
    try:
        gap_result = get_gap_flag(daily_candles, universe, settings, log)
    except Exception as exc:  # noqa: BLE001
        log(f"[engine] gap check failed ({exc!r}), assuming no large gap")
        gap_result = {"large_gap": False, "gap": None, "avg_range": None}
    large_gap = gap_result["large_gap"]

    _emit(on_event, {"type": "progress", "stage": "orb", "message": "Checking opening-range breakout..."})
    try:
        orb_result = get_orb_bias(
            kite_client, universe, universe["initial_quote"]["last_price"], settings, log,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"[engine] ORB check failed ({exc!r}), falling back to NEUTRAL")
        orb_result = {"orb_bias": "NEUTRAL", "orb_high": None, "orb_low": None, "orb_minutes": settings.get("orb_minutes", 15)}

    try:
        cpr_width_ratio = get_cpr_width_ratio(daily_candles, gap_result["avg_range"], log)
    except Exception as exc:  # noqa: BLE001
        log(f"[engine] CPR check failed ({exc!r}), skipping (no effect on Volatility Confidence)")
        cpr_width_ratio = None

    # technical_bias needs any 2 of these 3 independent signals to agree
    # (see option_signal.combine_bias) - requiring unanimous agreement
    # between just OI and VWAP flagged NEUTRAL far too often to ever
    # act on, which isn't useful for a system that buys options
    # (time decay keeps ticking whether or not a trade is ever taken).
    technical_bias = option_signal.combine_bias(
        oi_result["oi_bias"], vwap_result["vwap_bias"], orb_result["orb_bias"]
    )
    pcr_str = f"{oi_result['pcr']:.3f}" if oi_result["pcr"] is not None else "N/A"
    vwap_str = f"{vwap_result['vwap']:.2f}" if vwap_result["vwap"] is not None else "N/A"
    log(f"[engine] oi_bias={oi_result['oi_bias']} (pcr={pcr_str}) "
        f"vwap_bias={vwap_result['vwap_bias']} (vwap={vwap_str}) "
        f"orb_bias={orb_result['orb_bias']} -> technical_bias={technical_bias}")

    # ---- probability-of-move: India VIX expected range + OI walls ----
    _emit(on_event, {"type": "progress", "stage": "probability",
                      "message": "Estimating probability of move (India VIX + OI walls)..."})
    spot_price = universe["initial_quote"]["last_price"]
    day_open = universe["initial_quote"]["ohlc"].get("open")
    # ohlc["open"] can be None when the broker returns a partial quote
    # (e.g. pre-market, or an index that the broker doesn't fill open/close
    # for via the quotes endpoint).  Fall back to spot_price so the
    # VIX expected-move calculation still runs — anchoring to the current
    # price instead of today's open is a slight approximation, but far
    # better than skipping VIX entirely and losing the expected-move gauge,
    # probability-of-move bar, and all downstream signals that depend on it.
    if not day_open:
        log(f"[engine] WARNING: today's open price unavailable from quote (got {day_open!r}), "
            f"using current spot price ({spot_price}) as day_open fallback for VIX expected-move")
        day_open = spot_price
    try:
        india_vix = get_india_vix(kite_client, log)
        # Anchored to TODAY'S OPEN, not the live spot price: VIX implies a
        # total expected range for the whole session, fixed once at the
        # start of the day - recomputing it fresh from the current price
        # on every check would silently re-grant the full +/-move as if
        # nothing had happened yet, even after price has already used up
        # most of that range. Anchoring to the open lets us also show how
        # much of the expected move has already played out vs how much
        # room is left (see points_moved_from_open below).
        vix_expected_move = option_signal.compute_vix_expected_move(day_open, india_vix)
    except Exception as exc:  # noqa: BLE001
        log(f"[engine] India VIX / expected-move check failed ({exc!r}), skipping")
        india_vix = None
        vix_expected_move = None

    # Phase 7: VIX alone is a statistical/implied number - it says nothing
    # about whether NIFTY has actually been travelling that far from its
    # own open recently. Blend it with the real recent open-to-close range
    # (default: last 15 trading days - settings["historical_range_lookback_days"])
    # so the expected range used for targets/profitability isn't VIX-only.
    historical_oc_range = option_signal.compute_avg_open_to_close_range(
        daily_candles[-settings.get("historical_range_lookback_days", 15):]
    ) if daily_candles else None
    if vix_expected_move is not None and historical_oc_range is not None:
        expected_move = (vix_expected_move + historical_oc_range) / 2
        log(f"[engine] expected move blend: VIX-implied=+/-{vix_expected_move:.1f}pts, "
            f"historical open-to-close avg (last {settings.get('historical_range_lookback_days', 15)} days)="
            f"+/-{historical_oc_range:.1f}pts -> blended=+/-{expected_move:.1f}pts")
    else:
        expected_move = vix_expected_move
        if vix_expected_move is not None:
            log("[engine] historical open-to-close range unavailable, using VIX-only expected move")

    points_moved_from_open = spot_price - day_open

    try:
        # oi_walls/oi_change were already computed above (feeding oi_bias
        # too, Phase 10) - reused here as-is, not re-fetched.
        # Phase 2: weight each wall by how close it actually is to spot
        # right now, not just its raw OI size. Phase 3: also scale by
        # whether that wall's OI is FRESH build-up or unwinding since
        # yesterday's close, not just its current absolute size (see
        # compute_direction_probability's proximity + oi_change formulas).
        base_prob_result = option_signal.compute_direction_probability(
            oi_walls["support_oi"], oi_walls["resistance_oi"],
            support_strike=oi_walls["support_strike"], resistance_strike=oi_walls["resistance_strike"],
            spot_price=spot_price, strike_interval=universe["strike_interval"],
            support_oi_change_ratio=oi_change["support_oi_change_ratio"],
            resistance_oi_change_ratio=oi_change["resistance_oi_change_ratio"],
            oi_change_clamp=settings.get("oi_change_clamp", 0.5),
        )
        resistance_distance = (
            abs(oi_walls["resistance_strike"] - spot_price) if oi_walls["resistance_strike"] is not None else None
        )
        support_distance = (
            abs(oi_walls["support_strike"] - spot_price) if oi_walls["support_strike"] is not None else None
        )
        log(f"[engine] OI-wall proximity: resistance {oi_walls['resistance_strike']} is "
            f"{resistance_distance if resistance_distance is not None else 'N/A'}pts away, "
            f"support {oi_walls['support_strike']} is "
            f"{support_distance if support_distance is not None else 'N/A'}pts away "
            f"(closer wall counts for more) -> proximity-weighted base_upside="
            f"{base_prob_result['upside_probability']*100:.0f}%")
    except Exception as exc:  # noqa: BLE001
        log(f"[engine] probability check failed ({exc!r}), falling back to 50/50")
        resistance_distance = None
        support_distance = None
        base_prob_result = {"upside_probability": 0.5, "downside_probability": 0.5}

    if expected_move is not None:
        log(f"[engine] probability check (OI-wall base only, before other signals): "
            f"india_vix={india_vix} expected_move=+/-{expected_move:.1f}pts (anchored to today's "
            f"open={day_open}) moved_from_open={points_moved_from_open:+.1f}pts "
            f"resistance={oi_walls['resistance_strike']} support={oi_walls['support_strike']} "
            f"base_upside={base_prob_result['upside_probability']*100:.0f}%")

    min_move = settings.get("min_expected_move_points", 30)
    range_too_tight = expected_move is not None and expected_move < min_move
    if range_too_tight:
        log(f"[engine] expected move (+/-{expected_move:.1f}pts) is below min_expected_move_points "
            f"({min_move}pts) - not worth paying time decay for, skipping trade regardless of bias")

    # Is this trade actually likely to be PROFITABLE - not just "is a big
    # move expected" in the abstract. That requires comparing the
    # market's own expected move against what THIS TRADE actually costs
    # right now: today's REAL, LIVE ATM CALL/PUT premium. A fixed/guessed
    # points number can never answer this correctly, because the same
    # points move means completely different things on a day the option
    # is cheap vs a day it's expensive (different IV, different time-to-
    # expiry decay already priced in) - the option's own live price
    # already encodes all of that, so it's the only honest yardstick.
    _emit(on_event, {"type": "progress", "stage": "premium",
                      "message": "Reading live ATM CALL/PUT premium (for profitability check)..."})
    try:
        premiums = get_atm_premiums(kite_client, universe, log)
        ce_premium = premiums["ce_premium"]
        pe_premium = premiums["pe_premium"]
    except Exception as exc:  # noqa: BLE001
        log(f"[engine] ATM premium check failed ({exc!r}) - profitability can't be verified, "
            f"any trade this run would otherwise make will be skipped")
        ce_premium = None
        pe_premium = None

    # Breakeven-at-expiry for a bought option is exactly its own premium
    # (CALL needs the underlying to rise by at least what you paid; PUT
    # needs it to fall by at least what you paid; a straddle needs
    # either side to move by at least the TWO premiums combined, since
    # both legs were paid for). profit_margin_factor adds a safety
    # cushion above pure breakeven (default 15%) so the bar is "likely
    # profitable", not merely "might not lose money" - still entirely
    # derived from today's real premium, never an arbitrary constant.
    profit_margin_factor = settings.get("profit_margin_factor", 1.15)
    required_move_call = ce_premium * profit_margin_factor if ce_premium is not None else None
    required_move_put = pe_premium * profit_margin_factor if pe_premium is not None else None
    required_move_both = (
        (ce_premium + pe_premium) * profit_margin_factor
        if ce_premium is not None and pe_premium is not None else None
    )
    if ce_premium is not None and pe_premium is not None:
        log(f"[engine] profitability check: ATM CE premium={ce_premium:.2f} PE premium={pe_premium:.2f} "
            f"(x{profit_margin_factor} margin) -> needs >={required_move_call:.1f}pts up for CALL, "
            f">={required_move_put:.1f}pts down for PUT, >={required_move_both:.1f}pts either way for "
            f"BOTH; blended expected_move=+/-{(expected_move or 0):.1f}pts")

    # Phase 1 of the multi-signal upgrade: today's OWN already-realized move
    # (points_moved_from_open) was previously computed only for display -
    # never fed into the probability at all. Sized against VIX's own
    # expected_move (not a fixed points number), and deliberately EXCLUDED
    # once the move is mostly used up (see compute_momentum_bias) so this
    # doesn't end up chasing an already-exhausted move.
    momentum_min_fraction = settings.get("momentum_min_fraction", 0.25)
    momentum_max_fraction = settings.get("momentum_max_fraction", 0.65)
    momentum_result = option_signal.compute_momentum_bias(
        points_moved_from_open, expected_move,
        min_fraction=momentum_min_fraction, max_fraction=momentum_max_fraction,
    )
    if momentum_result["used_fraction"] is not None:
        log(f"[engine] momentum check: moved {points_moved_from_open:+.1f}pts of +/-{expected_move:.1f}pts "
            f"expected ({momentum_result['used_fraction']*100:.0f}% used, band "
            f"{momentum_min_fraction*100:.0f}-{momentum_max_fraction*100:.0f}%) -> "
            f"momentum_bias={momentum_result['momentum_bias']}")

    # Phase 4 (+ OI/option-chain extension): a SEPARATE "how likely is a
    # genuinely big move today" gauge - independent of the Up/Down
    # probability split above, which answers a different question (WHICH
    # WAY, not HOW MUCH). Combines the already-computed real signals
    # (VIX-vs-recent-range ratio, ORB breakout, today's realized momentum,
    # today's OI build-up/unwind magnitude at the two nearest walls - the
    # exact same oi_change ratios used for the probability above, reused
    # here by magnitude not direction - and CPR narrowness) - see
    # option_signal.compute_volatility_confidence.
    volatility_confidence = option_signal.compute_volatility_confidence(
        expected_move, gap_result["avg_range"], orb_result["orb_bias"], momentum_result["used_fraction"],
        resistance_oi_change_ratio=oi_change["resistance_oi_change_ratio"],
        support_oi_change_ratio=oi_change["support_oi_change_ratio"],
        oi_change_clamp=settings.get("oi_change_clamp", 0.5),
        cpr_width_ratio=cpr_width_ratio,
        ratio_scale=settings.get("volatility_confidence_ratio_scale", 50),
        orb_bonus=settings.get("volatility_confidence_orb_bonus", 15),
        momentum_scale=settings.get("volatility_confidence_momentum_scale", 20),
        oi_buildup_scale=settings.get("volatility_confidence_oi_buildup_scale", 15),
        cpr_narrow_scale=settings.get("volatility_confidence_cpr_narrow_scale", 15),
    )
    if volatility_confidence is not None:
        log(f"[engine] volatility confidence: {volatility_confidence:.0f}% (expected_move/avg_range="
            f"{expected_move/gap_result['avg_range']:.2f}x, orb_bias={orb_result['orb_bias']}, "
            f"momentum_used={momentum_result['used_fraction']}, "
            f"resistance_oi_change={oi_change['resistance_oi_change_ratio']}, "
            f"support_oi_change={oi_change['support_oi_change_ratio']}, "
            f"cpr_width_ratio={cpr_width_ratio})")

    market_context = None
    if expected_move is not None:
        market_context = {
            "spot": spot_price, "vix": india_vix, "expected_move": expected_move,
            "resistance_strike": oi_walls["resistance_strike"], "support_strike": oi_walls["support_strike"],
            "resistance_distance": resistance_distance, "support_distance": support_distance,
            "resistance_oi_change_ratio": oi_change["resistance_oi_change_ratio"],
            "support_oi_change_ratio": oi_change["support_oi_change_ratio"],
            "orb_bias": orb_result["orb_bias"],
            "points_moved_from_open": points_moved_from_open,
            "momentum_used_fraction": momentum_result["used_fraction"],
            "momentum_bias": momentum_result["momentum_bias"],
            "volatility_confidence": volatility_confidence,
            "upside_probability": base_prob_result["upside_probability"],
            "downside_probability": base_prob_result["downside_probability"],
        }

    _emit(on_event, {"type": "progress", "stage": "ai",
                      "message": "Asking AI for today's sentiment (this can take up to a minute)..."})
    ai_result = ai_sentiment.get_ai_sentiment(settings, market_context=market_context, log=log)  # already safe

    # Final probability: the OI-wall base, nudged by PCR/VWAP/ORB/gap/
    # AI-sentiment/momentum together - see option_signal.combine_probability_signals
    # for exactly how each of the 6 inputs contributes. This is THE SAME
    # number shown on the dashboard, and (below) the same number
    # decision.decide_direction() actually acts on - not a separate,
    # disconnected calculation. Uses the proximity-weighted PCR (Phase 6),
    # not the raw whole-window one, so this nudge also reflects what's
    # actually happening near spot right now, not the far edges too.
    # Phase 7: fresh OI build-up/unwind CALL vs PUT specifically AT the ATM
    # strike (today vs yesterday) - distinct from the resistance/support
    # walls above (always OTM by construction) and from pcr above (the
    # broader ATM+/-5 window's static ratio) - this is what's freshly
    # changing right at the money today.
    atm_oi_bias = option_signal.compute_atm_oi_change_bias(
        oi_change["atm_ce_oi_change_ratio"], oi_change["atm_pe_oi_change_ratio"],
        threshold=settings.get("atm_oi_change_threshold", 0.1),
    )
    log(f"[engine] ATM OI-change bias: CE_ratio={oi_change['atm_ce_oi_change_ratio']} "
        f"PE_ratio={oi_change['atm_pe_oi_change_ratio']} -> atm_oi_bias={atm_oi_bias}")

    prob_result = option_signal.combine_probability_signals(
        base_prob_result["upside_probability"], oi_result["weighted_pcr"], settings["pcr_threshold"],
        vwap_result["vwap_bias"], orb_result["orb_bias"], gap_result["gap"], ai_result["sentiment"],
        momentum_bias=momentum_result["momentum_bias"], atm_oi_bias=atm_oi_bias,
    )
    log(f"[engine] combined probability (option chain + VWAP + ORB + gap + AI sentiment + momentum + "
        f"ATM OI-change): upside={prob_result['upside_probability']*100:.0f}% "
        f"downside={prob_result['downside_probability']*100:.0f}%")

    call_threshold = settings.get("call_probability_threshold", 0.55)
    put_threshold = settings.get("put_probability_threshold", 0.45)
    direction = decision.decide_direction(
        prob_result["upside_probability"], ai_result["sentiment"], large_gap,
        range_too_tight=range_too_tight, expected_move=expected_move,
        required_move_call=required_move_call, required_move_put=required_move_put,
        required_move_both=required_move_both,
        call_threshold=call_threshold, put_threshold=put_threshold,
    )
    log(f"[engine] FINAL DECISION: {direction} "
        f"(upside_probability={prob_result['upside_probability']*100:.0f}% "
        f"[need >={call_threshold*100:.0f}% for CALL or <={put_threshold*100:.0f}% for PUT], "
        f"technical_bias={technical_bias} [informational only], ai_sentiment={ai_result['sentiment']}, "
        f"momentum_bias={momentum_result['momentum_bias']}, "
        f"large_gap={large_gap}, range_too_tight={range_too_tight}, "
        f"required_move(call/put/both)={required_move_call}/{required_move_put}/{required_move_both})")

    _emit(on_event, {
        "type": "decision",
        "technical_bias": technical_bias,
        "oi_bias": oi_result["oi_bias"],
        "pcr": oi_result["pcr"],
        "weighted_pcr": oi_result["weighted_pcr"],
        "atm_oi_bias": atm_oi_bias,
        "atm_ce_oi_change_ratio": oi_change["atm_ce_oi_change_ratio"],
        "atm_pe_oi_change_ratio": oi_change["atm_pe_oi_change_ratio"],
        "vwap_bias": vwap_result["vwap_bias"],
        "vwap": vwap_result["vwap"],
        "vwap_source_tradingsymbol": universe.get("futures_tradingsymbol"),
        "orb_bias": orb_result["orb_bias"],
        "orb_high": orb_result["orb_high"],
        "orb_low": orb_result["orb_low"],
        "orb_minutes": orb_result["orb_minutes"],
        "cpr_width_ratio": cpr_width_ratio,
        "ai_sentiment": ai_result["sentiment"],
        "ai_reason": ai_result["reason"],
        "large_gap": large_gap,
        "gap_points": gap_result["gap"],
        "avg_range": gap_result["avg_range"],
        "range_too_tight": range_too_tight,
        "ce_premium": ce_premium,
        "pe_premium": pe_premium,
        "profit_margin_factor": profit_margin_factor,
        "required_move_call": required_move_call,
        "required_move_put": required_move_put,
        "required_move_both": required_move_both,
        "india_vix": india_vix,
        "expected_move": expected_move,
        "vix_expected_move": vix_expected_move,
        "historical_oc_range": historical_oc_range,
        "historical_range_lookback_days": settings.get("historical_range_lookback_days", 15),
        "day_open": day_open,
        "points_moved_from_open": points_moved_from_open,
        "momentum_bias": momentum_result["momentum_bias"],
        "momentum_used_fraction": momentum_result["used_fraction"],
        "momentum_min_fraction": momentum_min_fraction,
        "momentum_max_fraction": momentum_max_fraction,
        "volatility_confidence": volatility_confidence,
        "expected_upside_target": (day_open + expected_move) if expected_move is not None else None,
        "expected_downside_target": (day_open - expected_move) if expected_move is not None else None,
        "resistance_strike": oi_walls["resistance_strike"],
        "support_strike": oi_walls["support_strike"],
        "resistance_distance": resistance_distance,
        "support_distance": support_distance,
        "resistance_oi_change_ratio": oi_change["resistance_oi_change_ratio"],
        "support_oi_change_ratio": oi_change["support_oi_change_ratio"],
        "resistance_volume": (
            strike_volume_data.get(oi_walls["resistance_strike"], {}).get("CE")
            if oi_walls["resistance_strike"] is not None else None
        ),
        "support_volume": (
            strike_volume_data.get(oi_walls["support_strike"], {}).get("PE")
            if oi_walls["support_strike"] is not None else None
        ),
        "upside_probability": prob_result["upside_probability"],
        "downside_probability": prob_result["downside_probability"],
        "call_probability_threshold": call_threshold,
        "put_probability_threshold": put_threshold,
        "direction": direction,
    })

    return direction


def run(stop_event=None, settings_path="settings.json", log=print, kite_client=None, on_event=None, on_tick=None):
    """
    kite_client: pass an already-logged-in KiteClient (e.g. from a separate
                 "Login" action in a UI) to reuse it instead of logging in
                 again here. If omitted (the headless/console default),
                 a new KiteClient is created and logged in inline.
    on_event:    optional callback(dict) fired at each decision/entry/exit
                 point - e.g. to push updates to a web dashboard. Never
                 allowed to affect trading logic (see _emit()).
    on_tick:     optional callback(tick_dict) fired for every raw tick from
                 the KiteTicker - e.g. to stream live prices to a chart.
    """
    settings = load_settings(settings_path)
    stop_event = stop_event or threading.Event()

    if kite_client is None:
        kite_client = make_client(settings, log=log)
        kite_client.login()
    else:
        # Always use the freshly-loaded settings for this run (in particular
        # dry_run/lots/SL/target) - a client handed in from an earlier
        # Login must never keep acting on a stale settings snapshot.
        kite_client.settings = settings

    positions = []
    try:
        universe = _setup_universe_and_ticker(kite_client, settings, log, on_event, on_tick)
        direction = _run_analysis(kite_client, universe, settings, log, on_event)

        if direction == "NO_TRADE":
            log("[engine] NO_TRADE - no order placed, run ending.")
            _emit(on_event, {"type": "no_trade"})
            return {"direction": direction, "positions": []}

        positions = enter_positions(kite_client, universe, direction, settings, log)
        _emit(on_event, {"type": "positions_entered", "positions": _positions_snapshot(positions)})

        # ---- monitoring loop: no AI/network-analysis calls from here on ----
        monitor_and_exit(kite_client, positions, settings, stop_event, log, on_event=on_event, settings_path=settings_path)

        return {"direction": direction, "positions": positions}

    finally:
        _safety_net_exit(kite_client, positions, log, on_event)


def analyze_only(settings_path="settings.json", log=print, kite_client=None, on_event=None, on_tick=None):
    """
    Runs the EXACT same one-shot analysis pipeline as run() (_run_analysis -
    same OI/VWAP/ORB/gap/VIX/live-ATM-premium/AI calls, same
    decision.decide_direction() truth table) and reports the resulting
    direction - but NEVER calls enter_positions(). No order is ever placed
    here, in ANY mode (dry_run or LIVE); this is a pure, read-only market
    read for someone who wants to see what the system would decide right
    now without committing to a trade. Because it shares _run_analysis with
    run() rather than recomputing anything separately, the result is
    guaranteed identical to what a real Execute would have decided from the
    same inputs at the same moment - never a cheaper/different "preview".
    """
    settings = load_settings(settings_path)

    if kite_client is None:
        kite_client = make_client(settings, log=log)
        kite_client.login()
    else:
        kite_client.settings = settings

    try:
        universe = _setup_universe_and_ticker(kite_client, settings, log, on_event, on_tick)
        direction = _run_analysis(kite_client, universe, settings, log, on_event)
        log(f"[engine] ANALYZE ONLY: decision was {direction} - no order placed (analysis-only run, "
            f"never enters a position in any mode)")
        _emit(on_event, {"type": "analysis_complete", "direction": direction})
        return {"direction": direction, "positions": []}
    finally:
        kite_client.stop_ticker()


VALID_MANUAL_DIRECTIONS = ("CALL", "PUT", "BOTH")


def manual_run(direction, stop_event=None, settings_path="settings.json", log=print,
                kite_client=None, on_event=None, on_tick=None):
    """
    Manual override entry point: the user picks CALL/PUT/BOTH directly from
    the dashboard, skipping the entire one-shot analysis (OI/VWAP/ORB/VIX/
    AI/probability) that run() performs. Everything AFTER entry - SL/target/
    time-exit/force-exit monitoring via monitor_and_exit() - is the exact
    same safety net run() uses, so a manual choice is never less protected
    than an automated one; only how the direction was chosen differs.
    """
    if direction not in VALID_MANUAL_DIRECTIONS:
        raise ValueError(f"invalid direction: {direction!r} - must be one of {VALID_MANUAL_DIRECTIONS}")

    settings = load_settings(settings_path)
    stop_event = stop_event or threading.Event()

    if kite_client is None:
        kite_client = make_client(settings, log=log)
        kite_client.login()
    else:
        kite_client.settings = settings

    positions = []
    try:
        universe = _setup_universe_and_ticker(kite_client, settings, log, on_event, on_tick)

        log(f"[engine] MANUAL OVERRIDE: entering {direction} directly - no analysis performed")
        _emit(on_event, {"type": "decision", "direction": direction, "manual": True})

        positions = enter_positions(kite_client, universe, direction, settings, log)
        _emit(on_event, {"type": "positions_entered", "positions": _positions_snapshot(positions)})

        monitor_and_exit(kite_client, positions, settings, stop_event, log, on_event=on_event, settings_path=settings_path)

        return {"direction": direction, "positions": positions}

    finally:
        _safety_net_exit(kite_client, positions, log, on_event)
