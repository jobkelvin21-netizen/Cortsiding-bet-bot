import re
import difflib
from datetime import datetime
from typing import Callable, Dict, Optional
from dataclasses import dataclass
from loguru import logger
from config import Config


def normalize_team_name(name: str) -> str:
    if not name:
        return ''
    name = name.lower().strip()
    name = re.sub(r'\b(fc|cf|sc|afc|cd|ac|fk|nk|sk|as|ss|rc|ca|cs|us|sd)\b', '', name)
    name = re.sub(r'\b(united|city|town|rovers|athletic|sporting|club)\b', '', name)
    name = re.sub(r'\b(u1[0-9]|u2[0-3]|u21|u20|u19|u18|u17|u16|u15)\b', '', name)
    name = re.sub(r'[^a-z0-9]', '', name)
    return name


def _is_paused(period: str) -> bool:
    if not period:
        return False
    p = str(period).upper().strip()
    return p in ('HT', 'HALFTIME', 'BREAK', 'PAUSED', 'HALF TIME')


@dataclass
class MatchRecord:
    key: str
    home_team: str = ''
    away_team: str = ''

    fast_played: Optional[float] = None
    fast_period: str = ''
    fast_score: tuple = (0, 0)
    fast_updated: Optional[datetime] = None

    sb_event_id: Optional[str] = None
    sb_played: Optional[float] = None
    sb_period: str = ''
    sb_score: tuple = (0, 0)
    sb_updated: Optional[datetime] = None

    below_streak: int = 0


class SlowGameDetector:
    def __init__(self):
        self.records: Dict[str, MatchRecord] = {}
        self.slow_games: Dict[str, dict] = {}
        self.threshold = Config.SLOW_THRESHOLD_SECONDS
        self.UNFLAG_STREAK = 2
        self.MAX_FAST_AGE = 8.0
        self.MAX_SB_AGE = 7.0
        self.FUZZY_MATCH_THRESHOLD = 0.82

    def _base_key(self, home: str, away: str) -> str:
        h = normalize_team_name(home)
        a = normalize_team_name(away)
        if h > a:
            h, a = a, h
        return f"{h}_vs_{a}"

    def _fuzzy_find_key(self, base: str) -> Optional[str]:
        """Fall back to closest-matching existing key if no exact match found."""
        if not self.records:
            return None

        candidates = list(self.records.keys())
        best_match = difflib.get_close_matches(base, candidates, n=1, cutoff=self.FUZZY_MATCH_THRESHOLD)

        if candidates:
            scored = [(c, difflib.SequenceMatcher(None, base, c).ratio()) for c in candidates]
            scored.sort(key=lambda x: x[1], reverse=True)
            top_key, top_score = scored[0]
            logger.debug(f"[FUZZY] new='{base}' closest='{top_key}' score={top_score:.3f} "
                         f"(threshold={self.FUZZY_MATCH_THRESHOLD})")

        if best_match:
            logger.debug(f"[FUZZY] MATCHED '{base}' -> existing key '{best_match[0]}'")
            return best_match[0]

        return None

    def _resolve_key(self, home: str, away: str, league: str = '') -> str:
        base = self._base_key(home, away)
        league_part = re.sub(r'[^a-z0-9]', '', (league or '').lower())
        full = f"{league_part}_{base}" if league_part else base

        if full in self.records:
            return full

        for k in self.records:
            if k.endswith(base) or k == base:
                return k

        fuzzy_key = self._fuzzy_find_key(base)
        if fuzzy_key:
            return fuzzy_key

        return full

    def _get_or_create(self, key: str, home: str, away: str) -> MatchRecord:
        if key not in self.records:
            self.records[key] = MatchRecord(key=key, home_team=home, away_team=away)
        return self.records[key]

    async def on_bet365(self, data: dict, callback: Optional[Callable] = None):
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            key = self._resolve_key(home, away, data.get('league', ''))
            played = data.get('played_seconds')

            logger.info(f"[FAST] {home} vs {away} -> key={key} played={played}")

            rec = self._get_or_create(key, home, away)
            rec.fast_played = played
            rec.fast_period = data.get('period', '')
            rec.fast_score = (data.get('home_score', 0), data.get('away_score', 0))
            rec.fast_updated = datetime.now()

            was_slow = key in self.slow_games
            self._evaluate(key)

            if not was_slow and key in self.slow_games and callback:
                await callback(self.slow_games[key])

        except Exception as e:
            logger.error(f"Fast feed error: {e}")

    async def on_sportybet(self, data: dict, callback: Optional[Callable] = None):
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            key = self._resolve_key(home, away)
            played = data.get('played_seconds')

            logger.info(f"[SB] {home} vs {away} -> key={key} played={played}")

            rec = self._get_or_create(key, home, away)
            rec.sb_event_id = data.get('match_id')
            rec.sb_played = played
            rec.sb_period = data.get('match_status') or data.get('period', '')
            rec.sb_score = (data.get('home_score', 0), data.get('away_score', 0))
            rec.sb_updated = datetime.now()

            was_slow = key in self.slow_games
            self._evaluate(key)

            if not was_slow and key in self.slow_games and callback:
                await callback(self.slow_games[key])

        except Exception as e:
            logger.error(f"SportyBet feed error: {e}")

    def _evaluate(self, key: str):
        rec = self.records.get(key)
        if not rec:
            return

        if (rec.fast_played is None or rec.sb_played is None or
            rec.fast_updated is None or rec.sb_updated is None):
            return

        if _is_paused(rec.fast_period) or _is_paused(rec.sb_period):
            return

        now = datetime.now()
        fast_age = (now - rec.fast_updated).total_seconds()
        sb_age = (now - rec.sb_updated).total_seconds()

        if fast_age > self.MAX_FAST_AGE or sb_age > self.MAX_SB_AGE:
            return

        gap = rec.fast_played - rec.sb_played

        if gap >= self.threshold:
            rec.below_streak = 0
            if key not in self.slow_games:
                self.slow_games[key] = {
                    'match_id': rec.sb_event_id or key,
                    'home_team': rec.home_team,
                    'away_team': rec.away_team,
                    'gap_seconds': gap,
                    '_last_score': rec.sb_score,
                }
                logger.info(f"⚡ SLOW: {rec.home_team} vs {rec.away_team} (lag {gap:.1f}s)")
            else:
                self.slow_games[key]['gap_seconds'] = gap
        else:
            if key in self.slow_games:
                rec.below_streak += 1
                if rec.below_streak >= self.UNFLAG_STREAK:
                    logger.info(f"No longer slow: {rec.home_team} vs {rec.away_team}")
                    del self.slow_games[key]
                    rec.below_streak = 0

    def is_slow(self, match_id: str) -> bool:
        for v in self.slow_games.values():
            if v.get('match_id') == match_id:
                return True
        return match_id in self.slow_games

    def get(self, match_id: str) -> Optional[dict]:
        for v in self.slow_games.values():
            if v.get('match_id') == match_id:
                return v
        return self.slow_games.get(match_id)
