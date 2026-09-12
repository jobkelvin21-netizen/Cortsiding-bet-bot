import asyncio
import re
import time
import difflib
import unicodedata
from typing import Callable, Dict, Optional

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
    words = re.findall(r"[a-z0-9]+", name)
    filler = {
        "fc", "cf", "sc", "afc", "cd", "ac", "fk", "nk", "sk", "as", "ss",
        "rc", "ca", "cs", "us", "sd", "ec", "ud", "cp", "ce", "sad",
        "united", "city", "town", "rovers", "athletic", "sporting", "club",
    }
    words = [w for w in words if w not in filler]
    if len(words) > 1 and len(words[-1]) <= 3:
        words = words[:-1]
    return "".join(words)


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

        # Tracks when a linked SportyBet match first went missing, so a
        # single transient poll miss doesn't instantly destroy the link
        self._missing_since: Dict[str, float] = {}

        self.goal_callback: Optional[Callable] = None
        self.running = False
        self.FUZZY_THRESHOLD = 0.75
        self.RECONCILE_INTERVAL = 2.0
        self.GRACE_PERIOD = Config.LINK_GRACE_PERIOD_SECONDS

        self._reconcile_task = None

    def set_goal_callback(self, callback: Callable):
        self.goal_callback = callback

    def set_alerter(self, alerter):
        self.alerter = alerter

    def _find_sportybet_match(self, home: str, away: str) -> Optional[dict]:
        target_key = match_key(home, away)

        for sb_match in self.sportybet_matches.values():
            sb_key = match_key(sb_match.get("home_team", ""), sb_match.get("away_team", ""))
            if sb_key == target_key:
                return sb_match

        best_match = None
        best_score = 0.0
        for sb_match in self.sportybet_matches.values():
            sb_key = match_key(sb_match.get("home_team", ""), sb_match.get("away_team", ""))
            score = difflib.SequenceMatcher(None, target_key, sb_key).ratio()
            if score > best_score:
                best_score = score
                best_match = sb_match

        if best_match and best_score >= self.FUZZY_THRESHOLD:
            logger.debug(
                f"[LINK] Fuzzy match {home} vs {away} -> "
                f"{best_match.get('home_team')} vs {best_match.get('away_team')} "
                f"(score={best_score:.2f})"
            )
            return best_match

        return None

    def _notify_link(self, home: str, away: str, sb_match: dict):
        if not self.alerter:
            return
        sb_home = sb_match.get("home_team", "")
        sb_away = sb_match.get("away_team", "")
        sb_id = sb_match.get("match_id", "")

        msg = (
            f"🔗 <b>MATCH LINKED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Polymarket: {home} vs {away}\n"
            f"SportyBet: {sb_home} vs {sb_away}\n"
            f"🆔 SportyBet ID: <code>{sb_id}</code>\n"
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
            f"SportyBet match no longer live (after grace period) — link removed."
        )
        try:
            asyncio.get_running_loop()
            asyncio.create_task(self.alerter.send(msg))
        except RuntimeError:
            pass

    def _reconcile_once(self):
        poly_matches = self.polymarket_feed.get_matches()
        newly_linked = 0
        newly_unlinked = 0

        current_sb_ids = {m.get("match_id") for m in self.sportybet_matches.values()}

        # Handle links whose SportyBet match is currently missing
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
                logger.debug(
                    f"[LINK] match_id={sb_id} missing from this poll — "
                    f"starting {self.GRACE_PERIOD}s grace period before unlinking"
                )
                continue

            if (now - first_missed) < self.GRACE_PERIOD:
                continue

            poly_match = next(
                (m for m in poly_matches if m.get("match_id") == poly_id), None
            )
            if poly_match:
                home = poly_match.get("home_team", "")
                away = poly_match.get("away_team", "")
                logger.warning(f"[LINK] Lost link for '{home}' vs '{away}' (grace period expired)")
                self._notify_unlink(home, away)

            del self.links[poly_id]
            self._missing_since.pop(poly_id, None)
            newly_unlinked += 1

        # Find new links
        for poly_match in poly_matches:
            poly_id = poly_match.get("match_id")
            if not poly_id:
                continue

            home = poly_match.get("home_team", "")
            away = poly_match.get("away_team", "")

            existing_link = self.links.get(poly_id)

            if existing_link and existing_link.get("match_id") in current_sb_ids:
                continue

            sb_match = self._find_sportybet_match(home, away)

            if sb_match:
                newly_linked += 1
                logger.success(f"[LINK] '{home}' vs '{away}' -> SportyBet match_id={sb_match.get('match_id')}")
                self.links[poly_id] = sb_match
                self._missing_since.pop(poly_id, None)
                self._notify_link(home, away, sb_match)

        if newly_linked or newly_unlinked:
            logger.info(
                f"[LINK] Reconcile: {len(self.links)} active links "
                f"(+{newly_linked} new, -{newly_unlinked} lost)"
            )

    async def _reconcile_loop(self):
        logger.info("MatchLinker: starting continuous match-linking loop...")
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
            logger.debug(f"[TRACKING] New match: {home} vs {away} (score={new_score})")

            if poly_id not in self.links:
                sb_match = self._find_sportybet_match(home, away)
                if sb_match:
                    self.links[poly_id] = sb_match
                    logger.success(f"[LINK] Immediate link: {home} vs {away} -> SportyBet {sb_match.get('match_id')}")
                    self._notify_link(home, away, sb_match)

            return

        if new_score == old_score:
            return

        total_old = sum(old_score)
        total_new = sum(new_score)

        if total_new <= total_old:
            return

        if new_score[0] > old_score[0]:
            scoring_team = home
        elif new_score[1] > old_score[1]:
            scoring_team = away
        else:
            logger.warning(
                f"[GOAL] Unable to determine scorer for {home} "
                f"{old_score[0]}-{old_score[1]} -> {new_score[0]}-{new_score[1]} {away}"
            )
            return

        goal_num = total_new

        sb_match = self.links.get(poly_id)

        if not sb_match:
            logger.info(
                f"[LINK] Goal detected before normal reconciliation for "
                f"'{home}' vs '{away}'. Attempting immediate SportyBet lookup..."
            )
            sb_match = self._find_sportybet_match(home, away)
            if sb_match:
                self.links[poly_id] = sb_match
                logger.success(f"[LINK] Immediate goal-time link: {home} vs {away} -> SportyBet {sb_match.get('match_id')}")
                self._notify_link(home, away, sb_match)

        if not sb_match:
            logger.warning(f"⚠️ Goal detected for '{home}' vs '{away}' but no active SportyBet link exists.")
            return

        logger.success(
            f"⚽ GOAL: {home} {new_score[0]}-{new_score[1]} {away} "
            f"(goal #{goal_num}, scorer: {scoring_team})"
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
            })
