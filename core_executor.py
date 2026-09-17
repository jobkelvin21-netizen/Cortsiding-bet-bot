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
    def __init__(self, page: Page, home_team: str, away_team: str):
        self.page = page
        self.home_team = home_team
        self.away_team = away_team
        self.odds_by_team: Dict[str, float] = {}
        self.stake_by_team: Dict[str, float] = {}
        self.active_market: str = ""
        self.armed_side: str = ""
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.last_refresh_at: Optional[float] = None
        self.last_odds_fingerprint: str = ""


class BetExecutor:
    def __init__(self, alerter: TelegramAlerter, account_manager,
                 sportybet_matches_ref: dict, learning=None):
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

    # ----- stake helper -----
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
                url = (page.url or "").lower()

                all_tab = page.get_by_text(re.compile(r"^\s*All\s*$"))
                goals_tab = page.get_by_text(re.compile(r"^\s*Goals\s*$"))
                main_tab = page.get_by_text(re.compile(r"^\s*Main\s*$"))
                has_tabs = (
                    (await all_tab.count()) > 0
                    or (await goals_tab.count()) > 0
                    or (await main_tab.count()) > 0
                )

                has_teams = True
                if home_tok and away_tok:
                    h = page.get_by_text(home_tok, exact=False)
                    a = page.get_by_text(away_tok, exact=False)
                    has_teams = (await h.count()) > 0 and (await a.count()) > 0

                on_list_only = ("live_list" in url) and not has_tabs

                if has_tabs and has_teams and not on_list_only:
                    return True
                if has_tabs and "live_list" not in url:
                    return True
            except Exception as e:
                logger.debug(f"[PAGE_READY_CHECK] {e}")
            await asyncio.sleep(0.5)

        try:
            logger.warning(
                f"[PAGE_READY_CHECK] timeout url={page.url!r}"
            )
        except Exception:
            pass
        return False

    async def open_all_tab(self, page: Page) -> bool:
        if await self._safe_closed(page):
            return False
        candidates = [
            page.get_by_role("tab", name=re.compile(r"^\s*All\s*$", re.I)),
            page.get_by_text(re.compile(r"^\s*All\s*$")),
            page.locator('[role="tab"]').filter(has_text=re.compile(r"^\s*All\s*$", re.I)),
        ]
        for loc in candidates:
            try:
                if await loc.count() > 0:
                    await loc.first.click(timeout=1500)
                    await page.wait_for_timeout(550)
                    return True
            except Exception:
                continue
        return False

    async def prepare_goal_market(self, page: Page, watch: Optional[MatchWatch] = None) -> str:
        if await self._safe_closed(page):
            return ""
        await self.wait_match_details_ready(page, timeout_s=5.0)
        await self.open_all_tab(page)

        patterns = [
            re.compile(r"1st\s*Half\s*[-–]?\s*\d*(?:st|nd|rd|th)?\s*Goal", re.I),
            re.compile(r"First\s*Half\s*[-–]?\s*\d*(?:st|nd|rd|th)?\s*Goal", re.I),
            re.compile(r"\d+(?:st|nd|rd|th)?\s*Goal", re.I),
            re.compile(r"Next\s*Goal", re.I),
        ]

        async def try_patterns() -> str:
            for pat in patterns:
                loc = page.get_by_text(pat)
                try:
                    n = await loc.count()
                    for i in range(min(n, 10)):
                        label = ((await loc.nth(i).text_content()) or "").strip()
                        if not label or len(label) > 48:
                            continue
                        low = label.lower()
                        if any(x in low for x in ("over", "under", "corner", "handicap", "double")):
                            continue
                        await loc.nth(i).click(timeout=1200)
                        if watch is not None:
                            watch.active_market = label
                        logger.info(f"[MARKET] {label}")
                        return label
                except Exception:
                    continue
            return ""

        found = await try_patterns()
        if found:
            return found

        try:
            goals = page.get_by_role("tab", name=re.compile(r"^\s*Goals\s*$", re.I))
            if await goals.count() == 0:
                goals = page.get_by_text(re.compile(r"^\s*Goals\s*$", re.I))
            if await goals.count() > 0:
                await goals.first.click(timeout=1200)
                await page.wait_for_timeout(500)
                found = await try_patterns()
                if found:
                    return found
        except Exception:
            pass

        logger.debug("[MARKET] No goal market found yet")
        return ""

    async def _read_score_from_page(self, page: Page) -> Optional[Tuple[int, int]]:
        for sel in (
            '[data-testid="match-score"]',
            '[class*="match-score"]',
            '[class*="score-board"]',
            '[class*="scoreboard"]',
        ):
            loc = page.locator(sel)
            try:
                if await loc.count() == 0:
                    continue
                text = (await loc.first.text_content(timeout=700)) or ""
                m = re.search(r"(\d+)\s*[-:]\s*(\d+)", text)
                if m:
                    return int(m.group(1)), int(m.group(2))
            except Exception:
                continue
        try:
            nums = page.locator("div, span").filter(has_text=re.compile(r"^\d{1,2}$"))
            count = min(await nums.count(), 12)
            values: List[int] = []
            for i in range(count):
                t = ((await nums.nth(i).text_content(timeout=250)) or "").strip()
                if re.fullmatch(r"\d{1,2}", t):
                    values.append(int(t))
                if len(values) >= 2:
                    break
            if len(values) >= 2 and values[0] <= 20 and values[1] <= 20:
                return values[0], values[1]
        except Exception:
            pass
        return None

    async def _score_still_matches(
        self, page: Page, expected_home: int, expected_away: int, match_name: str
    ) -> bool:
        current = await self._read_score_from_page(page)
        if current is None:
            logger.warning(f"[SAFETY] {match_name} score unavailable")
            return False
        ch, ca = current
        if ch + ca < expected_home + expected_away:
            logger.info(f"[SAFETY] {match_name} page {ch}-{ca} lag — allow")
            return True
        logger.warning(f"SAFETY ABORT {match_name} page {ch}-{ca}")
        return False

    async def _verify_match_identity(self, page: Page, expected_home: str, expected_away: str) -> bool:
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

    async def _read_odds_from_click(self, page: Page, selection_text: str) -> float:
        if not selection_text:
            return 0.0
        for loc in (
            page.get_by_role("button", name=selection_text, exact=False),
            page.locator('[class*="selection"]').filter(has_text=selection_text),
            page.get_by_text(selection_text, exact=False),
        ):
            try:
                if await loc.count() == 0:
                    continue
                txt = await loc.first.text_content(timeout=700)
                if not txt:
                    continue
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
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() == 0:
                    continue
                text = await loc.first.text_content(timeout=700)
                if not text:
                    continue
                m = re.search(
                    r"([\d.]+)",
                    text.replace("₦", "").replace("NGN", "").replace(",", ""),
                )
                if m:
                    bal = float(m.group(1))
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
                    await btn.first.click(timeout=500)
            except Exception:
                pass
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    async def _set_stake_input(self, page: Page, stake: float) -> bool:
        box = page.locator(
            'input[type="number"], input[type="tel"], input[inputmode="numeric"], '
            'input[placeholder*="Stake" i], input[placeholder*="stake" i]'
        )
        try:
            if await box.count() > 0:
                await box.first.fill(str(int(stake)))
                return True
        except Exception:
            pass
        try:
            return bool(await self.fast.fast_type(page, "input", str(int(stake)), human_delay=False))
        except Exception:
            return False

    async def _click_confirm(self, page: Page) -> bool:
        for name in ("Confirm", "Accept Changes", "Place Bet"):
            btn = page.get_by_role("button", name=re.compile(name, re.I))
            try:
                if await btn.count() > 0:
                    await btn.first.click(timeout=1200)
                    return True
            except Exception:
                continue
        for sel in (
            'button:has-text("Confirm")',
            'button:has-text("Accept Changes")',
            'button:has-text("Place Bet")',
        ):
            try:
                if await self.fast.fast_click(page, sel, timeout=700, human_delay=False):
                    return True
            except Exception:
                continue
        return False

    async def _arm_side(self, page: Page, side_label: str, stake: float) -> bool:
        try:
            btn = page.get_by_role("button", name=side_label, exact=False)
            if await btn.count() == 0:
                btn = page.get_by_text(side_label, exact=False)
            if await btn.count() == 0:
                return False
            await btn.first.click(force=True, timeout=1200)
            await page.wait_for_timeout(250)
            return await self._set_stake_input(page, stake)
        except Exception:
            return False

    def start_watching(self, match_id: str, page: Page, home_team: str, away_team: str):
        if match_id in self.watches:
            return
        watch = MatchWatch(page, home_team, away_team)
        watch.running = True
        watch.task = asyncio.create_task(self._watch_loop(match_id, watch))
        self.watches[match_id] = watch
        logger.debug(f"[WATCH] started {home_team} vs {away_team}")

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
                    if watch.last_refresh_at is None or (time.time() - watch.last_refresh_at) > 8:
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
                        team = watch.home_team if side == "Home" else watch.away_team
                        stake = watch.stake_by_team.get(team, 0.0)
                        if stake > 0:
                            await self._arm_side(watch.page, side, stake)

                    watch.last_refresh_at = time.time()
                except Exception as e:
                    logger.debug(f"[WATCH] {e}")
                await asyncio.sleep(self._odds_interval)
        except asyncio.CancelledError:
            pass

    async def _try_rebet_once(self, page: Page) -> bool:
        rebet = page.get_by_role("button", name=re.compile(r"^\s*Rebet\s*$", re.I))
        if await rebet.count() == 0:
            rebet = page.get_by_text(re.compile(r"^\s*Rebet\s*$", re.I))
        if await rebet.count() == 0:
            return False
        await rebet.first.click(timeout=1500)
        await page.wait_for_timeout(500)
        await self.fetch_balance(page)
        stake = self.calc_stake(self.last_odds_used or 0.0)
        if stake < 10:
            return False
        if not await self._set_stake_input(page, stake):
            return False
        if not await self._click_confirm(page):
            return False
        try:
            await page.wait_for_selector(
                'text=Bet Successful, text=Rebet, text=Success, [class*="success"]',
                timeout=getattr(Config, "CONFIRMATION_TIMEOUT_MS", 8000),
            )
            if self.balance:
                self.balance = max(0.0, self.balance - stake)
            return True
        except Exception:
            return False

    async def _rebet_loop(self, page: Page, match_name: str, team: str, goal_num: int) -> int:
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
                match_name, team, stake, self.last_odds_used, bet_id, i + 1, self.max_rebet
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

        match_id = match.get("match_id")
        match_name = f"{match.get('home_team')} vs {match.get('away_team')}"

        if not await self._verify_match_identity(
            page, match.get("home_team", ""), match.get("away_team", "")
        ):
            self._record_race_result(detected_at, False, match_name, "identity")
            return BetResult.ABORTED_SAFETY

        if expected_home_score is not None and expected_away_score is not None:
            if not await self._score_still_matches(
                page, expected_home_score, expected_away_score, match_name
            ):
                self._record_race_result(detected_at, False, match_name, "score_moved")
                return BetResult.ABORTED_SAFETY

        self._record_race_result(detected_at, True, match_name, "confirmed")
        await self.prepare_goal_market(page, self.get_watch(match_id))

        bal = await self.fetch_balance(page)
        if bal is None and (not self.balance or self.balance <= 0):
            logger.error("[BALANCE] cannot read balance")
            return BetResult.FAILED

        watch = self.get_watch(match_id)
        odds = 0.0
        if watch and team in watch.odds_by_team:
            odds = watch.odds_by_team[team]
        side = "Home" if team == match.get("home_team") else "Away"
        if odds <= 0:
            odds = await self._read_odds_from_click(page, side)
        if odds <= 0:
            odds = await self._read_odds_from_click(page, team)
        if odds <= 0:
            logger.error(f"[ODDS] none for {team}")
            return BetResult.FAILED

        self.last_odds_used = odds
        stake = self.calc_stake(odds)
        if stake < 10:
            return BetResult.FAILED

        if not self.validation_bet_placed:
            self.validation_bet_placed = True
            ok = await self.run_validation_bet(page, match, team, odds, goal_num, match_name)
            if ok:
                self.validation_passed = True
                await self._rebet_loop(page, match_name, team, goal_num)
                return BetResult.SUCCESS
            self.stopped = True
            return BetResult.FAILED

        if not self.validation_passed:
            return BetResult.FAILED

        await self.alerter.notify_goal_detected(match_name, team, odds, goal_num)
        ok = await self.place_single_bet(page, match, team, stake, odds, 1, goal_num, 1)
        if ok:
            await self._rebet_loop(page, match_name, team, goal_num)
            return BetResult.SUCCESS
        return BetResult.FAILED

    async def run_validation_bet(self, page, match, team, odds, goal_num, match_name) -> bool:
        await self.fetch_balance(page)
        stake = min(float(Config.VALIDATION_STAKE), float(self.balance or Config.VALIDATION_STAKE))
        await self.alerter.notify_validation_start(match_name, team, odds, goal_num, stake)
        if self.balance is None:
            await self.alerter.notify_validation_step("Balance read", "FAILED")
            return False
        await self.alerter.notify_validation_step("Balance read", f"₦{self.balance:,.2f}")
        try:
            ok = await self.place_single_bet(page, match, team, stake, odds, 1, goal_num, 1)
        except Exception as e:
            logger.exception(e)
            ok = False
        await self.alerter.notify_validation_result(ok, match_name, team, stake, odds)
        return ok

    async def place_single_bet(
        self, page, match, team, stake, odds, stack_num, goal_num, total_stack=1,
        skip_market_selection: bool = False,
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
            await page.bring_to_front()
            await self.prepare_goal_market(page, self.get_watch(match.get("match_id")))

            side = "Home" if team == match.get("home_team") else "Away"
            if not skip_market_selection:
                if not await self._arm_side(page, side, stake):
                    if not await self._arm_side(page, team, stake):
                        logger.warning(f"[SELECT] could not arm {team}")
                        return False

            watch = self.get_watch(match.get("match_id"))
            if watch:
                watch.armed_side = side

            if not await self._click_confirm(page):
                await self.alerter.notify_submission_slow(match_name, "No confirm", 0)
                return False

            try:
                await page.wait_for_selector(
                    'text=Bet Successful, text=Rebet, text=Success, [class*="success"]',
                    timeout=getattr(Config, "CONFIRMATION_TIMEOUT_MS", 8000),
                )
                elapsed = time.time() - start
                if elapsed > Config.SUBMISSION_TIMEOUT:
                    await self.alerter.notify_submission_slow(match_name, "slow", elapsed)
                    result = await self.handle_slow(match)
                    return result in (BetResult.SUCCESS, BetResult.SWITCHED)

                self.consecutive_slow = 0
                bet_id = f"{match.get('match_id')}_G{goal_num}_S{stack_num}_{uuid.uuid4().hex[:8]}"
                await self.alerter.notify_bet_placed(
                    match_name, team, stake, odds, bet_id, stack_num, total_stack
                )
                with open("bets.log", "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now()}: {match_name}|{team}|₦{stake}|@{odds}|{bet_id}\n")
                if self.balance:
                    self.balance = max(0.0, self.balance - stake)
                return True
            except Exception as e:
                elapsed = time.time() - start
                await self.alerter.notify_submission_slow(match_name, str(e), elapsed)
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
