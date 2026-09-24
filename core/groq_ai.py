"""
Groq AI — plan Over market only (strict period rules).
Manual slow; no ticker lock required for execution.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from groq import Groq
from loguru import logger

from config import Config

_client: Optional[Groq] = None


def _client_or_none() -> Optional[Groq]:
    global _client
    if _client is not None:
        return _client
    key = (Config.GROQ_API_KEY or "").strip()
    if not key:
        logger.error("[GROQ] GROQ_API_KEY missing")
        return None
    _client = Groq(api_key=key)
    return _client


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _normalize_period(period: str, page_text: str = "") -> str:
    """
    Live page truth ALWAYS wins over a stale hint. The hint (watch.period)
    is only used as a last resort when the page itself gives no clear signal.
    """
    t = (page_text or "").lower()

    # 1. Look at the live page first — this is the actual match state.
    sample = t[:1500]  # clock/score area is always near the top of the page

    is_2h_page = bool(
        re.search(r"\b2nd\s*half\b|\bsecond\s*half\b", sample)
    )
    is_1h_page = bool(
        re.search(r"\b1st\s*half\b|\bfirst\s*half\b", sample)
    )
    is_ht_page = bool(
        re.search(r"\bht\b|\bhalf\s*time\b|\bhalf-time\b", sample)
    )

    # Minute-based fallback: "46'" or higher means 2nd half has started.
    minute = None
    m = re.search(r"\b(\d{1,3})\s*'\s*(?:\+\d+)?\b", sample)
    if not m:
        m = re.search(r"\b(\d{1,2})\s*:\s*\d{0,2}\b", sample)
    if m:
        try:
            minute = int(m.group(1))
        except Exception:
            minute = None

    if is_2h_page:
        return "2H"
    if is_ht_page:
        # Half time has passed — treat as heading into 2nd half.
        return "2H"
    if minute is not None and minute >= 46:
        return "2H"
    if is_1h_page:
        return "1H"
    if minute is not None and minute <= 45:
        return "1H"

    # 2. Page gave nothing usable — fall back to the hint.
    p = (period or "").lower()
    if any(x in p for x in ("2h", "2nd", "second half", "2nd half")):
        return "2H"
    if any(x in p for x in ("1h", "1st", "first half", "1st half")):
        return "1H"
    if any(x in p for x in ("ht", "half time", "half-time")):
        return "HT"
    if any(x in p for x in ("ft", "full time", "ended", "finished")):
        return "FT"

    return "1H"


def _extract_score_hint(page_text: str) -> Optional[Tuple[int, int]]:
    """
    Cheap regex pre-scan so Groq isn't starting from nothing — same idea
    as _normalize_period's minute-based fallback. This is only a HINT:
    the prompt tells Groq to verify it against the PAGE text and correct
    it if wrong. Groq is still the one doing the actual "reading of the
    score" decision; this just gives it a fast starting point and lets us
    sanity-check its answer afterward.
    """
    t = page_text or ""
    sample = t[:1500]

    m = re.search(r"\b(\d{1,2})\s*:\s*(\d{1,2})\b", sample)
    if not m:
        m = re.search(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b", sample)
    if m:
        try:
            h, a = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 15 and 0 <= a <= 15:
                return h, a
        except Exception:
            pass
    return None


def plan_over_market(
    page_text: str,
    home: str,
    away: str,
    home_score: int,
    away_score: int,
    period: str,
    max_profit: float,
    balance: float,
) -> Dict[str, Any]:
    """
    Choose Over only.
    1H: 1st-half Over if present, else Full-time Over. NEVER 2nd-half Over.
    2H: 2nd-half Over if present, else Full-time Over.
    Stake = full hard cap.

    Groq also READS the current score off the page itself (score_home /
    score_away in the response) rather than blindly trusting whatever was
    passed in — the passed-in home_score/away_score and the regex hint
    below are just starting points it's told to verify and correct.
    """
    client = _client_or_none()
    if not client:
        return {"market_found": False, "error": "no_api_key"}

    per = _normalize_period(period, page_text)

    hint = _extract_score_hint(page_text)
    if hint:
        hint_h, hint_a = hint
    else:
        hint_h, hint_a = int(home_score), int(away_score)
    hint_target = float(hint_h + hint_a) + 0.5

    if per == "1H":
        period_rules = f"""
PERIOD = FIRST HALF.
ALLOWED (in order):
  1) 1st Half / First Half Over {hint_target} (or nearest Over line for 1H goals)
  2) If 1st Half Over NOT on page → Full Time / Match Over {hint_target}
FORBIDDEN:
  - Any 2nd Half / Second Half market (NEVER in first half)
  - Under (never)
"""
    elif per == "2H":
        period_rules = f"""
PERIOD = SECOND HALF.
ALLOWED (in order):
  1) 2nd Half / Second Half Over {hint_target}
  2) If not on page → Full Time / Match Over {hint_target}
FORBIDDEN:
  - 1st Half markets
  - Under
"""
    else:
        period_rules = f"""
PERIOD = {per}.
Use Full Time / Match Over {hint_target} only. Never Under.
"""

    prompt = f"""
SportyBet live page for {home} vs {away}.
Score hint from a quick scan (VERIFY this against the PAGE text below and
correct it if it's wrong — read the actual score shown on the page):
{hint_h}-{hint_a}.
Period hint: {per}.
Balance: {balance}. MAX_PROFIT_PER_BET: {max_profit}.

{period_rules}

STRICT:
- Read the ACTUAL current score from the PAGE text below (it is shown near
  the team names / match clock, usually as "H : A"). Use that as ground
  truth — return it as score_home / score_away.
- selection must be Over only (never Under).
- line should be (the score you actually read) total + 0.5.
- stake = min(balance, max_profit / (odds - 1)). Minimum 10.
- market_found=false if no valid Over market on page for the required period.
- reason must say which market you picked, the score you read, and why.

Return JSON ONLY:
{{
  "market_found": true,
  "market_name": "1st Half Over {hint_target}",
  "line": {hint_target},
  "selection": "Over",
  "period_market": "1H",
  "score_home": {hint_h},
  "score_away": {hint_a},
  "odds": 1.85,
  "stake": 0,
  "reason": ""
}}
period_market must be one of: "1H", "2H", "FT".

PAGE:
{page_text[:9000]}
"""
    try:
        resp = client.chat.completions.create(
            model=getattr(Config, "GROQ_TEXT_MODEL", "llama-3.3-70b-versatile"),
            temperature=0,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _parse_json(resp.choices[0].message.content or "")
        if not data.get("market_found"):
            return data

        # Hard reject illegal 2H in 1H
        name = (data.get("market_name") or "").lower()
        pmarket = (data.get("period_market") or "").upper()
        if per == "1H" and (
            pmarket == "2H"
            or "2nd half" in name
            or "second half" in name
        ):
            logger.warning(f"[GROQ] rejected illegal 2H market in 1H: {data}")
            return {
                "market_found": False,
                "error": "rejected_2h_in_1h",
                "reason": data.get("reason", ""),
            }

        # Groq read (or was given) the score — sanity-check it, and always
        # recompute "line" deterministically from that score rather than
        # trusting the LLM's own arithmetic for it. This is the one place
        # we don't just take Groq's word for it, since a wrong line value
        # is the one thing that must never drift.
        try:
            sh = int(data.get("score_home"))
            sa = int(data.get("score_away"))
            if not (0 <= sh <= 15 and 0 <= sa <= 15):
                raise ValueError("out of range")
        except Exception:
            sh, sa = hint_h, hint_a  # fall back to the regex hint

        data["score_home"] = sh
        data["score_away"] = sa
        data["line"] = float(sh + sa) + 0.5

        odds = float(data.get("odds") or 0)
        if odds > 1.01:
            stake = min(float(balance), float(max_profit) / (odds - 1.0))
            data["stake"] = round(max(10.0, stake), 2)
        data["selection"] = "Over"
        return data
    except Exception as e:
        logger.error(f"[GROQ][MARKET] {e}")
        return {"market_found": False, "error": str(e)}


# Optional vision helpers kept for compatibility (manual slow does not need them)
async def analyze_tickers(
    bet365_png: bytes,
    sporty_png: bytes,
    lag_seconds: float = None,
) -> Dict[str, Any]:
    return {"links": [], "error": "manual_slow_mode"}


class SlowGameAI:
    def __init__(self):
        self.locked: Optional[dict] = None

    def clear(self, reason: str = ""):
        self.locked = None
