"""
Thin wrapper around the OpenAlgo Python SDK (pip install openalgo>=2.0.0).

Exposes the same surface as KiteClient so engine.py can use either broker
without any changes to the decision core (option_signal, ai_sentiment,
decision remain completely untouched — they are pure Python with no I/O).

Key differences vs KiteClient:
  - Login = API key validation only (no browser OAuth, no redirect URL).
  - KiteTicker is replaced by OpenAlgo WebSocket (subscribe_quote/ltp).
  - Order placement uses optionsorder / optionsmultiorder for ATM entries;
    placeorder is used for square-offs where the tradingsymbol is already known.
  - OI-change history uses client.history() (interval="D", source="api").
  - Option chain and ATM premium come from client.optionchain().
  - instrument_token is set to the tradingsymbol string (OpenAlgo has no
    numeric token concept — symbols are the canonical identifier).

Design goal: minimum surprise. Every method name / return shape that engine.py
relies on is reproduced exactly so the engine can be switched with a one-line
setting change and nothing else.
"""

import datetime
import threading
import time


# ------------------------------------------------------------------ helpers

def _kite_to_oa_exchange(exch):
    """
    Map KiteConnect exchange names to OpenAlgo exchange names.
    e.g. "NSE" -> "NSE", "NFO" -> "NFO", "NSE_INDEX" stays as-is.
    """
    mapping = {
        "NSE": "NSE",
        "BSE": "BSE",
        "NFO": "NFO",
        "BFO": "BFO",
        "MCX": "MCX",
        "CDS": "CDS",
        "NSE_INDEX": "NSE_INDEX",
        "BSE_INDEX": "BSE_INDEX",
    }
    return mapping.get(exch.upper(), exch.upper())


def _kite_interval_to_oa(interval):
    """
    Map KiteConnect interval strings to OpenAlgo interval strings.
    e.g. "minute" -> "1m", "5minute" -> "5m", "day" -> "D".
    """
    mapping = {
        "minute":    "1m",
        "3minute":   "3m",
        "5minute":   "5m",
        "10minute":  "10m",
        "15minute":  "15m",
        "30minute":  "30m",
        "60minute":  "1h",
        "day":       "D",
    }
    return mapping.get(interval, interval)


def _token_to_sym_exch(instrument_token):
    """
    When engine.py passes an instrument_token that we set to a string like
    "NIFTY 50" or "NIFTY10JUL25FUT" or "NIFTY10JUL2525000CE", figure out
    the best exchange guess.  Engine always passes index tokens as "NIFTY 50"
    style (NSE_INDEX), futures as "NIFTYddMMMyyFUT" (NFO), options as full
    tradingsymbol (NFO).
    """
    token = str(instrument_token)
    if token.endswith("FUT"):
        return token, "NFO"
    if token.endswith("CE") or token.endswith("PE"):
        return token, "NFO"
    # index spot symbol like "NIFTY 50" or "NIFTY BANK"
    return token, "NSE_INDEX"


_OA_INDEX_EXCHANGE = {
    "NIFTY":      "NSE_INDEX",
    "BANKNIFTY":  "NSE_INDEX",
    "FINNIFTY":   "NSE_INDEX",
    "MIDCPNIFTY": "NSE_INDEX",
    "SENSEX":     "BSE_INDEX",
}


def _parse_expiry_str(s):
    """Parse "10-JUL-25" or "10-JUL-2025" into a date."""
    for fmt in ("%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse expiry date: {s!r}")


def _expiry_to_oa_str(d):
    """Convert a date into OpenAlgo expiry string e.g. "10JUL25"."""
    return d.strftime("%d%b%y").upper()


# ------------------------------------------------------------------ client

class OpenAlgoClient:
    """
    Drop-in alternative to KiteClient, backed by the OpenAlgo SDK.

    Usage (identical to KiteClient):
        client = OpenAlgoClient(settings, log=print)
        client.login()          # validates API key — no browser needed
    """

    def __init__(self, settings, log=print):
        self.settings = settings
        self.log = log

        # Lazy import so the dependency is optional when running Kite mode.
        from openalgo import api as _OAApi  # noqa: N813
        self._oa = _OAApi(
            api_key=settings["openalgo_api_key"],
            host=settings.get("openalgo_host", "http://127.0.0.1:5000"),
        )

        # Thread-safe tick store — same design as KiteClient.
        self._tick_lock = threading.Lock()
        self._tick_store = {}        # key (symbol string) -> tick dict
        self._last_tick_time = None
        self._ticker_connected = threading.Event()
        self._ws_instruments = []    # held for unsubscribe on stop_ticker()

    # ---------------------------------------------------------------- auth

    def login(self, redirect_display_url=None):
        """
        Validate the OpenAlgo API key by calling funds().
        No browser, no redirect URL.
        redirect_display_url is accepted but ignored (KiteClient compat).
        """
        try:
            result = self._oa.funds()
            if isinstance(result, dict) and result.get("status") == "success":
                self.log("[openalgo_client] API key validated — connected to OpenAlgo")
                return
            raise RuntimeError(f"OpenAlgo funds() returned: {result}")
        except Exception as exc:
            raise RuntimeError(
                f"Cannot reach OpenAlgo at "
                f"{self.settings.get('openalgo_host', 'http://127.0.0.1:5000')} "
                f"or API key invalid: {exc}"
            ) from exc

    def complete_login(self, request_token):
        """Not used for OpenAlgo (no OAuth). Raises with a clear message."""
        raise NotImplementedError(
            "OpenAlgo does not use a request_token/OAuth flow. "
            "Set execution_provider='openalgo' and provide openalgo_api_key."
        )

    # --------------------------------------------------------- instruments

    def get_nfo_option_chain(self, name, today=None):
        """
        Returns (nearest_expiry_date, list_of_instrument_dicts) in exactly
        the same shape as KiteClient so engine.build_option_universe works
        without modification.

        Uses client.expiry() to find the nearest expiry, then
        client.optionchain() for the full chain. Instrument dicts are
        normalised to match the KiteConnect shape:
            tradingsymbol, instrument_type, strike, expiry,
            lot_size, instrument_token, exchange, name, _ltp, _oi
        """
        today = today or datetime.date.today()
        exchange_oa = _OA_INDEX_EXCHANGE.get(name, "NSE_INDEX")

        # -- nearest expiry --
        expiry_resp = self._oa.expiry(symbol=name, exchange="NFO", instrumenttype="options")
        if expiry_resp.get("status") != "success":
            raise ValueError(f"OpenAlgo expiry lookup failed for {name!r}: {expiry_resp}")

        all_expiries = [_parse_expiry_str(e) for e in expiry_resp.get("data", [])]
        future_expiries = sorted(e for e in all_expiries if e >= today)
        if not future_expiries:
            raise ValueError(f"No upcoming expiry found for {name!r} via OpenAlgo")
        nearest_expiry = future_expiries[0]
        expiry_str = _expiry_to_oa_str(nearest_expiry)

        # -- full option chain --
        chain_resp = self._oa.optionchain(
            underlying=name,
            exchange=exchange_oa,
            expiry_date=expiry_str,
        )
        if chain_resp.get("status") != "success":
            raise ValueError(
                f"OpenAlgo optionchain failed for {name!r} {expiry_str}: {chain_resp}"
            )

        instruments = []
        for row in chain_resp.get("chain", []):
            strike = float(row["strike"])
            for opt_key, opt_type in (("ce", "CE"), ("pe", "PE")):
                leg = row.get(opt_key)
                if not leg:
                    continue
                instruments.append({
                    "tradingsymbol":    leg["symbol"],
                    "instrument_type":  opt_type,
                    "strike":           strike,
                    "expiry":           nearest_expiry,
                    "lot_size":         leg.get("lotsize", 75),
                    "instrument_token": leg["symbol"],   # symbol IS the token in OpenAlgo
                    "exchange":         "NFO",
                    "name":             name,
                    "_ltp":             leg.get("ltp"),  # immediate premium — no extra quote() needed
                    "_oi":              leg.get("oi", 0),
                })

        if not instruments:
            raise ValueError(f"OpenAlgo returned an empty chain for {name!r} {expiry_str}")

        return nearest_expiry, instruments

    def get_index_instrument(self, tradingsymbol, exchange="NSE"):
        """
        Returns a minimal instrument dict for the index spot symbol.
        instrument_token = tradingsymbol (OpenAlgo has no numeric token).
        """
        return {
            "tradingsymbol":    tradingsymbol,
            "instrument_token": tradingsymbol,
            "exchange":         exchange,
        }

    def get_nfo_futures_instrument(self, name, today=None):
        """
        Nearest-expiry NFO futures contract — used as VWAP proxy in engine.py.
        """
        today = today or datetime.date.today()
        expiry_resp = self._oa.expiry(symbol=name, exchange="NFO", instrumenttype="futures")
        if expiry_resp.get("status") != "success":
            raise ValueError(f"OpenAlgo futures expiry failed for {name!r}: {expiry_resp}")

        all_expiries = [_parse_expiry_str(e) for e in expiry_resp.get("data", [])]
        future_expiries = sorted(e for e in all_expiries if e >= today)
        if not future_expiries:
            raise ValueError(f"No upcoming futures expiry for {name!r}")
        nearest_expiry = future_expiries[0]
        expiry_str = _expiry_to_oa_str(nearest_expiry)
        futures_sym = f"{name}{expiry_str}FUT"   # e.g. "NIFTY10JUL25FUT"

        return {
            "tradingsymbol":    futures_sym,
            "instrument_token": futures_sym,
            "expiry":           nearest_expiry,
            "exchange":         "NFO",
            "name":             name,
        }

    def get_quote(self, exchange_tradingsymbols):
        """
        Accepts a list of "EXCH:TRADINGSYMBOL" strings (KiteConnect style)
        and returns a dict keyed the same way with at minimum last_price,
        ohlc, oi, volume — same shape as KiteConnect quote().

        average_price is synthesised as (open + last_price) / 2 for traded
        instruments when the broker does not return a real VWAP figure.
        This is a reasonable intraday proxy — engine.get_vwap() uses it as
        the VWAP source for the NIFTY futures contract, and a midpoint
        between today's open and the current price tracks the running VWAP
        closely enough for the directional signal it drives.  It is always
        None for index symbols (NIFTY 50, INDIA VIX, etc.) which have no
        volume and therefore no meaningful average price — engine.get_vwap()
        only calls this on the futures contract, never on the index itself,
        so this is correct behaviour.
        """
        result = {}
        for key in exchange_tradingsymbols:
            exch, sym = key.split(":", 1)
            exch_oa = _kite_to_oa_exchange(exch)
            try:
                resp = self._oa.quotes(symbol=sym, exchange=exch_oa)
                if resp.get("status") == "success":
                    d = resp["data"]
                    ltp   = d.get("ltp")
                    open_ = d.get("open")
                    vol   = d.get("volume", 0)
                    # Fallback: some broker plugins don't populate open/high/low
                    # in the quotes response for index symbols (NSE_INDEX).
                    # depth() reliably returns these fields — use it as a
                    # one-time top-up when open is missing or zero.
                    if not open_:
                        try:
                            dep = self._oa.depth(symbol=sym, exchange=exch_oa)
                            if dep.get("status") == "success":
                                dd = dep["data"]
                                open_val = dd.get("open")
                                if open_val:
                                    open_ = open_val
                                ltp = ltp or dd.get("ltp")
                        except Exception:  # noqa: BLE001
                            pass
                    # Synthesise average_price for traded instruments only.
                    # Indices (VIX, NIFTY 50 …) have no volume so average_price
                    # stays None — engine.get_vwap() only reads it on futures.
                    if ltp is not None and open_ is not None and vol:
                        avg_price = (open_ + ltp) / 2
                    else:
                        avg_price = None
                    result[key] = {
                        "last_price":    ltp,
                        "average_price": avg_price,
                        "ohlc": {
                            "open":  open_,
                            "high":  d.get("high"),
                            "low":   d.get("low"),
                            # OpenAlgo quotes returns prev_close for yesterday's
                            # closing price — map to "close" so engine.py's
                            # ohlc["close"] (yesterday's close for gap check) works.
                            "close": d.get("prev_close"),
                        },
                        "oi":     d.get("oi", 0),
                        "volume": vol,
                    }
                else:
                    self.log(f"[openalgo_client] quotes failed for {key}: {resp}")
                    result[key] = {"last_price": None, "average_price": None,
                                   "ohlc": {}, "oi": 0, "volume": 0}
            except Exception as exc:  # noqa: BLE001
                self.log(f"[openalgo_client] quote error for {key}: {exc!r}")
                result[key] = {"last_price": None, "average_price": None,
                               "ohlc": {}, "oi": 0, "volume": 0}
        return result

    def get_daily_history(self, instrument_token, from_date, to_date, oi=False):
        """
        Returns a list of OHLC candle dicts like KiteConnect historical_data().
        instrument_token is the tradingsymbol string for OpenAlgo.
        """
        sym, exch = _token_to_sym_exch(instrument_token)
        try:
            df = self._oa.history(
                symbol=sym,
                exchange=exch,
                interval="D",
                start_date=_date_str(from_date),
                end_date=_date_str(to_date),
                source="api",
            )
            return _df_to_candles(df, include_oi=oi)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[openalgo_client] daily history error for {instrument_token}: {exc!r}")
            return []

    def get_intraday_history(self, instrument_token, from_dt, to_dt, interval="5minute"):
        """
        Returns sub-day candles. KiteConnect interval names are translated
        to OpenAlgo format ("5minute" -> "5m" etc.).
        """
        sym, exch = _token_to_sym_exch(instrument_token)
        oa_interval = _kite_interval_to_oa(interval)
        try:
            df = self._oa.history(
                symbol=sym,
                exchange=exch,
                interval=oa_interval,
                start_date=_date_str(from_dt),
                end_date=_date_str(to_dt),
                source="api",
            )
            return _df_to_candles(df)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[openalgo_client] intraday history error for {instrument_token}: {exc!r}")
            return []

    # -------------------------------------------------------------- ticker

    def start_ticker(self, instrument_tokens, token_meta=None, on_tick=None):
        """
        Subscribes to OpenAlgo's WebSocket quote feed.

        instrument_tokens is a list of tradingsymbol strings (since we set
        instrument_token = tradingsymbol for OpenAlgo instruments).

        The on_tick callback receives a dict matching KiteTicker shape:
            {"instrument_token": ..., "last_price": ..., "oi": ...,
             "average_price": ..., "volume_traded": ...}
        """
        self._ws_instruments = []
        for token in instrument_tokens:
            sym, exch = _token_to_sym_exch(token)
            self._ws_instruments.append({"exchange": exch, "symbol": sym})

        def _on_data(data):
            sym  = data.get("symbol") or data.get("tradingsymbol", "")
            exch = data.get("exchange", "")
            ltp   = data.get("ltp") or data.get("last_price")
            open_ = data.get("open")
            vol   = data.get("volume", 0)
            # Synthesise average_price for traded instruments (same logic as get_quote).
            if ltp is not None and open_ is not None and vol:
                avg_price = (open_ + ltp) / 2
            else:
                avg_price = None
            tick = {
                "instrument_token": sym,
                "last_price":       ltp,
                "average_price":    avg_price,
                "oi":               data.get("oi", 0),
                "volume_traded":    vol,
            }
            composite_key = f"{exch}:{sym}" if exch else sym
            with self._tick_lock:
                self._tick_store[sym]           = tick
                self._tick_store[composite_key] = tick
                self._last_tick_time = time.time()
            if on_tick is not None:
                try:
                    on_tick(tick)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"[openalgo_client] on_tick callback raised {exc!r} (ignored)")

        def _connect():
            try:
                self._oa.connect()
                # Subscribe all instruments for quote updates (ltp + oi for options).
                self._oa.subscribe_quote(self._ws_instruments, on_data_received=_on_data)

                # Also subscribe index instruments for LTP-only updates — some
                # broker plugins only stream NSE_INDEX symbols via subscribe_ltp,
                # not subscribe_quote. Subscribing both ensures the spot LTP
                # always arrives regardless of broker plugin behaviour.
                index_instruments = [
                    i for i in self._ws_instruments if i["exchange"] in ("NSE_INDEX", "BSE_INDEX")
                ]
                if index_instruments:
                    try:
                        self._oa.subscribe_ltp(index_instruments, on_data_received=_on_data)
                    except Exception:  # noqa: BLE001 — not all SDK versions support subscribe_ltp
                        pass

                self._ticker_connected.set()
                self.log(
                    f"[openalgo_client] WebSocket connected, subscribed "
                    f"{len(self._ws_instruments)} instruments"
                )
            except Exception as exc:  # noqa: BLE001
                self.log(
                    f"[openalgo_client] WebSocket connect failed ({exc!r}) — "
                    f"live prices will rely on REST quote() polling only"
                )
                self._ticker_connected.set()   # unblock wait_for_ticker even on failure

        threading.Thread(target=_connect, daemon=True).start()

    def wait_for_ticker(self, timeout=10):
        return self._ticker_connected.wait(timeout=timeout)

    def seconds_since_last_tick(self):
        if self._last_tick_time is None:
            return None
        return time.time() - self._last_tick_time

    def stop_ticker(self):
        try:
            if self._ws_instruments:
                self._oa.unsubscribe_quote(self._ws_instruments)
                index_instruments = [
                    i for i in self._ws_instruments if i["exchange"] in ("NSE_INDEX", "BSE_INDEX")
                ]
                if index_instruments:
                    try:
                        self._oa.unsubscribe_ltp(index_instruments)
                    except Exception:  # noqa: BLE001
                        pass
            self._oa.disconnect()
        except Exception:  # noqa: BLE001
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
        Direct market order by resolved tradingsymbol — used for square-offs
        (SELL) and as the fallback entry path.

        transaction_type: "BUY" or "SELL" (or KiteConnect constant strings,
        which are also "BUY"/"SELL").
        """
        action = str(transaction_type).upper()
        exch_oa = _kite_to_oa_exchange(exchange)

        if self.settings.get("dry_run", True):
            self.log(
                f"[DRY_RUN/openalgo] would placeorder: {action} {quantity} x "
                f"{tradingsymbol} ({exch_oa}) MARKET"
            )
            return {
                "dry_run": True,
                "tradingsymbol":    tradingsymbol,
                "transaction_type": action,
                "quantity":         quantity,
            }

        resp = self._oa.placeorder(
            strategy="AlgoAI",
            symbol=tradingsymbol,
            action=action,
            exchange=exch_oa,
            price_type="MARKET",
            product="NRML",
            quantity=quantity,
        )
        if resp.get("status") != "success":
            raise RuntimeError(f"OpenAlgo placeorder failed: {resp}")
        order_id = resp.get("orderid")
        self.log(
            f"[openalgo_client] LIVE order: {action} {quantity} x "
            f"{tradingsymbol} -> order_id={order_id}"
        )
        return {
            "dry_run": False, "order_id": order_id,
            "tradingsymbol": tradingsymbol, "transaction_type": action, "quantity": quantity,
        }

    def place_options_order(self, underlying, exchange_oa, expiry_str, option_type, quantity):
        """
        High-level ATM options entry via client.optionsorder().
        Used by enter_positions() in engine.py for the OpenAlgo path.
        Returns the full OpenAlgo response dict (includes resolved 'symbol').
        """
        action = "BUY"
        if self.settings.get("dry_run", True):
            self.log(
                f"[DRY_RUN/openalgo] would optionsorder: {underlying} {expiry_str} "
                f"ATM {option_type} {action} qty={quantity}"
            )
            return {
                "dry_run": True, "symbol": f"{underlying}ATM{option_type}(dry)",
                "underlying": underlying, "option_type": option_type, "quantity": quantity,
            }

        resp = self._oa.optionsorder(
            strategy="AlgoAI",
            underlying=underlying,
            exchange=exchange_oa,
            expiry_date=expiry_str,
            offset="ATM",
            option_type=option_type,
            action=action,
            quantity=quantity,
            pricetype="MARKET",
            product="NRML",
        )
        if resp.get("status") != "success":
            raise RuntimeError(f"OpenAlgo optionsorder failed: {resp}")
        self.log(
            f"[openalgo_client] LIVE optionsorder: {underlying} {expiry_str} ATM "
            f"{option_type} -> symbol={resp.get('symbol')} order_id={resp.get('orderid')}"
        )
        return resp

    def place_options_straddle(self, underlying, exchange_oa, expiry_str, quantity):
        """
        Places ATM CALL + PUT simultaneously via optionsmultiorder (straddle).
        Returns the full OpenAlgo response dict.
        """
        if self.settings.get("dry_run", True):
            self.log(
                f"[DRY_RUN/openalgo] would optionsmultiorder straddle: "
                f"{underlying} {expiry_str} ATM CALL+PUT qty={quantity} each"
            )
            return {
                "dry_run": True, "underlying": underlying,
                "quantity": quantity, "results": [],
            }

        resp = self._oa.optionsmultiorder(
            strategy="AlgoAI",
            underlying=underlying,
            exchange=exchange_oa,
            expiry_date=expiry_str,
            legs=[
                {"offset": "ATM", "option_type": "CE", "action": "BUY", "quantity": quantity},
                {"offset": "ATM", "option_type": "PE", "action": "BUY", "quantity": quantity},
            ],
        )
        if resp.get("status") != "success":
            raise RuntimeError(f"OpenAlgo optionsmultiorder straddle failed: {resp}")
        symbols = [r.get("symbol") for r in resp.get("results", [])]
        self.log(
            f"[openalgo_client] LIVE straddle: {underlying} {expiry_str} ATM -> {symbols}"
        )
        return resp

    def get_actual_position_qty(self, tradingsymbol, exchange="NFO", product="MIS", expected_qty=None):
        """
        Real live position quantity check — mirrors KiteClient's safety check
        before every square-off. Falls back to expected_qty in dry_run.
        """
        if self.settings.get("dry_run", True):
            return expected_qty

        resp = self._oa.openposition(
            strategy="AlgoAI",
            symbol=tradingsymbol,
            exchange=_kite_to_oa_exchange(exchange),
            product=product,
        )
        if resp.get("status") != "success":
            self.log(
                f"[openalgo_client] openposition check failed for {tradingsymbol}: {resp} "
                f"— using expected_qty={expected_qty}"
            )
            return expected_qty
        try:
            qty = int(resp.get("quantity", 0))
        except (TypeError, ValueError):
            qty = expected_qty
        return qty


# ------------------------------------------------------------------ utils

def _date_str(d):
    """Accept date, datetime, or string — return ISO date string."""
    if hasattr(d, "isoformat"):
        return d.date().isoformat() if hasattr(d, "date") and callable(d.date) else d.isoformat()
    return str(d)[:10]


def _df_to_candles(df, include_oi=False):
    """Convert a pandas DataFrame from client.history() to a list of candle dicts."""
    if df is None:
        return []
    try:
        if hasattr(df, "empty") and df.empty:
            return []
        candles = []
        for ts, row in df.iterrows():
            c = {
                "date":   ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": int(row.get("volume", 0)),
            }
            if include_oi and "oi" in row:
                c["oi"] = int(row["oi"])
            candles.append(c)
        return candles
    except Exception:  # noqa: BLE001
        return []
