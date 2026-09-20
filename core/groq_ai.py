"""
Groq AI:
  - Link matches + detect SLOW from both frontends' MATCH TICKERS only (≥3s)
  - Find Over market + stake (hard cap)
Playwright still clicks Confirm on Bet365 WS goal.
"""

from __future__ import annotations

import base64
import json
import re
import time
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


async def analyze_tickers(
    bet365_png: bytes,
    sporty_png: bytes,
    lag_seconds: float = None,
) -> Dict[str, Any]:
    """
    Human-style: compare match tickers on both UIs only.
    Returns links + is_slow (Bet365 ahead by >= lag_seconds).
    """
    lag_seconds = float(lag_seconds if lag_seconds is not None else Config.SLOW_LAG_SECONDS)
    client = _client_or_none()
    if not client:
        return {"links": [], "error": "no_api_key"}

    prompt = f"""
You compare Bet365 and SportyBet LIVE football screenshots.

STRICT RULES:
1) Match identity = team names (allow small spelling differences).
2) SLOW detection uses ONLY match clocks/tickers (e.g. 13:51 vs 1st|13:43).
3) Do NOT use score, odds, or markets to decide slow.
4) is_slow = true only if Bet365 ticker is AHEAD of SportyBet by >= {lag_seconds} seconds.
5) Convert MM:SS or M:SS to total seconds (13:51 -> 831).

Return JSON ONLY:
{{
  "links": [
    {{
      "bet365_home": "",
      "bet365_away": "",
      "sporty_home": "",
      "sporty_away": "",
      "bet365_ticker_seconds": 0,
      "sporty_ticker_seconds": 0,
      "lag_seconds": 0,
      "is_slow": false,
      "bet365_score": "",
      "sporty_score": "",
      "period": ""
    }}
  ]
}}
"""
    try:
        resp = client.chat.completions.create(
            model=Config.GROQ_VISION_MODEL,
            temperature=0,
            max_tokens=1400,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "text", "text": "IMAGE 1 = Bet365"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{_b64(bet365_png)}"
                            },
                        },
                        {"type": "text", "text": "IMAGE 2 = SportyBet"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{_b64(sporty_png)}"
                            },
                        },
                    ],
                }
            ],
        )
        raw = resp.choices[0].message.content or ""
        data = _parse_json(raw)
        links = data.get("links") or []
        for L in links:
            try:
                lag = float(L.get("lag_seconds") or 0)
            except Exception:
                lag = 0.0
            L["lag_seconds"] = lag
            L["is_slow"] = lag >= lag_seconds
        return {"links": links}
    except Exception as e:
        logger.error(f"[GROQ][VISION] {e}")
        return {"links": [], "error": str(e)}


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
    """AI chooses Over line + stake. Does not click Confirm."""
    client = _client_or_none()
    if not client:
        return {"market_found": False, "error": "no_api_key"}

    total = int(home_score) + int(away_score)
    target = total + 0.5

    prompt = f"""
SportyBet live betting page text for {home} vs {away}.
Score {home_score}-{away_score}. Period: {period}.
Balance: {balance}. MAX_PROFIT_PER_BET: {max_profit}.
Need OVER only (never Under). Preferred line Over {target}.
Prefer 1st-half Over if still first half and available; else full-time Over.

Return JSON ONLY:
{{
  "market_found": true,
  "market_name": "Over/Under {target}",
  "line": {target},
  "selection": "Over",
  "odds": 1.80,
  "stake": 0,
  "reason": ""
}}
stake = min(balance, max_profit / (odds - 1)). Minimum stake 10 if needed.
If market not on page: market_found=false.

PAGE:
{page_text[:9000]}
"""
    try:
        resp = client.chat.completions.create(
            model=Config.GROQ_TEXT_MODEL,
            temperature=0,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _parse_json(resp.choices[0].message.content or "")
        odds = float(data.get("odds") or 0)
        if data.get("market_found") and odds > 1.01:
            stake = min(float(balance), float(max_profit) / (odds - 1.0))
            data["stake"] = round(max(10.0, stake), 2)
        return data
    except Exception as e:
        logger.error(f"[GROQ][MARKET] {e}")
        return {"market_found": False, "error": str(e)}


class SlowGameAI:
    """One locked slow game from ticker AI. Unlock only not-slow / HT / FT."""

    def __init__(self):
        self.locked: Optional[dict] = None

    def clear(self, reason: str = ""):
        if self.locked:
            logger.warning(
                f"[SLOW] cleared {self.locked.get('sporty_home')} vs "
                f"{self.locked.get('sporty_away')} ({reason})"
            )
        self.locked = None

    def _is_ht_ft(self, period: str) -> bool:
        p = (period or "").lower()
        return any(x in p for x in ("ht", "half time", "half-time", "ft", "full time", "ended", "finished"))

    async def update(self, bet365_png: bytes, sporty_png: bytes) -> Optional[dict]:
        result = await analyze_tickers(bet365_png, sporty_png)
        links: List[dict] = result.get("links") or []

        if self.locked:
            period = ""
            still_slow = False
            for L in links:
                same = (
                    (L.get("sporty_home") or "").lower()
                    == (self.locked.get("sporty_home") or "").lower()
                    and (L.get("sporty_away") or "").lower()
                    == (self.locked.get("sporty_away") or "").lower()
                ) or (
                    (L.get("bet365_home") or "").lower()
                    == (self.locked.get("bet365_home") or "").lower()
                    and (L.get("bet365_away") or "").lower()
                    == (self.locked.get("bet365_away") or "").lower()
                )
                if same:
                    period = L.get("period") or period
                    if self._is_ht_ft(period):
                        self.clear("HT/FT")
                        return None
                    if L.get("is_slow"):
                        still_slow = True
                        self.locked.update(L)
            if not still_slow:
                self.clear("lag below threshold")
                return None
            return self.locked

        for L in links:
            if L.get("is_slow") and not self._is_ht_ft(L.get("period") or ""):
                self.locked = dict(L)
                self.locked["locked_at"] = time.time()
                logger.success(
                    f"[SLOW LOCK] {L.get('bet365_home')} vs {L.get('bet365_away')} "
                    f"lag={L.get('lag_seconds')}s (tickers only)"
                )
                return self.locked
        return None
