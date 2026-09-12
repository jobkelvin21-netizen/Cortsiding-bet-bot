import re
import difflib
import unicodedata
from datetime import datetime
from typing import Callable, Dict, Optional
from dataclasses import dataclass
from loguru import logger


def strip_accents(text: str) -> str:
    if not text:
        return ''
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def normalize_team_name(name: str) -> str:
    """
    Turns raw team names from either feed into a comparable key.
    Handles the fact that Polymarket and SportyBet format team names very
    differently (e.g. 'Man United' vs 'Manchester United FC').
    """
    if not name:
        return ''
    name = strip_accents(name).lower().strip()
    words = re.findall(r'[a-z0-9]+', name)

    filler = {
        'fc', 'cf', 'sc', 'afc', 'cd', 'ac', 'fk', 'nk', 'sk', 'as', 'ss',
        'rc', 'ca', 'cs', 'us', 'sd', 'united', 'city', 'town', 'rovers',
        'athletic', 'sporting', 'club'
    }
    words = [w for w in words if w not in filler]
    words = [w for w in words if not re.match(r'^u1[5-9]$|^u2[0-3]$', w)]

    # Only strip a trailing short code (state/region tag, common in
    # SportyBet's lower-league naming) if it's not the only word left —
    # protects genuinely short names like "PSG".
    if len(words) > 1 and len(words[-1]) <= 3:
        words = words[:-1]

    return ''.join(words)


@dataclass
class MatchRecord:
    key: str
    home_team: str = ''
    away_team: str = ''

    fast_score: tuple = (0, 0)
    fast_updated: Optional[datetime] = None

    sb_event_id: Optional[str] = None
    sb_score: tuple = (0, 0)
    sb_updated: Optional[datetime] = None

    matched_notified: bool = False


class GoalMatchDetector:
    """
    Match-pairing + goal-event architecture:
    1. Tracks matches live on BOTH Polymarket and SportyBet at once.
    2. Fires 'matched_callback' the first time a match is confirmed live
       on both sides — used to send you a Telegram notification.
    3. Fires 'goal_callback' whenever Polymarket's score total increases
       for an already-matched match.
    4. is_market_still_valid() lets main.py check, right before betting,
       whether SportyBet's own feed already caught up to this goal (in
       which case their market has moved on and betting would hit the
       wrong market).
    """

    def __init__(self):
        self.records: Dict[str, MatchRecord] = {}
        self.FUZZY_MATCH_THRESHOLD = 0.85
        self.RECORD_EXPIRE_AFTER = 6 * 60 * 60

    def _base_key(self, home: str, away: str) -> str:
        h = normalize_team_name(home)
        a = normalize_team_name(away)
        if h > a:
            h, a = a, h
        return f"{h}_vs_{a}"

    def _fuzzy_find_key(self, base: str) -> Optional[str]:
        if not self.records:
            return None
        candidates = list(self.records.keys())
        best = difflib.get_close_matches(base, candidates, n=1, cutoff=self.FUZZY_MATCH_THRESHOLD)
        if best:
            logger.debug(f"[FUZZY MATCH] '{base}' -> existing '{best[0]}'")
            return best[0]
        return None

    def _resolve_key(self, home: str, away: str) -> str:
        base = self._base_key(home, away)
        if base in self.records:
            return base
        fuzzy = self._fuzzy_find_key(base)
        return fuzzy if fuzzy else base

    def _prune_expired(self):
        now = datetime.now()
        expired = []
        for key, rec in self.records.items():
            last_seen = max(rec.fast_updated or datetime.min, rec.sb_updated or datetime.min)
            if last_seen == datetime.min:
                continue
            if (now - last_seen).total_seconds() > self.RECORD_EXPIRE_AFTER:
                expired.append(key)
        for key in expired:
            del self.records[key]

    def _get_or_create(self, key: str, home: str, away: str) -> MatchRecord:
        if key not in self.records:
            self._prune_expired()
            self.records[key] = MatchRecord(key=key, home_team=home, away_team=away)
        return self.records[key]

    async def on_bet365(self, data: dict, matched_callback: Optional[Callable] = None,
                         goal_callback: Optional[Callable] = None):
        """Callback target for the fast feed (Polymarket)."""
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            key = self._resolve_key(home, away)
            new_score = (data.get('home_score', 0), data.get('away_score', 0))

            rec = self._get_or_create(key, home, away)
            prev_score = rec.fast_score
            rec.fast_score = new_score
            rec.fast_updated = datetime.now()

            now_matched = rec.sb_updated is not None
            if now_matched and not rec.matched_notified:
                rec.matched_notified = True
                logger.info(f"✅ MATCHED: {home} vs {away} (live on both Polymarket & SportyBet)")
                if matched_callback:
                    await matched_callback(rec)

            if now_matched and sum(new_score) > sum(prev_score):
                scoring_side = 'home' if new_score[0] > prev_score[0] else 'away'
                logger.info(
                    f"⚡ GOAL (Polymarket): {home} vs {away} "
                    f"{prev_score} -> {new_score} ({scoring_side} scored)"
                )
                if goal_callback:
                    await goal_callback(rec, scoring_side, prev_score, new_score)

        except Exception as e:
            logger.error(f"Fast feed error: {e}")

    async def on_sportybet(self, data: dict, matched_callback: Optional[Callable] = None):
        """Callback target for feeds/sportybet_api.py."""
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            key = self._resolve_key(home, away)
            rec = self._get_or_create(key, home, away)

            rec.sb_event_id = data.get('match_id')
            rec.sb_score = (data.get('home_score', 0), data.get('away_score', 0))
            rec.sb_updated = datetime.now()

            now_matched = rec.fast_updated is not None
            if now_matched and not rec.matched_notified:
                rec.matched_notified = True
                logger.info(f"✅ MATCHED: {home} vs {away} (live on both Polymarket & SportyBet)")
                if matched_callback:
                    await matched_callback(rec)

        except Exception as e:
            logger.error(f"SportyBet feed error: {e}")

    def is_market_still_valid(self, key: str, expected_total_before_goal: int) -> bool:
        """
        Safety check: only bet if SportyBet's own last-known score total
        still matches what it was BEFORE the goal Polymarket just reported.
        If SportyBet already shows the goal (or more), their market has
        already moved on to the next goal number — betting now would hit
        the wrong market.
        """
        rec = self.records.get(key)
        if not rec:
            return False
        sb_total = sum(rec.sb_score)
        return sb_total <= expected_total_before_goal

    def get(self, key: str) -> Optional[MatchRecord]:
        return self.records.get(key)

    def get_by_match_id(self, match_id: str) -> Optional[MatchRecord]:
        for rec in self.records.values():
            if rec.sb_event_id == match_id:
                return rec
        return None
