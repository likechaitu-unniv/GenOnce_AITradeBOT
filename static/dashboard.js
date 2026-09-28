(function () {
  "use strict";

  // ------------------------------------------------------------ helpers

  function $(id) { return document.getElementById(id); }

  function toast(message, type) {
    const container = $("toastContainer");
    const el = document.createElement("div");
    el.className = "toast" + (type ? " " + type : "");
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => el.remove(), 4500);
  }

  function fmtMoney(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    const sign = n < 0 ? "-" : "";
    return sign + "₹" + Math.abs(n).toFixed(2);
  }

  function fmtPrice(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    return n.toFixed(2);
  }

  // ------------------------------------------------------------ live IST clock
  // Purely client-side (Date + Intl, formatted to Asia/Kolkata regardless of
  // the machine's own timezone) - next to the Time Exit field so you can see
  // the actual current market time while picking an exit time, without
  // depending on being logged in / Kite being reachable. Runs continuously,
  // market open or closed, and is always correct on reopen since it's
  // computed fresh from the real clock every tick, never cached.
  const liveClockEl = $("liveClock");
  if (liveClockEl) {
    const updateLiveClock = () => {
      liveClockEl.textContent = new Date().toLocaleTimeString("en-IN", {
        timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true,
      });
    };
    updateLiveClock();
    setInterval(updateLiveClock, 1000);
  }

  async function postJSON(url, body) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || ("request failed: " + resp.status));
    }
    return data;
  }

  // ------------------------------------------------------------ settings form

  const providerSelect = $("ai_provider");
  const geminiField = $("geminiKeyField");
  const claudeField = $("claudeKeyField");

  function syncProviderFields() {
    const isGemini = providerSelect.value === "gemini";
    geminiField.style.display = isGemini ? "" : "none";
    claudeField.style.display = isGemini ? "none" : "";
  }
  providerSelect.addEventListener("change", syncProviderFields);
  syncProviderFields();

  // ---- execution provider: show/hide Kite vs OpenAlgo fields ----
  const execProviderSelect  = $("execution_provider");
  const loginBtn_           = $("loginBtn");
  const openalgoLoginBtn_   = $("openalgoLoginBtn");
  const kiteManualRow       = $("kiteManualLoginRow");

  function syncExecProviderFields() {
    const isOA = execProviderSelect && execProviderSelect.value === "openalgo";
    // Kite credential rows — each is a direct child of the form grid
    ["kite_api_key", "kite_api_secret"].forEach(id => {
      const el = $(id);
      if (el && el.closest(".field")) el.closest(".field").style.display = isOA ? "none" : "";
    });
    // OpenAlgo credential rows
    ["openalgo_api_key", "openalgo_host"].forEach(id => {
      const el = $(id);
      if (el && el.closest(".field")) el.closest(".field").style.display = isOA ? "" : "none";
    });
    // Login buttons
    if (loginBtn_)         loginBtn_.style.display         = isOA ? "none"        : "";
    if (openalgoLoginBtn_) openalgoLoginBtn_.style.display = isOA ? "inline-flex"  : "none";
    // Kite manual token row
    if (kiteManualRow) kiteManualRow.style.display = isOA ? "none" : "";
  }
  if (execProviderSelect) {
    execProviderSelect.addEventListener("change", syncExecProviderFields);
    syncExecProviderFields();
  }

  function bindToggle(checkboxId, inputId) {
    const cb = $(checkboxId);
    const input = $(inputId);
    function sync() { input.disabled = !cb.checked; }
    cb.addEventListener("change", sync);
    sync();
  }
  bindToggle("sl_enabled", "max_loss");
  bindToggle("target_enabled", "target_profit");
  bindToggle("time_exit_enabled", "time_exit");
  bindToggle("proxy_enabled", "proxy_host");

  const settingsPanel = $("settingsPanel");
  $("settingsToggleBtn").addEventListener("click", () => {
    settingsPanel.classList.toggle("collapsed");
    // Settings is a tall section - toggling it changes how much the page can
    // scroll, but the browser keeps whatever raw scroll position you were
    // at. Without this, clicking Settings again while scrolled deep into the
    // form can land you in the middle of an unrelated section instead of
    // visibly returning to the top - this makes "go back" always work the
    // same way: scroll to the top of whichever view is now showing. Instant
    // (not smooth) - smooth scrollTo can silently fail to complete in some
    // browser/OS "reduced motion" configurations, and this needs to be
    // 100% reliable, not just usually-nice.
    window.scrollTo(0, 0);
  });

  const modeBadge = $("modeBadge");
  let modeSwitching = false;
  modeBadge.addEventListener("click", async () => {
    if (modeSwitching || running) return;
    const goingLive = modeBadge.classList.contains("badge-dry");
    if (goingLive) {
      const ok = confirm(
        "WARNING: This switches to LIVE trading.\n\n" +
        "Every future Execute run will place REAL orders on your Zerodha " +
        "account with REAL money - current risk per trade: Stop-Loss/Target " +
        "as set in Settings.\n\n" +
        "Click OK only if you are certain. Click Cancel to stay in DRY RUN."
      );
      if (!ok) return;
    }
    modeSwitching = true;
    modeBadge.disabled = true;
    try {
      const resp = await fetch("/api/mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ live: goingLive }),
      });
      const data = await resp.json();
      if (!data.ok) {
        toast(data.error || "Could not switch mode", "error");
        return;
      }
      modeBadge.classList.toggle("badge-dry", data.dry_run);
      modeBadge.classList.toggle("badge-live", !data.dry_run);
      modeBadge.textContent = data.dry_run ? "DRY RUN" : "LIVE";
      toast(data.dry_run ? "Switched to DRY RUN (simulated)" : "Switched to LIVE - real orders will be placed", data.dry_run ? "info" : "error");
    } catch (err) {
      toast("Could not reach server to switch mode", "error");
    } finally {
      modeSwitching = false;
      modeBadge.disabled = false;
    }
  });

  $("settingsForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      execution_provider: execProviderSelect ? execProviderSelect.value : "kite",
      kite_api_key: $("kite_api_key").value.trim(),
      kite_api_secret: $("kite_api_secret").value.trim(),
      openalgo_api_key: $("openalgo_api_key") ? $("openalgo_api_key").value.trim() : "",
      openalgo_host: $("openalgo_host") ? $("openalgo_host").value.trim() : "http://127.0.0.1:5000",
      index: $("index").value.trim() || "NIFTY",
      lots: parseInt($("lots").value, 10) || 1,
      ai_provider: providerSelect.value,
      gemini_api_key: $("gemini_api_key").value.trim(),
      anthropic_api_key: $("anthropic_api_key").value.trim(),
      sl_enabled: $("sl_enabled").checked,
      max_loss: parseFloat($("max_loss").value) || 0,
      target_enabled: $("target_enabled").checked,
      target_profit: parseFloat($("target_profit").value) || 0,
      time_exit_enabled: $("time_exit_enabled").checked,
      time_exit: $("time_exit").value || "10:30",
      profit_margin_factor: parseFloat($("profit_margin_factor").value) || 1,
      momentum_min_fraction: parseFloat($("momentum_min_fraction").value) || 0,
      momentum_max_fraction: parseFloat($("momentum_max_fraction").value) || 1,
      oi_change_clamp: parseFloat($("oi_change_clamp").value) || 0,
      volatility_confidence_ratio_scale: parseFloat($("volatility_confidence_ratio_scale").value) || 0,
      volatility_confidence_orb_bonus: parseFloat($("volatility_confidence_orb_bonus").value) || 0,
      volatility_confidence_momentum_scale: parseFloat($("volatility_confidence_momentum_scale").value) || 0,
      volatility_confidence_oi_buildup_scale: parseFloat($("volatility_confidence_oi_buildup_scale").value) || 0,
      volatility_confidence_cpr_narrow_scale: parseFloat($("volatility_confidence_cpr_narrow_scale").value) || 0,
      custom_ai_note: $("custom_ai_note").value,
      proxy: {
        enabled: $("proxy_enabled").checked,
        host: $("proxy_host").value.trim(),
        port: $("proxy_port").value.trim(),
        user: $("proxy_user").value.trim(),
        pass: $("proxy_pass").value,
      },
    };
    try {
      const result = await postJSON("/api/settings", payload);
      toast("Settings saved", "success");
      window.__CONFIGURED__ = result.configured;
      refreshControlState();
      if (result.configured) settingsPanel.classList.add("collapsed");
      loadPromptPreview();
    } catch (err) {
      toast("Could not save settings: " + err.message, "error");
    }
  });

  function loadPromptPreview() {
    fetch("/api/prompt_preview")
      .then(r => r.json())
      .then(data => { $("promptPreview").value = data.prompt || ""; })
      .catch(() => { $("promptPreview").value = "(could not load prompt preview)"; });
  }
  loadPromptPreview();

  // ------------------------------------------------------------ control buttons

  const loginBtn = $("loginBtn");
  const loginDot = $("loginDot");
  const executeBtn = $("executeBtn");
  const analyzeBtn = $("analyzeBtn");
  const stopBtn = $("stopBtn");
  const engineStatusText = $("engineStatusText");
  const controlHint = $("controlHint");
  const manualTokenInput = $("manualTokenInput");
  const manualLoginBtn = $("manualLoginBtn");
  const manualTradeBtns = [$("manualCallBtn"), $("manualPutBtn"), $("manualBothBtn")];
  const manualEntryDetails = $("manualEntryDetails");
  const atmPreviewUnavailable = $("atmPreviewUnavailable");
  let atmPreviewTimer = null;
  let spotFeedTimer = null;
  let spotChartLoaded = false;

  let loggedIn = false;
  let running = false;
  let loggingIn = false;

  function setHint(text, kind) {
    controlHint.textContent = text || "";
    controlHint.className = "muted small" + (kind ? " hint-" + kind : "");
  }

  function refreshControlState() {
    loginBtn.disabled = running || loggingIn;
    executeBtn.disabled = !loggedIn || running || !window.__CONFIGURED__;
    analyzeBtn.disabled = !loggedIn || running || !window.__CONFIGURED__;
    stopBtn.disabled = !running;
    modeBadge.disabled = running;
    // Manual login is a fallback and stays usable even while the automatic
    // Login button is mid-attempt (e.g. still waiting on a redirect that
    // will never arrive) - only block it while the engine is running.
    manualLoginBtn.disabled = running;
    manualTokenInput.disabled = running;
    manualTradeBtns.forEach(btn => { btn.disabled = !loggedIn || running || !window.__CONFIGURED__; });
    loginDot.classList.toggle("on", loggedIn);
    engineStatusText.textContent = running ? "Running" : (loggedIn ? "Logged in - idle" : "Idle");
    syncAtmPreviewPolling();
    syncSpotFeed();
  }
  refreshControlState();

  // -------------------------------------------------- ATM CALL/PUT/BOTH live preview
  // Shows the live ATM premium each manual button would actually pay, BEFORE
  // any order is placed. Only polls while the Manual Entry section is
  // actually open (nobody's paying for quote() calls for a panel that's
  // hidden) and while idle (once running, the real position's own
  // pnl_update/tick events take over the main LTP box instead).
  function fmtOptionPrice(ltp) {
    if (ltp === null || ltp === undefined || Number.isNaN(ltp)) return "—";
    return `₹${ltp.toFixed(2)}`;
  }

  // Last lots value the preview actually saw in settings.json - shown in the
  // manual-trade confirm() dialog below so a forgotten "Save Settings" click
  // (typed a new Lots value but never saved it) is visible BEFORE the order
  // goes in, instead of only finding out afterward that it entered with the
  // old lots count.
  let lastKnownLots = null;

  async function fetchAtmPreview() {
    try {
      const resp = await fetch("/api/atm_preview");
      const data = await resp.json();
      if (!data.ok) {
        atmPreviewUnavailable.style.display = "block";
        $("atmPreviewError").textContent = data.error || "unknown error";
        $("previewCallPrice").textContent = "—";
        $("previewPutPrice").textContent = "—";
        $("previewBothPrice").textContent = "—";
        return;
      }
      atmPreviewUnavailable.style.display = "none";
      lastKnownLots = data.lots;
      $("previewCallPrice").textContent = fmtOptionPrice(data.call.ltp);
      $("previewPutPrice").textContent = fmtOptionPrice(data.put.ltp);
      $("previewBothPrice").textContent = fmtOptionPrice(data.both_combined_ltp);
      if (!running) {
        // Before Execute/manual entry there is no live ticker yet - this is
        // the only source of a live NIFTY number, so drive the same LTP box
        // the ticker drives once a run starts (see the "tick" handler below).
        $("ltpValue").textContent = fmtPrice(data.spot_ltp);
      }
    } catch (err) {
      // transient network hiccup - keep showing the last known values,
      // don't spam a toast on every failed 5s poll
    }
  }

  function syncAtmPreviewPolling() {
    const shouldPoll = loggedIn && !running && manualEntryDetails.open;
    if (shouldPoll && atmPreviewTimer === null) {
      fetchAtmPreview();
      atmPreviewTimer = setInterval(fetchAtmPreview, 5000);
    } else if (!shouldPoll && atmPreviewTimer !== null) {
      clearInterval(atmPreviewTimer);
      atmPreviewTimer = null;
    }
  }
  manualEntryDetails.addEventListener("toggle", syncAtmPreviewPolling);

  manualTradeBtns.forEach(btn => {
    btn.addEventListener("click", async () => {
      const direction = btn.dataset.direction;
      const isLive = modeBadge.classList.contains("badge-live");
      const label = direction === "BOTH" ? "CALL and PUT (straddle)" : direction;
      const lotsNote = lastKnownLots
        ? `\n\nLots: ${lastKnownLots} (from Settings - if you just changed this, make sure you clicked Save Settings first).`
        : "";
      const ok = confirm(
        `Manual entry: buy ${label} directly, with NO analysis - only your own judgment.\n\n` +
        (isLive
          ? "You are in LIVE mode - this places a REAL order with REAL money right now."
          : "You are in DRY RUN - this will be simulated, no real order.") +
        lotsNote +
        "\n\nThe usual Stop-Loss/Target/time-exit safety net still applies once entered.\n\n" +
        "Click OK to proceed, Cancel to back out."
      );
      if (!ok) return;
      manualTradeBtns.forEach(b => { b.disabled = true; });
      $("engineErrorBanner").style.display = "none";
      try {
        await postJSON("/api/manual_trade", { direction });
        toast(`Manual ${direction} entry started`, "success");
      } catch (err) {
        toast("Could not start manual entry: " + err.message, "error");
        refreshControlState();
      }
    });
  });

  loginBtn.addEventListener("click", async () => {
    if (!window.__CONFIGURED__) {
      toast("Save your Kite/AI API keys in Settings first", "error");
      settingsPanel.classList.remove("collapsed");
      return;
    }
    loggingIn = true;
    refreshControlState();
    setHint("Opening Kite login in your browser - complete it there...", "info");
    try {
      await postJSON("/api/login", {});
    } catch (err) {
      toast("Login failed to start: " + err.message, "error");
      loggingIn = false;
      refreshControlState();
      setHint("Login failed to start: " + err.message, "error");
    }
  });

  // ---- OpenAlgo connect button ----
  const openalgoLoginBtn = $("openalgoLoginBtn");
  if (openalgoLoginBtn) {
    openalgoLoginBtn.addEventListener("click", async () => {
      if (!window.__CONFIGURED__) {
        toast("Save your OpenAlgo API key in Settings first", "error");
        settingsPanel.classList.remove("collapsed");
        return;
      }
      loggingIn = true;
      refreshControlState();
      setHint("Connecting to OpenAlgo server...", "info");
      try {
        await postJSON("/api/openalgo_login", {});
      } catch (err) {
        toast("OpenAlgo connect failed: " + err.message, "error");
        loggingIn = false;
        refreshControlState();
        setHint("OpenAlgo connect failed: " + err.message, "error");
      }
    });
  }

  async function submitManualToken() {
    const raw = manualTokenInput.value.trim();
    if (!raw) {
      toast("Paste the redirect URL or request_token first", "error");
      return;
    }
    if (!window.__CONFIGURED__) {
      toast("Save your Kite/AI API keys in Settings first", "error");
      settingsPanel.classList.remove("collapsed");
      return;
    }
    manualLoginBtn.disabled = true;
    setHint("Verifying token...", "info");
    try {
      await postJSON("/api/login/manual", { token_input: raw });
      manualTokenInput.value = "";
    } catch (err) {
      setHint("Manual login failed: " + err.message, "error");
      toast("Manual login failed: " + err.message, "error");
    } finally {
      manualLoginBtn.disabled = running;
    }
  }

  manualLoginBtn.addEventListener("click", submitManualToken);
  manualTokenInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitManualToken();
  });

  executeBtn.addEventListener("click", async () => {
    executeBtn.disabled = true;
    $("engineErrorBanner").style.display = "none";
    try {
      await postJSON("/api/execute", {});
      toast("Execute started", "success");
    } catch (err) {
      toast("Could not start: " + err.message, "error");
      executeBtn.disabled = false;
    }
  });

  analyzeBtn.addEventListener("click", async () => {
    analyzeBtn.disabled = true;
    $("engineErrorBanner").style.display = "none";
    try {
      await postJSON("/api/analyze", {});
      toast("Analysis started - no order will be placed", "success");
    } catch (err) {
      toast("Could not start analysis: " + err.message, "error");
      analyzeBtn.disabled = false;
    }
  });

  stopBtn.addEventListener("click", async () => {
    stopBtn.disabled = true;
    try {
      await postJSON("/api/stop", {});
      toast("Stop signal sent", "success");
    } catch (err) {
      toast("Stop failed: " + err.message, "error");
      stopBtn.disabled = false;
    }
  });

  // ------------------------------------------------------------ chart

  const chartEl = $("chart");

  // Lightweight Charts formats epoch seconds in UTC by default - since this
  // is an India-market tool, format all displayed times in the *viewer's*
  // local timezone instead (correct for IST without hardcoding it).
  function formatLocalTime(time) {
    const d = new Date(time * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  }

  const chart = LightweightCharts.createChart(chartEl, {
    layout: { background: { color: "#ffffff" }, textColor: "#475467", fontFamily: "Inter, sans-serif" },
    grid: { vertLines: { color: "#F0F0F0" }, horzLines: { color: "#F0F0F0" } },
    rightPriceScale: { borderColor: "#E4E7EC" },
    timeScale: {
      borderColor: "#E4E7EC", timeVisible: true, secondsVisible: false,
      tickMarkFormatter: formatLocalTime,
    },
    localization: { timeFormatter: formatLocalTime },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    // The chart library captures mouse-wheel by default to zoom/pan its own
    // time axis - that "eats" the scroll wheel whenever the cursor happens
    // to be over the chart, making the PAGE feel like it randomly stops
    // scrolling. Disabling just the wheel handlers fixes that; click-drag
    // and touch (pinch/drag) interaction with the chart are left intact.
    handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
    handleScale: { mouseWheel: false, axisPressedMouseMove: true, pinch: true },
  });
  const candleSeries = chart.addCandlestickSeries({
    upColor: "#16A34A", downColor: "#DC2626",
    borderUpColor: "#16A34A", borderDownColor: "#DC2626",
    wickUpColor: "#16A34A", wickDownColor: "#DC2626",
  });

  window.addEventListener("resize", () => {
    chart.applyOptions({ width: chartEl.clientWidth, height: chartEl.clientHeight });
  });
  chart.applyOptions({ width: chartEl.clientWidth, height: chartEl.clientHeight });

  let currentCandle = null;
  let spotToken = null;

  const CANDLE_SECONDS = 300; // 5-minute candles, matching /api/spot_chart's backfill interval

  function feedSpotPrice(price) {
    const nowSec = Math.floor(Date.now() / 1000);
    const bucket = nowSec - (nowSec % CANDLE_SECONDS);
    if (!currentCandle || currentCandle.time !== bucket) {
      currentCandle = { time: bucket, open: price, high: price, low: price, close: price };
    } else {
      currentCandle.high = Math.max(currentCandle.high, price);
      currentCandle.low = Math.min(currentCandle.low, price);
      currentCandle.close = price;
    }
    candleSeries.update(currentCandle);
  }

  // -------------------------------------------------- always-on spot LTP/chart feed
  // Independent of Execute/manual trade entirely: as soon as you're logged
  // in, this backfills today's real 5-min candles (market open -> now) and
  // then keeps the LTP box + chart's current candle live via polling - so
  // the dashboard never sits blank just because no trade has been taken
  // yet. Once a real run starts, the actual tick stream (far more granular)
  // takes over and this polling pauses (see syncSpotFeed below).
  async function loadSpotChartBackfill() {
    try {
      const resp = await fetch("/api/spot_chart");
      const data = await resp.json();
      if (!data.ok || !data.candles || data.candles.length === 0) return;
      candleSeries.setData(data.candles);
      currentCandle = data.candles[data.candles.length - 1];
      spotToken = data.spot_token;
      $("chartTitle").textContent = data.spot_symbol + " — Live";
      spotChartLoaded = true;
    } catch (err) {
      // transient network hiccup - the periodic poll below will keep trying
      // to at least update the LTP box; chart backfill can be retried next
      // time syncSpotFeed() turns polling on (e.g. after a re-login)
    }
  }

  async function fetchSpotLtp() {
    try {
      const resp = await fetch("/api/spot_ltp");
      const data = await resp.json();
      if (!data.ok || data.ltp === null || data.ltp === undefined) return;
      $("ltpValue").textContent = fmtPrice(data.ltp);
      feedSpotPrice(data.ltp);
    } catch (err) {
      // transient network hiccup - keep showing the last known values
    }
  }

  function syncSpotFeed() {
    // Backfill runs once whenever logged in, REGARDLESS of running - a
    // freshly loaded/refreshed page has never seen the chart history yet
    // even if a run is already active (running=true), and the live tick
    // stream only ever adds NEW candles going forward, it never replays
    // history - so gating this on !running left the chart blank on any
    // refresh while a position was open.
    if (loggedIn && !spotChartLoaded) loadSpotChartBackfill();

    const shouldPoll = loggedIn && !running;
    if (shouldPoll && spotFeedTimer === null) {
      fetchSpotLtp();
      spotFeedTimer = setInterval(fetchSpotLtp, 5000);
    } else if (!shouldPoll && spotFeedTimer !== null) {
      clearInterval(spotFeedTimer);
      spotFeedTimer = null;
    }
    if (!loggedIn) spotChartLoaded = false; // re-backfill on next login (e.g. a different index)
  }

  // ------------------------------------------------------------ decision/positions UI

  function setDirectionBadge(el, value) {
    el.textContent = value || "—";
    el.className = "value direction-badge " + (value || "");
  }

  function setVolConfBadge(vc, evt) {
    const el = $("volConfBadge");
    if (vc === null || vc === undefined) {
      el.textContent = "—";
      el.className = "vol-conf-badge";
      el.title = "";
      return;
    }
    const rounded = Math.round(vc);
    el.textContent = rounded + "% big-move odds";
    el.className = "vol-conf-badge " + (rounded >= 70 ? "high" : rounded >= 40 ? "mid" : "low");

    const parts = [];
    if (evt && evt.expected_move !== null && evt.expected_move !== undefined && evt.avg_range) {
      parts.push(`VIX-implied move (${evt.expected_move.toFixed(0)}pts) vs recent avg daily range `
        + `(${evt.avg_range.toFixed(0)}pts) = ${(evt.expected_move / evt.avg_range).toFixed(2)}x`);
    }
    if (evt && evt.orb_bias && evt.orb_bias !== "NEUTRAL") {
      parts.push(`already broke today's ${evt.orb_minutes ?? 15}-min opening range (confirmed move)`);
    }
    if (evt && evt.momentum_used_fraction !== null && evt.momentum_used_fraction !== undefined) {
      parts.push(`${Math.round(evt.momentum_used_fraction * 100)}% of expected move already realized today`);
    }
    const oiRatios = evt
      ? [evt.resistance_oi_change_ratio, evt.support_oi_change_ratio].filter((r) => r !== null && r !== undefined)
      : [];
    if (oiRatios.length > 0) {
      const desc = oiRatios.map((r) => `${r >= 1 ? "+" : ""}${Math.round((r - 1) * 100)}%`).join(" / ");
      parts.push(`OI build-up/unwind at nearest walls vs yesterday: ${desc}`);
    }
    if (evt && evt.cpr_width_ratio !== null && evt.cpr_width_ratio !== undefined) {
      parts.push(`yesterday's CPR width ratio ${evt.cpr_width_ratio.toFixed(2)} (narrower = more likely a trend day)`);
    }
    el.title = parts.length > 0
      ? "Combines: " + parts.join(" · ")
      : "";
  }

  function renderPositions(positions) {
    const el = $("positionsList");
    if (!positions || positions.length === 0) {
      el.innerHTML = "No open position.";
      return;
    }
    el.innerHTML = positions.map(p => `
      <div class="position-row">
        <span>${p.tradingsymbol} (${p.type}) x${p.qty}</span>
        <span>${fmtPrice(p.entry_price)} → ${fmtPrice(p.ltp)}</span>
      </div>
    `).join("");
  }

  function setPnl(totalPnl) {
    const el = $("pnlValue");
    el.textContent = fmtMoney(totalPnl);
    el.className = "big-number " + (totalPnl > 0 ? "profit" : totalPnl < 0 ? "loss" : "neutral");
  }

  // ------------------------------------------------------------ socket.io

  const socket = io();

  socket.on("connect", () => {
    // no-op: don't clear controlHint here, it may be showing a persistent
    // login error/status the user hasn't acted on yet.
  });

  socket.on("log", (data) => {
    const panel = $("logPanel");
    const line = document.createElement("div");
    line.className = "log-line";
    const time = new Date().toLocaleTimeString();
    line.innerHTML = `<span class="log-time">${time}</span>${escapeHtml(data.message)}`;
    panel.appendChild(line);
    panel.scrollTop = panel.scrollHeight;
  });

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  socket.on("login_status", (data) => {
    if (data.status === "waiting") {
      loggingIn = true;
      setHint("Waiting for you to finish logging in to Kite in the other browser tab (up to 4 minutes)... " +
        "If that tab shows \"connection refused\", paste its URL into the box below instead.", "info");
    } else if (data.status === "success") {
      loggingIn = false;
      loggedIn = true;
      const isOA = execProviderSelect && execProviderSelect.value === "openalgo";
      setHint(isOA ? "✓ Connected to OpenAlgo." : "✓ Logged in to Kite.", "success");
      toast(isOA ? "OpenAlgo connected" : "Kite login successful", "success");
    } else if (data.source === "manual") {
      loggingIn = false;
      loggedIn = false;
      setHint("Login failed: " + (data.message || "unknown error") + " — the token may be wrong, expired, or already used. Complete Kite login again and paste the new URL/token.", "error");
      toast("Manual login failed - see the note under the Login button", "error");
    } else {
      loggingIn = false;
      loggedIn = false;
      setHint(
        "Login failed: " + (data.message || "unknown error") +
        " — double-check your Kite app's Redirect URL is set to EXACTLY http://127.0.0.1:5000/ " +
        "(with the port and trailing slash) in the Kite developer console, or use the paste-token box below instead.",
        "error"
      );
      toast("Kite login failed - see the note under the Login button", "error");
    }
    refreshControlState();
  });

  const progressEl = $("engineProgressText");
  const errorBannerEl = $("engineErrorBanner");

  function showProgress(message) {
    progressEl.textContent = message;
    progressEl.style.display = "flex";
  }
  function hideProgress() {
    progressEl.style.display = "none";
  }
  function showErrorBanner(message) {
    errorBannerEl.textContent = "⚠ " + message;
    errorBannerEl.style.display = "block";
  }
  function hideErrorBanner() {
    errorBannerEl.style.display = "none";
  }

  socket.on("engine_status", (data) => {
    running = !!data.running;
    if (running) {
      hideErrorBanner();
      $("probBar").style.display = "none";
      $("probBarEmpty").style.display = "block";
      $("probBarEmpty").textContent = "Analyzing...";
    } else {
      hideProgress();
      if ($("probBarEmpty").style.display !== "none") {
        $("probBarEmpty").textContent = "Not analyzed yet - click Execute";
      }
    }
    refreshControlState();
  });

  let lastTickReceivedAt = null;
  const feedStatusEl = $("feedStatus");
  const FEED_STALE_MS = 20000;

  socket.on("tick", (tick) => {
    if (spotToken !== null && tick.instrument_token === spotToken) {
      if (tick.last_price !== null && tick.last_price !== undefined) {
        $("ltpValue").textContent = fmtPrice(tick.last_price);
        feedSpotPrice(tick.last_price);
        lastTickReceivedAt = Date.now();
      }
    }
  });

  function refreshFeedStatus() {
    if (!running) {
      feedStatusEl.style.display = "none";
      return;
    }
    feedStatusEl.style.display = "inline-block";
    const isStale = lastTickReceivedAt === null || (Date.now() - lastTickReceivedAt > FEED_STALE_MS);
    feedStatusEl.className = "feed-status " + (isStale ? "stale" : "live");
    feedStatusEl.textContent = isStale ? "Stale" : "Live";
  }
  setInterval(refreshFeedStatus, 3000);

  // Named (not an inline socket.on callback) so the exact same rendering
  // can also be replayed from /api/last_analysis on page load/refresh -
  // see loadLastAnalysis() below. Without this, refreshing the page while
  // a position is open (or any time after Execute has already run) wiped
  // the whole Signal Breakdown/Probability/chart-title back to blank,
  // since "universe_ready"/"decision" are one-time events with nothing
  // that re-sends them - only pnl_update repeats every monitoring tick,
  // which is why P&L/Open Positions alone used to survive a refresh.
  function dispatchEngineEvent(evt) {
    switch (evt.type) {
      case "universe_ready":
        // Same underlying index/token the idle spot feed was already
        // showing - deliberately NOT resetting currentCandle here, so the
        // chart continues the same real candle series instead of jumping
        // to blank right as Execute/a manual trade is clicked.
        spotToken = evt.spot_token;
        $("chartTitle").textContent = evt.spot_symbol + " — Live";
        $("chartSubtitle").textContent =
          `ATM ${evt.atm_strike} · expiry ${evt.nearest_expiry} · strike step ${evt.strike_interval}`;
        lastTickReceivedAt = null;
        break;

      case "progress":
        showProgress(evt.message);
        break;

      case "decision": {
        hideProgress();

        if (evt.manual) {
          // Manual override: no analysis ran, so don't populate any of the
          // analysis fields with values that would look computed - show a
          // distinct, honest "no analysis" state instead.
          setDirectionBadge($("oiBias"), null); $("oiBias").title = "";
          setDirectionBadge($("atmOiBias"), null); $("atmOiBias").title = "";
          setDirectionBadge($("vwapBias"), null); $("vwapBias").title = "";
          setDirectionBadge($("orbBias"), null); $("orbBias").title = "";
          setDirectionBadge($("technicalBias"), null); $("technicalBias").title = "";
          setDirectionBadge($("aiSentiment"), null); $("aiSentiment").title = "";
          setDirectionBadge($("momentumBias"), null); $("momentumBias").title = "";
          $("largeGap").textContent = "—"; $("largeGap").title = "";
          setDirectionBadge($("finalDirection"), evt.direction);
          $("decisionRuleInfo").title = "Manual override - entered directly, no analysis performed";

          $("probBar").style.display = "none";
          $("probBarEmpty").style.display = "block";
          $("probBarEmpty").textContent = "Manual entry - no analysis performed";
          setVolConfBadge(null);
          $("rangeMeterSpotDot").style.display = "none";
          $("rangeMeterFill").style.display = "none";
          $("downsideTarget").textContent = "—";
          $("upsideTarget").textContent = "—";
          $("rangeMeterNote").textContent = "—";
          $("rangeMeterNote").title = "";
          $("callPremium").textContent = "—"; $("callRequiredMove").textContent = "—";
          $("putPremium").textContent = "—"; $("putRequiredMove").textContent = "—";
          $("netPremium").textContent = "—"; $("bothRequiredMove").textContent = "—"; $("bothRequiredMove").title = "";
          $("resistanceChipLine1").textContent = "—"; $("resistanceChipLine2").textContent = ""; $("resistanceChipLine2").title = "";
          $("supportChipLine1").textContent = "—"; $("supportChipLine2").textContent = ""; $("supportChipLine2").title = "";
          $("rangeTooTightNote").textContent = "";
          break;
        }

        setDirectionBadge($("oiBias"), evt.oi_bias);
        {
          const weightedNote = (evt.weighted_pcr !== null && evt.weighted_pcr !== undefined
            && Math.abs(evt.weighted_pcr - (evt.pcr ?? evt.weighted_pcr)) > 0.005)
            ? ` — proximity-weighted ${evt.weighted_pcr.toFixed(2)} decides bullish/bearish` : "";
          $("oiBias").title = (evt.pcr !== null && evt.pcr !== undefined)
            ? `Real PCR ${evt.pcr.toFixed(2)} (put OI / call OI across ATM ±5 strikes)${weightedNote}` : "";
        }

        setDirectionBadge($("atmOiBias"), evt.atm_oi_bias);
        {
          const fmtRatio = (r) => (r === null || r === undefined) ? "N/A" : `${r >= 1 ? "+" : ""}${Math.round((r - 1) * 100)}%`;
          $("atmOiBias").title = (evt.atm_ce_oi_change_ratio !== null && evt.atm_ce_oi_change_ratio !== undefined)
            || (evt.atm_pe_oi_change_ratio !== null && evt.atm_pe_oi_change_ratio !== undefined)
            ? `Fresh OI change right at ATM vs yesterday: CALL ${fmtRatio(evt.atm_ce_oi_change_ratio)}, `
              + `PUT ${fmtRatio(evt.atm_pe_oi_change_ratio)} - faster PUT build-up leans bullish, faster CALL build-up leans bearish`
            : "";
        }

        setDirectionBadge($("vwapBias"), evt.vwap_bias);
        {
          const src = evt.vwap_source_tradingsymbol ? ` (from ${evt.vwap_source_tradingsymbol} futures - the index itself has no volume)` : "";
          $("vwapBias").title = (evt.vwap !== null && evt.vwap !== undefined)
            ? `VWAP ${fmtPrice(evt.vwap)}${src}` : "";
        }

        setDirectionBadge($("orbBias"), evt.orb_bias);
        $("orbBias").title = (evt.orb_high !== null && evt.orb_high !== undefined)
          ? `${evt.orb_minutes ?? 15}-min opening range: ${fmtPrice(evt.orb_low)} - ${fmtPrice(evt.orb_high)}` : "";

        setDirectionBadge($("technicalBias"), evt.technical_bias);
        $("technicalBias").title = "Reference only (2-of-3 OI/VWAP/ORB vote) - the trade decision is driven by Probability of Move, not this.";

        setDirectionBadge($("aiSentiment"), evt.ai_sentiment);
        $("aiSentiment").title = evt.ai_reason ? ("AI: " + evt.ai_reason) : "";

        setDirectionBadge($("momentumBias"), evt.momentum_bias);
        if (evt.momentum_used_fraction !== null && evt.momentum_used_fraction !== undefined) {
          const usedPct = Math.round(evt.momentum_used_fraction * 100);
          const minPct = Math.round((evt.momentum_min_fraction ?? 0) * 100);
          const maxPct = Math.round((evt.momentum_max_fraction ?? 1) * 100);
          $("momentumBias").title =
            `${usedPct}% of expected move already used today (band ${minPct}-${maxPct}% counts as evidence)`;
        } else {
          $("momentumBias").title = "";
        }
        $("largeGap").textContent = evt.large_gap ? "Yes" : "No";
        $("largeGap").title = (evt.gap_points !== null && evt.gap_points !== undefined)
          ? `Gap ${evt.gap_points.toFixed(0)} pts vs avg range ${evt.avg_range.toFixed(0)} pts` : "";
        setDirectionBadge($("finalDirection"), evt.direction);

        $("probBarEmpty").style.display = "none";
        $("probBar").style.display = "flex";
        const upPct = Math.round((evt.upside_probability ?? 0.5) * 100);
        const downPct = 100 - upPct;
        $("probUpBar").style.width = upPct + "%";
        $("probUpBar").textContent = upPct + "%";
        $("probDownBar").style.width = downPct + "%";
        $("probDownBar").textContent = downPct + "%";

        setVolConfBadge(evt.volatility_confidence, evt);

        if (evt.expected_move !== null && evt.expected_move !== undefined) {
          $("upsideTarget").textContent = `${fmtPrice(evt.expected_upside_target)} (+${evt.expected_move.toFixed(0)})`;
          $("downsideTarget").textContent = `${fmtPrice(evt.expected_downside_target)} (−${evt.expected_move.toFixed(0)})`;
          const hasBlend = evt.historical_oc_range !== null && evt.historical_oc_range !== undefined;
          const rangeLabel = hasBlend ? `VIX ${evt.india_vix?.toFixed(1) ?? "—"} + ${evt.historical_range_lookback_days ?? 15}d avg` : `VIX ${evt.india_vix?.toFixed(1) ?? "—"}`;
          let movedStr = "";
          if (evt.points_moved_from_open !== null && evt.points_moved_from_open !== undefined) {
            const sign = evt.points_moved_from_open >= 0 ? "+" : "";
            movedStr = ` · Moved ${sign}${evt.points_moved_from_open.toFixed(0)} pts from open (${fmtPrice(evt.day_open)})`;
          }
          $("rangeMeterNote").textContent = rangeLabel + movedStr;
          $("rangeMeterNote").title = hasBlend
            ? `Expected range blend: VIX-implied ±${evt.vix_expected_move.toFixed(0)}pts, ` +
              `${evt.historical_range_lookback_days ?? 15}-day avg open→close ±${evt.historical_oc_range.toFixed(0)}pts ` +
              `-> blended ±${evt.expected_move.toFixed(0)}pts`
            : "";

          if (evt.points_moved_from_open !== null && evt.points_moved_from_open !== undefined && evt.expected_move > 0) {
            const pct = Math.max(0, Math.min(100,
              ((evt.points_moved_from_open + evt.expected_move) / (2 * evt.expected_move)) * 100));
            $("rangeMeterSpotDot").style.left = pct + "%";
            $("rangeMeterSpotDot").style.display = "block";

            const fillEl = $("rangeMeterFill");
            if (pct >= 50) {
              fillEl.className = "range-meter-fill up";
              fillEl.style.left = "50%";
              fillEl.style.width = (pct - 50) + "%";
            } else {
              fillEl.className = "range-meter-fill down";
              fillEl.style.left = pct + "%";
              fillEl.style.width = (50 - pct) + "%";
            }
            fillEl.style.display = "block";
          } else {
            $("rangeMeterSpotDot").style.display = "none";
            $("rangeMeterFill").style.display = "none";
          }
        } else {
          $("upsideTarget").textContent = "—";
          $("downsideTarget").textContent = "—";
          $("rangeMeterNote").textContent = "Range unavailable (need VIX + recent daily range data).";
          $("rangeMeterNote").title = "";
          $("rangeMeterSpotDot").style.display = "none";
          $("rangeMeterFill").style.display = "none";
        }

        {
          const fmtRequired = (pts) => (pts === null || pts === undefined) ? "—" : `need ${pts.toFixed(0)} pts`;
          if (evt.ce_premium !== null && evt.ce_premium !== undefined) {
            $("callPremium").textContent = fmtMoney(evt.ce_premium);
            $("callRequiredMove").textContent = fmtRequired(evt.required_move_call);
          } else {
            $("callPremium").textContent = "—";
            $("callRequiredMove").textContent = "—";
          }
          if (evt.pe_premium !== null && evt.pe_premium !== undefined) {
            $("putPremium").textContent = fmtMoney(evt.pe_premium);
            $("putRequiredMove").textContent = fmtRequired(evt.required_move_put);
          } else {
            $("putPremium").textContent = "—";
            $("putRequiredMove").textContent = "—";
          }
          if (evt.ce_premium !== null && evt.ce_premium !== undefined && evt.pe_premium !== null && evt.pe_premium !== undefined) {
            $("netPremium").textContent = fmtMoney(evt.ce_premium + evt.pe_premium);
            $("bothRequiredMove").textContent = fmtRequired(evt.required_move_both);
            $("bothRequiredMove").title = (evt.profit_margin_factor !== null && evt.profit_margin_factor !== undefined)
              ? `x${evt.profit_margin_factor} margin on live premium` : "";
          } else {
            $("netPremium").textContent = "—";
            $("bothRequiredMove").textContent = "—";
            $("bothRequiredMove").title = "";
          }
        }

        {
          const fmtWallChip = (strike, distance, oiRatio, volume) => {
            if (strike === null || strike === undefined) return { line1: "—", line2: "", title: "" };
            const oiPct = (oiRatio === null || oiRatio === undefined) ? null : Math.round((oiRatio - 1) * 100);
            const line1 = `${strike} · ${distance?.toFixed(0) ?? "?"}pts`;
            const line2 = oiPct === null ? "" : `OI ${oiPct >= 0 ? "+" : ""}${oiPct}%`;
            const title = (volume === null || volume === undefined) ? "" : `Volume: ${volume.toLocaleString()}`;
            return { line1, line2, title };
          };
          const res = fmtWallChip(evt.resistance_strike, evt.resistance_distance, evt.resistance_oi_change_ratio, evt.resistance_volume);
          $("resistanceChipLine1").textContent = res.line1;
          $("resistanceChipLine2").textContent = res.line2;
          $("resistanceChipLine2").title = res.title;
          const sup = fmtWallChip(evt.support_strike, evt.support_distance, evt.support_oi_change_ratio, evt.support_volume);
          $("supportChipLine1").textContent = sup.line1;
          $("supportChipLine2").textContent = sup.line2;
          $("supportChipLine2").title = sup.title;
        }

        if (evt.range_too_tight) {
          $("rangeTooTightNote").textContent =
            "Expected move is too small today - skipping trade regardless of bias (time-decay isn't worth it).";
        } else if (evt.direction === "BOTH" && !evt.large_gap) {
          $("rangeTooTightNote").textContent =
            "Direction unclear, but the expected move clears BOTH legs' combined live premium (with margin) - " +
            "straddling instead of skipping.";
        } else if (evt.direction === "NO_TRADE" && evt.required_move_call !== null && evt.required_move_call !== undefined) {
          $("rangeTooTightNote").textContent =
            "Expected move doesn't clear the live premium's breakeven (with margin) for any side - skipping " +
            "even though a directional/coin-flip read exists, since it's unlikely to be profitable today.";
        } else {
          $("rangeTooTightNote").textContent = "";
        }

        if (evt.call_probability_threshold !== null && evt.call_probability_threshold !== undefined) {
          const callPct = Math.round(evt.call_probability_threshold * 100);
          const putPct = Math.round(evt.put_probability_threshold * 100);
          $("decisionRuleInfo").title = `CALL if upside ≥${callPct}% AND move clears live CALL premium, ` +
            `PUT if upside ≤${putPct}% AND move clears live PUT premium, else BOTH if move clears the ` +
            `combined straddle premium, else NO_TRADE`;
        } else {
          $("decisionRuleInfo").title = "";
        }
        break;
      }

      case "no_trade":
        hideProgress();
        toast("Decision: NO_TRADE — no position taken this run", "success");
        break;

      case "analysis_complete":
        hideProgress();
        toast(`Analyze Only complete: ${evt.direction} — no order was placed`, "success");
        break;

      case "positions_entered":
        hideProgress();
        renderPositions(evt.positions);
        toast("Entered: " + evt.positions.map(p => p.tradingsymbol).join(", "), "success");
        break;

      case "pnl_update":
        renderPositions(evt.positions);
        setPnl(evt.total_pnl);
        break;

      case "exit":
      case "leg_exit":
        toast("Exit (" + evt.reason + ")", "success");
        if (evt.positions) renderPositions([]);
        break;

      case "error":
        hideProgress();
        showErrorBanner(evt.message);
        toast("Engine error - see the banner above the chart", "error");
        break;

      case "tick_stale":
        toast(`Live price feed hasn't updated in ${Math.round(evt.seconds_since_last_tick)}s - reconnecting...`, "error");
        break;

      default:
        break;
    }
  }
  socket.on("engine_event", dispatchEngineEvent);

  // Replays the last "universe_ready"/"decision" this server has seen
  // (persisted server-side in app.py's STATE, not just relayed once over
  // the socket) so a page refresh - including while a position is still
  // open and the engine is mid-run - shows the same analysis as before,
  // instead of resetting to blank. Safe to call unconditionally: it's a
  // read of in-memory cached data, no Kite/login needed.
  async function loadLastAnalysis() {
    try {
      const resp = await fetch("/api/last_analysis");
      const data = await resp.json();
      if (data.universe) dispatchEngineEvent(data.universe);
      if (data.decision) dispatchEngineEvent(data.decision);
    } catch (err) {
      // transient network hiccup - live events (if a run is active) will still arrive
    }
  }
  loadLastAnalysis();

  refreshControlState();

  // Sync state on load/refresh (e.g. if Execute was already running before
  // the page was reloaded), and keep polling for the restart-needed flag -
  // editing a .py file never hot-reloads the running server process, so we
  // proactively detect that (comparing the code's own last-modified time
  // against when this server process started) and make it impossible to
  // miss instead of leaving stale-backend confusion to guesswork.
  function applyStatus(data) {
    loggedIn = !!data.logged_in;
    running = !!data.running;
    refreshControlState();

    const banner = $("restartNeededBanner");
    if (data.restart_needed) {
      banner.style.display = "block";
      banner.textContent = `⚠ The code on disk changed after this server started (code updated ` +
        `${data.code_last_modified}, server started ${data.server_started_at}). Close this terminal ` +
        `window completely and run python app.py again - refreshing this page alone does not reload the backend.`;
    } else {
      banner.style.display = "none";
    }
  }

  fetch("/api/status").then(r => r.json()).then(applyStatus).catch(() => {});
  setInterval(() => {
    fetch("/api/status").then(r => r.json()).then(applyStatus).catch(() => {});
  }, 15000);
})();
