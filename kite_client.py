"""
Thin, proxy-aware wrapper around KiteConnect + KiteTicker.

Responsibilities:
  - login (browser + local redirect catcher), token.json caching per-day
  - instrument lookup (tradingsymbol + LIVE lot_size, nearest expiry) - never hardcoded
  - live tick store (thread-safe) fed by KiteTicker, so the rest of the
    system reads prices/OI from memory instead of polling REST in a loop
  - order placement, hard-gated by settings["dry_run"]
"""

import datetime
import json
import os
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from kiteconnect import KiteConnect, KiteTicker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")  # absolute - never dependent on the process's cwd
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 5000
REDIRECT_URL = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/"
LOGIN_TIMEOUT_SECONDS = 240


def load_cached_access_token():
    """
    Return today's cached Kite access_token from token.json, or None if it's
    missing or from a previous day. This is what lets a login stay valid for
    the whole trading day even if the app itself is restarted in between -
    Kite's own tokens are valid until ~6 AM the next day regardless of
    whether this process keeps running.
    """
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("date") != _today_str():
        return None
    return data.get("access_token")


def _today_str():
    return datetime.date.today().isoformat()


def _build_proxies(proxy_settings):
    if not proxy_settings or not proxy_settings.get("enabled"):
        return {}
    host = proxy_settings.get("host", "")
    port = proxy_settings.get("port", "")
    if not host or not port:
        return {}
    user = proxy_settings.get("user", "")
    pwd = proxy_settings.get("pass", "")
    auth = f"{user}:{pwd}@" if user else ""
    proxy_url = f"http://{auth}{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


class _RedirectCatcher(BaseHTTPRequestHandler):
    """Tiny local HTTP server that catches Kite's postback redirect and
    pulls request_token out of the query string."""

    captured_token = None
    captured_error = None
    redirect_display_url = None  # optional: send the user back to the dashboard tab

    def do_GET(self):  # noqa: N802 - required name by BaseHTTPRequestHandler
        query = parse_qs(urlparse(self.path).query)
        token = query.get("request_token", [None])[0]
        bounce = ""
        if _RedirectCatcher.redirect_display_url:
            bounce = (
                f'<meta http-equiv="refresh" content="2;url={_RedirectCatcher.redirect_display_url}">'
                f'<script>setTimeout(function(){{location.href="{_RedirectCatcher.redirect_display_url}";}}, 2000);</script>'
            )
        if token:
            _RedirectCatcher.captured_token = token
            body = (
                f"<html><head>{bounce}</head><body>"
                f"<h3>Login captured - redirecting you back to the dashboard...</h3>"
                f"<p>If it doesn't redirect automatically, go back to the dashboard tab yourself.</p>"
                f"</body></html>"
            ).encode()
            self.send_response(200)
        else:
            status = query.get("status", [None])[0]
            reason = query.get("message", [None])[0] or status or "no request_token in redirect"
            _RedirectCatcher.captured_error = reason
            body = (
                f"<html><head>{bounce}</head><body>"
                f"<h3>Kite login did not return a request_token ({reason}).</h3>"
                f"<p>Go back to the dashboard tab and try Login again.</p>"
                f"</body></html>"
            ).encode()
            self.send_response(400)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - silence default stderr logging
        pass


def _capture_request_token(timeout_seconds=LOGIN_TIMEOUT_SECONDS, redirect_display_url=None):
    _RedirectCatcher.captured_token = None
    _RedirectCatcher.captured_error = None
    _RedirectCatcher.redirect_display_url = redirect_display_url

    try:
        server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _RedirectCatcher)
    except OSError as exc:
        raise RuntimeError(
            f"Could not start the local login-redirect listener on {REDIRECT_URL} "
            f"(port {REDIRECT_PORT} may already be in use by another program): {exc}"
        ) from exc

    server.timeout = 1
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout_seconds)

    while _RedirectCatcher.captured_token is None and datetime.datetime.now() < deadline:
        server.handle_request()  # blocks up to server.timeout seconds per call
        if _RedirectCatcher.captured_error and _RedirectCatcher.captured_token is None:
            break

    server.server_close()

    if _RedirectCatcher.captured_error and _RedirectCatcher.captured_token is None:
        raise RuntimeError(f"Kite login failed: {_RedirectCatcher.captured_error}")

    if _RedirectCatcher.captured_token is None:
        raise TimeoutError(
            f"Login timed out after {timeout_seconds}s waiting for Kite's redirect on {REDIRECT_URL} - "
            f"double check your Kite app's Redirect URL is set to EXACTLY this address "
            f"(including the :5000 port and trailing slash) in the Kite developer console."
        )
    return _RedirectCatcher.captured_token


_TOKEN_RE = re.compile(r"request_token=([^&\s]+)")


def extract_request_token(raw_input):
    """
    Pull a request_token out of whatever the user pastes: the full (even if
    'can't be reached') redirect URL Kite sent them to, or just the bare
    token itself. This is the manual fallback for when the automatic local
    redirect-catcher can't be reached - usually because the Kite app's
    Redirect URL isn't registered as EXACTLY http://127.0.0.1:5000/.
    """
    raw_input = (raw_input or "").strip()
    if not raw_input:
        raise ValueError("Paste the redirect URL or request_token first.")

    match = _TOKEN_RE.search(raw_input)
    if match:
        return unquote(match.group(1))

    if raw_input.startswith("http") or " " in raw_input or "?" in raw_input:
        raise ValueError("Could not find a request_token in what you pasted - "
                          "paste the full URL from your browser's address bar after login, "
                          "or just the request_token value itself.")
    return raw_input


class KiteClient:
    def __init__(self, settings, log=print):
        self.settings = settings
        self.log = log
        self.proxies = _build_proxies(settings.get("proxy"))

        try:
            self.kite = KiteConnect(api_key=settings["kite_api_key"], proxies=self.proxies)
        except TypeError:
            # Installed kiteconnect version doesn't accept `proxies=` in the
            # constructor - fall back to setting it on the underlying session.
            self.kite = KiteConnect(api_key=settings["kite_api_key"])
            if self.proxies:
                self.kite.reqsession.proxies.update(self.proxies)

        self.ticker = None
        self._tick_lock = threading.Lock()
        self._tick_store = {}  # instrument_token -> latest tick dict
        self._token_meta = {}  # instrument_token -> {"tradingsymbol", "strike", "type", "lot_size"}
        self._ticker_connected = threading.Event()
        self._last_tick_time = None  # wall-clock time.time() of the most recent tick received (any instrument)

    # ---------------------------------------------------------------- auth

    def login(self, redirect_display_url=None):
        """Reuse today's cached access_token if present, else do a full
        browser-based login and cache the new token.

        redirect_display_url: optional URL (e.g. the web dashboard's own
        address) to bounce the user back to once Kite's redirect is caught -
        purely cosmetic, so they land back on the dashboard tab instead of
        staring at a bare confirmation page."""
        cached = self._load_cached_token()
        if cached:
            self.kite.set_access_token(cached)
            self.log(f"[kite_client] reused cached access_token from {TOKEN_FILE}")
            return

        login_url = self.kite.login_url()
        self.log(f"[kite_client] opening browser for login: {login_url}")
        webbrowser.open(login_url)

        self.log(
            f"[kite_client] waiting up to {LOGIN_TIMEOUT_SECONDS}s for Kite's redirect on {REDIRECT_URL} "
            f"- IMPORTANT: your Kite app's Redirect URL (in the Kite developer console) must be set to "
            f"EXACTLY {REDIRECT_URL} (including the :5000 port and trailing slash), or the browser will "
            f"land on the wrong address and show 'connection refused'."
        )
        request_token = _capture_request_token(redirect_display_url=redirect_display_url)
        self.complete_login(request_token)

    def complete_login(self, request_token):
        """
        Exchange a request_token for an access_token and cache it. This is
        the shared final step for both the automatic flow (login() above,
        which catches the token itself via the local redirect listener) and
        the manual fallback (the web UI's "paste redirect URL/token" box,
        for when the automatic listener can't be reached - e.g. the Kite
        app's Redirect URL isn't registered as exactly http://127.0.0.1:5000/).
        """
        data = self.kite.generate_session(request_token, api_secret=self.settings["kite_api_secret"])
        access_token = data["access_token"]
        self.kite.set_access_token(access_token)
        self._save_token(access_token)
        self.log("[kite_client] login successful, access_token cached")

    def _load_cached_token(self):
        return load_cached_access_token()

    def _save_token(self, access_token):
        with open(TOKEN_FILE, "w") as f:
            json.dump({"access_token": access_token, "date": _today_str()}, f)

    # --------------------------------------------------------- instruments

    def get_nfo_option_chain(self, name, today=None):
        """
        Returns (nearest_expiry, list_of_instrument_dicts) for the given
        index `name` (e.g. "NIFTY"), CE/PE only, nearest expiry >= today.
        Always fetched live from Kite - tradingsymbol and lot_size are never
        hardcoded, since NSE revises lot sizes periodically.
        """
        today = today or datetime.date.today()
        instruments = self.kite.instruments("NFO")
        options = [
            i
            for i in instruments
            if i["name"] == name and i["instrument_type"] in ("CE", "PE") and i["expiry"] >= today
        ]
        if not options:
            raise ValueError(f"no NFO options found for {name!r}")

        nearest_expiry = min(i["expiry"] for i in options)
        chain = [i for i in options if i["expiry"] == nearest_expiry]
        return nearest_expiry, chain

    def get_index_instrument(self, tradingsymbol, exchange="NSE"):
        instruments = self.kite.instruments(exchange)
        for i in instruments:
            if i["tradingsymbol"] == tradingsymbol:
                return i
        raise ValueError(f"instrument {tradingsymbol!r} not found on {exchange}")

    def get_nfo_futures_instrument(self, name, today=None):
        """
        Nearest-expiry NFO FUTURES instrument for the given index `name`
        (e.g. "NIFTY") - confirmed via Kite's own docs that INDEX tick
        packets never carry average_price/volume (indices aren't traded),
        so the index itself can never support a real VWAP. The futures
        contract IS a genuinely traded instrument (real volume, real
        average_price) and tracks the index closely intraday, making it the
        standard real-data proxy (see engine.get_vwap). Always fetched
        live, same as get_nfo_option_chain - tradingsymbol/instrument_token
        are never hardcoded since NFO revises contracts every expiry.
        """
        today = today or datetime.date.today()
        instruments = self.kite.instruments("NFO")
        futures = [
            i for i in instruments
            if i["name"] == name and i["instrument_type"] == "FUT" and i["expiry"] >= today
        ]
        if not futures:
            raise ValueError(f"no NFO futures found for {name!r}")

        nearest_expiry = min(i["expiry"] for i in futures)
        return next(i for i in futures if i["expiry"] == nearest_expiry)

    def get_quote(self, exchange_tradingsymbols):
        """One-off REST quote (list of "EXCH:TRADINGSYMBOL" strings). Used
        only for the initial snapshot before the ticker is streaming, or as
        a fallback if a specific token's tick hasn't arrived yet."""
        return self.kite.quote(exchange_tradingsymbols)

    def get_daily_history(self, instrument_token, from_date, to_date, oi=False):
        """
        oi=True adds an "oi" field (closing open interest) to each returned
        candle - confirmed via the installed kiteconnect SDK's own
        historical_data()/_format_historical() (only F&O instruments carry
        a meaningful value; the API silently returns none for equities/
        indices, same as it always has). Used to compare an option's
        current OI against yesterday's close (fresh build-up vs unwinding).
        """
        return self.kite.historical_data(instrument_token, from_date, to_date, interval="day", oi=oi)

    def get_intraday_history(self, instrument_token, from_dt, to_dt, interval="5minute"):
        """Same idea as get_daily_history but for sub-day candles (e.g. today's
        5-minute candles from market open till now) - used to backfill the
        dashboard's live chart with real history instead of starting blank."""
        return self.kite.historical_data(instrument_token, from_dt, to_dt, interval=interval)

    # -------------------------------------------------------------- ticker

    def start_ticker(self, instrument_tokens, token_meta=None, on_tick=None):
        """Connect KiteTicker in the background and subscribe in FULL mode
        (LTP + depth + OI). All subsequent price/OI reads should come from
        get_tick()/get_all_ticks(), not repeated REST polling.

        `on_tick`, if given, is called once per raw tick dict (e.g. to push
        it to a UI over WebSocket) IN ADDITION TO storing it - it is best-
        effort and sandboxed: any exception it raises is caught and logged,
        never allowed to affect the tick store or the trading logic."""
        self._token_meta = token_meta or {}

        # KiteTicker auto-reconnects by default (reconnect=True), with an
        # exponential backoff up to reconnect_max_delay and up to
        # reconnect_max_tries attempts, and auto-resubscribes on reconnect -
        # this is the underlying library's own behavior, not something we
        # implement here. What we DO need is visibility into it: without
        # logging on_reconnect/on_noreconnect, a mid-session disconnect would
        # be invisible until someone noticed prices had stopped moving.
        self.ticker = KiteTicker(
            self.settings["kite_api_key"], self.kite.access_token,
            reconnect=True, reconnect_max_delay=15, reconnect_max_tries=100,
        )

        def on_ticks(ws, ticks):
            with self._tick_lock:
                for t in ticks:
                    self._tick_store[t["instrument_token"]] = t
                self._last_tick_time = time.time()
            if on_tick is not None:
                for t in ticks:
                    try:
                        on_tick(t)
                    except Exception as exc:  # noqa: BLE001 - UI hook must never break the ticker
                        self.log(f"[kite_client] on_tick callback raised {exc!r} (ignored)")

        def on_connect(ws, response):
            ws.subscribe(instrument_tokens)
            ws.set_mode(ws.MODE_FULL, instrument_tokens)
            self._ticker_connected.set()
            self.log(f"[kite_client] ticker connected, subscribed {len(instrument_tokens)} instruments")

        def on_close(ws, code, reason):
            self.log(f"[kite_client] ticker closed: {code} {reason}")

        def on_error(ws, code, reason):
            self.log(f"[kite_client] ticker error: {code} {reason}")

        def on_reconnect(ws, attempts_count):
            self.log(f"[kite_client] ticker RECONNECTING (attempt {attempts_count}) - "
                      f"live prices may be briefly stale until this succeeds")

        def on_noreconnect(ws):
            self.log("[kite_client] ticker gave up reconnecting after repeated failures - "
                     "live prices will NOT update until Execute is run again. "
                     "Check your internet connection.")

        self.ticker.on_ticks = on_ticks
        self.ticker.on_connect = on_connect
        self.ticker.on_close = on_close
        self.ticker.on_error = on_error
        self.ticker.on_reconnect = on_reconnect
        self.ticker.on_noreconnect = on_noreconnect

        self.ticker.connect(threaded=True)

    def wait_for_ticker(self, timeout=10):
        return self._ticker_connected.wait(timeout=timeout)

    def seconds_since_last_tick(self):
        """None if no tick has ever been received yet; otherwise how long
        ago (in seconds) the most recent tick (any instrument) arrived.
        Used to detect a stale/stalled live feed while positions are open."""
        if self._last_tick_time is None:
            return None
        return time.time() - self._last_tick_time

    def stop_ticker(self):
        if self.ticker is not None:
            try:
                self.ticker.close()
            except Exception:  # noqa: BLE001 - best-effort shutdown
                pass

    def get_tick(self, instrument_token):
        with self._tick_lock:
            return self._tick_store.get(instrument_token)

    def get_all_ticks(self):
        with self._tick_lock:
            return dict(self._tick_store)

    # -------------------------------------------------------------- orders

    def place_market_order(self, tradingsymbol, exchange, transaction_type, quantity):
        """
        variety=REGULAR, product=MIS, order_type=MARKET.
        transaction_type: kite.TRANSACTION_TYPE_BUY or _SELL.
        Hard-gated by dry_run - if dry_run is true, NOTHING is sent to Kite,
        only printed.

        market_protection=MARKET_PROTECTION_AUTO (-1): NSE requires every
        MARKET (and SL-M) order to carry a market-protection band, capping
        how far the fill can move from the last traded price before the
        order is rejected instead of filling at an extreme/illiquid price -
        this is the exchange's own circuit-breaker for market orders, not
        something this project invents. -1 asks Kite to apply its own
        automatic protection band rather than a custom percentage. Without
        this, Kite's API rejects the order outright with "Market orders
        without market protection are not allowed via API" - confirmed
        against this installed kiteconnect SDK version, which accepts the
        parameter but does not default it for you.
        """
        if self.settings.get("dry_run", True):
            self.log(
                f"[DRY_RUN] would place order: {transaction_type} {quantity} x {tradingsymbol} "
                f"({exchange}) MARKET/MIS/REGULAR (market_protection=auto)"
            )
            return {"dry_run": True, "tradingsymbol": tradingsymbol, "transaction_type": transaction_type, "quantity": quantity}

        order_id = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET,
            market_protection=self.kite.MARKET_PROTECTION_AUTO,
        )
        self.log(f"[kite_client] LIVE order placed: {transaction_type} {quantity} x {tradingsymbol} -> order_id={order_id}")
        return {"dry_run": False, "order_id": order_id, "tradingsymbol": tradingsymbol, "transaction_type": transaction_type, "quantity": quantity}

    def get_actual_position_qty(self, tradingsymbol, exchange="NFO", product="MIS", expected_qty=None):
        """
        Real safety check used before every square-off SELL (see
        engine._square_off_one): asks Zerodha's own positions() API what net
        quantity is ACTUALLY still held for this tradingsymbol right now,
        instead of blindly trusting this project's in-memory record - which
        could be stale if the position was already exited manually (e.g. via
        the Zerodha app/website) outside this project. Placing a SELL for a
        position that's already flat would open a brand-new, unintended
        SHORT position instead of a harmless no-op, which this check exists
        specifically to prevent.

        Returns the real net quantity (positive = still long, 0 = flat,
        negative = net short - confirmed field meaning per Kite Connect's
        official /portfolio/positions docs). In dry_run, there's no real
        Zerodha position to check against, so this trusts the given
        expected_qty as-is (dry_run is pure simulation, never real state).
        """
        if self.settings.get("dry_run", True):
            return expected_qty

        positions = self.kite.positions()
        for p in positions.get("net", []):
            if (p.get("tradingsymbol") == tradingsymbol and p.get("exchange") == exchange
                    and p.get("product") == product):
                return p.get("quantity", 0)
        return 0
