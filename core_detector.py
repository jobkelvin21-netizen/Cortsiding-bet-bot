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
    name = re.sub(r'\b(fc|cf|sc|afc|cd|ac)\b', '', name)
    name = re.sub(r'[^a-z0-9]', '', name)
    return name


@dataclass
class MatchRecord:
    key: str
    fast_played_seconds: Optional[float] = None
    fast_home_score: int = 0
    fast_away_score: int = 0
    fast_last_update: Optional[datetime] = None

    sb_event_id: Optional[str] = None
    sb_played_seconds: Optional[float] = None
    sb_home_score: int = 0
    sb_away_score: int = 0
    sb_last_update: Optional[datetime] = None

    home_team: str = ''
    away_team: str = ''


class SlowGameDetector:
    def __init__(self):
        self.records: Dict[str, MatchRecord] = {}
        self.slow_games: Dict[str, dict] = {}
        self.threshold = Config.SLOW_THRESHOLD_SECONDS

    def _team_key(self, home: str, away: str) -> str:
        return f"{normalize_team_name(home)}_vs_{normalize_team_name(away)}"

    def _get_or_create(self, key: str, home: str, away: str) -> MatchRecord:
        if key not in self.records:
            self.records[key] = MatchRecord(key=key, home_team=home, away_team=away)
        return self.records[key]

    async def on_bet365(self, data: dict):
        """Callback for the fast feed (Polymarket)."""
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            key = self._team_key(home, away)
            record = self._get_or_create(key, home, away)

            record.fast_played_seconds = data.get('played_seconds')
            record.fast_home_score = data.get('home_score', record.fast_home_score)
            record.fast_away_score = data.get('away_score', record.fast_away_score)
            record.fast_last_update = datetime.now()

            self._evaluate(key)

        except Exception as e:
            logger.error(f"Fast feed handler error: {e}")

    async def on_sportybet(self, data: dict, callback: Callable):
        """Callback for feeds/sportybet_api.py."""
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            key = self._team_key(home, away)
            record = self._get_or_create(key, home, away)

            record.sb_event_id = data.get('match_id')
            record.sb_played_seconds = data.get('played_seconds')
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
        if record.fast_played_seconds is None or record.sb_played_seconds is None:
            return

        gap = record.sb_played_seconds - record.fast_played_seconds

        if gap >= self.threshold and key not in self.slow_games:
            self.slow_games[key] = {
                'match_id': record.sb_event_id or key,
                'home_team': record.home_team,
                'away_team': record.away_team,
                'gap_seconds': gap,
                '_last_score': (record.sb_home_score, record.sb_away_score),
            }
            logger.info(
                f"🐢 SLOW: {record.home_team} vs {record.away_team} "
                f"(SportyBet lagging by {gap:.1f}s)"
            )
        elif gap < self.threshold and key in self.slow_games:
            logger.info(f"Match no longer slow: {record.home_team} vs {record.away_team}")
            del self.slow_games[key]

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
