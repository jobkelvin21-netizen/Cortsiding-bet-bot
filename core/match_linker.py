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
        r"\b(fc|cf|sc|afc|cd|ac|fk|nk|sk|as|ss|rc|ca|cs|us|sd|ec|ud|cp|ce|sad|"
        r"club|united|city|town|rovers|athletic|sporting)\b",
        " ",
        name,
    )
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    words = [w for w in name.split() if len(w) > 1]
    if len(words) > 1 and len(words[-1]) <= 2:
        words = words[:-1]
    return " ".join(words)


def get_tokens(name: str) -> Set[str]:
    if not name:
        return set()
    name = strip_accents(name).lower()
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    tokens = {w for w in name.split() if len(w) > 2}
    return tokens - {"the", "and", "fc", "cf", "sc", "united", "city", "club"}


def match_key(home: str, away: str) -> str:
    h, a = normalize_team_name(home), normalize_team_name(away)
    if h > a:
        h, a = a, h
    return f"{h}_vs_{a}"


class MatchLinker:
    """
    AI provides slow lock (tickers).
    This class: map AI names → SportyBet match (for URL/id),
    fire goal only for locked match from Bet365 WS.
    """

    def __init__(self, fast_feed, sportybet_matches_ref: dict, alerter=None):
        self.fast_feed = fast_feed
        self.sportybet_matches = sportybet_matches_ref
        self.alerter = alerter

        self.links: Dict[str, dict] = {}  # bet365_mid -> sb match
        self.name_to_sb: Dict[str, dict] = {}

        self.slow_match: Optional[dict] = None  # SportyBet match dict
        self.slow_meta: Optional[dict] = None   # AI lock fields
        self.slow_fast_id: Optional[str] = None

        self.last_scores: Dict[str, tuple] = {}

        self.goal_callback: Optional[Callable] = None
        self.link_callback: Optional[Callable] = None
        self.unlink_callback: Optional[Callable] = None
        self.slow_callback: Optional[Callable] = None

        self.running = False
        self.FUZZY_THRESHOLD = 0.68
        self._reconcile_task = None

    def set_goal_callback(self, cb): self.goal_callback = cb
    def set_link_callback(self, cb): self.link_callback = cb
    def set_unlink_callback(self, cb): self.unlink_callback = cb
    def set_slow_callback(self, cb): self.slow_callback = cb

    def _find_sportybet_match(self, home: str, away: str) -> Optional[dict]:
        if not self.sportybet_matches:
            return None
        target_key = match_key(home, away)
        th, ta = get_tokens(home), get_tokens(away)

        for sb in self.sportybet_matches.values():
            if not isinstance(sb, dict):
                continue
            if match_key(sb.get("home_team", ""), sb.get("away_team", "")) == target_key:
                return sb

        best, best_score = None, 0.0
        for sb in self.sportybet_matches.values():
            if not isinstance(sb, dict):
                continue
            sh, sa = sb.get("home_team", ""), sb.get("away_team", "")
            score = difflib.SequenceMatcher(
                None, target_key, match_key(sh, sa)
            ).ratio()
            if th and ta:
                if len(th & get_tokens(sh)) >= 1 and len(ta & get_tokens(sa)) >= 1:
                    score += 0.12
            if score > best_score:
                best_score, best = score, sb
        if best and best_score >= self.FUZZY_THRESHOLD:
            return best
        return None

    async def apply_ai_lock(self, ai_lock: Optional[dict]):
        """Called from main when SlowGameAI updates."""
        if not ai_lock:
            if self.slow_match and self.unlink_callback:
                try:
                    await self.unlink_callback(dict(self.slow_match))
                except Exception:
                    pass
            self.slow_match = None
            self.slow_meta = None
            self.slow_fast_id = None
            return

        home = ai_lock.get("sporty_home") or ai_lock.get("bet365_home") or ""
        away = ai_lock.get("sporty_away") or ai_lock.get("bet365_away") or ""
        sb = self._find_sportybet_match(home, away)
        if not sb:
            logger.warning(f"[LINK] AI slow but no SportyBet row for {home} vs {away}")
            return

        self.slow_match = sb
        self.slow_meta = dict(ai_lock)
        logger.success(
            f"[LINK+SLOW] {sb.get('home_team')} vs {sb.get('away_team')} "
            f"id={sb.get('match_id')} lag={ai_lock.get('lag_seconds')}"
        )
        if self.alerter:
            try:
                await self.alerter.send(
                    f"🐢 <b>Slow lock</b>\n{sb.get('home_team')} vs {sb.get('away_team')}\n"
                    f"Lag: {ai_lock.get('lag_seconds')}s (tickers)"
                )
            except Exception:
                pass
        if self.slow_callback:
            await self.slow_callback(dict(sb))
        if self.link_callback:
            await self.link_callback(dict(sb))

    def bind_bet365_id(self, bet365_mid: str, home: str, away: str):
        """Optional: bind WS mid after names known."""
        if not bet365_mid or not self.slow_match:
            return
        if normalize_team_name(home) and normalize_team_name(away):
            # soft bind if names match locked
            sh = self.slow_match.get("home_team", "")
            sa = self.slow_match.get("away_team", "")
            if match_key(home, away) == match_key(sh, sa) or difflib.SequenceMatcher(
                None, match_key(home, away), match_key(sh, sa)
            ).ratio() >= 0.68:
                self.slow_fast_id = str(bet365_mid)
                self.links[str(bet365_mid)] = self.slow_match

    async def start(self):
        self.running = True

    def stop(self):
        self.running = False

    async def on_fast_feed(self, match: dict):
        """Bet365 WS updates — goal only if locked slow match."""
        if not self.slow_match:
            return

        fid = str(match.get("match_id") or "")
        home = match.get("home_team") or ""
        away = match.get("away_team") or ""

        # Bind id when names appear
        if fid and home and away:
            self.bind_bet365_id(fid, home, away)

        # Must be the locked fixture (by id or by name)
        locked = False
        if self.slow_fast_id and fid == self.slow_fast_id:
            locked = True
        else:
            sh = self.slow_match.get("home_team", "")
            sa = self.slow_match.get("away_team", "")
            if home and away and difflib.SequenceMatcher(
                None, match_key(home, away), match_key(sh, sa)
            ).ratio() >= 0.68:
                locked = True
                if fid:
                    self.slow_fast_id = fid
                    self.links[fid] = self.slow_match

        if not locked:
            return

        new_score = (
            int(match.get("home_score") or 0),
            int(match.get("away_score") or 0),
        )
        key = self.slow_fast_id or fid or match_key(
            self.slow_match.get("home_team", ""),
            self.slow_match.get("away_team", ""),
        )
        old_score = self.last_scores.get(key)
        self.last_scores[key] = new_score

        if old_score is None or new_score == old_score or sum(new_score) <= sum(old_score):
            return

        if new_score[0] > old_score[0]:
            scoring = self.slow_match.get("home_team")
        elif new_score[1] > old_score[1]:
            scoring = self.slow_match.get("away_team")
        else:
            return

        goal_num = sum(new_score)
        logger.success(
            f"⚽ GOAL WS (slow lock): {self.slow_match.get('home_team')} "
            f"{new_score[0]}-{new_score[1]} {self.slow_match.get('away_team')} #{goal_num}"
        )
        if self.goal_callback:
            await self.goal_callback({
                "match_id": self.slow_match.get("match_id"),
                "home_team": self.slow_match.get("home_team"),
                "away_team": self.slow_match.get("away_team"),
                "home_score": new_score[0],
                "away_score": new_score[1],
                "scoring_team": scoring,
                "goal_num": goal_num,
                "detected_at": time.time(),
                "is_slow": True,
            })
