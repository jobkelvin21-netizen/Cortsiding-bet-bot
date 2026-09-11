import re
import unicodedata
from typing import Callable, Dict, Optional
from loguru import logger


def strip_accents(text: str) -> str:
    if not text:
        return ''
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def normalize_team_name(name: str) -> str:
    if not name:
        return ''
    name = strip_accents(name).lower().strip()
    words = re.findall(r'[a-z0-9]+', name)
    filler = {'fc', 'cf', 'sc', 'afc', 'cd', 'ac', 'fk', 'nk', 'sk', 'as', 'ss',
              'rc', 'ca', 'cs', 'us', 'sd', 'united', 'city', 'town', 'rovers',
              'athletic', 'sporting', 'club'}
    words = [w for w in words if w not in filler]
    if len(words) > 1 and len(words[-1]) <= 3:
        words = words[:-1]
    return ''.join(words)


def match_key(home: str, away: str) -> str:
    h, a = normalize_team_name(home), normalize_team_name(away)
    if h > a:
        h, a = a, h
    return f"{h}_vs_{a}"


class FeedSwitcher:
    """
    Watches bet365 (primary) and Polymarket (backup). Routes goal
    events from whichever is currently active — never both at once.
    Links a fast-feed match to its SportyBet counterpart by team name,
    purely for routing which SportyBet page to bet on.
    """

    def __init__(self, bet365_feed, polymarket_feed, sportybet_matches_ref: dict):
        self.bet365 = bet365_feed
        self.polymarket = polymarket_feed
        self.sportybet_matches = sportybet_matches_ref
        self.active_feed = "bet365"
        self.last_scores: Dict[str, tuple] = {}
        self.goal_callback: Optional[Callable] = None

    def set_goal_callback(self, callback: Callable):
        self.goal_callback = callback

    def _check_health_and_switch(self):
        if self.bet365.is_healthy():
            if self.active_feed != "bet365":
                logger.success("bet365 recovered — switching back to primary feed")
                self.active_feed = "bet365"
        else:
            if self.active_feed != "polymarket":
                logger.warning("bet365 unhealthy — switching to Polymarket backup")
                self.active_feed = "polymarket"

    def _find_sportybet_match(self, home: str, away: str) -> Optional[dict]:
        target_key = match_key(home, away)
        for sb_match in self.sportybet_matches.values():
            sb_key = match_key(sb_match.get('home_team', ''), sb_match.get('away_team', ''))
            if sb_key == target_key:
                return sb_match
        return None

    async def _handle_match_update(self, match: dict, source: str):
        self._check_health_and_switch()

        if self.active_feed != source:
            return  # ignore updates from the inactive feed

        key = match_key(match.get('home_team', ''), match.get('away_team', ''))
        new_score = (match.get('home_score', 0), match.get('away_score', 0))
        old_score = self.last_scores.get(key)

        self.last_scores[key] = new_score

        if old_score is None:
            return  # first time seeing this match, nothing to compare yet

        if new_score == old_score:
            return  # no goal

        total_old = sum(old_score)
        total_new = sum(new_score)
        if total_new <= total_old:
            return  # score changed but didn't increase (correction, glitch) — ignore

        scoring_team = match['home_team'] if new_score[0] > old_score[0] else match['away_team']
        goal_num = total_new

        sb_match = self._find_sportybet_match(match['home_team'], match['away_team'])
        if not sb_match:
            logger.warning(f"⚠️ Goal detected ({source}) for '{match['home_team']}' vs "
                            f"'{match['away_team']}' but no matching SportyBet game found — skipping")
            return

        logger.info(f"⚽ GOAL via {source}: {match['home_team']} {new_score[0]}-{new_score[1]} "
                    f"{match['away_team']} (goal #{goal_num}, scorer side: {scoring_team})")

        if self.goal_callback:
            await self.goal_callback({
                'match_id': sb_match.get('match_id'),
                'home_team': sb_match.get('home_team'),
                'away_team': sb_match.get('away_team'),
                'home_score': new_score[0],
                'away_score': new_score[1],
                'scoring_team': scoring_team,
                'goal_num': goal_num,
                'source': source,
            })

    async def on_bet365(self, match: dict):
        await self._handle_match_update(match, "bet365")

    async def on_polymarket(self, match: dict):
        await self._handle_match_update(match, "polymarket")
