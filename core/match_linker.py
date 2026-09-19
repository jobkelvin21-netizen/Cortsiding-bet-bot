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
    def __init__(self, fast_feed, sportybet_matches_ref: dict, alerter=None):
        self.fast_feed = fast_feed
        self.sportybet_matches = sportybet_matches_ref
        self.alerter = alerter

        self.links: Dict[str, dict] = {}
        
        self.slow_match: Optional[dict] = None
        self.slow_fast_id: Optional[str] = None
        self.slow_start_time: Optional[float] = None
        
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

    def _get_b365_seconds(self, fast_match: dict) -> int:
        seconds = fast_match.get("played_seconds")
        if seconds is not None:
            try:
                return int(seconds)
            except (ValueError, TypeError):
                pass
        
        minute = fast_match.get("minute")
        if minute is not None:
            try:
                return int(float(minute)) * 60
            except (ValueError, TypeError):
                pass
        
        return 0

    def _sb_seconds(self, sb: dict) -> int:
        try:
            return int(sb.get("played_seconds") or 0)
        except (ValueError, TypeError):
            return 0

    def _find_sportybet_match(self, home: str, away: str) -> Optional[dict]:
        if not self.sportybet_matches:
            logger.warning("[LINK] No SportyBet matches!")
            return None
            
        logger.debug(f"[LINK] Looking for: '{home}' vs '{away}'")
        
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
                self._notify_link(home, away, sb.get('match_id'))
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
            self._notify_link(home, away, best.get('match_id'))
            return best
            
        logger.warning(f"[LINK] No match for: {home} vs {away}")
        return None

    def _notify_link(self, home: str, away: str, match_id: str):
        """Notify Telegram of successful link."""
        if self.alerter:
            try:
                task = asyncio.create_task(
                    self.alerter.notify_link_found(home, away, match_id)
                )
                task.add_done_callback(
                    lambda t: logger.error(f"Telegram link error: {t.exception()}") if t.exception() else None
                )
            except Exception as e:
                logger.error(f"Failed to send link notification: {e}")

    def _is_match_active(self, match: dict) -> bool:
        period = str(match.get("period") or match.get("match_status") or "").lower()
        
        if any(x in period for x in ["ht", "half", "break", "finished", "ft", "ended", "full"]):
            return False
        if "45" in period and "half" in period:
            return False
            
        return True

    def _clear_slow_match(self, reason: str):
        if self.slow_match and self.slow_fast_id:
            home = self.slow_match.get("home_team", "?")
            away = self.slow_match.get("away_team", "?")
            logger.warning(f"[SLOW] CLEARED {home} vs {away} - {reason}")
            
            if self.alerter:
                try:
                    asyncio.create_task(self.alerter.send(
                        f"⏹️ <b>Slow match cleared</b>\n{home} vs {away}\nReason: {reason}"
                    ))
                except Exception:
                    pass
            
            if self.unlink_callback:
                try:
                    asyncio.create_task(self.unlink_callback(dict(self.slow_match)))
                except Exception:
                    pass
        
        self.slow_match = None
        self.slow_fast_id = None
        self.slow_start_time = None

    def _check_slow(self, fast_id: str, fast_match: dict, sb: dict):
        b365_min = fast_match.get("minute", 0)
        if b365_min < self.SLOW_MIN_MINUTE and fast_id != self.slow_fast_id:
            return

        b365_sec = self._get_b365_seconds(fast_match)
        sb_sec = self._sb_seconds(sb)
        
        lag_seconds = b365_sec - sb_sec
        
        now = time.time()

        if self.slow_fast_id == fast_id and self.slow_match:
            if not self._is_match_active(fast_match):
                self._clear_slow_match("HT/FT detected")
                return

        if lag_seconds >= self.SLOW_LAG_SECONDS:
            st = self.clock_state.get(fast_id) or {"ahead_since": None}
            
            if st["ahead_since"] is None:
                st["ahead_since"] = now
                self.clock_state[fast_id] = st
                logger.debug(f"[LAG] {fast_id} lag={lag_seconds}s - STARTED")
            else:
                elapsed = now - st["ahead_since"]
                
                should_switch = False
                
                if self.slow_match is None:
                    should_switch = True
                elif fast_id == self.slow_fast_id:
                    pass
                else:
                    current_st = self.clock_state.get(self.slow_fast_id, {})
                    current_elapsed = now - (current_st.get("ahead_since") or now)
                    
                    if elapsed > current_elapsed + 5:
                        logger.info("[SLOW] Found better slow match - switching")
                        self._clear_slow_match("found better match")
                        should_switch = True
                
                if should_switch and elapsed >= self.SLOW_THRESHOLD_SECONDS:
                    self.slow_match = sb
                    self.slow_fast_id = fast_id
                    self.slow_start_time = now
                    
                    home = fast_match.get("home_team") or sb.get("home_team")
                    away = fast_match.get("away_team") or sb.get("away_team")
                    
                    logger.success("=" * 60)
                    logger.success(f"[SLOW LOCK] {home} vs {away}")
                    logger.success(f"  Bet365: {b365_sec}s ({b365_sec//60}:{b365_sec%60:02d})")
                    logger.success(f"  SportyBet: {sb_sec}s ({sb_sec//60}:{sb_sec%60:02d})")
                    logger.success(f"  Lag: {lag_seconds} seconds")
                    logger.success("=" * 60)
                    
                    if self.alerter:
                        try:
                            task = asyncio.create_task(
                                self.alerter.notify_slow_match_found(home, away, elapsed)
                            )
                            task.add_done_callback(
                                lambda t: logger.error(f"Telegram slow error: {t.exception()}") if t.exception() else None
                            )
                        except Exception as e:
                            logger.error(f"Failed to send slow notification: {e}")
                    
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
            if fast_id in self.clock_state:
                self.clock_state[fast_id]["ahead_since"] = None

    def _reconcile_once(self):
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
            
            if fid == self.slow_fast_id:
                self._clear_slow_match("match disappeared")
            
            del self.links[fid]
            self._missing_since.pop(fid, None)

        for fm in fast_matches:
            fid = str(fm.get("match_id") or "")
            if not fid:
                continue
            
            home, away = fm.get("home_team", ""), fm.get("away_team", "")
            if not home or not away:
                continue
            
            if not self._is_match_active(fm):
                continue
            
            existing = self.links.get(fid)
            if existing and existing.get("match_id") in current_sb:
                self._check_slow(fid, fm, existing)
                continue
            
            sb = self._find_sportybet_match(home, away)
            if sb:
                self.links[fid] = sb
                self._missing_since.pop(fid, None)
                logger.success(f"[LINK] {home} vs {away} → sb:{sb.get('match_id')}")
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

        if not self._is_match_active(match):
            if fid == self.slow_fast_id:
                self._clear_slow_match("HT/FT detected in callback")
            return

        sb = self.links.get(fid)
        if not sb and home and away:
            sb = self._find_sportybet_match(home, away)
            if sb:
                self.links[fid] = sb

        if sb:
            self._check_slow(fid, match, sb)

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
