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
    return "".join(words)


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
    Links Bet365 (primary) / Polymarket (backup) → SportyBet.
    Slow = Bet365 clock ahead of SportyBet for SLOW_THRESHOLD_SECONDS.
    Goal fire = Bet365 score increases on a slow (or linked) match.
    """

    def __init__(self, fast_feed, sportybet_matches_ref: dict, alerter=None):
        self.fast_feed = fast_feed
        self.sportybet_matches = sportybet_matches_ref
        self.alerter = alerter

        self.links: Dict[str, dict] = {}  # fast_id -> sb_match
        self.last_scores: Dict[str, tuple] = {}
        self.clock_state: Dict[str, dict] = {}  # fast_id -> lag tracking
        self.slow_ids: Set[str] = set()
        self._missing_since: Dict[str, float] = {}

        self.goal_callback: Optional[Callable] = None
        self.link_callback: Optional[Callable] = None
        self.unlink_callback: Optional[Callable] = None
        self.slow_callback: Optional[Callable] = None

        self.running = False
        self.FUZZY_THRESHOLD = 0.68
        self.RECONCILE_INTERVAL = 2.0
        self.GRACE_PERIOD = Config.LINK_GRACE_PERIOD_SECONDS
        self._reconcile_task = None

    def set_goal_callback(self, cb): self.goal_callback = cb
    def set_link_callback(self, cb): self.link_callback = cb
    def set_unlink_callback(self, cb): self.unlink_callback = cb
    def set_slow_callback(self, cb): self.slow_callback = cb
    def set_alerter(self, alerter): self.alerter = alerter

    def _find_sportybet_match(self, home: str, away: str) -> Optional[dict]:
        target_key = match_key(home, away)
        th, ta = get_tokens(home), get_tokens(away)
        for sb in self.sportybet_matches.values():
            if match_key(sb.get("home_team", ""), sb.get("away_team", "")) == target_key:
                return sb
        best, best_score = None, 0.0
        for sb in self.sportybet_matches.values():
            sh, sa = sb.get("home_team", ""), sb.get("away_team", "")
            score = difflib.SequenceMatcher(
                None, target_key, match_key(sh, sa)
            ).ratio()
            if len(th & get_tokens(sh)) >= 1 and len(ta & get_tokens(sa)) >= 1:
                score += 0.12
            if score > best_score:
                best_score, best = score, sb
        if best and best_score >= self.FUZZY_THRESHOLD:
            return best
        return None

    def _sb_minute(self, sb: dict) -> float:
        try:
            sec = float(sb.get("played_seconds") or 0)
            return sec / 60.0
        except Exception:
            return 0.0

    def _update_clock_lag(self, fast_id: str, fast_match: dict, sb: dict):
        """Clock-only slow detection."""
        b365_min = float(fast_match.get("minute") or 0)
        sb_min = self._sb_minute(sb)
        now = time.time()

        st = self.clock_state.get(fast_id) or {
            "ahead_since": None,
            "last_b365": 0.0,
            "last_sb": 0.0,
        }
        st["last_b365"] = b365_min
        st["last_sb"] = sb_min

        min_gate = Config.SLOW_MIN_MINUTE
        if b365_min < min_gate:
            st["ahead_since"] = None
            self.clock_state[fast_id] = st
            self.slow_ids.discard(fast_id)
            return

        # Bet365 clock ahead by \~1+ minute worth of signal
        if b365_min > sb_min + 0.4:
            if st["ahead_since"] is None:
                st["ahead_since"] = now
            elif (now - st["ahead_since"]) >= Config.SLOW_THRESHOLD_SECONDS:
                if fast_id not in self.slow_ids:
                    self.slow_ids.add(fast_id)
                    home = fast_match.get("home_team") or sb.get("home_team")
                    away = fast_match.get("away_team") or sb.get("away_team")
                    lag = now - st["ahead_since"]
                    logger.success(
                        f"[SLOW] {home} vs {away} | b365={b365_min:.0f}' "
                        f"sb={sb_min:.0f}' lag≥{lag:.1f}s"
                    )
                    if self.alerter:
                        try:
                            asyncio.get_running_loop().create_task(
                                self.alerter.notify_slow_match_found(
                                    home, away, lag
                                )
                            )
                        except Exception:
                            pass
                    if self.slow_callback:
                        try:
                            asyncio.get_running_loop().create_task(
                                self.slow_callback(dict(sb))
                            )
                        except Exception:
                            pass
                    # open page via link callback if not already
                    if self.link_callback:
                        try:
                            asyncio.get_running_loop().create_task(
                                self.link_callback(dict(sb))
                            )
                        except Exception:
                            pass
        else:
            st["ahead_since"] = None
            # do not auto-remove slow — keep armed until HT / unlink

        self.clock_state[fast_id] = st

    def _reconcile_once(self):
        fast_matches = []
        try:
            fast_matches = self.fast_feed.get_matches()
        except Exception:
            return

        current_sb = {m.get("match_id") for m in self.sportybet_matches.values()}
        newly_linked = newly_unlinked = 0

        for fid in list(self.links.keys()):
            linked = self.links.get(fid)
            if not linked:
                continue
            sid = linked.get("match_id")
            if sid in current_sb:
                self._missing_since.pop(fid, None)
                continue
            now = time.time()
            first = self._missing_since.get(fid)
            if first is None:
                self._missing_since[fid] = now
                continue
            if (now - first) < self.GRACE_PERIOD:
                continue
            home = linked.get("home_team", "")
            away = linked.get("away_team", "")
            logger.warning(f"[LINK] lost {home} vs {away}")
            if self.unlink_callback:
                try:
                    asyncio.get_running_loop().create_task(
                        self.unlink_callback(dict(linked))
                    )
                except Exception:
                    pass
            del self.links[fid]
            self.slow_ids.discard(fid)
            self.clock_state.pop(fid, None)
            self.last_scores.pop(fid, None)
            self._missing_since.pop(fid, None)
            newly_unlinked += 1

        for fm in fast_matches:
            fid = str(fm.get("match_id") or "")
            if not fid:
                continue
            home, away = fm.get("home_team", ""), fm.get("away_team", "")
            if not home or not away:
                continue
            existing = self.links.get(fid)
            if existing and existing.get("match_id") in current_sb:
                self._update_clock_lag(fid, fm, existing)
                continue
            sb = self._find_sportybet_match(home, away)
            if sb:
                newly_linked += 1
                self.links[fid] = sb
                self._missing_since.pop(fid, None)
                logger.success(
                    f"[LINK] {home} vs {away} → sb:{sb.get('match_id')}"
                )
                self._update_clock_lag(fid, fm, sb)

        if newly_linked or newly_unlinked:
            logger.info(
                f"[LINK] active={len(self.links)} slow={len(self.slow_ids)} "
                f"(+{newly_linked}/-{newly_unlinked})"
            )

    async def _reconcile_loop(self):
        while self.running:
            try:
                self._reconcile_once()
            except Exception as e:
                logger.error(f"[LINK] reconcile: {e}")
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

    async def on_fast_feed(self, match: dict):
        """Bet365 or Polymarket update."""
        fid = str(match.get("match_id") or "")
        if not fid:
            return
        home = match.get("home_team", "")
        away = match.get("away_team", "")
        new_score = (
            int(match.get("home_score") or 0),
            int(match.get("away_score") or 0),
        )
        old_score = self.last_scores.get(fid)
        self.last_scores[fid] = new_score

        sb = self.links.get(fid)
        if not sb and home and away:
            sb = self._find_sportybet_match(home, away)
            if sb:
                self.links[fid] = sb

        if sb:
            self._update_clock_lag(fid, match, sb)

        if old_score is None:
            return
        if new_score == old_score:
            return
        if sum(new_score) <= sum(old_score):
            return

        scoring = home if new_score[0] > old_score[0] else away
        goal_num = sum(new_score)

        if not sb:
            logger.warning(f"[GOAL] no SB link for {home} vs {away}")
            return

        # Prefer slow matches; still fire if linked (you can restrict to slow_ids only)
        detected_at = time.time()
        logger.success(
            f"⚽ GOAL: {home} {new_score[0]}-{new_score[1]} {away} "
            f"#{goal_num} slow={fid in self.slow_ids}"
        )
        if self.goal_callback:
            await self.goal_callback({
                "match_id": sb.get("match_id"),
                "home_team": sb.get("home_team"),
                "away_team": sb.get("away_team"),
                "home_score": new_score[0],
                "away_score": new_score[1],
                "scoring_team": scoring,
                "goal_num": goal_num,
                "detected_at": detected_at,
                "is_slow": fid in self.slow_ids,
            })

    # backward-compatible name used by main
    async def on_polymarket(self, match: dict):
        await self.on_fast_feed(match)
