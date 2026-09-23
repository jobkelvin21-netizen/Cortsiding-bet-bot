"""
BetExecutor — video flow:
  Click Over (right period) → must appear on betslip → stake → Place Bet → Confirm
  1H: 1st Half Over {goals+0.5} → else Full Time Over
  2H: 2nd Half Over {goals+0.5} → else Full Time Over
  Odds move → Accept Changes → restake → Place → Confirm
  Goal (locked FI) → Confirm only
  Suspended → stop, no next market
  Disallowed → Cashout tab → Cash Out → Confirm
"""

import asyncio
import re
import time
from enum import Enum, auto
from typing import Optional, Dict, Any

from playwright.async_api import Page
from loguru import logger

from config import Config
from utils.telegram import TelegramAlerter

try:
    from core.groq_ai import plan_over_market
except Exception:
    plan_over_market = None


class BetResult(Enum):
    SUCCESS = auto()
    FAILED = auto()
    ABORTED = auto()
    SUSPENDED = auto()


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
        self.flagged_suspended = False

    @property
    def total_goals(self) -> int:
        return self.home_score + self.away_score

    @property
    def over_line(self) -> float:
        return float(self.total_goals) + 0.5


class BetExecutor:
    def __init__(self, alerter: TelegramAlerter, account_manager, accounts, browser=None):
        self.alerter = alerter
        self.account_manager = account_manager
        self.balance = 0.0
        self.current_account = None
        self.validation_bet_placed = True
        self.validation_passed = True
        self.last_odds_used = 0.0
        self._watches: Dict[str, MatchWatch] = {}
        self.max_profit = float(getattr(Config, "MAX_PROFIT_PER_BET", 20000))

    def calc_stake(self, odds: float) -> float:
        if odds <= 1.01:
            return 0.0
        stake = self.max_profit / (odds - 1.0)
        if self.balance > 0:
            stake = min(stake, self.balance)
        return round(max(stake, 10.0), 2)

    async def _tg(self, msg: str):
        try:
            await self.alerter.send(msg)
        except Exception:
            pass

    def get_watch(self, mid: str) -> Optional[MatchWatch]:
        return self._watches.get(mid)

    def start_watching(
        self,
        mid: str,
        page: Page,
        home: str,
        away: str,
        home_score: int = 0,
        away_score: int = 0,
        match: Optional[dict] = None,
    ):
        if mid in self._watches:
            self.stop_watching(mid)
        w = MatchWatch(page, home, away, home_score, away_score)
        if match:
            w.home_score = int(match.get("home_score") or home_score)
            w.away_score = int(match.get("away_score") or away_score)
        self._watches[mid] = w
        w.running = True
        w.task = asyncio.create_task(self._watch_loop(mid, w))

    def stop_watching(self, mid: str):
        w = self._watches.pop(mid, None)
        if not w:
            return
        w.running = False
        if w.task and not w.task.done():
            w.task.cancel()

    async def wait_match_details_ready(
        self, page: Page, home: str, away: str, timeout: float = 15.0
    ) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                body = (await page.inner_text("body")).lower()
                if "over/under" in body or "1x2" in body or "place bet" in body:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.4)
        return False

    async def open_all_tab(self, page: Page):
        for sel in (
            'text="All"',
            '[role="tab"]:has-text("All")',
            'button:has-text("All")',
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                pass

    async def _scroll_markets(self, page: Page, steps: int = 14):
        for _ in range(steps):
            try:
                await page.mouse.wheel(0, 700)
            except Exception:
                try:
                    await page.evaluate("window.scrollBy(0, 700)")
                except Exception:
                    pass
            await asyncio.sleep(0.2)

    async def _detect_period(self, page: Page) -> str:
        try:
            t = (await page.inner_text("body")).lower()
            sample = t[:2000]
            if re.search(r"\b2nd\s*half\b|\bsecond\s*half\b", sample):
                return "2H"
            if re.search(r"\bht\b|\bhalf\s*time\b", sample):
                return "2H"
            m = re.search(r"\b(\d{1,3})\s*'", sample)
            if m and int(m.group(1)) >= 46:
                return "2H"
            if re.search(r"\b1st\s*half\b|\bfirst\s*half\b", sample):
                return "1H"
            return "1H"
        except Exception:
            return "1H"

    async def _betslip_has_selection(self, page: Page, line: float) -> bool:
        line_s = f"{line:g}"
        try:
            for root in (
                '[class*="betslip" i]',
                '[class*="BetSlip" i]',
                '[class*="bet-slip" i]',
                '[class*="m-betslip" i]',
                '#betslip',
                '[data-testid*="betslip" i]',
            ):
                loc = page.locator(root)
                if await loc.count() == 0:
                    continue
                txt = (await loc.first.inner_text(timeout=800)).lower()
                if f"over {line_s}" in txt or (
                    "over" in txt and line_s in txt
                ):
                    return True
            body = (await page.inner_text("body")).lower()
            if "place bet" in body and "over" in body and line_s in body:
                return True
        except Exception:
            pass
        return False

    async def _find_and_click_over(
        self,
        page: Page,
        line: float,
        require_kw: list,
        forbid_kw: list,
        label: str,
    ) -> bool:
        line_s = f"{line:g}"
        await self.open_all_tab(page)
        await self._scroll_markets(page, 14)

        candidates = page.locator(
            f'button:has-text("Over {line_s}"), '
            f'div:has-text("Over {line_s}"), '
            f'span:has-text("Over {line_s}"), '
            f'[class*="odd"]:has-text("Over")'
        )
        n = min(await candidates.count(), 80)
        for i in range(n):
            try:
                el = candidates.nth(i)
                if not await el.is_visible():
                    continue
                ctx = ""
                try:
                    ctx = await el.evaluate(
                        """(e) => {
                            let p = e;
                            for (let k = 0; k < 6 && p; k++) p = p.parentElement;
                            return (p && p.innerText) ? p.innerText.slice(0, 700).toLowerCase() : '';
                        }"""
                    ) or ""
                except Exception:
                    ctx = (await el.inner_text()).lower()

                if require_kw and not any(k in ctx for k in require_kw):
                    continue
                if forbid_kw and any(k in ctx for k in forbid_kw):
                    continue
                if f"over {line_s}" not in ctx and line_s not in ctx:
                    continue

                await el.scroll_into_view_if_needed()
                await asyncio.sleep(0.15)
                await el.click(timeout=1500)
                await asyncio.sleep(0.7)
                if await self._betslip_has_selection(page, line):
                    logger.success(f"[ARM] {label} Over {line_s} → on betslip")
                    return True
                await el.click(timeout=1000)
                await asyncio.sleep(0.6)
                if await self._betslip_has_selection(page, line):
                    logger.success(f"[ARM] {label} Over {line_s} → on betslip (retry)")
                    return True
            except Exception:
                continue

        blocks = page.locator("div, section, li, tr")
        n = min(await blocks.count(), 500)
        for i in range(n):
            try:
                el = blocks.nth(i)
                txt = (await el.inner_text(timeout=100)).strip().lower()
                if not txt or len(txt) > 280:
                    continue
                if f"over {line_s}" not in txt:
                    continue
                if require_kw and not any(k in txt for k in require_kw):
                    continue
                if forbid_kw and any(k in txt for k in forbid_kw):
                    continue
                over_btn = el.locator(
                    f'button:has-text("Over"), [class*="odd"]:has-text("Over"), '
                    f'text=/Over\\s*{re.escape(line_s)}/i'
                )
                target = over_btn.first if await over_btn.count() > 0 else el
                await target.scroll_into_view_if_needed()
                await target.click(timeout=1500)
                await asyncio.sleep(0.7)
                if await self._betslip_has_selection(page, line):
                    logger.success(f"[ARM] {label} Over {line_s} → on betslip")
                    return True
            except Exception:
                continue
        return False

    async def _click_correct_over(self, page: Page, watch: MatchWatch) -> bool:
        line = watch.over_line
        period = watch.period

        if period == "1H":
            ok = await self._find_and_click_over(
                page, line,
                require_kw=["1st half", "first half", "1h -", "1h–", "1h "],
                forbid_kw=["2nd half", "second half"],
                label="1st Half",
            )
            if ok:
                watch.active_market = f"1st Half Over {line:g}"
                watch.target_line = line
                return True
            ok = await self._find_and_click_over(
                page, line,
                require_kw=["over/under", "over under", "total"],
                forbid_kw=["1st half", "first half", "2nd half", "second half"],
                label="Full Time",
            )
            if ok:
                watch.active_market = f"Full Time Over {line:g}"
                watch.target_line = line
                return True
            return False

        ok = await self._find_and_click_over(
            page, line,
            require_kw=["2nd half", "second half"],
            forbid_kw=["1st half", "first half"],
            label="2nd Half",
        )
        if ok:
            watch.active_market = f"2nd Half Over {line:g}"
            watch.target_line = line
            return True
        ok = await self._find_and_click_over(
            page, line,
            require_kw=["over/under", "over under", "total"],
            forbid_kw=["1st half", "first half", "2nd half", "second half"],
            label="Full Time",
        )
        if ok:
            watch.active_market = f"Full Time Over {line:g}"
            watch.target_line = line
            return True
        return False

    async def _read_slip_odds(self, page: Page) -> float:
        try:
            for root in (
                '[class*="betslip" i]',
                '[class*="BetSlip" i]',
                '[class*="m-betslip" i]',
            ):
                loc = page.locator(root)
                if await loc.count() > 0:
                    txt = await loc.first.inner_text()
                    for x in re.findall(r"\b(\d+\.\d{2})\b", txt):
                        v = float(x)
                        if 1.01 < v < 50:
                            return v
            txt = await page.locator("body").inner_text()
            m = re.search(r"Over\s*[\d.]+\s*.*?(\d+\.\d{2})", txt, re.I | re.S)
            if m:
                return float(m.group(1))
            for x in re.findall(r"\b(\d+\.\d{2})\b", txt):
                v = float(x)
                if 1.01 < v < 50:
                    return v
        except Exception:
            pass
        return 0.0

    async def _set_stake(self, page: Page, stake: float) -> bool:
        amount = str(int(stake) if stake >= 10 else stake)
        slip_roots = [
            '[class*="betslip" i]',
            '[class*="BetSlip" i]',
            '[class*="bet-slip" i]',
            '[class*="m-betslip" i]',
            '#betslip',
        ]
        for root in slip_roots:
            try:
                root_loc = page.locator(root)
                if await root_loc.count() == 0:
                    continue
                inputs = root_loc.locator(
                    'input[type="tel"], input[type="number"], '
                    'input[placeholder*="Stake" i], input[placeholder*="Amount" i], '
                    'input[class*="stake" i], input'
                )
                n = await inputs.count()
                for i in range(n):
                    el = inputs.nth(i)
                    if not await el.is_visible():
                        continue
                    await el.click(timeout=800)
                    await el.fill("")
                    await el.fill(amount)
                    val = (await el.input_value() or "").replace(",", "")
                    if amount in val:
                        await asyncio.sleep(0.2)
                        return True
                    await el.press("Control+a")
                    await el.type(amount, delay=20)
                    val = (await el.input_value() or "").replace(",", "")
                    if amount in val:
                        return True
            except Exception:
                continue

        for sel in (
            'input[type="tel"]',
            'input[type="number"]',
            'input[placeholder*="Stake" i]',
            'input[placeholder*="Amount" i]',
            'input[class*="stake" i]',
        ):
            try:
                loc = page.locator(sel)
                n = await loc.count()
                for i in range(n):
                    el = loc.nth(i)
                    if not await el.is_visible():
                        continue
                    await el.click(timeout=800)
                    await el.fill("")
                    await el.fill(amount)
                    val = (await el.input_value() or "").replace(",", "")
                    if amount in val:
                        return True
            except Exception:
                continue
        logger.warning("[STAKE] betslip input not found")
        return False

    async def _click_place_bet(self, page: Page) -> bool:
        for root in (
            '[class*="betslip" i]',
            '[class*="BetSlip" i]',
            '[class*="m-betslip" i]',
            "body",
        ):
            try:
                base = page.locator(root) if root != "body" else page
                for sel in (
                    'button:has-text("Place Bet")',
                    'button:has-text("PLACE BET")',
                    'div[role="button"]:has-text("Place Bet")',
                ):
                    loc = base.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click(timeout=2000)
                        await asyncio.sleep(0.75)
                        return True
            except Exception:
                continue
        return False

    async def _click_accept_changes(self, page: Page) -> bool:
        for sel in (
            'button:has-text("Accept Changes")',
            'button:has-text("Accept")',
            'text="Accept Changes"',
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.4)
                    return True
            except Exception:
                pass
        return False

    async def _confirm_visible(self, page: Page) -> bool:
        try:
            loc = page.locator('button:has-text("Confirm")')
            return await loc.count() > 0 and await loc.first.is_visible()
        except Exception:
            return False

    async def _is_suspended(self, page: Page) -> bool:
        try:
            t = (await page.inner_text("body")).lower()
            return any(
                x in t
                for x in (
                    "suspended",
                    "market suspended",
                    "not available",
                    "bet closed",
                    "market closed",
                )
            )
        except Exception:
            return False

    async def ai_arm_from_plan(
        self, page: Page, plan: Optional[dict], watch: MatchWatch
    ) -> bool:
        if watch.flagged_suspended:
            await self._tg("⛔ Suspended — not arming")
            return False

        watch.period = await self._detect_period(page)
        watch.target_line = watch.over_line

        if plan_over_market and not plan:
            try:
                body = await page.inner_text("body")
                plan = plan_over_market(
                    body,
                    watch.home_team,
                    watch.away_team,
                    watch.home_score,
                    watch.away_score,
                    watch.period,
                    self.max_profit,
                    self.balance,
                )
            except Exception as e:
                logger.warning(f"[GROQ] {e}")
                plan = None

        if plan and plan.get("market_found"):
            pmarket = (plan.get("period_market") or "").upper()
            if watch.period == "1H" and pmarket == "2H":
                plan = None
            elif watch.period == "2H" and pmarket == "1H":
                plan = None

        ok = await self._click_correct_over(page, watch)
        if not ok:
            await self._tg(
                f"❌ Market not on betslip\n"
                f"{watch.period} Over {watch.target_line:g}\n"
                f"Score {watch.home_score}-{watch.away_score}\n"
                f"NOT betting"
            )
            return False

        if not await self._betslip_has_selection(page, watch.target_line):
            await self._tg("❌ Selection never reached betslip — NOT betting")
            return False

        await asyncio.sleep(0.4)
        odds = await self._read_slip_odds(page)
        if odds < 1.05:
            odds = float((plan or {}).get("odds") or 1.5)
        self.last_odds_used = odds
        stake = self.calc_stake(odds)
        if plan and plan.get("stake"):
            stake = float(plan["stake"])
        watch.odds_over = odds
        watch.stake_over = stake

        if not await self._set_stake(page, stake):
            await self._tg("❌ Stake failed (betslip) — NOT betting")
            return False
        if not await self._click_place_bet(page):
            await self._tg("❌ Place Bet failed — NOT betting")
            return False

        for _ in range(10):
            if await self._confirm_visible(page):
                watch.confirm_armed = True
                await self._tg(
                    f"✅ ARMED — RIGHT MARKET ON SLIP\n"
                    f"📌 {watch.active_market}\n"
                    f"📊 {watch.home_score}-{watch.away_score} ({watch.period})\n"
                    f"💵 ₦{stake:,.0f} @ {odds}\n"
                    f"On Confirm — waiting goal"
                )
                return True
            if await self._click_accept_changes(page):
                await self._set_stake(page, stake)
                await self._click_place_bet(page)
            await asyncio.sleep(0.35)

        await self._tg("❌ Confirm not reached — NOT armed")
        watch.confirm_armed = False
        return False

    async def click_confirm_only(self, page: Page) -> bool:
        if await self._is_suspended(page):
            return False
        await self._click_accept_changes(page)
        await asyncio.sleep(0.1)
        for sel in (
            'button:has-text("Confirm")',
            'button:has-text("CONFIRM")',
            'div[role="button"]:has-text("Confirm")',
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=1200)
                    await asyncio.sleep(0.9)
                    body = (await page.inner_text("body")).lower()
                    if any(
                        x in body
                        for x in (
                            "bet accepted",
                            "successful",
                            "placed",
                            "bet id",
                            "congratulations",
                            "your bet",
                        )
                    ):
                        await self._tg("✅ BET CREDITED")
                        return True
                    if not await self._confirm_visible(page):
                        await self._tg("✅ BET SUBMITTED")
                        return True
                    return True
            except Exception:
                pass
        return False

    async def cashout_disallowed(self, page: Page, description: str = "") -> bool:
        try:
            for sel in (
                'text="Cashout"',
                'text="Cash Out"',
                'button:has-text("Cashout")',
                'button:has-text("Cash Out")',
                '[role="tab"]:has-text("Cashout")',
                '[role="tab"]:has-text("Cash Out")',
                'a:has-text("Cashout")',
                '[class*="cashout" i]',
            ):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click(timeout=2000)
                        await asyncio.sleep(0.7)
                        break
                except Exception:
                    pass

            clicked = False
            for sel in (
                'button:has-text("Cash Out")',
                'button:has-text("Cashout")',
                'button:has-text("CASHOUT")',
                '[class*="cashout" i] button',
                'div[role="button"]:has-text("Cash Out")',
            ):
                try:
                    loc = page.locator(sel)
                    n = await loc.count()
                    for i in range(n):
                        el = loc.nth(i)
                        if await el.is_visible():
                            await el.click(timeout=1500)
                            await asyncio.sleep(0.55)
                            clicked = True
                            break
                    if clicked:
                        break
                except Exception:
                    continue

            for sel in (
                'button:has-text("Confirm")',
                'button:has-text("CONFIRM")',
                'button:has-text("Cash Out")',
                'button:has-text("Yes")',
            ):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click(timeout=1500)
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    pass

            await self._tg(f"💸 Cashout done\n{description}")
            return True
        except Exception as e:
            await self._tg(f"❌ Cashout failed: {e}")
            return False

    async def _watch_loop(self, mid: str, watch: MatchWatch):
        while watch.running and not watch.flagged_suspended:
            try:
                page = watch.page
                if page.is_closed():
                    break
                if await self._is_suspended(page):
                    watch.flagged_suspended = True
                    watch.confirm_armed = False
                    await self._tg(
                        f"⛔ MARKET SUSPENDED\n"
                        f"{watch.home_team} vs {watch.away_team}\n"
                        f"Was on: {watch.active_market}\n"
                        f"Stopped — will NOT switch market"
                    )
                    watch.running = False
                    break

                if await page.locator('button:has-text("Accept Changes")').count() > 0:
                    await self._click_accept_changes(page)
                    odds = await self._read_slip_odds(page) or watch.odds_over
                    if odds >= 1.05:
                        stake = self.calc_stake(odds)
                        await self._set_stake(page, stake)
                        watch.odds_over = odds
                        watch.stake_over = stake
                        self.last_odds_used = odds
                    await self._click_place_bet(page)
                    await asyncio.sleep(0.35)

                if not await self._confirm_visible(page) and watch.confirm_armed:
                    await self._click_place_bet(page)
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(0.7)
