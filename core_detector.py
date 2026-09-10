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


def _is_paused_period(period_text: str) -> bool:
    """Detects halftime/paused states where the clock isn't actually
    running, so linear time-projection would be meaningless."""
    if not period_text:
        return False
    p = str(period_text).upper().strip()
    return p in ('HT', 'HALFTIME', 'BREAK', 'PAUSED')


@dataclass
class MatchRecord:
    key: str
    home_team: str = ''
    away_team: str = ''

    # Anchor: the last CONFIRMED fast-feed reading, plus the real-world
    # clock time it was received. Used to project "what the fast feed's
    # clock should read right now" without needing a brand new message
    # at the exact same instant SportyBet reports.
    fast_anchor_played_seconds: Optional[float] = None
    fast_anchor_real_time: Optional[datetime] = None
    fast_period: str = ''
    fast_home_score: int = 0
    fast_away_score: int = 0

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
        # If the fast feed's anchor is older than this, the projection is
        # no longer trustworthy (feed may have stopped, match may have
        # ended) — skip evaluation rather than guess.
        self.MAX_ANCHOR_AGE_SECONDS = 20.0
        # SportyBet's own reading also can't be too old, independent of
        # the projection logic.
        self.MAX_SB_AGE_SECONDS = 10.0

    def _team_key(self, home: str, away: str, league: str = '') -> str:
        league_part = re.sub(r'[^a-z0-9]', '', league.lower().strip()) if league else ''
        base = f"{normalize_team_name(home)}_vs_{normalize_team_name(away)}"
        return f"{league_part}_{base}" if league_part else base

    def _get_or_create(self, key: str, home: str, away: str) -> MatchRecord:
        if key not in self.records:
            self.records[key] = MatchRecord(key=key, home_team=home, away_team=away)
        return self.records[key]

    async def on_bet365(self, data: dict, callback: Callable = None):
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            league = data.get('league', '')
            key = self._team_key(home, away, league)
            played_seconds = data.get('played_seconds')
            logger.info(f"[FAST] {home} vs {away} -> key={key} played_seconds={played_seconds}")

            record = self._get_or_create(key, home, away)
            # Re-anchor every time we get a genuine fresh reading —
            # this resets the "real time since last confirmed" clock.
            record.fast_anchor_played_seconds = played_seconds
            record.fast_anchor_real_time = datetime.now()
            record.fast_period = data.get('period', '')
            record.fast_home_score = data.get('home_score', record.fast_home_score)
            record.fast_away_score = data.get('away_score', record.fast_away_score)

            was_slow_before = key in self.slow_games
            self._evaluate(key)

            if not was_slow_before and key in self.slow_games and callback:
                await callback(self.slow_games[key])

        except Exception as e:
            logger.error(f"Fast feed handler error: {e}")

    async def on_sportybet(self, data: dict, callback: Callable = None):
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            key = self._team_key(home, away)
            played_seconds = data.get('played_seconds')
            logger.info(f"[SB] {home} vs {away} -> key={key} played_seconds={played_seconds}")

            matched_key = key
            if key not in self.records:
                for existing_key in self.records:
                    if existing_key.endswith(key) or existing_key == key:
                        matched_key = existing_key
                        break

            record = self._get_or_create(matched_key, home, away)
            record.sb_event_id = data.get('match_id')
            record.sb_played_seconds = played_seconds
            record.sb_period = data.get('match_status', data.get('period', ''))
            record.sb_home_score = data.get('home_score', record.sb_home_score)
            record.sb_away_score = data.get('away_score', record.sb_away_score)
            record.sb_last_update = datetime.now()

            was_slow_before = matched_key in self.slow_games
            self._evaluate(matched_key)

            if not was_slow_before and matched_key in self.slow_games and callback:
                await callback(self.slow_games[matched_key])

        except Exception as e:
            logger.error(f"SportyBet handler error: {e}")

    def _projected_fast_seconds(self, record: MatchRecord) -> Optional[float]:
        """Projects what the fast feed's clock SHOULD read right now,
        based on its last confirmed reading plus real time elapsed since
        then — instead of requiring a brand new message at this exact
        instant."""
        if record.fast_anchor_played_seconds is None or record.fast_anchor_real_time is None:
            return None

        age = (datetime.now() - record.fast_anchor_real_time).total_seconds()
        if age > self.MAX_ANCHOR_AGE_SECONDS:
            return None  # anchor too old to trust — feed may have stalled

        return record.fast_anchor_played_seconds + age

    def _evaluate(self, key: str):
        record = self.records.get(key)
        if not record:
            return
        if record.sb_played_seconds is None or record.sb_last_update is None:
            return

        # Don't evaluate during halftime/pauses on either side — the clock
        # isn't running, so any "gap" measured here would be meaningless.
        if _is_paused_period(record.fast_period) or _is_paused_period(record.sb_period):
            return

        sb_age = (datetime.now() - record.sb_last_update).total_seconds()
        if sb_age > self.MAX_SB_AGE_SECONDS:
            return

        projected_fast = self._projected_fast_seconds(record)
        if projected_fast is None:
            return

        gap = record.sb_played_seconds - projected_fast

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
