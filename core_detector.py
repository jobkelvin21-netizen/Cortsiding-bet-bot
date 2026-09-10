import re
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
    name = re.sub(r'[^a-z0-9]', '', name)
    return name


def _is_paused_period(period_text: str) -> bool:
    if not period_text:
        return False
    p = str(period_text).upper().strip()
    return p in ('HT', 'HALFTIME', 'BREAK', 'PAUSED', 'HALF TIME')


@dataclass
class MatchRecord:
    key: str
    home_team: str = ''
    away_team: str = ''

    fast_played_seconds: Optional[float] = None
    fast_period: str = ''
    fast_home_score: int = 0
    fast_away_score: int = 0
    fast_last_update: Optional[datetime] = None

    sb_event_id: Optional[str] = None
    sb_played_seconds: Optional[float] = None
    sb_period: str = ''
    sb_home_score: int = 0
    sb_away_score: int = 0
    sb_last_update: Optional[datetime] = None

    below_threshold_streak: int = 0


class SlowGameDetector:
    def __init__(self):
        self.records: Dict[str, MatchRecord] = {}
        self.slow_games: Dict[str, dict] = {}
        self.threshold = Config.SLOW_THRESHOLD_SECONDS
        self.UNFLAG_STREAK_REQUIRED = 2
        # Both feeds must have updated recently
        self.MAX_DATA_AGE_SECONDS = 12.0

    def _base_key(self, home: str, away: str) -> str:
        h = normalize_team_name(home)
        a = normalize_team_name(away)
        if h > a:
            h, a = a, h
        return f"{h}_vs_{a}"

    def _team_key(self, home: str, away: str, league: str = '') -> str:
        league_part = re.sub(r'[^a-z0-9]', '', league.lower().strip()) if league else ''
        base = self._base_key(home, away)
        return f"{league_part}_{base}" if league_part else base

    def _resolve_key(self, home: str, away: str, league: str = '') -> str:
        base = self._base_key(home, away)
        full = self._team_key(home, away, league)

        # Try exact full key
        if full in self.records:
            return full

        # Try base key match
        for existing_key in self.records:
            if existing_key.endswith(base) or existing_key == base:
                return existing_key

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

            league = data.get('league', '')
            key = self._resolve_key(home, away, league)
            played = data.get('played_seconds')

            logger.info(f"[FAST] {home} vs {away} -> key={key} played_seconds={played}")

            record = self._get_or_create(key, home, away)
            record.fast_played_seconds = played
            record.fast_period = data.get('period', '')
            record.fast_home_score = data.get('home_score', record.fast_home_score)
            record.fast_away_score = data.get('away_score', record.fast_away_score)
            record.fast_last_update = datetime.now()

            was_slow_before = key in self.slow_games
            self._evaluate(key)

            if not was_slow_before and key in self.slow_games and callback:
                await callback(self.slow_games[key])

        except Exception as e:
            logger.error(f"Fast feed handler error: {e}")

    async def on_sportybet(self, data: dict, callback: Optional[Callable] = None):
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            key = self._resolve_key(home, away)
            played = data.get('played_seconds')

            logger.info(f"[SB] {home} vs {away} -> key={key} played_seconds={played}")

            record = self._get_or_create(key, home, away)
            record.sb_event_id = data.get('match_id')
            record.sb_played_seconds = played
            record.sb_period = data.get('match_status', data.get('period', ''))
            record.sb_home_score = data.get('home_score', record.sb_home_score)
            record.sb_away_score = data.get('away_score', record.sb_away_score)
            record.sb_last_update = datetime.now()

            was_slow_before = key in self.slow_games
            self._evaluate(key)

            if not was_slow_before and key in self.slow_games and callback:
                await callback(self.slow_games[key])

        except Exception as e:
            logger.error(f"SportyBet handler error: {e}")

    def _evaluate(self, key: str):
        record = self.records.get(key)
        if not record:
            return

        # Need both sides
        if record.fast_played_seconds is None or record.sb_played_seconds is None:
            return
        if record.fast_last_update is None or record.sb_last_update is None:
            return

        # Skip paused periods
        if _is_paused_period(record.fast_period) or _is_paused_period(record.sb_period):
            return

        # Both must be fresh
        now = datetime.now()
        fast_age = (now - record.fast_last_update).total_seconds()
        sb_age = (now - record.sb_last_update).total_seconds()

        if fast_age > self.MAX_DATA_AGE_SECONDS or sb_age > self.MAX_DATA_AGE_SECONDS:
            return

        # Correct gap direction: positive = SportyBet is behind
        gap = record.fast_played_seconds - record.sb_played_seconds

        if gap >= self.threshold:
            record.below_threshold_streak = 0
            if key not in self.slow_games:
                self.slow_games[key] = {
                    'match_id': record.sb_event_id or key,
                    'home_team': record.home_team,
                    'away_team': record.away_team,
                    'gap_seconds': gap,
                    '_last_score': (record.sb_home_score, record.sb_away_score),
                }
                logger.info(
                    f"⚡ SLOW: {record.home_team} vs {record.away_team} "
                    f"(SportyBet lagging by {gap:.1f}s)"
                )
            else:
                self.slow_games[key]['gap_seconds'] = gap
        else:
            if key in self.slow_games:
                record.below_threshold_streak += 1
                if record.below_threshold_streak >= self.UNFLAG_STREAK_REQUIRED:
                    logger.info(f"Match no longer slow: {record.home_team} vs {record.away_team}")
                    del self.slow_games[key]
                    record.below_threshold_streak = 0

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
