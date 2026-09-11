import re
import difflib
import unicodedata
from datetime import datetime
from typing import Callable, Dict, Optional
from dataclasses import dataclass
from loguru import logger
from config import Config

HALF_LENGTH_SECONDS = 45 * 60  # 2700 — standard soccer half length


def strip_accents(text: str) -> str:
    if not text:
        return ''
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def normalize_team_name(name: str) -> str:
    """Turn a raw team name into a comparable key.
    Safe against nuking short names (e.g. 'PSG') — only strips a trailing
    short code (like 'GO', 'RS') when there's more than one word left.
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

    # Only strip a trailing short code (state/country tag) if it's not the
    # only word left — protects single-word short names like "PSG".
    if len(words) > 1 and len(words[-1]) <= 3:
        words = words[:-1]

    return ''.join(words)


def _is_paused(period: str) -> bool:
    if not period:
        return False
    p = str(period).upper().strip()
    return p in ('HT', 'HALFTIME', 'BREAK', 'PAUSED', 'HALF TIME')


def normalize_fast_played(played_seconds, period: str):
    """
    Fixes the Polymarket half-time clock reset: if the period is second
    half (or later) and the elapsed value looks like it restarted near
    zero, add one full half length so it's cumulative — matching how
    SportyBet reports played time.
    """
    if played_seconds is None:
        return None

    p = (period or '').upper().strip()

    if p == 'HT':
        return float(HALF_LENGTH_SECONDS)

    if p in ('2H', 'FT', 'FT OT', 'FT NR'):
        if played_seconds < HALF_LENGTH_SECONDS:
            return played_seconds + HALF_LENGTH_SECONDS

    return played_seconds


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
    unmatched_warned: bool = False


class SlowGameDetector:
    def __init__(self):
        self.records: Dict[str, MatchRecord] = {}
        self.slow_games: Dict[str, dict] = {}
        self.threshold = Config.SLOW_THRESHOLD_SECONDS

        self.UNFLAG_STREAK = 2
        self.MAX_FAST_AGE = 8.0
        self.MAX_SB_AGE = 7.0

        # If a flagged-slow match goes silent on either side for this long,
        # force-remove the flag instead of leaving it stuck forever.
        self.STALE_REMOVE_AFTER = 60.0

        # Drop records that haven't been touched by either feed in this
        # long — prevents unbounded memory growth over a long-running bot.
        self.RECORD_EXPIRE_AFTER = 6 * 60 * 60  # 6 hours

        self.FUZZY_MATCH_THRESHOLD = 0.82
        self.UNMATCHED_WARNING_AFTER = 20.0

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
        best_match = difflib.get_close_matches(base, candidates, n=1, cutoff=self.FUZZY_MATCH_THRESHOLD)
        if best_match:
            logger.debug(f"[FUZZY] MATCHED '{base}' -> existing key '{best_match[0]}'")
            return best_match[0]
        return None

    def _resolve_key(self, home: str, away: str) -> str:
        """Resolve to an existing MatchRecord key, using team names only.
        (League is intentionally not used — the two feeds format league
        names too differently to compare reliably.)
        """
        base = self._base_key(home, away)

        if base in self.records:
            return base

        fuzzy_key = self._fuzzy_find_key(base)
        if fuzzy_key:
            return fuzzy_key

        return base

    def _prune_expired_records(self):
        now = datetime.now()
        expired = []
        for key, rec in self.records.items():
            last_seen = max(
                rec.fast_updated or datetime.min,
                rec.sb_updated or datetime.min,
            )
            if last_seen == datetime.min:
                continue
            if (now - last_seen).total_seconds() > self.RECORD_EXPIRE_AFTER:
                expired.append(key)

        for key in expired:
            del self.records[key]
            self.slow_games.pop(key, None)

        if expired:
            logger.debug(f"Pruned {len(expired)} expired match record(s)")

    def _get_or_create(self, key: str, home: str, away: str) -> MatchRecord:
        if key not in self.records:
            self._prune_expired_records()
            self.records[key] = MatchRecord(key=key, home_team=home, away_team=away)
        return self.records[key]

    async def on_bet365(self, data: dict, callback: Optional[Callable] = None):
        try:
            home = data.get('home_team', '')
            away = data.get('away_team', '')
            if not home or not away:
                return

            key = self._resolve_key(home, away)
            period = data.get('period', '')
            played = normalize_fast_played(data.get('played_seconds'), period)

            logger.info(f"[FAST] {home} vs {away} -> key={key} played={played}")

            rec = self._get_or_create(key, home, away)
            rec.fast_played = played
            rec.fast_period = period
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

        now = datetime.now()

        # --- Visibility: warn once if only one side has ever reported ---
        if rec.fast_updated and not rec.sb_updated:
            age = (now - rec.fast_updated).total_seconds()
            if age > self.UNMATCHED_WARNING_AFTER and not rec.unmatched_warned:
                logger.warning(f"⚠️ UNMATCHED: '{rec.home_team}' vs '{rec.away_team}' seen on FAST feed "
                                f"but never on SportyBet after {age:.0f}s — possible name mismatch")
                rec.unmatched_warned = True
        elif rec.sb_updated and not rec.fast_updated:
            age = (now - rec.sb_updated).total_seconds()
            if age > self.UNMATCHED_WARNING_AFTER and not rec.unmatched_warned:
                logger.warning(f"⚠️ UNMATCHED: '{rec.home_team}' vs '{rec.away_team}' seen on SportyBet "
                                f"but never on FAST feed after {age:.0f}s — possible name mismatch")
                rec.unmatched_warned = True
        elif rec.fast_updated and rec.sb_updated:
            rec.unmatched_warned = False

        # --- Force-clear a stuck "slow" flag if a feed has gone silent ---
        if key in self.slow_games:
            fast_dead = (rec.fast_updated is None or
                         (now - rec.fast_updated).total_seconds() > self.STALE_REMOVE_AFTER)
            sb_dead = (rec.sb_updated is None or
                       (now - rec.sb_updated).total_seconds() > self.STALE_REMOVE_AFTER)
            if fast_dead or sb_dead:
                logger.info(f"Clearing stale SLOW flag (feed went silent): {rec.home_team} vs {rec.away_team}")
                del self.slow_games[key]
                rec.below_streak = 0
                return

        if (rec.fast_played is None or rec.sb_played is None or
                rec.fast_updated is None or rec.sb_updated is None):
            return

        if _is_paused(rec.fast_period) or _is_paused(rec.sb_period):
            return

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
