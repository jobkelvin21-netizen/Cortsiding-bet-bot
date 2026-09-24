"""
Groq AI — plan Over market only from SportyBet page text.
Knows real SportyBet headers from live UI:
  - "1st Half - Over/Under" / "2nd Half - Over/Under"
  - plain "Over/Under" (full time)
Never team totals, Rest of Match, Under, handicap, etc.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

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
    t = (page_text or "").lower()
    sample = t[:2000]

    if re.search(r"\b2nd\s*half\b|\bsecond\s*half\b", sample):
        return "2H"
    if re.search(r"\bhalf\s*time\b|\bhalf-time\b|\bht\b", sample):
        return "HT"
    if re.search(r"\b1st\s*half\b|\bfirst\s*half\b", sample):
        return "1H"

    m = re.search(r"\b(\d{1,3})\s*'\s*(?:\+\d+)?\b", sample)
    if not m:
        m = re.search(r"\b(\d{1,2})\s*:\s*\d{0,2}\b", sample)
    if m:
        try:
            minute = int(m.group(1))
            if minute >= 46:
                return "2H"
            if 1 <= minute <= 45:
                return "1H"
        except Exception:
            pass

    p = (period or "").lower()
    if any(x in p for x in ("2h", "2nd", "second half")):
        return "2H"
    if any(x in p for x in ("ht", "half time", "half-time")):
        return "HT"
    if any(x in p for x in ("1h", "1st", "first half")):
        return "1H"
    if any(x in p for x in ("ft", "full time", "ended", "finished")):
        return "FT"
    return "1H"


def _extract_score_hint(page_text: str) -> Optional[Tuple[int, int]]:
    sample = (page_text or "")[:2000]
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
    Choose Over only using SportyBet's real market headers.
    Fallbacks are period-based only (1H→FT, 2H→FT), never random markets.
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
Try in this exact order (STOP at first that exists on PAGE):
  1) Header exactly like "1st Half - Over/Under" or "1st Half Over/Under"
     → selection Over {hint_target} (or nearest Over line under that header)
  2) If that header is missing → plain "Over/Under" (full time, NOT team name)
     → selection Over {hint_target}
FORBIDDEN in 1H:
  - Any "2nd Half" market
  - Under
  - "Rest of the Match"
  - Team-named Over/Under (e.g. "{home} Over/Under", player/team totals)
  - Handicap, Asian, 1X2, Double Chance, Correct Score, GG/NG, Odd/Even
"""
    elif per == "2H":
        period_rules = f"""
PERIOD = SECOND HALF.
Try in this exact order:
  1) "2nd Half - Over/Under" / "2nd Half Over/Under" → Over {hint_target}
  2) Else plain "Over/Under" (full time, not team name) → Over {hint_target}
FORBIDDEN:
  - 1st Half markets
  - Under, Rest of Match, team totals, handicap, etc.
"""
    elif per == "HT":
        period_rules = f"""
PERIOD = HALF TIME.
Only plain full-time "Over/Under" → Over {hint_target}.
No 1st/2nd Half markets while HT.
"""
    else:
        period_rules = f"""
PERIOD = {per}.
Only plain full-time "Over/Under" → Over {hint_target}.
"""

    prompt = f"""
You are reading a SportyBet LIVE football match page for:
{home} vs {away}

Score hint (VERIFY on page near teams/clock, usually H:A or H-A): {hint_h}-{hint_a}
Period: {per}
Balance: {balance}
MAX_PROFIT_PER_BET: {max_profit}

HOW SPORTYBET MARKETS LOOK ON THIS SITE:
- Section title then green buttons, e.g.:
  "1st Half - Over/Under"
     Over 0.5 | Under 0.5
     Over 1.5 | Under 1.5
  "Over/Under"   ← full time match goals
     Over 1.5 | Under 1.5
     Over 2.5 | Under 2.5
  "2nd Half - Over/Under"
     Over ...
- Team totals look like: "{home} Over/Under" or long team name + Over/Under → IGNORE
- "Rest of the Match (current score ...)" → IGNORE

{period_rules}

STRICT:
- selection = Over only (never Under)
- line = (score you read total) + 0.5, must match a line that exists under the chosen header
- period_market must be "1H" or "2H" or "FT"
- market_found=false if none of the allowed headers+Over lines exist
- reason must name the exact header you used and the Over line
- stake = min(balance, max_profit / (odds - 1)), minimum 10

Return JSON ONLY:
{{
  "market_found": true,
  "market_name": "1st Half - Over/Under Over {hint_target}",
  "line": {hint_target},
  "selection": "Over",
  "period_market": "1H",
  "score_home": {hint_h},
  "score_away": {hint_a},
  "odds": 1.85,
  "stake": 0,
  "reason": "why this header and line"
}}

PAGE TEXT:
{page_text[:9500]}
"""
    try:
        resp = client.chat.completions.create(
            model=getattr(Config, "GROQ_TEXT_MODEL", "llama-3.3-70b-versatile"),
            temperature=0,
            max_tokens=450,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _parse_json(resp.choices[0].message.content or "")
        if not data.get("market_found"):
            return data

        name = (data.get("market_name") or "").lower()
        pmarket = (data.get("period_market") or "").upper()

        # Hard reject illegal period
        if per == "1H" and (
            pmarket == "2H" or "2nd half" in name or "second half" in name
        ):
            return {
                "market_found": False,
                "error": "rejected_2h_in_1h",
                "reason": data.get("reason", ""),
            }
        if per == "2H" and (
            pmarket == "1H" or "1st half" in name or "first half" in name
        ):
            return {
                "market_found": False,
                "error": "rejected_1h_in_2h",
                "reason": data.get("reason", ""),
            }

        # Reject team totals / ROM in name
        if "rest of" in name or re.search(
            r"\b(handicap|asian|double chance|correct score|gg/ng|odd/even)\b", name
        ):
            return {
                "market_found": False,
                "error": "rejected_forbidden_market",
                "reason": data.get("reason", ""),
            }

        try:
            sh = int(data.get("score_home"))
            sa = int(data.get("score_away"))
            if not (0 <= sh <= 15 and 0 <= sa <= 15):
                raise ValueError("bad score")
        except Exception:
            sh, sa = hint_h, hint_a

        data["score_home"] = sh
        data["score_away"] = sa
        data["line"] = float(sh + sa) + 0.5
        data["selection"] = "Over"

        odds = float(data.get("odds") or 0)
        if odds > 1.01:
            stake = min(float(balance or 0) or 10**9, float(max_profit) / (odds - 1.0))
            data["stake"] = round(max(10.0, stake), 2)
        return data
    except Exception as e:
        logger.error(f"[GROQ][MARKET] {e}")
        return {"market_found": False, "error": str(e)}


async def analyze_tickers(*args, **kwargs) -> Dict[str, Any]:
    return {"links": [], "error": "manual_slow_mode"}


class SlowGameAI:
    def __init__(self):
        self.locked: Optional[dict] = None

    def clear(self, reason: str = ""):
        self.locked = None
