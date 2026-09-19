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
        
        # Use your Config values - 5 seconds lag
        self.GRACE_PERIOD = getattr(Config, 'LINK_GRACE_PERIOD_SECONDS', 12)
        self.SLOW_LAG_SECONDS = getattr(Config, 'SLOW_LAG_SECONDS', 5)  # 5 seconds ahead (was 2)
        self.SLOW_THRESHOLD_SECONDS = getattr(Config, 'SLOW_THRESHOLD_SECONDS', 5)  # 5 seconds duration
        self.SLOW_MIN_MINUTE = getattr(Config, 'SLOW_MIN_MINUTE', 8)
        
        self._reconcile_task = None

    def set_goal_callback(self, cb): self.goal_callback = cb
    def set_link_callback(self, cb): self.link_callback = cb
    def set_unlink_callback(self, cb): self.unlink_callback = cb
    def set_slow_callback(self, cb): self.slow_callback = cb
    def set_alerter(self, alerter): self.alerter = alerter

    def _find_sportybet_match(self, home: str, away: str) -> Optional[dict]:
        """Find SportyBet match by exact or fuzzy team name matching."""
        if not self.sportybet_matches:
            return None
            
        target_key = match_key(home, away)
        th, ta = get_tokens(home), get_tokens(away)
        
        # First try exact match
        for sb in self.sportybet_matches.values():
            if not isinstance(sb, dict):
                continue
            sb_home = sb.get("home_team", "")
            sb_away = sb.get("away_team", "")
            if match_key(sb_home, sb_away) == target_key:
                return sb
        
        # Fuzzy fallback
        best, best_score = None, 0.0
        for sb in self.sportybet_matches.values():
            if not isinstance(sb, dict):
                continue
            sh, sa = sb.get("home_team", ""), sb.get("away_team", "")
            score = difflib.SequenceMatcher(
                None, target_key, match_key(sh, sa)
            ).ratio()
            
            if th and ta:
                sh_tokens, sa_tokens = get_tokens(sh), get_tokens(sa)
                if len(th & sh_tokens) >= 1 and len(ta & sa_tokens) >= 1:
                    score += 0.12
            
            if score > best_score:
                best_score, best = score, sb
                
        if best and best_score >= self.FUZZY_THRESHOLD:
            return best
        return None

    def _get_b365_minute(self, fast_match: dict) -> float:
        """Extract minute from Bet365 match."""
        minute = fast_match.get("minute")
        if minute is not None:
            try:
                return float(minute)
            except (ValueError, TypeError):
                pass
        
        seconds = fast_match.get("played_seconds")
        if seconds is not None:
            try:
                return float(seconds) / 60.0
            except (ValueError, TypeError):
                pass
        
        return 0.0

    def _sb_minute(self, sb: dict) -> float:
        """Extract minute from SportyBet match."""
        try:
            sec = float(sb.get("played_seconds") or 0)
            return sec / 60.0
        except (ValueError, TypeError):
            return 0.0

    def _update_clock_lag(self, fast_id: str, fast_match: dict, sb: dict):
        """Detect slow games - 5 seconds lag threshold."""
        b365_min = self._get_b365_minute(fast_match)
        sb_min = self._sb_minute(sb)
        now = time.time()

        st = self.clock_state.get(fast_id) or {
            "ahead_since": None,
            "last_b365": 0.0,
            "last_sb": 0.0,
        }
        st["last_b365"] = b365_min
        st["last_sb"] = sb_min

        # Convert 5 seconds lag to minutes (5/60 = 0.083 min)
        lag_threshold_min = self.SLOW_LAG_SECONDS / 60.0
        
        if b365_min < self.SLOW_MIN_MINUTE:
            st["ahead_since"] = None
            self.clock_state[fast_id] = st
            if fast_id in self.slow_ids and b365_min < 5:
                self.slow_ids.discard(fast_id)
            return

        # Bet365 clock ahead by 5+ seconds?
        if b365_min > sb_min + lag_threshold_min:
            if st["ahead_since"] is None:
                st["ahead_since"] = now
                logger.debug(f"[LAG] {fast_id} b365={b365_min:.2f}' sb={sb_min:.2f}' - tracking")
            elif (now - st["ahead_since"]) >= self.SLOW_THRESHOLD_SECONDS:
                # Been 5+ seconds ahead for 5+ seconds - mark slow!
                if fast_id not in self.slow_ids:
                    self.slow_ids.add(fast_id)
                    home = fast_match.get("home_team") or sb.get("home_team")
                    away = fast_match.get("away_team") or sb.get("away_team")
                    lag = now - st["ahead_since"]
                    logger.success(
                        f"[SLOW] {home} vs {away} | b365={b365_min:.1f}' "
                        f"sb={sb_min:.1f}' ahead_for={lag:.1f}s"
                    )
                    if self.alerter:
                        try:
                            asyncio.get_running_loop().create_task(
                                self.alerter.notify_slow_match_found(home, away, lag)
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
        else:
            if st["ahead_since"] is not None:
                logger.debug(f"[LAG] {fast_id} lag cleared")
            st["ahead_since"] = None

        self.clock_state[fast_id] = st

    def _reconcile_once(self):
        """Main reconciliation loop."""
        fast_matches = []
        try:
            fast_matches = self.fast_feed.get_matches()
        except Exception:
            return

        if not self.sportybet_matches:
            return

        current_sb = {
            m.get("match_id") 
            for m in self.sportybet_matches.values() 
            if isinstance(m, dict) and m.get("match_id")
        }
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
                    asyncio.get_running_loop().create_task(self.unlink_callback(linked))
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
                logger.success(f"[LINK] {home} vs {away} → sb:{sb.get('match_id')}")
                
                if self.link_callback:
                    try:
                        asyncio.get_running_loop().create_task(self.link_callback(dict(sb)))
                    except Exception:
                        pass
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
                if self.link_callback:
                    try:
                        asyncio.get_running_loop().create_task(self.link_callback(dict(sb)))
                    except Exception:
                        pass

        if sb:
            self._update_clock_lag(fid, match, sb)

        if old_score is None or new_score == old_score or sum(new_score) <= sum(old_score):
            return

        if new_score[0] > old_score[0]:
            scoring = home
        elif new_score[1] > old_score[1]:
            scoring = away
        else:
            return
        
        goal_num = sum(new_score)

        if not sb:
            logger.warning(f"[GOAL] no SB link for {home} vs {away}")
            return

        detected_at = time.time()
        is_slow = fid in self.slow_ids
        
        logger.success(
            f"⚽ GOAL: {home} {new_score[0]}-{new_score[1]} {away} "
            f"#{goal_num} slow={is_slow}"
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
                "is_slow": is_slow,
            })

    async def on_polymarket(self, match: dict):
        await self.on_fast_feed(match)
