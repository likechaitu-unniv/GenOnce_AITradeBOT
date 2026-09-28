"""
Phase 2: Flask + Flask-SocketIO web dashboard on top of the Phase 1 engine.

This file does NOT re-implement any trading/analysis logic - it only:
  - serves the single-page dashboard (templates/index.html)
  - reads/writes settings.json (the same file engine.py reads)
  - runs engine.run() in a background thread on "Execute"
  - relays engine.py's on_event/on_tick callbacks to the browser over
    WebSocket (Flask-SocketIO), and relays "Stop" clicks to the same
    stop_event that engine.py's monitoring loop already checks

Run with `python app.py` (or run.bat / run.sh). Opens your browser to
http://127.0.0.1:5050 automatically. This is a local-only tool - it binds
to 127.0.0.1 (not 0.0.0.0), so nothing outside this machine can reach it.
"""

import datetime
import json
import os
import threading
import webbrowser

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

import ai_sentiment
import engine
import option_signal
from kite_client import KiteClient, extract_request_token, load_cached_access_token
from openalgo_client import OpenAlgoClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
LAST_ANALYSIS_PATH = os.path.join(BASE_DIR, "last_analysis_state.json")
PORT = 5050  # deliberately different from the fixed Kite redirect port 5000

# Editing a .py file never hot-reloads a running Python process - only a
# full restart of `python app.py` picks up new code (unlike templates/
# static files, which Flask already re-reads fresh on every request). This
# has caused real, hard-to-diagnose confusion more than once: the browser
# shows the latest UI (fetched fresh) while the backend keeps running old
# logic. So: remember when THIS process started, and compare it against the
# .py files' own last-modified time on every page load / status check - if
# any of them changed after this process started, we know for certain a
# restart is needed and can say so plainly instead of guessing.
SERVER_STARTED_AT = datetime.datetime.now()
_WATCHED_PY_FILES = ["app.py", "engine.py", "kite_client.py", "openalgo_client.py",
                     "option_signal.py", "ai_sentiment.py", "decision.py"]


def _code_last_modified():
    mtimes = []
    for fname in _WATCHED_PY_FILES:
        path = os.path.join(BASE_DIR, fname)
        if os.path.exists(path):
            mtimes.append(datetime.datetime.fromtimestamp(os.path.getmtime(path)))
    return max(mtimes) if mtimes else None


def restart_status():
    code_modified = _code_last_modified()
    needed = code_modified is not None and code_modified > SERVER_STARTED_AT
    return {
        "restart_needed": needed,
        "server_started_at": SERVER_STARTED_AT.strftime("%H:%M:%S"),
        "code_last_modified": code_modified.strftime("%H:%M:%S") if code_modified else None,
    }

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
app.config["TEMPLATES_AUTO_RELOAD"] = True
socketio = SocketIO(app, async_mode="threading")

STATE_LOCK = threading.Lock()
STATE = {
    "kite_client": None,
    "logged_in": False,
    "running": False,
    "stop_event": None,
    # Last "universe_ready"/"decision" engine_event seen, so a page refresh
    # (including while a position is still open and the engine mid-run) can
    # redisplay the same analysis instead of resetting to blank - these are
    # one-time events with nothing that re-sends them, unlike pnl_update
    # which repeats every monitoring tick and is why P&L/positions alone
    # used to survive a refresh. Also mirrored to last_analysis_state.json
    # (see _save_last_analysis_to_disk) so a full process restart doesn't
    # lose it either - seeded below from that file, not just None. See
    # /api/last_analysis and dashboard.js's loadLastAnalysis().
    "last_universe_event": None,
    "last_decision_event": None,
}


def _load_last_analysis_from_disk():
    """
    STATE["last_universe_event"]/["last_decision_event"] alone only survive
    a browser refresh - they live in THIS process's memory, so a full
    restart (or crash) wipes them back to None even though the Signal
    Breakdown/Probability-of-Move numbers they represent are still exactly
    what led to whatever position is (or was) open. Mirrored to
    last_analysis_state.json on every update (see _make_event_relays) and
    reloaded here at startup, so a restart no longer loses that context -
    only a genuinely fresh analysis (a new Execute/Analyze run) replaces it.
    """
    try:
        with open(LAST_ANALYSIS_PATH, "r") as f:
            data = json.load(f)
        return data.get("universe"), data.get("decision")
    except (OSError, json.JSONDecodeError):
        return None, None


def _save_last_analysis_to_disk():
    try:
        with open(LAST_ANALYSIS_PATH, "w") as f:
            json.dump({"universe": STATE["last_universe_event"], "decision": STATE["last_decision_event"]}, f)
    except OSError as exc:  # noqa: BLE001 - best-effort persistence, must never crash the event relay
        print(f"[app] could not persist last-analysis snapshot to disk: {exc!r}")


STATE["last_universe_event"], STATE["last_decision_event"] = _load_last_analysis_from_disk()


def load_settings():
    with open(SETTINGS_PATH, "r") as f:
        return json.load(f)


def save_settings(settings):
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)


def settings_configured(settings):
    """Whether the minimum required keys are filled in to allow Login/Execute."""
    provider = settings.get("execution_provider", "kite").lower()
    if provider == "openalgo":
        if not settings.get("openalgo_api_key"):
            return False
    else:
        if not settings.get("kite_api_key") or not settings.get("kite_api_secret"):
            return False
    ai_provider = settings.get("ai_provider", "gemini")
    if ai_provider == "gemini" and not settings.get("gemini_api_key"):
        return False
    if ai_provider == "claude" and not settings.get("anthropic_api_key"):
        return False
    return True


def _make_log():
    def log(message):
        print(message)
        socketio.emit("log", {"message": message})
    return log


def _silent_log(message):
    pass


# Cache for the ATM CALL/PUT preview (see /api/atm_preview below): the full
# NFO instrument dump (build_option_universe) is expensive and only needs
# refetching when the underlying/expiry changes or the spot has drifted far
# enough that the ATM strike falls outside the previously fetched +/-5
# window - every other poll only needs two lightweight quote() calls.
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_UNIVERSE = {"data": None}


def _get_atm_preview(kite_client, settings):
    """
    Read-only, no-order-placement preview of exactly what a manual Buy CALL/
    PUT/BOTH click would enter right now: same ATM strike selection
    (engine.build_option_universe / option_signal.get_atm_strike) as
    enter_positions() itself uses, with live LTPs pulled straight from Kite.
    """
    index = settings["index"]
    is_openalgo = hasattr(kite_client, '_oa')
    spot_exch = "NSE_INDEX" if is_openalgo else "NSE"
    spot_symbol = engine._resolve_spot_tradingsymbol(index, is_openalgo=is_openalgo)
    spot_price = kite_client.get_quote([f"{spot_exch}:{spot_symbol}"])[f"{spot_exch}:{spot_symbol}"]["last_price"]

    with _PREVIEW_LOCK:
        universe = _PREVIEW_UNIVERSE["data"]
        needs_rebuild = (
            universe is None
            or universe["spot_symbol"] != spot_symbol
            or option_signal.get_atm_strike(spot_price, universe["strike_interval"]) not in universe["instruments_by_strike"]
        )
        if needs_rebuild:
            universe = engine.build_option_universe(kite_client, index, _silent_log)
            _PREVIEW_UNIVERSE["data"] = universe

    atm_strike = option_signal.get_atm_strike(spot_price, universe["strike_interval"])
    ce_instr = universe["instruments_by_strike"][atm_strike]["CE"]
    pe_instr = universe["instruments_by_strike"][atm_strike]["PE"]

    opt_quotes = kite_client.get_quote([f"NFO:{ce_instr['tradingsymbol']}", f"NFO:{pe_instr['tradingsymbol']}"])
    ce_ltp = opt_quotes[f"NFO:{ce_instr['tradingsymbol']}"]["last_price"]
    pe_ltp = opt_quotes[f"NFO:{pe_instr['tradingsymbol']}"]["last_price"]
    lot_size = ce_instr["lot_size"]
    lots = settings.get("lots", 1)

    return {
        "spot_ltp": spot_price,
        "atm_strike": atm_strike,
        "expiry": str(universe["nearest_expiry"]),
        "lot_size": lot_size,
        "lots": lots,
        "call": {"tradingsymbol": ce_instr["tradingsymbol"], "ltp": ce_ltp,
                 "total_cost": round(ce_ltp * lot_size * lots, 2)},
        "put": {"tradingsymbol": pe_instr["tradingsymbol"], "ltp": pe_ltp,
                "total_cost": round(pe_ltp * lot_size * lots, 2)},
        "both_combined_ltp": round(ce_ltp + pe_ltp, 2),
        "both_total_cost": round((ce_ltp + pe_ltp) * lot_size * lots, 2),
    }


# Separate, lightweight cache for just the spot instrument token (used by the
# always-on chart/LTP feed below) - get_index_instrument() fetches Kite's
# full NSE instrument dump, so this is cached independently of the (option-
# chain) ATM preview cache above, shared across /api/spot_chart and
# /api/spot_ltp so idle polling doesn't re-fetch that dump every few seconds.
_SPOT_LOCK = threading.Lock()
_SPOT_INSTRUMENT = {"symbol": None, "token": None}
MARKET_OPEN_TIME = datetime.time(9, 15)


def _get_spot_instrument(kite_client, settings):
    index = settings["index"]
    is_openalgo = hasattr(kite_client, '_oa')
    spot_symbol = engine._resolve_spot_tradingsymbol(index, is_openalgo=is_openalgo)
    spot_exch = "NSE_INDEX" if is_openalgo else "NSE"
    with _SPOT_LOCK:
        if _SPOT_INSTRUMENT["symbol"] != spot_symbol:
            instrument = kite_client.get_index_instrument(spot_symbol, exchange=spot_exch)
            _SPOT_INSTRUMENT["symbol"] = spot_symbol
            _SPOT_INSTRUMENT["token"] = instrument["instrument_token"]
    return _SPOT_INSTRUMENT["symbol"], _SPOT_INSTRUMENT["token"]


# --------------------------------------------------------------- page

@app.route("/")
def index():
    settings = load_settings()
    return render_template("index.html", settings=settings, configured=settings_configured(settings),
                            restart=restart_status())


# --------------------------------------------------------------- settings

@app.route("/api/settings", methods=["GET"])
def get_settings():
    settings = load_settings()
    return jsonify({"settings": settings, "configured": settings_configured(settings)})


@app.route("/api/settings", methods=["POST"])
def update_settings():
    incoming = request.get_json(force=True, silent=True) or {}
    settings = load_settings()

    # Only the fields the Settings section actually exposes are writable
    # here. Safety-critical/advanced fields (dry_run, force_exit_time,
    # pcr_threshold, range_lookback_days, historical_range_lookback_days,
    # gap_threshold_factor, orb_minutes, atm_oi_change_threshold) are
    # deliberately NOT editable from this endpoint - dry_run especially is
    # left as a manual settings.json edit on purpose, so nobody can flip
    # live trading on with one misplaced click (see README).
    str_fields = ["kite_api_key", "kite_api_secret", "ai_provider", "gemini_api_key",
                  "gemini_model", "anthropic_api_key", "claude_model", "index", "time_exit",
                  "custom_ai_note", "execution_provider", "openalgo_api_key", "openalgo_host"]
    for field in str_fields:
        if field in incoming:
            settings[field] = str(incoming[field])

    if "lots" in incoming:
        try:
            settings["lots"] = max(1, int(incoming["lots"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "lots must be a whole number"}), 400

    for bool_field in ["sl_enabled", "target_enabled", "time_exit_enabled"]:
        if bool_field in incoming:
            settings[bool_field] = bool(incoming[bool_field])

    for num_field in ["max_loss", "target_profit"]:
        if num_field in incoming:
            try:
                settings[num_field] = float(incoming[num_field])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{num_field} must be a number"}), 400

    if "profit_margin_factor" in incoming:
        try:
            settings["profit_margin_factor"] = max(1.0, float(incoming["profit_margin_factor"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "profit_margin_factor must be a number"}), 400

    for frac_field in ["momentum_min_fraction", "momentum_max_fraction"]:
        if frac_field in incoming:
            try:
                settings[frac_field] = max(0.0, min(1.0, float(incoming[frac_field])))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{frac_field} must be a number"}), 400

    if "oi_change_clamp" in incoming:
        try:
            settings["oi_change_clamp"] = max(0.0, min(1.0, float(incoming["oi_change_clamp"])))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "oi_change_clamp must be a number"}), 400

    for vc_field in ["volatility_confidence_ratio_scale", "volatility_confidence_orb_bonus",
                     "volatility_confidence_momentum_scale", "volatility_confidence_oi_buildup_scale",
                     "volatility_confidence_cpr_narrow_scale"]:
        if vc_field in incoming:
            try:
                settings[vc_field] = max(0.0, float(incoming[vc_field]))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{vc_field} must be a number"}), 400

    if isinstance(incoming.get("proxy"), dict):
        settings.setdefault("proxy", {})
        for k in ["enabled", "host", "port", "user", "pass"]:
            if k in incoming["proxy"]:
                settings["proxy"][k] = incoming["proxy"][k]

    save_settings(settings)
    return jsonify({"ok": True, "configured": settings_configured(settings)})


@app.route("/api/mode", methods=["POST"])
def set_mode():
    """
    Dedicated, standalone switch for dry_run/live - deliberately kept OUT of
    the generic /api/settings save (see the comment there) so flipping live
    trading on/off is always its own explicit action, never bundled with an
    unrelated settings change. Refuses while an Execute run is in progress,
    so a run never has its safety mode changed out from under it mid-flight.
    """
    with STATE_LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "cannot change mode while the engine is running - Stop first"}), 400

    incoming = request.get_json(force=True, silent=True) or {}
    if "live" not in incoming:
        return jsonify({"ok": False, "error": "missing 'live' field"}), 400

    settings = load_settings()
    settings["dry_run"] = not bool(incoming["live"])
    save_settings(settings)
    log = _make_log()
    log(f"[app] mode switched to {'LIVE - real orders will be placed' if incoming['live'] else 'DRY RUN'}")
    return jsonify({"ok": True, "dry_run": settings["dry_run"]})


@app.route("/api/atm_preview", methods=["GET"])
def atm_preview():
    """
    Polled every few seconds by the dashboard while logged in AND the Manual
    Entry section is open, so the manual CALL/PUT/BOTH buttons show live ATM
    symbols + prices BEFORE any order is placed, instead of only finding out
    the entry price after a real order already went in. Purely read-only
    (quote() calls) - never touches order placement. (The LTP box and chart
    are driven separately by /api/spot_ltp/spot_chart below, so they stay
    live even while this section is collapsed.)
    """
    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "not logged in"}), 400
        kite_client = STATE["kite_client"]

    settings = load_settings()
    try:
        data = _get_atm_preview(kite_client, settings)
        return jsonify({"ok": True, **data})
    except Exception as exc:  # noqa: BLE001 - a preview hiccup must never break the dashboard
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/spot_chart", methods=["GET"])
def spot_chart():
    """
    Today's 5-minute candles for the underlying index, from market open
    (09:15 IST) to now - lets the dashboard's live chart show real history
    immediately after Login, instead of starting blank and only filling in
    from whatever moment Execute/a manual trade happens to be clicked.
    Entirely independent of Execute/manual trade - a pure market-data read,
    places no orders and touches no position state. Called once after Login
    (or on page load if already logged in) to backfill the chart; the
    still-forming current candle is then kept live via /api/spot_ltp polling
    (idle) or the real tick stream (once a run is active).
    """
    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "not logged in"}), 400
        kite_client = STATE["kite_client"]

    settings = load_settings()
    try:
        spot_symbol, spot_token = _get_spot_instrument(kite_client, settings)
        today = datetime.date.today()
        market_open = datetime.datetime.combine(today, MARKET_OPEN_TIME)
        now = datetime.datetime.now()
        candles = []
        if now > market_open:
            raw = kite_client.get_intraday_history(spot_token, market_open, now, interval="5minute")
            candles = [
                {"time": int(c["date"].timestamp()), "open": c["open"], "high": c["high"],
                 "low": c["low"], "close": c["close"]}
                for c in raw
            ]
        return jsonify({"ok": True, "spot_symbol": spot_symbol, "spot_token": spot_token, "candles": candles})
    except Exception as exc:  # noqa: BLE001 - a chart-backfill hiccup must never break the dashboard
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/spot_ltp", methods=["GET"])
def spot_ltp():
    """
    Cheap, single-quote spot LTP - polled every few seconds while logged in
    and idle (no run active) so the LTP box and the chart's still-forming
    candle stay live regardless of whether the Manual Entry section is open
    or Execute has ever been clicked. Once a real run starts, the actual
    KiteTicker "tick" events take over (far more real-time) and this polling
    pauses - see syncSpotFeed() in dashboard.js.
    """
    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "not logged in"}), 400
        kite_client = STATE["kite_client"]

    settings = load_settings()
    try:
        spot_symbol, _ = _get_spot_instrument(kite_client, settings)
        is_openalgo = hasattr(kite_client, '_oa')
        spot_exch = "NSE_INDEX" if is_openalgo else "NSE"
        ltp = kite_client.get_quote([f"{spot_exch}:{spot_symbol}"])[f"{spot_exch}:{spot_symbol}"]["last_price"]
        return jsonify({"ok": True, "spot_symbol": spot_symbol, "ltp": ltp})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/prompt_preview", methods=["GET"])
def prompt_preview():
    """
    Read-only preview of the exact AI prompt (minus the market-context
    section, which only exists once numbers are computed mid-Execute) - so
    the previously-invisible prompt, plus whatever custom_ai_note is
    currently saved, is visible in the Advanced section before you ever
    click Execute.
    """
    settings = load_settings()
    custom_note = (settings.get("custom_ai_note") or "").strip() or None
    return jsonify({"prompt": ai_sentiment.build_prompt_preview(custom_note=custom_note)})


# --------------------------------------------------------------- control

@app.route("/api/login", methods=["POST"])
def login():
    with STATE_LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "engine is running - stop it before logging in again"}), 400

    settings = load_settings()
    if not settings.get("kite_api_key") or not settings.get("kite_api_secret"):
        return jsonify({"ok": False, "error": "Kite API key/secret not set - save Settings first"}), 400

    log = _make_log()
    # Whatever host the browser used to reach THIS request (localhost when
    # run locally, the VM's public IP when deployed remotely) - purely
    # cosmetic (see complete_login's docstring), so it bounces the user back
    # to wherever they actually are instead of always assuming localhost.
    dashboard_url = request.host_url

    def _do_login():
        socketio.emit("login_status", {"status": "waiting", "source": "auto"})
        try:
            client = KiteClient(settings, log=log)
            client.login(redirect_display_url=dashboard_url)
            with STATE_LOCK:
                STATE["kite_client"] = client
                STATE["logged_in"] = True
            socketio.emit("login_status", {"status": "success", "source": "auto"})
            _resume_existing_position_if_any(log)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI, never crash the server
            log(f"[app] login failed: {exc!r}")
            socketio.emit("login_status", {"status": "error", "message": str(exc), "source": "auto"})

    threading.Thread(target=_do_login, daemon=True).start()
    return jsonify({"ok": True, "message": "Login started - complete it in the browser tab that just opened."})


@app.route("/api/login/manual", methods=["POST"])
def login_manual():
    """
    Fallback for when the automatic local redirect-catcher (port 5000)
    never gets Kite's callback - usually because the Kite app's Redirect
    URL isn't registered as exactly http://127.0.0.1:5000/. The user
    completes login in the browser as usual, then pastes whatever URL/page
    they landed on (even an error page) here - we just need the
    request_token out of its query string.
    """
    with STATE_LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "engine is running - stop it before logging in again"}), 400

    settings = load_settings()
    if not settings.get("kite_api_key") or not settings.get("kite_api_secret"):
        return jsonify({"ok": False, "error": "Kite API key/secret not set - save Settings first"}), 400

    incoming = request.get_json(force=True, silent=True) or {}
    raw_input = str(incoming.get("token_input", ""))

    try:
        request_token = extract_request_token(raw_input)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    log = _make_log()

    def _do_manual_login():
        try:
            client = KiteClient(settings, log=log)
            client.complete_login(request_token)
            with STATE_LOCK:
                STATE["kite_client"] = client
                STATE["logged_in"] = True
            socketio.emit("login_status", {"status": "success", "source": "manual"})
            _resume_existing_position_if_any(log)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI, never crash the server
            log(f"[app] manual login failed: {exc!r}")
            socketio.emit("login_status", {"status": "error", "message": str(exc), "source": "manual"})

    threading.Thread(target=_do_manual_login, daemon=True).start()
    return jsonify({"ok": True, "message": "Verifying token..."})


@app.route("/api/openalgo_login", methods=["POST"])
def openalgo_login():
    """
    OpenAlgo login: just validates the API key against the running OpenAlgo
    server.  No browser, no redirect URL — the key either works or it doesn't.
    On success, the session is marked logged-in and position reconciliation
    runs exactly as it does after a Kite login.
    """
    with STATE_LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "engine is running — stop it before logging in again"}), 400

    settings = load_settings()
    if not settings.get("openalgo_api_key"):
        return jsonify({"ok": False, "error": "OpenAlgo API key not set — save Settings first"}), 400

    log = _make_log()

    def _do_oa_login():
        socketio.emit("login_status", {"status": "waiting", "source": "openalgo"})
        try:
            client = OpenAlgoClient(settings, log=log)
            client.login()
            with STATE_LOCK:
                STATE["kite_client"] = client
                STATE["logged_in"] = True
            socketio.emit("login_status", {"status": "success", "source": "openalgo"})
            _resume_existing_position_if_any(log)
        except Exception as exc:  # noqa: BLE001
            log(f"[app] OpenAlgo login failed: {exc!r}")
            socketio.emit("login_status", {"status": "error", "message": str(exc), "source": "openalgo"})

    threading.Thread(target=_do_oa_login, daemon=True).start()
    return jsonify({"ok": True, "message": "Connecting to OpenAlgo server..."})


def _make_event_relays():
    """Shared by /api/execute, /api/manual_trade, and /api/analyze - all
    relay the exact same engine.py event/tick shapes to the browser over
    WebSocket."""
    def on_event(evt):
        if evt.get("type") == "universe_ready":
            with STATE_LOCK:
                STATE["last_universe_event"] = evt
                _save_last_analysis_to_disk()
        elif evt.get("type") == "decision":
            with STATE_LOCK:
                STATE["last_decision_event"] = evt
                _save_last_analysis_to_disk()
        socketio.emit("engine_event", evt)

    def on_tick(tick):
        socketio.emit("tick", {
            "instrument_token": tick.get("instrument_token"),
            "last_price": tick.get("last_price"),
            "average_price": tick.get("average_price"),
            "oi": tick.get("oi"),
        })

    return on_event, on_tick


def _resume_existing_position_if_any(log):
    """
    Called right after every successful login (automatic and manual-token
    flows alike). If this process was just (re)started while a real
    position was already open on Zerodha - e.g. the app crashed or was
    restarted mid-trade - this reconciles from Zerodha's own real position
    data and resumes the exact same SL/target/time-exit monitoring run()
    uses, automatically, with no click needed: the position "just is what it
    is" on Zerodha's side regardless of this app's process lifecycle, same
    as it would be if you'd closed and reopened the Zerodha app itself. A
    fast no-op on the normal login where nothing matching is open.
    """
    with STATE_LOCK:
        if STATE["running"]:
            return
        STATE["running"] = True
        stop_event = threading.Event()
        STATE["stop_event"] = stop_event
        kite_client = STATE["kite_client"]

    on_event, on_tick = _make_event_relays()
    _spawn_engine_thread(
        lambda: engine.resume_existing_position(
            stop_event=stop_event, settings_path=SETTINGS_PATH, log=log,
            kite_client=kite_client, on_event=on_event, on_tick=on_tick,
        ),
        log,
    )


def _spawn_engine_thread(runnable, log):
    """Shared STATE/thread/socketio boilerplate for both the automated
    Execute run and the manual direction override - the only difference
    between them is which engine function `runnable` actually calls."""
    def _run():
        try:
            runnable()
        except Exception as exc:  # noqa: BLE001 - surface to UI, never crash the server process
            log(f"[app] engine run failed: {exc!r}")
            socketio.emit("engine_event", {"type": "error", "message": str(exc)})
        finally:
            with STATE_LOCK:
                STATE["running"] = False
                STATE["stop_event"] = None
            socketio.emit("engine_status", {"running": False})

    socketio.emit("engine_status", {"running": True})
    threading.Thread(target=_run, daemon=True).start()


@app.route("/api/execute", methods=["POST"])
def execute():
    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "please Login first"}), 400
        if STATE["running"]:
            return jsonify({"ok": False, "error": "engine is already running"}), 400
        STATE["running"] = True
        stop_event = threading.Event()
        STATE["stop_event"] = stop_event
        kite_client = STATE["kite_client"]

    log = _make_log()
    on_event, on_tick = _make_event_relays()

    _spawn_engine_thread(
        lambda: engine.run(
            stop_event=stop_event, settings_path=SETTINGS_PATH, log=log,
            kite_client=kite_client, on_event=on_event, on_tick=on_tick,
        ),
        log,
    )
    return jsonify({"ok": True})


@app.route("/api/manual_trade", methods=["POST"])
def manual_trade():
    """
    Manual override: the user picks the direction directly, skipping the
    automated analysis entirely. Still goes through engine.manual_run(),
    which uses the exact same order-placement and SL/target/time-exit/
    force-exit monitoring code as the automated Execute path - only how the
    direction was chosen differs, never the safety net around it.
    """
    incoming = request.get_json(force=True, silent=True) or {}
    direction = str(incoming.get("direction", "")).upper()
    if direction not in engine.VALID_MANUAL_DIRECTIONS:
        return jsonify({"ok": False, "error": "direction must be CALL, PUT, or BOTH"}), 400

    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "please Login first"}), 400
        if STATE["running"]:
            return jsonify({"ok": False, "error": "engine is already running"}), 400
        STATE["running"] = True
        stop_event = threading.Event()
        STATE["stop_event"] = stop_event
        kite_client = STATE["kite_client"]

    log = _make_log()
    on_event, on_tick = _make_event_relays()

    _spawn_engine_thread(
        lambda: engine.manual_run(
            direction, stop_event=stop_event, settings_path=SETTINGS_PATH, log=log,
            kite_client=kite_client, on_event=on_event, on_tick=on_tick,
        ),
        log,
    )
    return jsonify({"ok": True, "direction": direction})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """
    Runs the exact same analysis engine.run() itself uses (engine.analyze_only,
    sharing engine._run_analysis with it) and reports the decision - but
    NEVER places an order, in any mode (dry_run or LIVE). For watching what
    the system would decide without committing to a trade.
    """
    with STATE_LOCK:
        if not STATE["logged_in"] or STATE["kite_client"] is None:
            return jsonify({"ok": False, "error": "please Login first"}), 400
        if STATE["running"]:
            return jsonify({"ok": False, "error": "engine is already running"}), 400
        STATE["running"] = True
        STATE["stop_event"] = None  # nothing to interrupt - analysis-only has no monitoring loop
        kite_client = STATE["kite_client"]

    log = _make_log()
    on_event, on_tick = _make_event_relays()

    _spawn_engine_thread(
        lambda: engine.analyze_only(
            settings_path=SETTINGS_PATH, log=log, kite_client=kite_client, on_event=on_event, on_tick=on_tick,
        ),
        log,
    )
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    with STATE_LOCK:
        stop_event = STATE["stop_event"]
        running = STATE["running"]
    if not running or stop_event is None:
        return jsonify({"ok": False, "error": "engine is not running"}), 400
    stop_event.set()
    return jsonify({"ok": True, "message": "Stop signal sent - squaring off any open position now."})


@app.route("/api/status", methods=["GET"])
def status():
    with STATE_LOCK:
        result = {"logged_in": STATE["logged_in"], "running": STATE["running"]}
    result.update(restart_status())
    return jsonify(result)


@app.route("/api/last_analysis", methods=["GET"])
def last_analysis():
    """The last "universe_ready"/"decision" engine_event this server has
    seen (if any), so the dashboard can redisplay the same analysis on a
    page refresh instead of resetting to blank - see dashboard.js's
    loadLastAnalysis(). Read-only, no Kite/login needed - just whatever is
    already cached in memory from the last Execute/Analyze Only/manual
    entry since this server process started."""
    with STATE_LOCK:
        return jsonify({
            "universe": STATE["last_universe_event"],
            "decision": STATE["last_decision_event"],
        })


def _try_restore_session():
    """
    On startup, silently restore the logged-in session if possible:
    - Kite:     today's access_token cached in token.json (valid until ~6 AM next day).
    - OpenAlgo: just re-validate the API key (stateless — no token to cache).
    Never opens a browser or blocks. If restoration fails, Login works as normal.
    """
    settings = load_settings()
    provider = settings.get("execution_provider", "kite").lower()

    if provider == "openalgo":
        if not settings.get("openalgo_api_key"):
            return
        try:
            client = OpenAlgoClient(settings, log=print)
            client.login()
            with STATE_LOCK:
                STATE["kite_client"] = client
                STATE["logged_in"] = True
            print("[app] restored OpenAlgo session — no need to log in again")
            _resume_existing_position_if_any(print)
        except Exception as exc:  # noqa: BLE001
            print(f"[app] could not restore OpenAlgo session ({exc!r}) — Login will be needed")
        return

    # Kite path (original)
    if not settings.get("kite_api_key") or not settings.get("kite_api_secret"):
        return
    cached_token = load_cached_access_token()
    if not cached_token:
        return
    try:
        client = KiteClient(settings, log=print)
        client.kite.set_access_token(cached_token)
        with STATE_LOCK:
            STATE["kite_client"] = client
            STATE["logged_in"] = True
        print("[app] restored today's Kite session from token.json — no need to log in again")
        _resume_existing_position_if_any(print)
    except Exception as exc:  # noqa: BLE001
        print(f"[app] could not restore cached Kite session ({exc!r}) — Login will be needed")


if __name__ == "__main__":
    _try_restore_session()
    # HOST defaults to 127.0.0.1 (local-only, unreachable from outside this
    # machine - see module docstring). Set HOST=0.0.0.0 only when deploying
    # on a remote server (e.g. a cloud VM whose static IP is registered in
    # Kite's IP Whitelist for order placement) that you intend to reach from
    # elsewhere - then also lock down inbound access at the firewall/security
    # list level instead of relying on this app for that.
    host = os.environ.get("HOST", "127.0.0.1")
    local_url = f"http://127.0.0.1:{PORT}"
    if host == "127.0.0.1":
        threading.Timer(1.2, lambda: webbrowser.open(local_url)).start()
        print(f"[app] starting dashboard at {local_url}")
    else:
        print(f"[app] starting dashboard bound to {host}:{PORT} - "
              f"open http://<this-machine's-public-IP>:{PORT} from your browser")
    socketio.run(app, host=host, port=PORT)
