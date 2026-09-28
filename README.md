# NIFTY Weekly Options - Semi-Automated Intraday BUYING Engine

A local, single-user tool for semi-automated intraday BUYING of NIFTY weekly
options via Zerodha Kite Connect, with a web dashboard on top. Everything
runs on your own machine - nothing is hosted, nothing leaves your computer
except calls to Kite's own API and your chosen AI provider's API.

## ⚠️ Disclaimer

This is **not a guaranteed-profit system**. It is a tool that automates order
placement and monitoring based on rules you configure - it does not
guarantee any outcome, and options trading carries real risk of loss.
Lot sizes, margin requirements, and SEBI/exchange compliance rules change
over time (this tool always fetches lot size live from Kite so it never
trades a stale lot size, but you are still responsible for staying current
on margin and compliance rules that apply to you). **Use entirely at your
own risk.** Test thoroughly in `dry_run` mode before ever going live, and
never risk money you can't afford to lose.

## What this is

- BUYING only (NIFTY weekly options) - never selling to open a position.
- One AI/analysis call per Execute run, at the very start. The monitoring
  loop after that never calls AI or does network "analysis" again - only
  arithmetic on live tick data already sitting in memory.
- `dry_run: true` (the default in `settings.json`) means **no real order is
  ever sent** - everything is printed/logged instead. This is deliberately
  **not** a toggle in the web UI (see "Going live" below) - you flip it by
  hand in `settings.json`, so nobody can switch on real trading with one
  misplaced click in a browser.
- A day-end safety exit (`force_exit_time`, default 15:15) is always active
  and can't be turned off from Settings, no matter what else you disable.

## Quick start (web dashboard)

**Windows:** double-click [run.bat](run.bat)
**Mac/Linux:** `./run.sh`

Or manually:

```bash
pip install -r requirements.txt
python app.py
```

Your browser opens automatically to `http://127.0.0.1:5050`. Nothing is
exposed outside your machine (the server binds to 127.0.0.1 only).

**If you ever edit any of the `.py` files** (or pull an update) while the
server is already running: refreshing the browser page alone is **not**
enough - Python does not reload code from disk on a running process, only
a full restart of `python app.py` does. To make this unmissable instead of
a silent source of confusion, the top bar always shows "Server started
HH:MM:SS", and a bold red banner appears automatically (checked every 15s,
no refresh needed) if any `.py` file changed after this process started,
telling you exactly to close the terminal and run `python app.py` again.

The first time you open it, `settings.json` is an empty template, so the
**Settings** panel opens automatically. Fill in:
- **Kite API Key / Secret** - from https://developers.kite.trade. In that
  app's settings, set the **Redirect URL** to exactly `http://127.0.0.1:5000/`
  (a different port from the dashboard itself - this is the fixed address
  the Login flow uses to catch Kite's redirect).
- **AI Provider** (Gemini or Claude) + that provider's API key.
- **Lots**, and your **Stop Loss / Target / Time Exit** toggles + amounts.

Click **Save Settings**, then use the three Control buttons:

1. **Login** - opens a Kite login tab. Nothing else starts automatically.
2. **Execute** - runs one full analysis + decision + entry, then starts
   live monitoring. Repeatable any time after Login (no need to log in
   again for each Execute).
3. **Stop** - immediately squares off any open position and stops this
   tool's monitoring. It does **not** touch your Zerodha account otherwise -
   Kite's own app/website stay fully, separately accessible at all times;
   this tool never holds any exclusive lock on your account.

The live dashboard shows: a candlestick chart of the underlying built from
live ticks, the big current LTP, a P&L card (green when in profit, red when
in loss), and a **Decision** panel showing exactly *why* the system took the
position it did (technical bias, AI sentiment + its one-line reason, the
large-gap flag, and the final CALL/PUT/BOTH/NO_TRADE call) - plus a
collapsible technical log of everything happening under the hood.

## Going live (dry_run → real orders)

1. Test thoroughly with the default `dry_run: true` first - run full
   Execute cycles and confirm the decisions, entries, and exits in the log
   all look correct to you.
2. When ready, open `settings.json` in a text editor and change:
   ```json
   "dry_run": false
   ```
3. Save the file and restart the app (`python app.py` again, or re-run
   `run.bat`/`run.sh`).
4. The mode badge in the top-right will now show **LIVE** instead of
   **DRY RUN**. From this point, Execute places real orders on your Kite
   account with real money.

This one switch is intentionally kept out of the web UI's Settings form.

## Running the console/headless version (Phase 1, no GUI)

```bash
python run_headless.py
```

Same engine, no browser dashboard - useful for quick testing or running
on a machine without a display. Type `stop` + Enter or press Ctrl+C to
trigger the same immediate square-off as the web UI's Stop button.

## Running the tests

```bash
pytest test_option_signal.py test_decision.py -v
```

These test the two pure-Python modules (OI/VWAP bias math, gap math, the
decision truth table) with fake data - no Kite connection, AI call, or
browser needed, and they're safe to run any time.

## How it all fits together

1. `engine.run()` loads `settings.json`, logs in to Kite (or reuses an
   already-logged-in session), fetches the live NIFTY option chain, and
   figures out the ATM strike and ATM +/- 5 strikes to track.
2. It starts KiteTicker, streaming ticks (LTP + OI) for those strikes plus
   the underlying index.
2v. The "OI Bias" badge's `weighted_pcr` (2z below) used to be proximity-
   weighted ONLY, never by fresh OI-change - inconsistent with the OI-wall
   base probability (2b) and ATM's own OI-change (2y), both of which already
   used it, and a real reported gap: the separate "ATM OI" chip could read
   BULLISH from heavy fresh PUT build-up while "OI Bias" stayed stuck
   NEUTRAL right next to it, for the exact same underlying data. Phase 10
   upgrade: `compute_oi_bias` now takes an optional `strike_oi_change_ratios`
   dict and reweights whichever strikes it has fresh OI-change numbers for
   (in practice: the identified walls + ATM, the only strikes this project
   fetches OI-change for - no extra API calls) by the same clamped
   multiplier already used elsewhere (`oi_change_clamp`). Required moving
   the OI-wall + OI-change fetch (previously only done later, for the base
   probability) up to run right where oi_bias itself is computed, so both
   reuse the identical numbers rather than fetching twice. The real,
   unweighted `pcr` is untouched either way - still shown as-is.
2w. The Opening Range Breakout (ORB, `option_signal.compute_orb_bias`,
   Phase 9 upgrade) replaces the classic floor-trader Pivot R1/S1 breakout
   this project used before, as the third `technical_bias` vote, as one of
   the 7 nudges into `combine_probability_signals`, and as the breakout
   bonus in Volatility Confidence (2c below). Both ask "has today broken
   out of a reference range", but Pivot's R1/S1 band spans close to a FULL
   prior day's typical range - a rare, late, "this is definitely a trend
   day" confirmation, correctly NEUTRAL on most ordinary days by design
   (verified against real logged numbers, not a calibration bug). ORB uses
   TODAY's own first `orb_minutes` (settings.json, default 15) of 1-minute
   candles on the index itself as the reference instead - valid Open/High/
   Low/Close is available for an index even though volume isn't (that's
   what broke VWAP - see the VWAP note further down - not this, so no
   futures proxy is needed here) - a far narrower, well-established,
   widely-watched intraday level for NIFTY option buyers specifically, so
   it resolves much earlier in the session and triggers on more days.
   Reads NEUTRAL (with no high/low) if the opening range itself hasn't
   finished forming yet when the analysis runs. `engine.get_orb_bias`.
2x. The "expected range" (upside/downside targets, and the yardstick
   `required_move_call/put/both` and Volatility Confidence are sized
   against) used to be VIX-implied only (`option_signal.compute_vix_
   expected_move`) - a purely statistical/implied number that says nothing
   about whether NIFTY has actually been travelling that far recently. Phase
   7 upgrade: it's now BLENDED with the real recent open-to-close range
   (`option_signal.compute_avg_open_to_close_range`, averaged over the last
   `historical_range_lookback_days` - default 15 - completed trading days,
   a *different*, narrower measure than 2b's high-low `avg_range`, since
   open-to-close is the same "move by end of day" question VIX's number is
   meant to predict) - final `expected_move = (vix_expected_move +
   historical_oc_range) / 2`. Both the pure VIX figure and the historical
   figure are kept and shown separately (dashboard: hover "Today's Expected
   Range") alongside the blended one actually used, so the blend is always
   verifiable against its two real inputs, never a black box. Falls back to
   VIX-only if historical daily candles aren't available.
2y. ATM's OWN CALL vs PUT fresh OI-change (today vs yesterday's close,
   `option_signal.compute_atm_oi_change_bias`) is now a distinct directional
   signal (Phase 7 upgrade) - separate from the resistance/support walls
   above (always OTM strikes by construction, see `compute_oi_walls`) and
   from the broader ATM+/-5 PCR (2z below, a static/aggregate ratio). This
   asks specifically "what's freshly changing right at the current price
   today": relatively faster PUT OI build-up right at the money is read
   BULLISH (fresh conviction backing the floor directly under spot),
   relatively faster CALL OI build-up is read BEARISH (fresh conviction
   capping it directly above) - same interpretation convention already used
   for the OI walls. Fed into `combine_probability_signals` as a 7th bounded
   nudge, and shown on the dashboard as its own "ATM OI" chip.
2z. The plain, whole-ATM+/-5-window PCR (`option_signal.compute_oi_bias`) that
   drives the "OI Bias" badge/`technical_bias` can sit at a mild, NEUTRAL-
   looking ratio (e.g. 1.1) even while the specific strike right next to
   spot is heavily skewed one way and a far strike (near the tracked
   window's edge) happens to balance the whole-window total back down -
   the same "PCR alone isn't enough without WHERE the nearest strike is"
   gap already fixed for the OI-wall base probability (2a below) also
   applied here (Phase 6 upgrade, later extended with OI-change weighting
   too - see 2v above). A SEPARATE `weighted_pcr` is now
   computed the same proximity(+OI-change)-weighted way and is what actually decides
   BULLISH/BEARISH/NEUTRAL (and is what's fed into
   `combine_probability_signals`'s PCR nudge) - the real, unweighted `pcr`
   is left untouched and still shown on the dashboard, so it always
   matches what you'd see cross-checking the real option chain.
2a. The OI-wall base probability (`option_signal.compute_direction_probability`)
   now weights each wall's OI by how CLOSE it is to spot, not just its raw
   size (Phase 2 upgrade) - a heavy wall sitting right next to price matters
   far more right now than an equally heavy wall sitting at the far edge of
   the tracked ATM+/-5 window, which price would have to travel much
   further to even reach. Weight decays linearly from full (wall exactly at
   spot) to zero (wall at the edge of the tracked window); a wall exactly at
   the edge contributes nothing regardless of its OI size. The dashboard's
   "Resistance / Support" row now also shows each wall's distance in points.
2b. Each wall's (proximity-weighted) OI is further scaled by whether it's
   FRESH build-up or unwinding since yesterday's close (Phase 3 upgrade,
   `engine.get_oi_change`) - a wall's raw OI size doesn't say whether that
   conviction is being actively added to right now or actually being
   covered/unwound (weaker than it looks). Yesterday's closing OI for just
   the two identified wall instruments is fetched via Kite's
   `historical_data(..., oi=True)` (confirmed supported by the installed
   SDK) and compared to today's OI: ratio > 1 strengthens that wall, < 1
   weakens it, clamped to `oi_change_clamp` (default +/-50%) so one unusual
   day's swing can't dominate on its own. Volume (`volume_traded`, already
   present in FULL-mode ticks) is captured at the same time at no extra API
   cost and shown on the dashboard for context, but is NOT yet folded into
   the probability itself - a sound volume-weighting formula needs a
   per-instrument "normal" baseline volume this project doesn't track.
2c. A SEPARATE "Volatility Confidence" gauge (0-100%, Phase 4 upgrade,
   `option_signal.compute_volatility_confidence`) - independent of the
   Up/Down probability split above, which answers a different question
   (WHICH WAY, not HOW MUCH). Combines: today's VIX-implied expected move
   relative to the recent average daily range (the core "is today unusual"
   ratio, mapped via `volatility_confidence_ratio_scale`, default 50 - ratio
   1.0 = 50%, ratio 2.0 = 100%), a flat bonus if price has already broken
   out past today's own opening-range high/low (`volatility_confidence_orb_bonus`,
   default 15, Phase 9 - replaces the old Pivot R1/S1 breakout bonus), a
   bonus scaling with how much of today's expected move
   is already realized (`volatility_confidence_momentum_scale`, default 20),
   a bonus scaling with how much fresh OI build-up/unwind is happening
   RIGHT NOW at the two nearest walls (`volatility_confidence_oi_buildup_scale`,
   default 15) - the exact same `resistance_oi_change_ratio`/
   `support_oi_change_ratio` numbers already computed for 2b above, reused
   here by their MAGNITUDE rather than direction: heavy fresh positioning
   near spot, either build-up or unwind, is real evidence a big move is
   more likely today - VIX alone only gives the statistical/implied size of
   that move, not whether real option-chain activity is actually backing it
   up right now. Averaged across whichever wall(s) have a ratio available,
   clamped the same way as 2b (`oi_change_clamp`) so one outlier strike
   can't dominate. A fifth bonus (Phase 9, `volatility_confidence_cpr_narrow_scale`,
   default 15) scales with how NARROW yesterday's Central Pivot Range (CPR)
   was (`option_signal.compute_cpr_width`, a pre-market "will today trend or
   chop" classifier from yesterday's H/L/C alone - a narrow-range-day-often-
   precedes-a-bigger-move is a well-established technical observation, not
   folklore invented for this project) - normalized against the real,
   mathematically-derived maximum of the width/range ratio (1/3, reached
   when yesterday's close sat exactly at its own high or low), not an
   invented cutoff; a wide CPR contributes nothing (never subtracts). This
   is a reasoned combination of already-computed real signals, not a
   backtested formula, so all five weights are exposed as settings rather
   than presented as more precise than they are. Shown on the dashboard as
   its own bar (hover it for the full breakdown with real numbers), never
   summed with or affecting the Up/Down split.
3. **Once**, it computes three independent technical signals - OI-bias (PCR),
   VWAP-bias, and an Opening Range Breakout check (2w above) - and combines
   them into a `technical_bias`
   whenever **any 2 of the 3 agree** (`option_signal.combine_bias`). This is
   shown on the dashboard for context, but it is **not** what decides the
   trade (see points 4-5). It also checks today's gap-vs-average-range,
   estimates a probability-of-move (expected range + option-chain OI
   walls), and calls the configured AI provider once for a
   POSITIVE/NEGATIVE/NEUTRAL read - the AI call also receives the VIX/OI-wall/
   ORB numbers already computed (Phase 5 upgrade: now including each
   wall's distance from spot, its OI build-up/unwind ratio vs yesterday,
   today's already-realized move and momentum fraction, and the Volatility
   Confidence score - the exact same Phase 1-4 numbers the dashboard shows,
   see `ai_sentiment._fixed_prompt`), so it can weigh them alongside news,
   without ever becoming a second call. This is read-only context for the
   AI - it never changes what the AI is allowed to answer with (still
   exactly one of POSITIVE/NEGATIVE/NEUTRAL) or how much its read can move
   the final probability (still one bounded +/-5-percentage-point nudge,
   same as every other signal) - Python still makes the actual decision.
4. Seven inputs - option chain (PCR + OI walls), ATM's own fresh OI-change
   (2y above), VWAP, ORB, gap, AI sentiment, and momentum - are combined
   into **one final upside/downside probability**
   (`option_signal.combine_probability_signals`), which tells
   `decision.decide_direction(...)` **which way** the market is more likely
   to move: CALL-favoring if upside probability clears
   `call_probability_threshold` (default 55%), PUT-favoring if it falls
   below `put_probability_threshold` (default 45%), otherwise unclear
   (coin-flip band). The **momentum** signal (`option_signal.compute_momentum_bias`)
   is today's own already-realized move (`points_moved_from_open`, previously
   computed only for display) sized against the (now blended, 2x above)
   `expected_move`: it only
   counts as evidence once it's used up a moderate fraction of that expected
   move (`momentum_min_fraction`-`momentum_max_fraction`, default 25%-65%) -
   below that band it's dismissed as normal noise, and above it it's
   deliberately EXCLUDED rather than treated as extra-strong evidence, since
   a move that's already used up most of today's expected range is more
   likely near-exhausted than about to extend further (avoids chasing/buying
   near the top of a move that's about to stall).
5. Probability alone is never enough to place an order - a trade must also
   be judged likely **profitable**, not just directionally plausible. Right
   after building the option universe, `engine.get_atm_premiums()` reads
   the REAL, LIVE ATM CALL and PUT premium from Kite (tick first, one-time
   REST `quote()` fallback). A bought option's breakeven is exactly its own
   premium (CALL needs the underlying to rise by at least what you paid,
   PUT to fall by at least what you paid, a straddle needs either side to
   move by the two premiums combined) - `required_move_call/put/both` is
   that breakeven times `profit_margin_factor` (default 1.15x, a safety
   cushion above pure breakeven). `decide_direction` only returns CALL/PUT
   if the (blended, 2x above) `expected_move` clears that side's required move, and
   only returns BOTH (when direction is unclear, or a large gap, or the AI
   sentiment actively conflicts with a probability-favored side) if it
   clears the combined straddle's required move - otherwise NO_TRADE. This
   is deliberately never a fixed/guessed points number: the same points
   move means something completely different on a day the option is cheap
   vs expensive, and today's live premium already encodes that day's own
   implied volatility and time decay, which a generic constant never could.
   The dashboard's "ATM CALL / PUT Premium" and "Required Move to Profit"
   rows show these live numbers directly. A large gap still forces BOTH
   unconditionally (win-or-lose-either-way day, not a probability call) and
   a too-tight (blended) expected range still forces NO_TRADE outright regardless
   of everything else (see `decision.py` for the exact rules).
6. Instead of the automated path above, the dashboard's "Buy CALL"/"Buy
   PUT"/"Buy BOTH (ATM)" buttons call `engine.manual_run()`, which skips the
   entire analysis pass and enters the chosen direction directly - useful
   when you want to act on your own judgment rather than the algorithm's.
   Everything from here on (order placement, SL/target/time-exit/force-exit
   monitoring) is the exact same code path either way.
7. If not NO_TRADE, it places BUY order(s) for `settings.lots` x the
   *live* lot_size Kite reports (never hardcoded - NSE revises lot sizes
   over time), then enters a monitoring loop.
8. The loop checks, about once a second: manual STOP, SL/target (per
   `pnl_mode`), your optional time-exit, and the always-on
   `force_exit_time` safety net - whichever hits first squares off the
   position(s) with a market SELL order. SL/target/time-exit/pnl_mode are
   **re-read from `settings.json` on every one of these ticks** (see
   `engine._read_live_exit_settings`), so changing them from the dashboard's
   Settings panel and clicking Save takes effect immediately on an
   already-open position too - you never have to Stop and re-enter just to
   tighten or loosen a stop-loss mid-trade. Only these safety-net fields are
   live-reloaded; `lots`, `index`, and API keys stay fixed to whatever they
   were at entry, since those can't retroactively change what was already
   bought.
9. Before any square-off SELL - Stop, SL, target, time-exit, or force-exit,
   automated or manual - `engine._square_off_one()` first asks Zerodha's own
   `positions()` API what quantity is *actually* still held for that
   tradingsymbol right now (skipped in `dry_run`, which has no real position
   to check against). If you already closed that position yourself on the
   real Zerodha app/website, this project sees it's already flat and skips
   placing a SELL entirely, instead of blindly re-selling into a fresh,
   unintended SHORT position; a manual *partial* exit is also handled
   correctly - it squares off exactly the real remaining quantity, not a
   stale in-memory number.
10. `app.py` (the web layer) doesn't reimplement any of this - it calls
   `engine.run()` (or `engine.manual_run()`) in a background thread and
   relays its `on_event`/`on_tick` callbacks to the browser over WebSocket
   (Flask-SocketIO), and relays the Stop button to the same `stop_event` the
   engine already checks.

## Design notes / assumptions worth knowing about

- **Technical bias = any 2 of 3 signals agree (reference only)**: OI-bias,
  VWAP-bias, and an Opening Range Breakout check (Phase 9 - today's price
  vs today's own first `orb_minutes` of price action; replaces the classic
  floor-trader Pivot R1/S1 breakout this project used before - see 2w
  above for why). Originally this required *both* OI and
  VWAP to agree, which flagged NEUTRAL far too often to ever be useful for a
  system that only buys options - adding a third independent signal and
  requiring only 2-of-3 agreement (`option_signal.combine_bias`) was the
  first fix. It is shown on the dashboard for context, but the trade itself
  is now decided by the probability below, not by this vote.
- **Probability-of-move drives the trade decision**: India VIX (fetched
  live, same pattern as the spot quote) implies a 1-day expected move via
  the standard options-industry formula
  `today's_open * (vix/100) * sqrt(1/252)` (252 trading days/year is the
  standard annualization convention, not a custom guess) - **anchored to
  today's open**, not the live spot price, so the Upside/Downside Target
  lines stay fixed for the whole session and the dashboard can separately
  show how many points have already moved from the open vs how much of that
  expected range is still left. Separately, the option chain's OTM Call OI
  (strikes above ATM) and OTM Put OI (strikes below ATM) are compared to
  find the nearest resistance/support "walls" from the option sellers'
  perspective, giving a base upside/downside split
  (`support_wall_oi / (support_wall_oi + resistance_wall_oi)`). This base is
  then nudged by all of PCR, VWAP-bias, ORB-bias, today's gap, and AI
  sentiment together (`option_signal.combine_probability_signals`) - each
  agreeing/disagreeing signal shifts the probability by 5 percentage points,
  clamped to [5%, 95%]. **This final combined number is what
  `decision.decide_direction()` acts on directly**: CALL if upside
  probability >= `settings["call_probability_threshold"]` (default 0.55),
  PUT if <= `settings["put_probability_threshold"]` (default 0.45),
  otherwise BOTH if `settings["volatile_entry_move_points"]` (default 100,
  deliberately stricter than `min_expected_move_points` below) is cleared -
  "direction unclear, but VIX implies a genuinely large move, so straddle
  the volatility instead of skipping" - otherwise NO_TRADE as too close to a
  coin flip (and no big move expected either) to risk time decay on. A
  strongly opposing AI sentiment downgrades a would-be CALL/PUT to BOTH
  instead of picking a side against it. If the VIX-implied expected move is
  below `settings["min_expected_move_points"]` (default 30), the run skips
  the trade entirely (`NO_TRADE`) regardless of probability or the volatile
  threshold, since a tight expected range usually isn't worth paying an
  option's time decay for at all. All of this is computed in the same
  single analysis pass and fed into the *same* one-shot AI call as extra
  context (see `ai_sentiment.py`) - never a second AI request. If the AI
  provider's search-grounding quota is exhausted (HTTP 429), the same call
  is retried once without live web search rather than silently defaulting
  to NEUTRAL every run.
- **AI prompt is visible and extensible, but never user-controlled**: the
  dashboard's "Advanced: AI & Strategy" section shows the exact fixed prompt
  read-only (`ai_sentiment.build_prompt_preview()`), and lets you save an
  optional `custom_ai_note` that gets APPENDED as one more factor to weigh -
  it can never replace the fixed instruction or the required
  `{"sentiment": ..., "reason": ...}` response format, so no matter what the
  note says, the AI's output is still constrained to exactly one of
  POSITIVE/NEGATIVE/NEUTRAL and can still only ever influence the
  directional read, never order quantity, instrument, or anything else.
- **Manual override**: `engine.manual_run(direction)` (wired to the
  dashboard's Buy CALL/PUT/BOTH buttons) enters the chosen direction
  directly, skipping OI/VWAP/ORB/VIX/AI/probability entirely - for when
  you'd rather act on your own read of the market than the algorithm's. It
  still builds the option universe and live tick feed the same way, and the
  SL/target/time-exit/force-exit monitoring loop afterward is byte-for-byte
  the same `monitor_and_exit()` the automated path uses - a manual choice is
  never less protected than an automated one, only how the direction was
  picked differs.
- **pnl_mode**: `"COMBINED"` (the default) sums P&L across all open legs and
  checks SL/target against the total. Any other value (e.g. `"PER_LEG"`) is
  treated as per-leg mode: each leg's own P&L is checked independently, and
  only that leg is squared off when it triggers. Not exposed in the web UI
  (edit `settings.json` directly if you want per-leg mode).
- **BOTH direction** (straddle) buys a full `lots` quantity on *both* the
  ATM CE and ATM PE - it does not split one `lots` quantity across the two
  legs.
- **Entry price** used for P&L is the live tick LTP at the moment the BUY
  order is placed (falling back to one REST `quote()` call if no tick has
  arrived yet).
- **VWAP** (Phase 8 fix): Kite's tick/quote `average_price` field *is* the
  day's cumulative VWAP for a tradable instrument - but the NIFTY 50 INDEX
  itself is never one: confirmed via Kite's own docs, index tick packets
  omit `average_price`/volume entirely, and the index's own historical
  candles report zero volume for the same reason (indices aren't traded),
  so a minute-candle fallback on the index can't work either - VWAP was
  silently unavailable (falling back to NEUTRAL) on every run before this
  fix. `build_option_universe()` now also looks up the nearest NIFTY
  **futures** contract (`kite_client.get_nfo_futures_instrument`) - a
  genuinely traded instrument with real volume and `average_price`, price
  tracking the index closely intraday - and `engine.get_vwap()` reads VWAP
  from IT instead (tick first, one-time REST `quote()` fallback, minute-
  candle fallback last), while `compute_vwap_bias` still compares that
  number against the INDEX's own current price - only the VWAP number's
  *source* changed. Shown on the dashboard: hover "VWAP Bias" to see which
  futures contract it came from.
- **Strike interval & lot size**: both derived live from Kite's own
  instrument dump every run - never hardcoded.
- **Index -> spot tradingsymbol** mapping (`"NIFTY"` -> `"NIFTY 50"`,
  `"BANKNIFTY"` -> `"NIFTY BANK"`, etc.) is a small fixed lookup in
  `engine.py` - it only resolves an instrument *name*, not any tradable
  price/quantity data, so it doesn't conflict with the "never hardcode
  from Kite" rule for lot sizes.
- **Advanced settings not in the web form** (`force_exit_time`, `dry_run`,
  `pnl_mode`, `pcr_threshold`, `range_lookback_days`, `gap_threshold_factor`,
  `index`, proxy) can all still be hand-edited in `settings.json` - the web
  Settings panel exposes the fields listed in the spec (Kite keys, AI
  provider + key, Lots, SL/Target/Time-exit) plus an optional Proxy section.
- **Chart/Socket.IO libraries are bundled locally** under `static/`
  (`socket.io.min.js`, `lightweight-charts.standalone.production.js`)
  instead of pulled from a CDN at runtime. For a live trading dashboard,
  not depending on a third-party CDN being reachable at the moment you need
  it felt like the safer default; both files together are under 250KB, so
  the folder stays light. Google Fonts (Inter) is still loaded from its own
  CDN purely for cosmetics, with a system-font fallback if it's unreachable.
- **Machine date/time**: `token.json` caching and all `HH:MM` exit-time
  comparisons use the machine's local date/time - make sure the machine
  running this is set to IST during market hours.
- **Ticker proxy**: the `proxy` setting is applied to all REST calls
  (`KiteConnect`). The `kiteconnect` library's `KiteTicker` (WebSocket) has
  no proxy option, so the live tick stream always connects directly.

## File overview

| File | Purpose |
|---|---|
| `settings.json` | All user-editable config - both the web UI and console read/write this same file. |
| `kite_client.py` | Proxy-aware Kite login, instrument lookup, ticker tick-store, order placement (dry-run gated). |
| `option_signal.py` | Pure functions: OI-bias (PCR), VWAP-bias, combined technical bias, gap/range flag. |
| `ai_sentiment.py` | One fixed-prompt AI call (Gemini or Claude), safe NEUTRAL fallback on any failure. |
| `decision.py` | Standalone final-direction truth table (CALL/PUT/BOTH/NO_TRADE). |
| `engine.py` | Wires everything together: one Execute run, then the safety-first monitoring loop. |
| `run_headless.py` | Console entry point + manual STOP listener (no browser needed). |
| `app.py` | Flask + Flask-SocketIO web server - runs `engine.run()` in the background and streams events/ticks to the browser. |
| `templates/index.html`, `static/style.css`, `static/dashboard.js` | The single-page web dashboard. |
| `static/socket.io.min.js`, `static/lightweight-charts.standalone.production.js` | Bundled front-end libraries (see note above). |
| `run.bat` / `run.sh` | Double-click launchers (Windows / Mac-Linux). |
| `test_option_signal.py`, `test_decision.py` | Unit tests for the pure-Python logic. |
