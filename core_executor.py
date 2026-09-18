import asyncio
import re
import time
import uuid
from enum import Enum, auto
from datetime import datetime
from typing import Optional, Dict, List, Tuple

from playwright.async_api import Page
from loguru import logger

from config import Config
from browser.fast_executor import FastExecutor
from utils.telegram import TelegramAlerter


class BetResult(Enum):
    SUCCESS = auto()
    FAILED = auto()
    SWITCHED = auto()
    ABORTED_SAFETY = auto()


class MatchWatch:
    def __init__(
        self,
        page: Page,
        home_team: str,
        away_team: str,
        home_score: int = 0,
        away_score: int = 0,
    ):
        self.page = page
        self.home_team = home_team
        self.away_team = away_team
        # Score from SportyBet REST once — then tracked in memory only
        self.home_score = int(home_score)
        self.away_score = int(away_score)
        self.score_initialized = True

        self.odds_by_team: Dict[str, float] = {}
        self.stake_by_team: Dict[str, float] = {}
        self.active_market: str = ""
        self.armed_side: str = ""
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.last_refresh_at: Optional[float] = None
        self.last_market_check_at: float = 0.0
        self.last_no_market_log_at: float = 0.0

    @property
    def total_goals(self) -> int:
        return self.home_score + self.away_score

    @property
    def next_goal_number(self) -> int:
        return self.total_goals + 1


class BetExecutor:
    SUCCESS_PATTERNS = (
        "Submission Successful",
        "Bet Successful",
    )

    MARKET_REVALIDATE_INTERVAL = 14.0
    NO_MARKET_LOG_INTERVAL = 12.0

    def __init__(
        self,
        alerter: TelegramAlerter,
        account_manager,
        sportybet_matches_ref: dict,
        learning=None,
    ):
        self.alerter = alerter
        self.account_manager = account_manager
        self.sportybet_matches = sportybet_matches_ref
        self.learning = learning

        self.balance: Optional[float] = 0.0
        self.consecutive_slow = 0
        self.stopped = False
        self.fast = FastExecutor()
        self.current_account = None
        self.last_odds_used = 0.0

        self.validation_bet_placed = False
        self.validation_passed = False

        self.race_wins = 0
        self.race_losses = 0
        self.race_gaps_ms: List[float] = []
        self.watches: Dict[str, MatchWatch] = {}
        self._odds_interval = float(getattr(Config, "ODDS_TRACK_INTERVAL", 1.5))
        self.max_rebet = int(getattr(Config, "MAX_STACK_PER_GOAL", 3))
        self.rebet_delay = float(getattr(Config, "MIN_STACK_DELAY", 0.8))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def calc_stake(self, odds: float) -> float:
        if odds <= 1.0 or not self.balance or self.balance <= 0:
            return 0.0
        target = float(Config.MAX_PROFIT_PER_BET) / (odds - 1.0)
        if self.balance < target:
            return float(self.balance)
        return float(target)

    async def _safe_closed(self, page: Page) -> bool:
        try:
            return page is None or page.is_closed()
        except Exception:
            return True

    async def _dismiss_popups(self, page: Page) -> None:
        for sel in (
            'button:has-text("Accept")',
            'button:has-text("OK")',
            'button:has-text("Got it")',
            'button:has-text("Close")',
            'button:has-text("I understand")',
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click(timeout=400)
            except Exception:
                pass

    def _parse_score_from_match(self, match: dict) -> Tuple[int, int]:
        """One-time score from SportyBet REST match object (no page, no re-pull)."""
        for hk, ak in (
            ("home_score", "away_score"),
            ("homeScore", "awayScore"),
            ("score_home", "score_away"),
        ):
            if hk in match and ak in match:
                try:
                    return int(match[hk]), int(match[ak])
                except Exception:
                    pass

        score = match.get("score") or match.get("current_score")
        if isinstance(score, (list, tuple)) and len(score) >= 2:
            try:
                return int(score[0]), int(score[1])
            except Exception:
                pass
        if isinstance(score, str):
            m = re.search(r"(\d+)\s*[:\-]\s*(\d+)", score)
            if m:
                return int(m.group(1)), int(m.group(2))

        return 0, 0

    # ------------------------------------------------------------------
    # Page ready / All tab / markets
    # ------------------------------------------------------------------

    async def wait_match_details_ready(
        self, page: Page, home: str = "", away: str = "", timeout_s: float = 18.0
    ) -> bool:
        if await self._safe_closed(page):
            return False

        deadline = time.time() + timeout_s
        home_tok = (home.split()[0] if home else "").strip()
        away_tok = (away.split()[0] if away else "").strip()

        while time.time() < deadline:
            try:
                await self._dismiss_popups(page)
                url = (page.url or "").lower()
                all_tab = page.get_by_text(re.compile(r"^\s*All\s*$"))
                main_tab = page.get_by_text(re.compile(r"^\s*Main\s*$"))
                has_tabs = (await all_tab.count()) > 0 or (await main_tab.count()) > 0

                has_teams = True
                if home_tok and away_tok:
                    h = page.get_by_text(home_tok, exact=False)
                    a = page.get_by_text(away_tok, exact=False)
                    has_teams = (await h.count()) > 0 and (await a.count()) > 0

                betslip = page.get_by_text(re.compile(r"Betslip", re.I))
                has_betslip = (await betslip.count()) > 0
                on_list = "live_list" in url

                if has_tabs and has_teams and not on_list:
                    return True
                if has_tabs and has_betslip and not on_list:
                    return True
            except Exception as e:
                logger.debug(f"[PAGE_READY_CHECK] {e}")
            await asyncio.sleep(0.3)

        try:
            logger.warning(f"[PAGE_READY_CHECK] timeout url={page.url!r}")
        except Exception:
            pass
        return False

    async def open_all_tab(self, page: Page) -> bool:
        if await self._safe_closed(page):
            return False
        for loc in (
            page.get_by_role("tab", name=re.compile(r"^\s*All\s*$", re.I)),
            page.get_by_text(re.compile(r"^\s*All\s*$")),
            page.locator('[role="tab"]').filter(
                has_text=re.compile(r"^\s*All\s*$", re.I)
            ),
        ):
            try:
                if await loc.count() > 0:
                    await loc.first.click(timeout=1500)
                    await page.wait_for_timeout(300)
                    return True
            except Exception:
                continue
        return False

    async def _scroll_markets_panel(self, page: Page, amount: int = 400) -> None:
        try:
            scrolled = await page.evaluate(
                """(amount) => {
                    const nodes = Array.from(document.querySelectorAll(
                        'div, section, main, [class*="market"], [class*="Market"],' +
                        ' [class*="scroll"], [class*="Scroll"], [class*="content"]'
                    ));
                    let best = null, bestOverflow = 0;
                    for (const el of nodes) {
                        const sh = el.scrollHeight || 0;
                        const ch = el.clientHeight || 0;
                        if (sh > ch + 100) {
                            const overflow = sh - ch;
                            const rect = el.getBoundingClientRect();
                            if (rect.width < 220) continue;
                            if (overflow > bestOverflow) {
                                bestOverflow = overflow;
                                best = el;
                            }
                        }
                    }
                    if (best) {
                        best.scrollTop = Math.min(
                            best.scrollTop + amount, best.scrollHeight
                        );
                        return true;
                    }
                    window.scrollBy(0, amount);
                    return false;
                }""",
                amount,
            )
            if not scrolled:
                try:
                    await page.mouse.wheel(0, amount)
                except Exception:
                    pass
        except Exception:
            try:
                await page.mouse.wheel(0, amount)
            except Exception:
                pass

    async def prepare_goal_market(
        self, page: Page, watch: Optional[MatchWatch] = None
    ) -> str:
        """Find goal market on All tab. Prefer market matching next goal number."""
        if await self._safe_closed(page):
            return ""

        now = time.time()
        next_n = watch.next_goal_number if watch else None

        if watch is not None and watch.active_market:
            if (now - watch.last_market_check_at) < self.MARKET_REVALIDATE_INTERVAL:
                return watch.active_market
            watch.last_market_check_at = now
            try:
                still = page.get_by_text(watch.active_market, exact=False)
                if await still.count() > 0:
                    return watch.active_market
                logger.debug(f"[MARKET] Cached '{watch.active_market}' gone — rescan")
                watch.active_market = ""
            except Exception:
                return watch.active_market

        await self.wait_match_details_ready(page, timeout_s=4.0)
        await self.open_all_tab(page)

        # Prefer 1st-half next goal, then open-play next goal, then any Nth goal
        patterns = []
        if next_n:
            patterns.extend(
                [
                    re.compile(
                        rf"1st\s*Half\s*[-–]?\s*{next_n}(?:st|nd|rd|th)?\s*Goal",
                        re.I,
                    ),
                    re.compile(
                        rf"First\s*Half\s*[-–]?\s*{next_n}(?:st|nd|rd|th)?\s*Goal",
                        re.I,
                    ),
                    re.compile(rf"{next_n}(?:st|nd|rd|th)\s*Goal", re.I),
                ]
            )
        patterns.extend(
            [
                re.compile(r"1st\s*Half\s*[-–]?\s*\d*(?:st|nd|rd|th)?\s*Goal", re.I),
                re.compile(r"First\s*Half\s*[-–]?\s*\d*(?:st|nd|rd|th)?\s*Goal", re.I),
                re.compile(r"\d+(?:st|nd|rd|th)\s*Goal", re.I),
                re.compile(r"Next\s*Goal", re.I),
            ]
        )

        async def find_on_view() -> str:
            for pat in patterns:
                loc = page.get_by_text(pat)
                try:
                    n = await loc.count()
                    for i in range(min(n, 16)):
                        el = loc.nth(i)
                        raw = ((await el.text_content()) or "").strip()
                        label = re.sub(r"\s+", " ", raw.split("\n")[0]).strip()
                        if not label or len(label) > 42:
                            continue
                        low = label.lower()
                        if any(
                            x in low
                            for x in (
                                "over",
                                "under",
                                "corner",
                                "handicap",
                                "double",
                                "season",
                                "scored",
                                "booking",
                                "card",
                            )
                        ):
                            continue
                        if "goal" not in low:
                            continue
                        try:
                            await el.scroll_into_view_if_needed(timeout=800)
                        except Exception:
                            pass
                        await el.click(timeout=1200)
                        await page.wait_for_timeout(180)
                        if watch is not None:
                            watch.active_market = label
                            watch.last_market_check_at = time.time()
                        logger.info(
                            f"[MARKET] {label} (tracked score "
                            f"{watch.home_score if watch else '?'}-"
                            f"{watch.away_score if watch else '?'} "
                            f"next={next_n})"
                        )
                        return label
                except Exception:
                    continue
            return ""

        for step in range(10):
            found = await find_on_view()
            if found:
                return found
            await self._scroll_markets_panel(page, amount=360 + step * 50)
            await page.wait_for_timeout(180)

        if watch is not None:
            if (now - watch.last_no_market_log_at) > self.NO_MARKET_LOG_INTERVAL:
                logger.debug(
                    f"[MARKET] No goal market (next={next_n}) on All tab after scroll"
                )
                watch.last_no_market_log_at = now
        return ""

    # ------------------------------------------------------------------
    # Memory score safety (NO page score, NO re-pull)
    # ------------------------------------------------------------------

    def _memory_score_ok(
        self,
        watch: MatchWatch,
        expected_home: int,
        expected_away: int,
        scoring_team: str,
        match_name: str,
    ) -> bool:
        """
        tracked = last known SportyBet score (REST once, then memory).
        expected = Polymarket score AFTER the goal.

        Allow only a single +1 goal from tracked → expected on the correct side.
        """
        th, ta = watch.home_score, watch.away_score
        eh, ea = int(expected_home), int(expected_away)

        logger.info(
            f"[SCORE_MEM] {match_name} tracked={th}-{ta} "
            f"poly_expected={eh}-{ea} next_goal={watch.next_goal_number}"
        )

        # Polymarket still at or behind tracked → nothing new / stale
        if eh + ea <= th + ta:
            logger.warning(
                f"[SAFETY] ABORT {match_name} poly {eh}-{ea} not ahead of "
                f"tracked {th}-{ta}"
            )
            return False

        # Must be exactly +1 total goal
        if (eh + ea) != (th + ta + 1):
            logger.warning(
                f"[SAFETY] ABORT {match_name} poly jumped {th}-{ta} → {eh}-{ea} "
                f"(want exactly +1)"
            )
            return False

        # Side must match: home left / away right
        home_scored = eh == th + 1 and ea == ta
        away_scored = ea == ta + 1 and eh == th

        if home_scored:
            if scoring_team and scoring_team != watch.home_team:
                # team string might be short name — allow if not contradictory
                if scoring_team == watch.away_team:
                    logger.warning(
                        f"[SAFETY] ABORT {match_name} poly home goal but "
                        f"team={scoring_team} is away"
                    )
                    return False
            logger.info(
                f"[SAFETY] ALLOW {match_name} HOME goal "
                f"{th}-{ta} → {eh}-{ea} (memory race)"
            )
            return True

        if away_scored:
            if scoring_team and scoring_team == watch.home_team:
                logger.warning(
                    f"[SAFETY] ABORT {match_name} poly away goal but "
                    f"team={scoring_team} is home"
                )
                return False
            logger.info(
                f"[SAFETY] ALLOW {match_name} AWAY goal "
                f"{th}-{ta} → {eh}-{ea} (memory race)"
            )
            return True

        logger.warning(
            f"[SAFETY] ABORT {match_name} invalid transition "
            f"{th}-{ta} → {eh}-{ea}"
        )
        return False

    def advance_tracked_score(self, match_id: str, home: int, away: int) -> None:
        """Call after a goal is accepted / bet placed so memory stays in sync."""
        watch = self.watches.get(match_id)
        if not watch:
            return
        watch.home_score = int(home)
        watch.away_score = int(away)
        watch.active_market = ""  # force market rescan for next goal number
        logger.info(
            f"[SCORE_MEM] advanced {watch.home_team} vs {watch.away_team} "
            f"→ {home}-{away} (next={watch.next_goal_number})"
        )

    async def _verify_match_identity(
        self, page: Page, expected_home: str, expected_away: str
    ) -> bool:
        try:
            ht = expected_home.split()[0] if expected_home else ""
            at = expected_away.split()[0] if expected_away else ""
            if not ht or not at:
                return False
            h = page.get_by_text(ht, exact=False)
            a = page.get_by_text(at, exact=False)
            return (await h.count()) > 0 and (await a.count()) > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Odds / balance / stake / place / confirm
    # ------------------------------------------------------------------

    async def _read_odds_from_click(self, page: Page, selection_text: str) -> float:
        if not selection_text:
            return 0.0
        for loc in (
            page.get_by_role(
                "button",
                name=re.compile(rf"^\s*{re.escape(selection_text)}\s*$", re.I),
            ),
            page.get_by_role("button", name=selection_text, exact=False),
            page.locator('[class*="selection"]').filter(has_text=selection_text),
            page.get_by_text(selection_text, exact=False),
        ):
            try:
                if await loc.count() == 0:
                    continue
                n = min(await loc.count(), 8)
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if not await el.is_visible():
                            continue
                    except Exception:
                        pass
                    txt = (await el.text_content(timeout=400)) or ""
                    m = re.search(r"(\d+\.\d{1,3})", txt.replace(",", "."))
                    if m:
                        odds = float(m.group(1))
                        if 1.01 <= odds <= 500:
                            return odds
            except Exception:
                continue
        return 0.0

    async def fetch_balance(self, page: Page) -> Optional[float]:
        selectors = [
            ".user-balance",
            ".account-balance",
            '[data-testid="balance"]',
            '[class*="balance"]',
            'span:has-text("₦")',
            'span:has-text("NGN")',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() == 0:
                    continue
                text = await loc.first.text_content(timeout=600)
                if not text:
                    continue
                m = re.search(
                    r"([\d.,]+)",
                    text.replace("₦", "").replace("NGN", "").replace(",", ""),
                )
                if m:
                    bal = float(m.group(1).replace(",", ""))
                    self.balance = bal
                    if self.current_account:
                        try:
                            self.account_manager.update_balance(
                                self.current_account["username"], bal
                            )
                        except Exception:
                            pass
                    return bal
            except Exception:
                continue
        return None

    async def _dismiss_slip(self, page: Page):
        for name in ("Cancel", "Close"):
            btn = page.get_by_role("button", name=re.compile(name, re.I))
            try:
                if await btn.count() > 0:
                    await btn.first.click(timeout=400)
            except Exception:
                pass
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    async def _set_stake_input(self, page: Page, stake: float) -> bool:
        box = page.locator(
            'input[type="number"], input[type="tel"], input[inputmode="numeric"], '
            'input[placeholder*="Stake" i], input[placeholder*="stake" i], '
            'input[placeholder*="Amount" i], input[placeholder*="Total" i]'
        )
        try:
            if await box.count() > 0:
                el = box.first
                await el.click(timeout=600)
                await el.fill("")
                await el.fill(str(int(stake)))
                return True
        except Exception:
            pass
        try:
            return bool(
                await self.fast.fast_type(
                    page, "input", str(int(stake)), human_delay=False
                )
            )
        except Exception:
            return False

    async def _click_place_bet(self, page: Page) -> bool:
        btn = page.get_by_role("button", name=re.compile(r"^\s*Place\s*Bet\s*$", re.I))
        try:
            if await btn.count() > 0:
                await btn.first.click(timeout=1200)
                return True
        except Exception:
            pass
        try:
            return bool(
                await self.fast.fast_click(
                    page, 'button:has-text("Place Bet")', timeout=800, human_delay=False
                )
            )
        except Exception:
            return False

    async def _click_confirm_dialog(self, page: Page) -> bool:
        btn = page.get_by_role("button", name=re.compile(r"^\s*Confirm\s*$", re.I))
        try:
            if await btn.count() > 0:
                await btn.first.click(timeout=1200)
                return True
        except Exception:
            pass
        try:
            return bool(
                await self.fast.fast_click(
                    page, 'button:has-text("Confirm")', timeout=800, human_delay=False
                )
            )
        except Exception:
            return False

    async def _confirm_visible(self, page: Page) -> bool:
        btn = page.get_by_role("button", name=re.compile(r"^\s*Confirm\s*$", re.I))
        try:
            return (await btn.count()) > 0 and await btn.first.is_visible()
        except Exception:
            return False

    async def _wait_bet_success(self, page: Page, timeout_ms: int = 8000) -> bool:
        for text in self.SUCCESS_PATTERNS:
            try:
                await page.wait_for_selector(f"text={text}", timeout=timeout_ms // 2)
                return True
            except Exception:
                continue
        return False

    async def _arm_side(self, page: Page, side_label: str, stake: float) -> bool:
        """
        side_label is 'Home' or 'Away'.
        SportyBet: Home = left selection, Away = right selection.
        """
        try:
            btn = page.get_by_role(
                "button", name=re.compile(rf"^\s*{re.escape(side_label)}\s*", re.I)
            )
            if await btn.count() == 0:
                btn = page.get_by_text(
                    re.compile(rf"^\s*{re.escape(side_label)}\s*", re.I)
                )
            if await btn.count() == 0:
                return False
            await btn.first.click(force=True, timeout=1200)
            await page.wait_for_timeout(150)
            if not await self._set_stake_input(page, stake):
                return False
            if not await self._click_place_bet(page):
                return False
            await page.wait_for_timeout(200)
            return await self._confirm_visible(page)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Watch loop
    # ------------------------------------------------------------------

    def start_watching(
        self,
        match_id: str,
        page: Page,
        home_team: str,
        away_team: str,
        home_score: int = 0,
        away_score: int = 0,
        match: Optional[dict] = None,
    ):
        if match_id in self.watches:
            return

        if match is not None:
            hs, aws = self._parse_score_from_match(match)
            home_score, away_score = hs, aws

        watch = MatchWatch(
            page, home_team, away_team, home_score=home_score, away_score=away_score
        )
        watch.running = True
        watch.task = asyncio.create_task(self._watch_loop(match_id, watch))
        self.watches[match_id] = watch
        logger.info(
            f"[WATCH] started {home_team} vs {away_team} "
            f"REST score={home_score}-{away_score} next_goal={watch.next_goal_number}"
        )

    def stop_watching(self, match_id: str):
        watch = self.watches.pop(match_id, None)
        if not watch:
            return
        watch.running = False
        if watch.task:
            watch.task.cancel()
        watch.odds_by_team.clear()
        watch.stake_by_team.clear()

    def get_watch(self, match_id: str) -> Optional[MatchWatch]:
        return self.watches.get(match_id)

    async def _watch_loop(self, match_id: str, watch: MatchWatch):
        try:
            while watch.running:
                try:
                    if await self._safe_closed(watch.page):
                        break

                    await self.prepare_goal_market(watch.page, watch)

                    if watch.last_refresh_at is None or (
                        time.time() - watch.last_refresh_at
                    ) > 8:
                        await self.fetch_balance(watch.page)

                    home_odds = await self._read_odds_from_click(watch.page, "Home")
                    away_odds = await self._read_odds_from_click(watch.page, "Away")
                    changed = False
                    for team, odds in (
                        (watch.home_team, home_odds),
                        (watch.away_team, away_odds),
                    ):
                        if odds <= 0:
                            continue
                        old = watch.odds_by_team.get(team)
                        watch.odds_by_team[team] = odds
                        watch.stake_by_team[team] = self.calc_stake(odds)
                        if old is None or abs(old - odds) >= 0.01:
                            changed = True

                    if changed and watch.armed_side:
                        await self._dismiss_slip(watch.page)
                        side = watch.armed_side
                        team = (
                            watch.home_team if side == "Home" else watch.away_team
                        )
                        stake = watch.stake_by_team.get(team, 0.0)
                        if stake > 0:
                            ok = await self._arm_side(watch.page, side, stake)
                            if not ok:
                                watch.armed_side = ""

                    # Pre-arm Home (left) by default so Confirm is ready
                    if not watch.armed_side:
                        for side, team in (
                            ("Home", watch.home_team),
                            ("Away", watch.away_team),
                        ):
                            stake = watch.stake_by_team.get(team, 0.0)
                            if stake >= 10:
                                if await self._arm_side(watch.page, side, stake):
                                    watch.armed_side = side
                                    logger.debug(
                                        f"[WATCH] pre-armed {side} stake={stake:.0f}"
                                    )
                                    break

                    watch.last_refresh_at = time.time()
                except Exception as e:
                    logger.debug(f"[WATCH] {e}")
                await asyncio.sleep(self._odds_interval)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Rebet / race / execute
    # ------------------------------------------------------------------

    async def _try_rebet_once(self, page: Page) -> bool:
        rebet = page.get_by_role("button", name=re.compile(r"^\s*Rebet\s*$", re.I))
        if await rebet.count() == 0:
            rebet = page.get_by_text(re.compile(r"^\s*Rebet\s*$", re.I))
        if await rebet.count() == 0:
            return False
        await rebet.first.click(timeout=1500)
        await page.wait_for_timeout(350)
        await self.fetch_balance(page)
        stake = self.calc_stake(self.last_odds_used or 0.0)
        if stake < 10:
            return False
        if not await self._set_stake_input(page, stake):
            return False
        if not await self._click_place_bet(page):
            return False
        if not await self._click_confirm_dialog(page):
            return False
        if await self._wait_bet_success(
            page, timeout_ms=getattr(Config, "CONFIRMATION_TIMEOUT_MS", 8000)
        ):
            if self.balance:
                self.balance = max(0.0, self.balance - stake)
            return True
        return False

    async def _rebet_loop(
        self, page: Page, match_name: str, team: str, goal_num: int
    ) -> int:
        extra = 0
        for i in range(1, self.max_rebet):
            await asyncio.sleep(self.rebet_delay)
            ok = await self._try_rebet_once(page)
            if not ok:
                break
            extra += 1
            await self.fetch_balance(page)
            bet_id = f"rebet_{goal_num}_{i}_{uuid.uuid4().hex[:6]}"
            stake = self.calc_stake(self.last_odds_used or 0.0)
            await self.alerter.notify_bet_placed(
                match_name,
                team,
                stake,
                self.last_odds_used,
                bet_id,
                i + 1,
                self.max_rebet,
            )
            logger.success(f"[REBET] #{i + 1} on {match_name}")
        return extra

    def _record_race_result(self, detected_at, won, match_name, reason):
        if detected_at is None:
            return
        gap = (time.time() - detected_at) * 1000.0
        self.race_gaps_ms.append(gap)
        if len(self.race_gaps_ms) > 200:
            self.race_gaps_ms.pop(0)
        if won:
            self.race_wins += 1
        else:
            self.race_losses += 1
        logger.info(
            f"[RACE] {match_name} | {'WIN' if won else 'LOSS'} ({reason}) | gap={gap:.0f}ms"
        )

    async def execute(
        self,
        page: Page,
        match: dict,
        team: str,
        goal_num: int = 1,
        expected_home_score: Optional[int] = None,
        expected_away_score: Optional[int] = None,
        detected_at: Optional[float] = None,
    ) -> BetResult:
        if self.stopped or await self._safe_closed(page):
            return BetResult.FAILED

        match_id = str(match.get("match_id") or "")
        home_team = match.get("home_team", "")
        away_team = match.get("away_team", "")
        match_name = f"{home_team} vs {away_team}"

        watch = self.get_watch(match_id)
        if watch is None:
            # Late init from REST match object once
            hs, aws = self._parse_score_from_match(match)
            self.start_watching(
                match_id, page, home_team, away_team, hs, aws, match=match
            )
            watch = self.get_watch(match_id)

        if not await self._verify_match_identity(page, home_team, away_team):
            self._record_race_result(detected_at, False, match_name, "identity")
            return BetResult.ABORTED_SAFETY

        # Memory safety only — no Playwright score, no REST re-pull
        if (
            watch
            and expected_home_score is not None
            and expected_away_score is not None
        ):
            if not self._memory_score_ok(
                watch,
                expected_home_score,
                expected_away_score,
                team,
                match_name,
            ):
                self._record_race_result(detected_at, False, match_name, "score_moved")
                return BetResult.ABORTED_SAFETY

        self._record_race_result(detected_at, True, match_name, "confirmed")

        # Home = left, Away = right on SportyBet goal markets
        if team == home_team:
            side = "Home"
        elif team == away_team:
            side = "Away"
        else:
            # fallback: prefer home/away token match
            tl = (team or "").lower()
            if home_team and home_team.split()[0].lower() in tl:
                side = "Home"
            elif away_team and away_team.split()[0].lower() in tl:
                side = "Away"
            else:
                logger.error(f"[SIDE] cannot map team={team!r} to Home/Away")
                return BetResult.FAILED

        await self.prepare_goal_market(page, watch)

        bal = await self.fetch_balance(page)
        if bal is None and (not self.balance or self.balance <= 0):
            logger.error("[BALANCE] cannot read balance")
            return BetResult.FAILED

        odds = 0.0
        if watch and team in watch.odds_by_team:
            odds = watch.odds_by_team[team]
        if odds <= 0:
            odds = await self._read_odds_from_click(page, side)
        if odds <= 0:
            logger.error(f"[ODDS] none for side={side} team={team}")
            return BetResult.FAILED

        self.last_odds_used = odds
        stake = self.calc_stake(odds)
        if stake < 10:
            return BetResult.FAILED

        if not self.validation_bet_placed:
            self.validation_bet_placed = True
            ok = await self.run_validation_bet(
                page, match, team, odds, goal_num, match_name, side=side
            )
            if ok:
                self.validation_passed = True
                if expected_home_score is not None and expected_away_score is not None:
                    self.advance_tracked_score(
                        match_id, expected_home_score, expected_away_score
                    )
                await self._rebet_loop(page, match_name, team, goal_num)
                return BetResult.SUCCESS
            self.stopped = True
            return BetResult.FAILED

        if not self.validation_passed:
            return BetResult.FAILED

        await self.alerter.notify_goal_detected(match_name, team, odds, goal_num)
        ok = await self.place_single_bet(
            page, match, team, stake, odds, 1, goal_num, 1, side=side
        )
        if ok:
            if expected_home_score is not None and expected_away_score is not None:
                self.advance_tracked_score(
                    match_id, expected_home_score, expected_away_score
                )
            await self._rebet_loop(page, match_name, team, goal_num)
            return BetResult.SUCCESS
        return BetResult.FAILED

    async def run_validation_bet(
        self, page, match, team, odds, goal_num, match_name, side: str = "Home"
    ) -> bool:
        await self.fetch_balance(page)
        stake = min(
            float(Config.VALIDATION_STAKE),
            float(self.balance or Config.VALIDATION_STAKE),
        )
        await self.alerter.notify_validation_start(
            match_name, team, odds, goal_num, stake
        )
        if self.balance is None:
            await self.alerter.notify_validation_step("Balance read", "FAILED")
            return False
        await self.alerter.notify_validation_step(
            "Balance read", f"₦{self.balance:,.2f}"
        )
        try:
            ok = await self.place_single_bet(
                page, match, team, stake, odds, 1, goal_num, 1, side=side
            )
        except Exception as e:
            logger.exception(e)
            ok = False
        await self.alerter.notify_validation_result(
            ok, match_name, team, stake, odds
        )
        return ok

    async def place_single_bet(
        self,
        page,
        match,
        team,
        stake,
        odds,
        stack_num,
        goal_num,
        total_stack=1,
        skip_market_selection: bool = False,
        side: str = "Home",
    ) -> bool:
        try:
            if await self._safe_closed(page):
                return False

            await self.fetch_balance(page)
            stake = self.calc_stake(odds)
            if self.balance and stake > self.balance:
                stake = float(self.balance)
            if stake < 10:
                return False

            start = time.time()
            match_name = f"{match.get('home_team')} vs {match.get('away_team')}"
            try:
                await page.bring_to_front()
            except Exception:
                pass

            watch = self.get_watch(str(match.get("match_id") or ""))
            already_armed = bool(watch and watch.armed_side == side)

            if not skip_market_selection and not already_armed:
                await self.prepare_goal_market(page, watch)
                if not await self._arm_side(page, side, stake):
                    logger.warning(f"[SELECT] could not arm side={side}")
                    return False
                if watch:
                    watch.armed_side = side

            # Fast path: Confirm only
            if not await self._click_confirm_dialog(page):
                if not await self._arm_side(page, side, stake):
                    await self.alerter.notify_submission_slow(
                        match_name, "No confirm dialog open", 0
                    )
                    return False
                if watch:
                    watch.armed_side = side
                if not await self._click_confirm_dialog(page):
                    await self.alerter.notify_submission_slow(
                        match_name, "No confirm dialog open", 0
                    )
                    return False

            if await self._wait_bet_success(
                page, timeout_ms=getattr(Config, "CONFIRMATION_TIMEOUT_MS", 8000)
            ):
                elapsed = time.time() - start
                if elapsed > Config.SUBMISSION_TIMEOUT:
                    await self.alerter.notify_submission_slow(
                        match_name, "slow", elapsed
                    )
                    result = await self.handle_slow(match)
                    return result in (BetResult.SUCCESS, BetResult.SWITCHED)

                self.consecutive_slow = 0
                bet_id = (
                    f"{match.get('match_id')}_G{goal_num}_S{stack_num}_"
                    f"{uuid.uuid4().hex[:8]}"
                )
                await self.alerter.notify_bet_placed(
                    match_name, team, stake, odds, bet_id, stack_num, total_stack
                )
                with open("bets.log", "a", encoding="utf-8") as f:
                    f.write(
                        f"{datetime.now()}: {match_name}|{team}|{side}|₦{stake}|@{odds}|{bet_id}\n"
                    )
                if self.balance:
                    self.balance = max(0.0, self.balance - stake)
                if watch:
                    watch.armed_side = ""
                return True

            elapsed = time.time() - start
            await self.alerter.notify_submission_slow(
                match_name, "success text not found", elapsed
            )
            if elapsed > Config.SUBMISSION_TIMEOUT:
                result = await self.handle_slow(match)
                return result in (BetResult.SUCCESS, BetResult.SWITCHED)
            return False
        except Exception as e:
            logger.exception(e)
            return False

    async def handle_slow(self, match) -> BetResult:
        self.consecutive_slow += 1
        if self.consecutive_slow >= Config.MAX_CONSECUTIVE_SLOW:
            old_user = (
                self.current_account["username"]
                if self.current_account
                else "unknown"
            )
            await self.alerter.notify_account_flagged(old_user)
            old, new = self.account_manager.mark_limited()
            if new:
                await self.alerter.notify_account_switched(old, new["username"])
                self.current_account = new
                self.consecutive_slow = 0
                return BetResult.SWITCHED
            self.stopped = True
            return BetResult.FAILED
        return BetResult.FAILED
