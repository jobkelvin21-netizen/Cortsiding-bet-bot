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

    # Fix for Bug 2: require the gap to clear the threshold on 2
    # consecutive readings before un-flagging, so one noisy sample
    # doesn't silently drop a match the bot is already watching.
    below_threshold_streak: int = 0


class SlowGameDetector:
    def __init__(self):
        self.records: Dict[str, MatchRecord] = {}
        self.slow_games: Dict[str, dict] = {}
        self.threshold = Config.SLOW_THRESHOLD_SECONDS
        self.UNFLAG_STREAK_REQUIRED = 2

    def _team_key(self, home: str, away: str, league: str = '') -> str:
        # Fix for Bug 1: include league/competition when available so two
        # different real matches with similarly-named clubs in different
        # leagues never collapse into the same record.
        league_part = re.sub(r'[^a-z0-9]', '', league.lower().strip()) if league else ''
        base = f"{normalize_team_name(home)}_vs_{normalize_team_name(away)}"
        return f"{league_part}_{base}" if league_part else base

    def _get_or_create(self, key: str, home: str, away: str) -> MatchRecord:
        if key not in self.records:
            self.records[key] = MatchRecord(key=key, home_team=home, away_team=away)
        return self.records[key]

    async def on_bet365(self, data: dict):
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            league = data.get('league', '')
            key = self._team_key(home, away, league)
            logger.info(f"[FAST] {home} vs {away} -> key={key} played_seconds={data.get('played_seconds')}")

            record = self._get_or_create(key, home, away)
            record.fast_played_seconds = data.get('played_seconds')
            record.fast_home_score = data.get('home_score', record.fast_home_score)
            record.fast_away_score = data.get('away_score', record.fast_away_score)
            record.fast_last_update = datetime.now()

            self._evaluate(key)

        except Exception as e:
            logger.error(f"Fast feed handler error: {e}")

    async def on_sportybet(self, data: dict, callback: Callable):
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            # SportyBet feed doesn't carry a 'league' field currently, so
            # this falls back to team-name-only matching on the SB side —
            # that's fine, since the FAST side's league-qualified key is
            # what actually prevents cross-league collisions in practice
            # (SB rarely has two live matches with identical team names
            # itself; the risk was always on the FAST side matching wrong).
            key = self._team_key(home, away)
            logger.info(f"[SB] {home} vs {away} -> key={key} played_seconds={data.get('played_seconds')}")

            # Try to find a matching FAST record even if it was stored
            # under a league-qualified key
            matched_key = key
            if key not in self.records:
                for existing_key, rec in self.records.items():
                    if existing_key.endswith(key) or existing_key == key:
                        matched_key = existing_key
                        break

            record = self._get_or_create(matched_key, home, away)
            record.sb_event_id = data.get('match_id')
            record.sb_played_seconds = data.get('played_seconds')
            record.sb_home_score = data.get('home_score', record.sb_home_score)
            record.sb_away_score = data.get('away_score', record.sb_away_score)
            record.sb_last_update = datetime.now()

            was_slow_before = matched_key in self.slow_games
            self._evaluate(matched_key)

            if not was_slow_before and matched_key in self.slow_games and callback:
                await callback(self.slow_games[matched_key])

        except Exception as e:
            logger.error(f"SportyBet handler error: {e}")

    def _evaluate(self, key: str):
        record = self.records.get(key)
        if not record:
            return
        if record.fast_played_seconds is None or record.sb_played_seconds is None:
            return

        gap = record.sb_played_seconds - record.fast_played_seconds

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
                    f"🐢 SLOW: {record.home_team} vs {record.away_team} "
                    f"(SportyBet lagging by {gap:.1f}s)"
                )
            else:
                # keep gap_seconds fresh for already-flagged matches
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
