"""
BetExecutor — Groq arms market/stake/Confirm; Playwright clicks Confirm on WS goal only.
"""

import asyncio
import re
import time
import uuid
from enum import Enum, auto
from datetime import datetime
from typing import Optional, Dict, List, Any

from playwright.async_api import Page
from loguru import logger

from config import Config
from browser.fast_executor import FastExecutor
from utils.telegram import TelegramAlerter
from core.groq_ai import plan_over_market


class BetResult(Enum):
    SUCCESS = auto()
    FAILED = auto()
    SWITCHED = auto()
    ABORTED_SAFETY = auto()


class MatchWatch:
    def __init__(self, page: Page, home_team: str, away_team: str, home_score: int = 0, away_score: int = 0):
        self.page = page
        self.home_team = home_team
        self.away_team = away_team
        self.home_score = int(home_score)
        self.away_score = int(away_score)
        self.odds_over: float = 0.0
        self.stake_over: float = 0.0
        self.active_market: str = ""
        self.target_line: float = 0.5
        self.confirm_armed: bool = False
        self.period: str = "1H"
        self.task: Optional[asyncio.Task] = None
        self.running = False

    @property
    def total_goals(self) -> int:
        return self.home_score + self.away_score

    @property
    def over_line(self) -> float:
        return float(self.total_goals) + 0.5


class BetExecutor:
    SUCCESS_PATTERNS = ("Submission Successful", "Bet Successful")

    def __init__(self, alerter: TelegramAlerter, account_manager, sportybet_matches_ref: dict, learning=None):
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

        self.watches: Dict[str, MatchWatch] = {}
        self.max_rebet = int(getattr(Config, "MAX_STACK_PER_GOAL", 3))
        self.rebet_delay = float(getattr(Config, "MIN_STACK_DELAY", 0.8))
        self._rearm_interval = float(getattr(Config, "ODDS_TRACK_INTERVAL", 2.0))

    # ----- stake -----
    def calc_stake(self, odds: float) -> float:
        if odds <= 1.0 or not self.balance or self.balance <= 0:
            return 0.0
        target = float(Config.MAX_PROFIT_PER_BET) / (odds - 1.0)
        return float(self.balance) if self.balance < target else float(target)

    # ----- page helpers -----
    async def _safe_closed(self, page: Page) -> bool:
        try:
            return page is None or page.is_closed()
        except Exception:
            return True

    async def _page_alive(self, page: Page) -> bool:
        if await self._safe_closed(page):
            return False
        try:
            url = (page.url or "").lower()
            return bool(url) and not url.startswith("about:")
        except Exception:
            return False

    async def wait_match_details_ready(self, page: Page, home: str = "", away: str = "", timeout_s: float = 18.0) -> bool:
        if not await self._page_alive(page):
            return False
        deadline = time.time() + timeout_s
        ht = (home.split()[0] if home else "").strip()
        at = (away.split()[0] if away else "").strip()
        while time.time() < deadline:
            if not await self._page_alive(page):
                return False
            try:
                url = (page.url or "").lower()
                has_all = (await page.get_by_text(re.compile(r"^\s*All\s*$")).count()) > 0
                has_teams = True
                if ht and at:
                    has_teams = (await page.get_by_text(ht, exact=False).count()) > 0 and (
                        await page.get_by_text(at, exact=False).count()
                    ) > 0
                if has_all and has_teams and "live_list" not in url:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.3)
        return False

    async def open_all_tab(self, page: Page) -> bool:
        if not await self._page_alive(page):
            return False
        for loc in (
            page.get_by_role("tab", name=re.compile(r"^\s*All\s*$", re.I)),
            page.get_by_text(re.compile(r"^\s*All\s*$")),
        ):
            try:
                if await loc.count() > 0:
                    await loc.first.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return True
            except Exception:
                continue
        return False

    async def fetch_balance(self, page: Page) -> Optional[float]:
        for sel in (
            '[class*="balance"]',
            '[class*="Balance"]',
            'text=/₦\\s*[\\d,]+/',
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    t = await loc.first.inner_text()
                    m = re.search(r"([\d,]+(?:\.\d+)?)", t.replace(",", ""))
                    if m:
                        bal = float(m.group(1).replace(",", ""))
                        self.balance = bal
                        return bal
            except Exception:
                continue
        return self.balance

    async def _set_stake_input(self, page: Page, stake: float) -> bool:
        box = page.locator(
            'input[type="number"], input[type="tel"], input[inputmode="numeric"], '
            'input[placeholder*="Stake" i], input[placeholder*="Amount" i]'
        )
        try:
            if await box.count() > 0:
                el = box.first
                await el.click(timeout=700)
                await el.fill("")
                await el.fill(str(int(stake)))
                return True
        except Exception:
            pass
        try:
            return bool(await self.fast.fast_type(page, "input", str(int(stake)), human_delay=False))
        except Exception:
            return False

    async def _click_accept_changes(self, page: Page) -> bool:
        for getter in (
            lambda: page.get_by_role("button", name=re.compile(r"Accept\s*Changes", re.I)),
            lambda: page.get_by_text(re.compile(r"Accept\s*Changes", re.I)),
        ):
            try:
                btn = getter()
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click(timeout=1000)
                    await page.wait_for_timeout(150)
                    return True
            except Exception:
                pass
        return False

    async def _click_place_bet(self, page: Page) -> bool:
        btn = page.get_by_role("button", name=re.compile(r"^\s*Place\s*Bet\s*$", re.I))
        try:
            if await btn.count() > 0 and await btn.first.is_visible():
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

    async def _confirm_visible(self, page: Page) -> bool:
        btn = page.get_by_role("button", name=re.compile(r"^\s*Confirm\s*$", re.I))
        try:
            return (await btn.count()) > 0 and await btn.first.is_visible()
        except Exception:
            return False

    async def _click_confirm_dialog(self, page: Page) -> bool:
        btn = page.get_by_role("button", name=re.compile(r"^\s*Confirm\s*$", re.I))
        try:
            if await btn.count() > 0 and await btn.first.is_visible():
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

    async def _wait_bet_success(self, page: Page, timeout_ms: int = 8000) -> bool:
        for text in self.SUCCESS_PATTERNS:
            try:
                await page.wait_for_selector(f"text={text}", timeout=timeout_ms // 2)
                return True
            except Exception:
                continue
        return False

    # =================================================================
    # GROQ arms: market + stake + Place Bet → stay on Confirm
    # =================================================================
    async def ai_arm_from_plan(self, page: Page, plan: Optional[dict] = None, watch: Optional[MatchWatch] = None) -> bool:
        if not await self._page_alive(page):
            return False

        if watch is None:
            # build minimal context from plan only
            home = away = ""
            hs = as_ = 0
            period = ""
        else:
            home, away = watch.home_team, watch.away_team
            hs, as_ = watch.home_score, watch.away_score
            period = watch.period

        if not plan or not plan.get("market_found"):
            try:
                text = await page.inner_text("body")
            except Exception:
                text = ""
            await self.fetch_balance(page)
            plan = plan_over_market(
                text,
                home,
                away,
                hs,
                as_,
                period,
                float(Config.MAX_PROFIT_PER_BET),
                float(self.balance or 0),
            )

        if not plan.get("market_found"):
            logger.warning(f"[AI ARM] market not found: {plan}")
            if watch:
                watch.confirm_armed = False
            return False

        line = plan.get("line")
        line_s = f"{float(line):.1f}" if line is not None else ""
        odds = float(plan.get("odds") or 0)
        stake = float(plan.get("stake") or 0)
        if odds > 1.01:
            stake = self.calc_stake(odds) or stake
        if stake < 10:
            logger.warning("[AI ARM] stake < 10")
            if watch:
                watch.confirm_armed = False
            return False

        try:
            await self.open_all_tab(page)

            # Click Over line Groq chose
            clicked = False
            if line_s:
                try:
                    loc = page.get_by_text(
                        re.compile(rf"^\s*Over\s*{re.escape(line_s)}\s*$", re.I)
                    )
                    if await loc.count() > 0:
                        await loc.first.scroll_into_view_if_needed(timeout=800)
                        await loc.first.click(timeout=2000)
                        clicked = True
                except Exception:
                    pass
            if not clicked:
                try:
                    await page.get_by_text(re.compile(r"^\s*Over\s*$", re.I)).first.click(timeout=1500)
                    clicked = True
                except Exception:
                    pass

            if not await self._set_stake_input(page, stake):
                logger.warning("[AI ARM] stake input failed")
                if watch:
                    watch.confirm_armed = False
                return False

            await self._click_accept_changes(page)
            if not await self._click_place_bet(page):
                await self._click_accept_changes(page)
                if not await self._click_place_bet(page):
                    logger.warning("[AI ARM] Place Bet failed")
                    if watch:
                        watch.confirm_armed = False
                    return False

            await page.wait_for_timeout(300)
            ok = await self._confirm_visible(page)

            if watch:
                watch.confirm_armed = ok
                watch.odds_over = odds
                watch.stake_over = stake
                watch.active_market = plan.get("market_name") or f"Over {line_s}"
                watch.target_line = float(line) if line is not None else watch.over_line

            self.last_odds_used = odds if odds > 1 else self.last_odds_used

            if ok:
                logger.success(
                    f"[AI ARM] ON CONFIRM {watch.active_market if watch else line_s} "
                    f"stake={stake:.0f} odds={odds}"
                )
                try:
                    await self.alerter.send_message(
                        f"🟢 ON CONFIRM (Groq)\n"
                        f"{home} vs {away}\n"
                        f"{plan.get('market_name', line_s)} @ {odds}\n"
                        f"Stake ₦{stake:,.0f}\nWaiting Bet365 WS goal…"
                    )
                except Exception:
                    pass
            else:
                logger.warning("[AI ARM] Confirm not visible")
            return ok
        except Exception as e:
            logger.error(f"[AI ARM] {e}")
            if watch:
                watch.confirm_armed = False
            return False

    # =================================================================
    # PLAYWRIGHT only — WS goal
    # =================================================================
    async def click_confirm_only(self, page: Page) -> bool:
        if not await self._page_alive(page):
            return False
        try:
            await page.bring_to_front()
        except Exception:
            pass

        start = time.time()
        if not await self._confirm_visible(page):
            await self._click_accept_changes(page)

        if not await self._click_confirm_dialog(page):
            logger.error("[CONFIRM] button not found")
            return False

        ok = await self._wait_bet_success(
            page, timeout_ms=getattr(Config, "CONFIRMATION_TIMEOUT_MS", 8000)
        )
        elapsed = time.time() - start
        if ok:
            logger.success(f"[CONFIRM] bet OK in {elapsed*1000:.0f}ms")
            if self.balance and self.last_odds_used > 1:
                self.balance = max(0.0, self.balance - self.calc_stake(self.last_odds_used))
            self.consecutive_slow = 0
            return True

        if elapsed > Config.SUBMISSION_TIMEOUT:
            await self.alerter.notify_submission_slow("match", "slow confirm", elapsed)
        logger.error("[CONFIRM] no success text")
        return False

    # ----- watch: keep Confirm armed via Groq when odds move -----
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
        match_id = str(match_id)
        if match_id in self.watches:
            return

        h, a = home_score, away_score
        if match:
            try:
                h = int(match.get("home_score", h) or h)
                a = int(match.get("away_score", a) or a)
            except Exception:
                pass
        try:
            live = (self.sportybet_matches or {}).get(match_id)
            if live:
                h = int(live.get("home_score", h) or h)
                a = int(live.get("away_score", a) or a)
        except Exception:
            pass

        watch = MatchWatch(page, home_team, away_team, home_score=h, away_score=a)
        watch.running = True
        watch.task = asyncio.create_task(self._watch_loop(match_id, watch))
        self.watches[match_id] = watch
        logger.info(f"[WATCH] {home_team} vs {away_team} score {h}-{a} → Over {watch.over_line}")

    def stop_watching(self, match_id: str):
        match_id = str(match_id)
        watch = self.watches.pop(match_id, None)
        if not watch:
            return
        watch.running = False
        if watch.task:
            watch.task.cancel()

    def get_watch(self, match_id: str) -> Optional[MatchWatch]:
        return self.watches.get(str(match_id))

    def advance_tracked_score(self, match_id: str, home: int, away: int) -> None:
        watch = self.watches.get(str(match_id))
        if not watch:
            return
        watch.home_score = int(home)
        watch.away_score = int(away)
        watch.confirm_armed = False
        watch.active_market = ""
        logger.info(f"[SCORE] → {home}-{away} next Over {watch.over_line}")

    async def _watch_loop(self, match_id: str, watch: MatchWatch):
        try:
            # First arm with Groq
            await self.ai_arm_from_plan(watch.page, None, watch)
            while watch.running:
                try:
                    if not await self._page_alive(watch.page):
                        watch.running = False
                        break
                    await self.fetch_balance(watch.page)
                    confirm_up = await self._confirm_visible(watch.page)
                    if not confirm_up or not watch.confirm_armed:
                        await self.ai_arm_from_plan(watch.page, None, watch)
                except Exception as e:
                    logger.debug(f"[WATCH] {e}")
                await asyncio.sleep(self._rearm_interval)
        except asyncio.CancelledError:
            pass

    # ----- goal path used by main -----
    async def execute(
        self,
        page: Page,
        match: dict,
        team: str = "",
        goal_num: int = 1,
        expected_home_score: Optional[int] = None,
        expected_away_score: Optional[int] = None,
        detected_at: Optional[float] = None,
    ) -> BetResult:
        """Prefer Confirm-only; re-arm with Groq only if Confirm missing."""
        if self.stopped or not await self._page_alive(page):
            return BetResult.FAILED

        match_id = str(match.get("match_id") or "")
        home_team = match.get("home_team", "")
        away_team = match.get("away_team", "")
        match_name = f"{home_team} vs {away_team}"

        watch = self.get_watch(match_id)
        if watch is None:
            self.start_watching(match_id, page, home_team, away_team, match=match)
            watch = self.get_watch(match_id)

        # If already on Confirm → click only
        if await self._confirm_visible(page):
            ok = await self.click_confirm_only(page)
        else:
            await self.ai_arm_from_plan(page, None, watch)
            ok = await self.click_confirm_only(page)

        if not ok:
            return await self.handle_slow(match)

        if not self.validation_bet_placed:
            self.validation_bet_placed = True
            self.validation_passed = True

        if expected_home_score is not None and expected_away_score is not None:
            self.advance_tracked_score(match_id, expected_home_score, expected_away_score)

        stake = self.calc_stake(self.last_odds_used or 1.5)
        bet_id = f"{match_id}_G{goal_num}_{uuid.uuid4().hex[:8]}"
        try:
            await self.alerter.notify_bet_placed(
                match_name,
                f"Over {watch.over_line if watch else '?'}",
                stake,
                self.last_odds_used,
                bet_id,
                1,
                1,
            )
        except Exception:
            pass
        with open("bets.log", "a", encoding="utf-8") as f:
            f.write(
                f"{datetime.now()}: {match_name}|Over|{stake}|@{self.last_odds_used}|{bet_id}\n"
            )

        await self._rebet_loop(page, match_name, f"Over", goal_num)
        return BetResult.SUCCESS

    async def _try_rebet_once(self, page: Page) -> bool:
        rebet = page.get_by_role("button", name=re.compile(r"^\s*Rebet\s*$", re.I))
        if await rebet.count() == 0:
            rebet = page.get_by_text(re.compile(r"^\s*Rebet\s*$", re.I))
        if await rebet.count() == 0:
            return False
        await rebet.first.click(timeout=1500)
        await page.wait_for_timeout(300)
        await self.fetch_balance(page)
        stake = self.calc_stake(self.last_odds_used or 0.0)
        if stake < 10:
            return False
        if not await self._set_stake_input(page, stake):
            return False
        await self._click_accept_changes(page)
        if not await self._click_place_bet(page):
            return False
        if not await self._click_confirm_dialog(page):
            return False
        return await self._wait_bet_success(
            page, timeout_ms=getattr(Config, "CONFIRMATION_TIMEOUT_MS", 8000)
        )

    async def _rebet_loop(self, page: Page, match_name: str, team: str, goal_num: int) -> int:
        extra = 0
        for i in range(1, self.max_rebet):
            await asyncio.sleep(self.rebet_delay)
            if not await self._try_rebet_once(page):
                break
            extra += 1
            logger.success(f"[REBET] #{i + 1} {match_name}")
        return extra

    async def handle_slow(self, match) -> BetResult:
        self.consecutive_slow += 1
        if self.consecutive_slow >= Config.MAX_CONSECUTIVE_SLOW:
            old_user = self.current_account["username"] if self.current_account else "unknown"
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
