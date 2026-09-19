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
    FOCUS: One slow game at a time.
    Logic: 1) Link by name, 2) Compare clocks, 3) Mark slow if Bet365 AHEAD.
    """

    def __init__(self, fast_feed, sportybet_matches_ref: dict, alerter=None):
        self.fast_feed = fast_feed
        self.sportybet_matches = sportybet_matches_ref
        self.alerter = alerter

        self.links: Dict[str, dict] = {}  # fast_id -> sb_match
        
        # FOCUS: Only ONE slow match at a time
        self.slow_match: Optional[dict] = None
        self.slow_fast_id: Optional[str] = None
        
        self.last_scores: Dict[str, tuple] = {}
        self.clock_state: Dict[str, dict] = {}
        self._missing_since: Dict[str, float] = {}

        self.goal_callback: Optional[Callable] = None
        self.link_callback: Optional[Callable] = None
        self.unlink_callback: Optional[Callable] = None
        self.slow_callback: Optional[Callable] = None

        self.running = False
        self.FUZZY_THRESHOLD = 0.68
        self.RECONCILE_INTERVAL = 2.0
        
        self.GRACE_PERIOD = getattr(Config, 'LINK_GRACE_PERIOD_SECONDS', 12)
        self.SLOW_LAG_SECONDS = getattr(Config, 'SLOW_LAG_SECONDS', 5)
        self.SLOW_THRESHOLD_SECONDS = getattr(Config, 'SLOW_THRESHOLD_SECONDS', 5)
        self.SLOW_MIN_MINUTE = getattr(Config, 'SLOW_MIN_MINUTE', 8)
        
        self._reconcile_task = None

    def set_goal_callback(self, cb): self.goal_callback = cb
    def set_link_callback(self, cb): self.link_callback = cb
    def set_unlink_callback(self, cb): self.unlink_callback = cb
    def set_slow_callback(self, cb): self.slow_callback = cb
    def set_alerter(self, alerter): self.alerter = alerter

    def _find_sportybet_match(self, home: str, away: str) -> Optional[dict]:
        """STEP 1: Link by team name matching."""
        if not self.sportybet_matches:
            logger.warning("[LINK] No SportyBet matches available!")
            return None
            
        logger.debug(f"[LINK] Searching for: '{home}' vs '{away}'")
        
        target_key = match_key(home, away)
        th, ta = get_tokens(home), get_tokens(away)
        
        # Exact match first
        for sb in self.sportybet_matches.values():
            if not isinstance(sb, dict):
                continue
            sb_home = sb.get("home_team", "")
            sb_away = sb.get("away_team", "")
            if match_key(sb_home, sb_away) == target_key:
                logger.success(f"[LINK] Exact match: {home} vs {away}")
                return sb
        
        # Fuzzy fallback
        best, best_score = None, 0.0
        for sb in self.sportybet_matches.values():
            if not isinstance(sb, dict):
                continue
            sh, sa = sb.get("home_team", ""), sb.get("away_team", "")
            score = difflib.SequenceMatcher(None, target_key, match_key(sh, sa)).ratio()
            
            if th and ta:
                sh_tokens, sa_tokens = get_tokens(sh), get_tokens(sa)
                if len(th & sh_tokens) >= 1 and len(ta & sa_tokens) >= 1:
                    score += 0.12
            
            if score > best_score:
                best_score, best = score, sb
                
        if best and best_score >= self.FUZZY_THRESHOLD:
            logger.success(f"[LINK] Fuzzy match ({best_score:.2f}): {home} vs {away}")
            return best
            
        logger.warning(f"[LINK] No match found for: {home} vs {away}")
        return None

    def _get_b365_minute(self, fast_match: dict) -> float:
        """Extract minute from Bet365."""
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
        """Extract minute from SportyBet."""
        try:
            sec = float(sb.get("played_seconds") or 0)
            return sec / 60.0
        except (ValueError, TypeError):
            return 0.0

    def _check_slow(self, fast_id: str, fast_match: dict, sb: dict):
        """
        STEP 2: Compare clocks.
        SLOW = Bet365 clock AHEAD of SportyBet by SLOW_LAG_SECONDS for SLOW_THRESHOLD_SECONDS.
        """
        # Ignore if already have a different slow match
        if self.slow_match is not None and fast_id != self.slow_fast_id:
            return
            
        b365_min = self._get_b365_minute(fast_match)
        sb_min = self._sb_minute(sb)
        now = time.time()

        # Must be past minimum minute
        if b365_min < self.SLOW_MIN_MINUTE:
            return

        # Check if Bet365 is AHEAD (not behind)
        lag_threshold_min = self.SLOW_LAG_SECONDS / 60.0
        
        if b365_min > sb_min + lag_threshold_min:
            # Bet365 is ahead - track duration
            st = self.clock_state.get(fast_id) or {"ahead_since": None}
            
            if st["ahead_since"] is None:
                st["ahead_since"] = now
                self.clock_state[fast_id] = st
                logger.debug(f"[LAG] {fast_id} b365={b365_min:.2f}' sb={sb_min:.2f}' - STARTED")
            else:
                elapsed = now - st["ahead_since"]
                if elapsed >= self.SLOW_THRESHOLD_SECONDS:
                    # SLOW MATCH CONFIRMED!
                    if self.slow_match is None:
                        self.slow_match = sb
                        self.slow_fast_id = fast_id
                        home = fast_match.get("home_team") or sb.get("home_team")
                        away = fast_match.get("away_team") or sb.get("away_team")
                        logger.success("=" * 60)
                        logger.success(f"[SLOW LOCK] {home} vs {away}")
                        logger.success(f"  Bet365: {b365_min:.1f} min (AHEAD)")
                        logger.success(f"  SportyBet: {sb_min:.1f} min")
                        logger.success(f"  Gap: {(b365_min - sb_min)*60:.0f} seconds")
                        logger.success(f"  Duration: {elapsed:.1f}s")
                        logger.success("=" * 60)
                        
                        if self.alerter:
                            try:
                                asyncio.create_task(self.alerter.notify_slow_match_found(home, away, elapsed))
                            except Exception:
                                pass
                        if self.slow_callback:
                            try:
                                asyncio.create_task(self.slow_callback(dict(sb)))
                            except Exception:
                                pass
                        if self.link_callback:
                            try:
                                asyncio.create_task(self.link_callback(dict(sb)))
                            except Exception:
                                pass
        else:
            # Not ahead - reset
            if fast_id in self.clock_state:
                self.clock_state[fast_id]["ahead_since"] = None

    def _reconcile_once(self):
        """Main loop: Link by name, then check for slow."""
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

        # Check existing links
        for fid in list(self.links.keys()):
            linked = self.links.get(fid)
            if not linked:
                continue
            sid = linked.get("match_id")
            if sid in current_sb:
                self._missing_since.pop(fid, None)
                continue
            
            # Match disappeared
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
            
            if fid == self.slow_fast_id:
                self.slow_match = None
                self.slow_fast_id = None
                logger.warning("[SLOW] Cleared slow match (disappeared)")
            
            if self.unlink_callback:
                try:
                    asyncio.create_task(self.unlink_callback(linked))
                except Exception:
                    pass
            
            del self.links[fid]
            self._missing_since.pop(fid, None)

        # Link new matches
        for fm in fast_matches:
            fid = str(fm.get("match_id") or "")
            if not fid:
                continue
            
            home, away = fm.get("home_team", ""), fm.get("away_team", "")
            if not home or not away:
                continue
            
            existing = self.links.get(fid)
            if existing and existing.get("match_id") in current_sb:
                # Already linked - check if slow
                self._check_slow(fid, fm, existing)
                continue
            
            # Try to link by name
            sb = self._find_sportybet_match(home, away)
            if sb:
                self.links[fid] = sb
                self._missing_since.pop(fid, None)
                logger.success(f"[LINK] {home} vs {away} → sb:{sb.get('match_id')}")
                # Now check if slow
                self._check_slow(fid, fm, sb)

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
        """Process Bet365 update."""
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

        # Only process if this is our slow match or we're hunting
        if self.slow_fast_id and fid != self.slow_fast_id:
            return
        
        sb = self.links.get(fid)
        if not sb and home and away:
            sb = self._find_sportybet_match(home, away)
            if sb:
                self.links[fid] = sb

        if sb:
            self._check_slow(fid, match, sb)

        # Goal detection - only for slow match
        if fid != self.slow_fast_id:
            return
            
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
        
        logger.success(
            f"⚽ GOAL: {home} {new_score[0]}-{new_score[1]} {away} "
            f"#{goal_num} [SLOW MATCH - BETTING!]"
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
                "is_slow": True,
            })

    async def on_polymarket(self, match: dict):
        await self.on_fast_feed(match)
