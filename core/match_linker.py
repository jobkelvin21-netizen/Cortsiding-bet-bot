import re
import difflib
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
    filler = {
        'fc', 'cf', 'sc', 'afc', 'cd', 'ac', 'fk', 'nk', 'sk', 'as', 'ss',
        'rc', 'ca', 'cs', 'us', 'sd', 'ec',
        'united', 'city', 'town', 'rovers', 'athletic', 'sporting', 'club'
    }
    words = [w for w in words if w not in filler]
    if len(words) > 1 and len(words[-1]) <= 3:
        words = words[:-1]
    return ''.join(words)


def match_key(home: str, away: str) -> str:
    h, a = normalize_team_name(home), normalize_team_name(away)
    if h > a:
        h, a = a, h
    return f"{h}_vs_{a}"


class MatchLinker:
    def __init__(self, sportybet_matches_ref: dict):
        self.sportybet_matches = sportybet_matches_ref
        self.last_scores: Dict[str, tuple] = {}
        self.links: Dict[str, str] = {}          # poly match_id -> sportybet match_id (cached)
        self.goal_callback: Optional[Callable] = None
        self.FUZZY_THRESHOLD = 0.75

    def set_goal_callback(self, callback: Callable):
        self.goal_callback = callback

    def _find_sportybet_match(self, home: str, away: str) -> Optional[dict]:
        target_key = match_key(home, away)
        snapshot = list(self.sportybet_matches.values())

        for sb_match in snapshot:
            sb_key = match_key(sb_match.get('home_team', ''), sb_match.get('away_team', ''))
            if sb_key == target_key:
                logger.debug(f"[MATCH] Exact: '{target_key}'")
                return sb_match

        best_match = None
        best_score = 0.0
        for sb_match in snapshot:
            sb_key = match_key(sb_match.get('home_team', ''), sb_match.get('away_team', ''))
            score = difflib.SequenceMatcher(None, target_key, sb_key).ratio()
            if score > best_score:
                best_score = score
                best_match = sb_match

        if best_match and best_score >= self.FUZZY_THRESHOLD:
            best_key = match_key(best_match.get('home_team', ''), best_match.get('away_team', ''))
            logger.info(f"[MATCH] Fuzzy: '{target_key}' ~ '{best_key}' (score={best_score:.2f})")
            return best_match

        closest = (match_key(best_match.get('home_team', ''), best_match.get('away_team', ''))
                   if best_match else 'none available')
        logger.warning(f"[MATCH] NO MATCH for '{home}' vs '{away}' (key='{target_key}'). "
                        f"Closest: {closest} (score={best_score:.2f})")
        return None

    async def on_polymarket(self, match: dict):
        poly_id = match.get('match_id', '')          # <-- FIX: track by Polymarket's own id, not team-key
        home, away = match.get('home_team', ''), match.get('away_team', '')

        new_score = (match.get('home_score', 0), match.get('away_score', 0))
        old_score = self.last_scores.get(poly_id)
        self.last_scores[poly_id] = new_score

        if match.get('ended'):                        # <-- FIX: clean up finished matches
            self.last_scores.pop(poly_id, None)
            self.links.pop(poly_id, None)
            return

        if old_score is None:
            logger.debug(f"[TRACKING] New match: {home} vs {away} (score={new_score})")
            return

        if new_score == old_score:
            return

        total_old = sum(old_score)
        total_new = sum(new_score)
        if total_new <= total_old:                     # score correction, not a goal
            return

        scoring_team = home if new_score[0] > old_score[0] else away
        goal_num = total_new

        # FIX: reuse a cached link if we already found this match before,
        # instead of re-searching (and possibly landing on a different candidate) every time.
        sb_id = self.links.get(poly_id)
        sb_match = self.sportybet_matches.get(sb_id) if sb_id else None
        if sb_match is None:
            sb_match = self._find_sportybet_match(home, away)
            if sb_match:
                self.links[poly_id] = sb_match.get('match_id')

        if not sb_match:
            return

        logger.info(f"⚽ GOAL: {home} {new_score[0]}-{new_score[1]} "
                    f"{away} (goal #{goal_num}, scorer: {scoring_team})")

        if self.goal_callback:
            await self.goal_callback({
                'match_id': sb_match.get('match_id'),
                'home_team': sb_match.get('home_team'),
                'away_team': sb_match.get('away_team'),
                'home_score': new_score[0],
                'away_score': new_score[1],
                'scoring_team': scoring_team,
                'goal_num': goal_num,
            })
