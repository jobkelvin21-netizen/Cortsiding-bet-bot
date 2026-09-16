import asyncio
import re
import time
import difflib
import unicodedata
from typing import Callable, Dict, Optional, Set

from loguru import logger
from config import Config


def strip_accents(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_team_name(name: str) -> str:
    if not name:
        return ""
    name = strip_accents(name).lower().strip()
    name = re.sub(
        r'\b(fc|cf|sc|afc|cd|ac|fk|nk|sk|as|ss|rc|ca|cs|us|sd|ec|ud|cp|ce|sad|club|united|city|town|rovers|athletic|sporting)\b',
        ' ',
        name,
    )
    name = re.sub(r'[^a-z0-9\s]', ' ', name)
    words = [w for w in name.split() if len(w) > 1]
    if len(words) > 1 and len(words[-1]) <= 2:
        words = words[:-1]
    return "".join(words)


def get_tokens(name: str) -> Set[str]:
    if not name:
        return set()
    name = strip_accents(name).lower()
    name = re.sub(r'[^a-z0-9\s]', ' ', name)
    tokens = {w for w in name.split() if len(w) > 2}
    filler = {"the", "and", "fc", "cf", "sc", "united", "city", "club"}
    return tokens - filler


def match_key(home: str, away: str) -> str:
    h = normalize_team_name(home)
    a = normalize_team_name(away)
    if h > a:
        h, a = a, h
    return f"{h}_vs_{a}"


class MatchLinker:
    def __init__(self, polymarket_feed, sportybet_matches_ref: dict, alerter=None):
        self.polymarket_feed = polymarket_feed
        self.sportybet_matches = sportybet_matches_ref
        self.alerter = alerter

        self.links: Dict[str, dict] = {}
        self.last_scores: Dict[str, tuple] = {}
        self._missing_since: Dict[str, float] = {}

        self.goal_callback: Optional[Callable] = None
        self.link_callback: Optional[Callable] = None
        self.unlink_callback: Optional[Callable] = None

        self.running = False
        self.FUZZY_THRESHOLD = 0.68
        self.RECONCILE_INTERVAL = 2.0
        self.GRACE_PERIOD = Config.LINK_GRACE_PERIOD_SECONDS
        self._reconcile_task = None

    def set_goal_callback(self, callback: Callable):
        self.goal_callback = callback

    def set_link_callback(self, callback: Callable):
        self.link_callback = callback

    def set_unlink_callback(self, callback: Callable):
        self.unlink_callback = callback

    def set_alerter(self, alerter):
        self.alerter = alerter

    def _find_sportybet_match(self, home: str, away: str) -> Optional[dict]:
        target_key = match_key(home, away)
        target_home_tokens = get_tokens(home)
        target_away_tokens = get_tokens(away)

        for sb_match in self.sportybet_matches.values():
            sb_key = match_key(sb_match.get("home_team", ""), sb_match.get("away_team", ""))
            if sb_key == target_key:
                return sb_match

        best_match = None
        best_score = 0.0

        for sb_match in self.sportybet_matches.values():
            sb_home = sb_match.get("home_team", "")
            sb_away = sb_match.get("away_team", "")
            sb_key = match_key(sb_home, sb_away)
            score = difflib.SequenceMatcher(None, target_key, sb_key).ratio()

            sb_home_tokens = get_tokens(sb_home)
            sb_away_tokens = get_tokens(sb_away)
            home_overlap = len(target_home_tokens & sb_home_tokens) + len(
                target_home_tokens & sb_away_tokens
            )
            away_overlap = len(target_away_tokens & sb_away_tokens) + len(
                target_away_tokens & sb_home_tokens
            )
            if home_overlap >= 1 and away_overlap >= 1:
                score += 0.12

            if score > best_score:
                best_score = score
                best_match = sb_match

        if best_match and best_score >= self.FUZZY_THRESHOLD:
            return best_match
        return None

    def _notify_link(self, home: str, away: str, sb_match: dict):
        if not self.alerter:
            return
        msg = (
            f"🔗 <b>MATCH LINKED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Polymarket: {home} vs {away}\n"
            f"SportyBet: {sb_match.get('home_team')} vs {sb_match.get('away_team')}\n"
            f"🆔 SportyBet ID: <code>{sb_match.get('match_id')}</code>\n"
            f"Ready to bet on this match's next goal."
        )
        try:
            asyncio.get_running_loop()
            asyncio.create_task(self.alerter.send(msg))
        except RuntimeError:
            pass

    def _notify_unlink(self, home: str, away: str):
        if not self.alerter:
            return
        msg = (
            f"🔌 <b>MATCH UNLINKED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{home} vs {away}\n"
            f"Link removed — page will be closed."
        )
        try:
            asyncio.get_running_loop()
            asyncio.create_task(self.alerter.send(msg))
        except RuntimeError:
            pass

    def _fire_link_callback(self, sb_match: dict):
        if not self.link_callback:
            return
        try:
            asyncio.get_running_loop()
            asyncio.create_task(self.link_callback(sb_match))
        except RuntimeError:
            pass

    def _fire_unlink_callback(self, sb_match: dict):
        if not self.unlink_callback:
            return
        try:
            asyncio.get_running_loop()
            asyncio.create_task(self.unlink_callback(sb_match))
        except RuntimeError:
            pass

    def _reconcile_once(self):
        poly_matches = self.polymarket_feed.get_matches()
        newly_linked = 0
        newly_unlinked = 0
        current_sb_ids = {m.get("match_id") for m in self.sportybet_matches.values()}

        for poly_id in list(self.links.keys()):
            linked_sb = self.links.get(poly_id)
            if not linked_sb:
                continue
            sb_id = linked_sb.get("match_id")
            if sb_id in current_sb_ids:
                self._missing_since.pop(poly_id, None)
                continue

            now = time.time()
            first_missed = self._missing_since.get(poly_id)
            if first_missed is None:
                self._missing_since[poly_id] = now
                continue
            if (now - first_missed) < self.GRACE_PERIOD:
                continue

            poly_match = next((m for m in poly_matches if m.get("match_id") == poly_id), None)
            home = (poly_match or {}).get("home_team") or linked_sb.get("home_team", "")
            away = (poly_match or {}).get("away_team") or linked_sb.get("away_team", "")
            logger.warning(f"[LINK] Lost link for '{home}' vs '{away}'")
            self._notify_unlink(home, away)
            self._fire_unlink_callback(dict(linked_sb))
            del self.links[poly_id]
            self._missing_since.pop(poly_id, None)
            self.last_scores.pop(poly_id, None)
            newly_unlinked += 1

        for poly_match in poly_matches:
            poly_id = poly_match.get("match_id")
            if not poly_id:
                continue
            home = poly_match.get("home_team", "")
            away = poly_match.get("away_team", "")
            existing = self.links.get(poly_id)
            if existing and existing.get("match_id") in current_sb_ids:
                continue
            sb_match = self._find_sportybet_match(home, away)
            if sb_match:
                newly_linked += 1
                logger.success(
                    f"[LINK] '{home}' vs '{away}' → SportyBet {sb_match.get('match_id')}"
                )
                self.links[poly_id] = sb_match
                self._missing_since.pop(poly_id, None)
                self._notify_link(home, away, sb_match)
                self._fire_link_callback(sb_match)

        if newly_linked or newly_unlinked:
            logger.info(
                f"[LINK] Reconcile: {len(self.links)} active "
                f"(+{newly_linked}/-{newly_unlinked})"
            )

    async def _reconcile_loop(self):
        logger.info("MatchLinker: continuous linking loop started")
        while self.running:
            try:
                self._reconcile_once()
            except Exception as e:
                logger.error(f"MatchLinker reconcile error: {e}")
            await asyncio.sleep(self.RECONCILE_INTERVAL)

    async def start(self):
        if self.running:
            return
        self.running = True
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    def stop(self):
        self.running = False
        if self._reconcile_task:
            self._reconcile_task.cancel()
            self._reconcile_task = None

    async def on_polymarket(self, match: dict):
        poly_id = match.get("match_id")
        if not poly_id:
            return

        home = match.get("home_team", "")
        away = match.get("away_team", "")
        new_score = (match.get("home_score", 0), match.get("away_score", 0))
        old_score = self.last_scores.get(poly_id)
        self.last_scores[poly_id] = new_score

        if old_score is None:
            if poly_id not in self.links:
                sb_match = self._find_sportybet_match(home, away)
                if sb_match:
                    self.links[poly_id] = sb_match
                    self._notify_link(home, away, sb_match)
                    self._fire_link_callback(sb_match)
            return

        if new_score == old_score:
            return
        if sum(new_score) <= sum(old_score):
            return

        if new_score[0] > old_score[0]:
            scoring_team = home
        elif new_score[1] > old_score[1]:
            scoring_team = away
        else:
            return

        goal_num = sum(new_score)
        sb_match = self.links.get(poly_id)
        if not sb_match:
            sb_match = self._find_sportybet_match(home, away)
            if sb_match:
                self.links[poly_id] = sb_match
                self._notify_link(home, away, sb_match)
                self._fire_link_callback(sb_match)
        if not sb_match:
            logger.warning(f"Goal for '{home}' vs '{away}' but no SportyBet link")
            return

        detected_at = time.time()
        logger.success(
            f"⚽ GOAL: {home} {new_score[0]}-{new_score[1]} {away} "
            f"(#{goal_num}, {scoring_team})"
        )
        if self.goal_callback:
            await self.goal_callback({
                "match_id": sb_match.get("match_id"),
                "home_team": sb_match.get("home_team"),
                "away_team": sb_match.get("away_team"),
                "home_score": new_score[0],
                "away_score": new_score[1],
                "scoring_team": scoring_team,
                "goal_num": goal_num,
                "detected_at": detected_at,
            })
