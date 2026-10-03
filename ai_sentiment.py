"""
Exactly ONE AI call per Execute run (engine.py enforces this - this module
does not loop or retry against live prices; it is called once, at the start,
before the monitoring loop begins).

The prompt sent to the AI is FIXED here, never built from user free-text -
this keeps the AI from ever influencing order quantity/instrument selection,
only the directional sentiment used by decision.py.
"""

import datetime
import json
import re

import requests


def _fixed_prompt(market_context=None, custom_note=None):
    """
    Built fresh each call from a hardcoded template + today's date (system
    data, not user free-text) - still a single fixed instruction, so the AI
    can never be steered by anything typed by the user in a way that changes
    what it's ALLOWED to answer with. Deliberately covers the specific
    factors that most often drive NIFTY's intraday direction, instead of a
    generic "check the news" ask.

    market_context: optional dict (from engine.py's one-shot analysis) with
    the quantitative option-chain/VIX picture already computed in pure
    Python - {"vix", "spot", "expected_move", "resistance_strike",
    "support_strike", "resistance_distance", "support_distance",
    "resistance_oi_change_ratio", "support_oi_change_ratio", "orb_bias",
    "points_moved_from_open", "momentum_used_fraction", "momentum_bias",
    "volatility_confidence", "upside_probability", "downside_probability"}.
    When given, it's added to the SAME prompt/call so the AI weighs it
    alongside news - this never becomes a second AI call. This is
    read-only context for the AI to factor in - it never changes what the
    AI is ALLOWED to answer with (still exactly one of POSITIVE/NEGATIVE/
    NEUTRAL, see the required JSON format below), and the AI's read is
    still only ever ONE bounded +/-5-percentage-point nudge into
    option_signal.combine_probability_signals - it never decides anything
    by itself (see decision.py).

    custom_note: optional user-supplied free text (settings.json
    "custom_ai_note") APPENDED as one more factor to weigh - never used to
    replace the fixed instruction or the required JSON response format, so
    no matter what this text says, the AI's output is still constrained to
    exactly {"sentiment": "POSITIVE|NEGATIVE|NEUTRAL", "reason": "..."} and
    can still only ever influence ai_sentiment.get_ai_sentiment()'s directional
    read - never order quantity, instrument, or anything else.
    """
    today = datetime.date.today().strftime("%d %B %Y")
    prompt = (
        f"Today is {today}. For INTRADAY NIFTY 50 trading today, research and weigh these "
        f"specific factors together:\n"
        f"1) Global cues: US market close (Dow Jones/S&P 500/Nasdaq) and today's Asian market "
        f"trend (Nikkei, Hang Seng, SGX Nifty/GIFT Nifty futures).\n"
        f"2) Crude oil price movement (Brent/WTI) - India is a major oil importer, so rising "
        f"crude is generally negative for NIFTY.\n"
        f"3) USD/INR currency movement - a weakening rupee is generally negative for NIFTY.\n"
        f"4) Any major scheduled event today or this week that could cause unusual volatility: "
        f"RBI monetary policy, US Fed/FOMC decision, big NIFTY-heavy-stock earnings, Union Budget, "
        f"elections, or geopolitical/war escalation news.\n"
        f"5) General NIFTY 50 / Indian market news and sector sentiment.\n"
    )

    next_point = 6
    if market_context:
        resistance_distance = market_context.get("resistance_distance")
        support_distance = market_context.get("support_distance")
        resistance_oi_chg = market_context.get("resistance_oi_change_ratio")
        support_oi_chg = market_context.get("support_oi_change_ratio")
        moved_from_open = market_context.get("points_moved_from_open")
        momentum_frac = market_context.get("momentum_used_fraction")
        vol_confidence = market_context.get("volatility_confidence")

        # Each optional detail is built as its own plain string FIRST (not
        # woven as a bare if/else into the middle of an f-string
        # concatenation chain below) - mixing a conditional expression into
        # adjacent string-literal concatenation silently changes which
        # lines end up in the prompt depending on operator precedence, which
        # is exactly the kind of "looks right, isn't" bug this project's
        # own testing discipline exists to catch.
        resistance_extra = ""
        if resistance_distance is not None:
            resistance_extra += f" ({resistance_distance:.0f} points away from spot)"
        if resistance_oi_chg is not None:
            resistance_extra += (
                f", OI {(resistance_oi_chg - 1) * 100:+.0f}% vs yesterday close "
                f"(fresh build-up if positive, unwinding if negative)"
            )

        support_extra = ""
        if support_distance is not None:
            support_extra += f" ({support_distance:.0f} points away from spot)"
        if support_oi_chg is not None:
            support_extra += (
                f", OI {(support_oi_chg - 1) * 100:+.0f}% vs yesterday close "
                f"(fresh build-up if positive, unwinding if negative)"
            )

        if moved_from_open is not None:
            realized_line = f"   - Already realized today: {moved_from_open:+.0f} points from today's open"
            if momentum_frac is not None:
                realized_line += f" ({momentum_frac * 100:.0f}% of the VIX-implied expected move already used up)"
            realized_line += "\n"
        else:
            realized_line = ""

        vol_confidence_str = f"{vol_confidence:.0f}/100" if vol_confidence is not None else "unavailable"

        prompt += (
            f"{next_point}) Quantitative option-chain context already computed for right now (factor this "
            f"in too, alongside the above):\n"
            f"   - NIFTY spot: {market_context.get('spot')}\n"
            f"   - India VIX: {market_context.get('vix')}\n"
            f"   - VIX-implied expected move for today (anchored to today's open): "
            f"+/-{market_context.get('expected_move', 0):.0f} points\n"
            f"   - Nearest resistance (heaviest OTM Call OI wall): {market_context.get('resistance_strike')}{resistance_extra}\n"
            f"   - Nearest support (heaviest OTM Put OI wall): {market_context.get('support_strike')}{support_extra}\n"
            f"   - Opening Range Breakout bias (today's price vs today's own opening-range high/low): "
            f"{market_context.get('orb_bias', 'NEUTRAL')}\n"
            f"{realized_line}"
            f"   - Volatility Confidence (independent 0-100 estimate of how likely a genuinely large move "
            f"is today, separate from direction): {vol_confidence_str}\n"
            f"   - Option-chain-implied odds: {market_context.get('upside_probability', 0.5) * 100:.0f}% "
            f"upside vs {market_context.get('downside_probability', 0.5) * 100:.0f}% downside\n"
        )
        next_point += 1

    if custom_note:
        prompt += f"{next_point}) Additional note from the trader, weigh this alongside everything above: {custom_note}\n"
        next_point += 1

    prompt += (
        f"Weigh all of these together and give ONE overall call: POSITIVE, NEGATIVE, or NEUTRAL, "
        f"with a short one-line reason naming the key driver(s). "
        f"Respond with ONLY this JSON, no other text: "
        f'{{"sentiment": "POSITIVE|NEGATIVE|NEUTRAL", "reason": "..."}}'
    )
    return prompt

def _normalise_gemini_model(model):
    """
    Map legacy / wrong model names to their correct Gemini API identifiers.
    Gemini API returns HTTP 400 for unrecognised model names, which was
    causing every AI sentiment call to fail silently with NEUTRAL.

    Canonical list as of mid-2025:
      gemini-2.0-flash      (fast, grounding-capable, recommended default)
      gemini-1.5-flash      (stable, grounding-capable)
      gemini-1.5-pro        (larger)
      gemini-2.5-pro-preview-06-05
    """
    _ALIAS = {
        # Shut-down / legacy / wrong names → current live model (gemini-3.5-flash)
        "gemini-flash-latest":     "gemini-3.5-flash",
        "gemini-flash":            "gemini-3.5-flash",
        "gemini-1.5-flash":        "gemini-3.5-flash",
        "gemini-1.5-flash-latest": "gemini-3.5-flash",
        "gemini-2.0-flash":        "gemini-3.5-flash",  # shut down Oct 2026
        "gemini-2.0-flash-latest": "gemini-3.5-flash",
        "gemini-2.0-flash-exp":    "gemini-3.5-flash",
        "gemini-2.0-flash-lite":   "gemini-3.5-flash",
        "gemini-2.5-flash":        "gemini-3.5-flash",  # superseded
        "gemini-2.5-flash-lite":   "gemini-3.5-flash",
        "gemini-pro":              "gemini-3.5-flash",
        "gemini-1.0-pro":          "gemini-3.5-flash",
    }
    return _ALIAS.get(model, model)  # pass unknown names through unchanged
_SAFE_DEFAULT = {"sentiment": "NEUTRAL", "reason": "safe default (no successful AI response)"}

_JSON_OBJ_RE = re.compile(r"\{.*?\}", re.DOTALL)


def _parse_sentiment_json(text):
    """Extract {"sentiment": ..., "reason": ...} from a free-form AI reply."""
    match = _JSON_OBJ_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return None

    sentiment = str(data.get("sentiment", "")).strip().upper()
    if sentiment not in VALID_SENTIMENTS:
        return None

    return {"sentiment": sentiment, "reason": str(data.get("reason", "")).strip()}


def _call_gemini(settings, market_context=None, custom_note=None, timeout=45, log=print):
    api_key = settings["gemini_api_key"]
    raw_model = settings["gemini_model"]
    model = _normalise_gemini_model(raw_model)
    if model != raw_model:
        log(f"[ai_sentiment] gemini_model {raw_model!r} normalised to {model!r} "
            f"(update settings.json to silence this warning)")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    prompt = _fixed_prompt(market_context, custom_note)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
    }
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    if resp.status_code == 429:
        # Google Search grounding has its own, much smaller quota than the
        # base model on the free tier and can run out independently of it
        # (confirmed: the identical prompt without the google_search tool
        # still returns 200). Rather than let one exhausted quota force a
        # hardcoded NEUTRAL every single run, retry once WITHOUT grounding -
        # still exactly one AI opinion for this run, just without today's
        # live web results factored in; the numeric market_context (VIX,
        # OI walls, probability) still gets weighed either way.
        log("[ai_sentiment] Google Search grounding quota exhausted (429) - "
            "retrying this same call without live web search")
        body_no_search = {"contents": [{"parts": [{"text": prompt}]}]}
        resp = requests.post(url, headers=headers, json=body_no_search, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts)
    return text


def _call_claude(settings, market_context=None, custom_note=None, timeout=45):
    api_key = settings["anthropic_api_key"]
    model = settings["claude_model"]
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": 1536,
        "messages": [{"role": "user", "content": _fixed_prompt(market_context, custom_note)}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )
    return text


def get_ai_sentiment(settings, market_context=None, log=print):
    """
    Returns {"sentiment": "POSITIVE"|"NEGATIVE"|"NEUTRAL", "reason": str}.
    Never raises - any network/parse/config failure falls back to a safe
    NEUTRAL result so a flaky AI call can never crash or silently bias a
    live trading run.

    market_context: optional dict of already-computed VIX/option-chain
    numbers (see _fixed_prompt) - passed into this SAME single call, never
    triggering a second AI request.

    settings["custom_ai_note"], if set, is appended to that same prompt as
    one more factor to weigh (see _fixed_prompt) - it never replaces the
    fixed instruction or required response format.
    """
    provider = settings.get("ai_provider", "gemini")
    custom_note = (settings.get("custom_ai_note") or "").strip() or None
    try:
        if provider == "gemini":
            raw_text = _call_gemini(settings, market_context=market_context, custom_note=custom_note, log=log)
        elif provider == "claude":
            raw_text = _call_claude(settings, market_context=market_context, custom_note=custom_note)
        else:
            log(f"[ai_sentiment] unknown ai_provider {provider!r}, using safe default")
            return dict(_SAFE_DEFAULT)

        parsed = _parse_sentiment_json(raw_text)
        if parsed is None:
            log(f"[ai_sentiment] could not parse AI response, using safe default. raw={raw_text!r}")
            return dict(_SAFE_DEFAULT)

        log(f"[ai_sentiment] provider={provider} sentiment={parsed['sentiment']} reason={parsed['reason']!r}")
        return parsed

    except Exception as exc:  # noqa: BLE001 - intentionally broad: never crash the run
        log(f"[ai_sentiment] call failed ({exc!r}), using safe default")
        return dict(_SAFE_DEFAULT)


def build_prompt_preview(custom_note=None):
    """
    Public, read-only preview of the exact fixed prompt (minus the
    market_context section, which only exists once numbers are computed
    mid-Execute) - for the dashboard's "Advanced: AI & Strategy" section, so
    the prompt is no longer invisible/hidden. Never used for an actual AI
    call - get_ai_sentiment() builds its own fresh copy at Execute time.
    """
    return _fixed_prompt(market_context=None, custom_note=custom_note)
