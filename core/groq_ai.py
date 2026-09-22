"""
Groq AI — plan Over market only (strict period rules).
Manual slow; no ticker lock required for execution.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, List, Optional

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
    p = (period or "").lower()
    t = (page_text or "").lower()
    if any(x in p for x in ("2h", "2nd", "second half", "2nd half")):
        return "2H"
    if any(x in p for x in ("1h", "1st", "first half", "1st half")):
        return "1H"
    if any(x in p for x in ("ht", "half time", "half-time")):
        return "HT"
    if any(x in p for x in ("ft", "full time", "ended", "finished")):
        return "FT"
    # Infer from page
    if re.search(r"\b2nd\s*half\b|\bsecond\s*half\b|\b2h\b", t) and not re.search(
        r"\b1st\s*half\b|\bfirst\s*half\b", t[:500]
    ):
        return "2H"
    if re.search(r"\b1st\s*half\b|\bfirst\s*half\b|\b1h\b", t):
        return "1H"
    return "1H"


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
    """
    client = _client_or_none()
    if not client:
        return {"market_found": False, "error": "no_api_key"}

    total = int(home_score) + int(away_score)
    target = float(total) + 0.5
    per = _normalize_period(period, page_text)

    if per == "1H":
        period_rules = f"""
PERIOD = FIRST HALF.
ALLOWED (in order):
  1) 1st Half / First Half Over {target} (or nearest Over line for 1H goals)
  2) If 1st Half Over NOT on page → Full Time / Match Over {target}
FORBIDDEN:
  - Any 2nd Half / Second Half market (NEVER in first half)
  - Under (never)
"""
    elif per == "2H":
        period_rules = f"""
PERIOD = SECOND HALF.
ALLOWED (in order):
  1) 2nd Half / Second Half Over {target}
  2) If not on page → Full Time / Match Over {target}
FORBIDDEN:
  - 1st Half markets
  - Under
"""
    else:
        period_rules = f"""
PERIOD = {per}.
Use Full Time / Match Over {target} only. Never Under.
"""

    prompt = f"""
SportyBet live page for {home} vs {away}.
Score: {home_score}-{away_score} (total goals = {total}).
Period hint: {per}.
Balance: {balance}. MAX_PROFIT_PER_BET: {max_profit}.

{period_rules}

STRICT:
- selection must be Over only (never Under).
- line should be {target} when that market exists (total+0.5).
- stake = min(balance, max_profit / (odds - 1)). Minimum 10.
- market_found=false if no valid Over market on page.
- reason must say which market you picked and why.

Return JSON ONLY:
{{
  "market_found": true,
  "market_name": "1st Half Over {target}",
  "line": {target},
  "selection": "Over",
  "period_market": "1H",
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
