"""
core/executor.py — pure code, no AI.
line = home + away + 0.5 → click Over under right header → stake hard-cap → Place Bet → Confirm.
When Confirm disappears: Accept Changes / restake hard-cap / Place Bet / Confirm again.
Confirm ONLY via confirm_goal_click (Bet365 goal). Cashout: tab → button → open_bets confirm.

SPEED FIX (this version): staying parked on Confirm is now a DEDICATED fast
background task per match, ticking every 0.3s, completely independent of
the slower main watch loop (which handles score/period/suspension checks
on its own 1.2s cadence). Previously this check was inline in the slow
loop, so a disappeared Confirm could go unnoticed for up to ~1.2s before
recovery even started. Now it's checked and acted on almost immediately,
and the hard-cap check still runs every tick while Confirm is showing.
"""

import asyncio
import re
import time
from enum import Enum, auto
from typing import Optional, Dict, Tuple

from playwright.async_api import Page
from loguru import logger

from config import Config

CONFIRM_WATCH_TICK = 0.3  # seconds between "is Confirm still up?" checks


def _xq(s: str) -> str:
    return f'"{s}"' if '"' not in s else f"'{s}'"


class BetResult(Enum):
    SUCCESS = auto()
    FAILED = auto()
    ABORTED_SAFETY = auto()


class MatchWatch:
    def __init__(
        self,
        page: Page,
        home_team: str,
        away_team: str,
        home_score: int = 0,
        away_score: int = 0,
        match_id: str = "",
    ):
        self.page = page
        self.home_team = home_team or ""
        self.away_team = away_team or ""
        self.home_score = int(home_score)
        self.away_score = int(away_score)
        self.match_id = str(match_id or "")
        self.odds_over: float = 0.0
        self.stake_over: float = 0.0
        self.active_market: str = ""
        self.target_line: float = 0.5
        self.confirm_armed: bool = False
        self.period: str = "1H"
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.last_period_hint: str = "1H"
        self.ht_seen: bool = False
        self.ht_alerted: bool = False
        # New: dedicated fast task that keeps this match parked on Confirm.
        self.confirm_watch_task: Optional[asyncio.Task] = None

    @property
    def total_goals(self) -> int:
        return int(self.home_score) + int(self.away_score)

    @property
    def over_line(self) -> float:
        return float(self.total_goals) + 0.5


class BetExecutor:
    def __init__(self, alerter=None):
        self.alerter = alerter
        self.watches: Dict[str, MatchWatch] = {}
        self.balance = 0.0
        self.current_account = None

    def set_alerter(self, alerter):
        self.alerter = alerter

    async def _tg(self, text: str):
        if self.alerter:
            try:
                await self.alerter.send(text)
            except Exception:
                pass

    # ── score / clock / period ──────────────────────────────────────────

    async def _page_text(self, page: Page, limit: int = 6000) -> str:
        try:
            return (await page.inner_text("body", timeout=2500) or "")[:limit]
        except Exception:
            return ""

    async def _quick_score(self, page: Page) -> Tuple[Optional[int], Optional[int]]:
        try:
            h_loc = page.locator("div.sr-lmt-plus-scb__result-team.srm-team1")
            a_loc = page.locator("div.sr-lmt-plus-scb__result-team.srm-team2")
            if await h_loc.count() > 0 and await a_loc.count() > 0:
                h_txt = (await h_loc.first.inner_text(timeout=1000)).strip()
                a_txt = (await a_loc.first.inner_text(timeout=1000)).strip()
                if h_txt.isdigit() and a_txt.isdigit():
                    return int(h_txt), int(a_txt)
        except Exception:
            pass
        text = await self._page_text(page, 3000)
        for pat in (r"\b(\d{1,2})\s*:\s*(\d{1,2})\b", r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b"):
            for m in re.finditer(pat, text):
                try:
                    h, a = int(m.group(1)), int(m.group(2))
                    if 0 <= h <= 15 and 0 <= a <= 15:
                        return h, a
                except Exception:
                    continue
        return None, None

    async def _detect_period(self, page: Page) -> str:
        try:
            clock = page.locator("div.sr-lmt-clock__time")
            if await clock.count() > 0:
                raw = (await clock.first.inner_text(timeout=800) or "").lower()
                if "1st" in raw:
                    return "1H"
                if "2nd" in raw:
                    return "2H"
        except Exception:
            pass
        text = (await self._page_text(page, 4000)).lower()
        m = re.search(r"\b(\d{1,2})\s*'\s*(?:\+\d+)?\b", text)
        if m:
            try:
                minute = int(m.group(1))
                if 1 <= minute <= 45:
                    return "1H"
                if 46 <= minute <= 120:
                    return "2H"
            except Exception:
                pass
        if re.search(r"\b(half\s*time|half-time|\bht\b)\b", text):
            if not re.search(r"\b(1st\s*half|2nd\s*half|live)\b", text):
                return "HT"
        if re.search(r"\b(2nd\s*half|second\s*half)\b", text):
            return "2H"
        if re.search(r"\b(1st\s*half|first\s*half)\b", text):
            return "1H"
        return "1H"

    async def detect_period(self, page: Page) -> str:
        """Public wrapper — used by main.py to poll a parked match's
        period without touching anything else."""
        return await self._detect_period(page)

    async def _match_ended(self, page: Page) -> bool:
        text = (await self._page_text(page, 4000)).lower()
        if not re.search(r"\b(full\s*time|match\s*ended|match\s*finished)\b", text):
            return False
        if re.search(r"\b(1st\s*half|2nd\s*half|live|ongoing)\b|\b\d{1,2}\s*'", text):
            return False
        return True

    async def _selection_unavailable(self, page: Page) -> bool:
        try:
            slip = page.locator(
                "[class*='betslip'], [class*='Betslip'], [class*='bet-slip'], #betslip"
            )
            if await slip.count() == 0:
                return False
            t = (await slip.first.inner_text(timeout=1200) or "").lower()
            return "unavailable" in t
        except Exception:
            return False

    async def _is_suspended(self, page: Page) -> bool:
        t = (await self._page_text(page, 3000)).lower()
        return "suspended" in t and "unavailable" not in t

    # ── betslip helpers ─────────────────────────────────────────────────

    async def _betslip_has_over(self, page: Page, line: float) -> bool:
        try:
            roots = page.locator(
                "[class*='betslip'], [class*='Betslip'], [class*='bet-slip'], #betslip"
            )
            n = await roots.count()
            if n == 0:
                return False
            needle = f"over {line:g}".lower()
            for i in range(min(n, 3)):
                t = (await roots.nth(i).inner_text(timeout=1200) or "").lower()
                if needle in t or f"over {line}" in t:
                    if "unavailable" in t:
                        return False
                    return True
        except Exception:
            pass
        return False

    async def _wait_betslip_has_over(
        self, page: Page, line: float, timeout: float = 2.5
    ) -> bool:
        """Polls instead of blindly re-clicking. A second blind click on a
        selection that actually worked (just slow to render) would
        un-select it."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if await self._betslip_has_over(page, line):
                return True
            await asyncio.sleep(0.2)
        return False

    async def _confirm_visible(self, page: Page) -> bool:
        try:
            loc = page.locator("button:has-text('Confirm')")
            return await loc.count() > 0 and await loc.first.is_visible()
        except Exception:
            return False

    async def _click_all_tab(self, page: Page):
        for loc in (
            page.locator("div.m-nav-item").get_by_text("All", exact=True),
            page.locator("[role='tab']").get_by_text("All", exact=True),
            page.get_by_text("All", exact=True),
        ):
            try:
                if await loc.count() > 0:
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.4)
                    return
            except Exception:
                continue

    async def _clear_betslip(self, page: Page):
        try:
            for _ in range(10):
                loc = page.locator("i.m-icon-delete")
                if await loc.count() == 0:
                    break
                await loc.first.click(timeout=800)
                await asyncio.sleep(0.15)
        except Exception:
            pass

    async def _click_over_in_section(
        self, page: Page, heading: str, over_text: str
    ) -> bool:
        xs = _xq(heading)
        headings = page.locator(f"xpath=//*[normalize-space(text())={xs}]")
        n = await headings.count()
        for h in range(n):
            heading_el = headings.nth(h)
            for level in range(1, 9):
                container = heading_el.locator(f"xpath=ancestor::*[{level}]")
                if await container.count() == 0:
                    break
                same = container.locator(f"xpath=.//*[normalize-space(text())={xs}]")
                if await same.count() != 1:
                    break
                hits = container.get_by_text(over_text, exact=True)
                if await hits.count() > 0:
                    target = hits.first
                    if await target.is_visible():
                        await target.scroll_into_view_if_needed(timeout=1500)
                        await target.click(timeout=2000)
                        return True
        return False

    async def _find_and_click_line(
        self, page: Page, line: float, prefer: Optional[str]
    ) -> Tuple[bool, str]:
        await self._click_all_tab(page)
        await asyncio.sleep(0.35)
        over_text = f"Over {line:g}"

        if prefer == "1H":
            order = [("1H", "1st Half - Over/Under"), ("FT", "Over/Under")]
        elif prefer == "2H":
            order = [("2H", "2nd Half - Over/Under"), ("FT", "Over/Under")]
        else:
            order = [("FT", "Over/Under")]

        for label, heading in order:
            try:
                ok = await self._click_over_in_section(page, heading, over_text)
                if not ok:
                    continue
                if await self._wait_betslip_has_over(page, line, timeout=2.5):
                    return True, f"{label} Over {line:g}"
                # Confirmed NOT selected in time — clear before next
                # attempt so no stale selection lingers. NEVER re-click
                # blindly here.
                await self._clear_betslip(page)
            except Exception as e:
                logger.warning(f"[ARM] click {heading} {over_text}: {e}")
                await self._clear_betslip(page)
        return False, over_text

    def _stake_for_odds(self, odds: float) -> float:
        """Hard-cap profit from Config.MAX_PROFIT_PER_BET."""
        if odds <= 1.01:
            odds = 1.5
        max_profit = float(Config.MAX_PROFIT_PER_BET)
        stake = max(10.0, max_profit / (odds - 1.0))
        if self.balance and self.balance > 0:
            stake = min(stake, float(self.balance))
        return round(stake, 2)

    async def _set_stake(self, page: Page, stake: float) -> bool:
        stake_str = f"{stake:.0f}" if stake >= 10 else f"{stake:.2f}"
        for sel in (
            "[class*='betslip'] input[type='text']",
            "[class*='betslip'] input[type='number']",
            "[class*='Betslip'] input",
            "#betslip input",
            "input[placeholder*='min' i]",
            "input.m-input",
        ):
            try:
                locs = page.locator(sel)
                for i in range(min(await locs.count(), 4)):
                    el = locs.nth(i)
                    if not await el.is_visible():
                        continue
                    await el.click(timeout=800)
                    await el.fill("")
                    await el.type(stake_str, delay=18)
                    return True
            except Exception:
                continue
        return False

    async def _click_place_bet(self, page: Page) -> bool:
        for sel in (
            "button:has(span[data-cms-key='place_bet'])",
            "button[data-op='desktop-betslip-place-bet-button']",
            "button:has-text('Place Bet')",
            "text=Place Bet",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=2000)
                    await asyncio.sleep(0.35)
                    return True
            except Exception:
                continue
        return False

    async def _click_confirm(self, page: Page) -> bool:
        for sel in (
            "button:has(span[data-cms-key='confirm'])",
            "button:has-text('Confirm')",
            "text=Confirm",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.25)
                    return True
            except Exception:
                continue
        return False

    async def _click_accept_changes(self, page: Page) -> bool:
        for sel in (
            "button:has(span[data-cms-key='accept_changes'])",
            "button:has-text('Accept Changes')",
            "text=Accept Changes",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                continue
        return False

    async def _click_cancel(self, page: Page) -> bool:
        """Backs out of an open Confirm dialog to the editable betslip.
        NOTE: data-cms-key='cancel' was confirmed inside the CASHOUT
        confirm dialog. Reused here for the regular bet confirm dialog on
        the assumption it's a shared component — this reuse is inferred,
        not independently captured for this exact dialog."""
        for sel in (
            "button:has(span[data-cms-key='cancel'])",
            "button:has-text('Cancel')",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=1200)
                    return True
            except Exception:
                continue
        return False

    async def _click_rebet(self, page: Page) -> bool:
        """Short poll (up to ~3s) instead of a single check, since a bet
        just confirmed can take a moment for SportyBet to render the
        Rebet button — checking only once was falling through to the
        slow full re-arm path far more often than necessary."""
        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline:
            for sel in (
                "button:has(span[data-cms-key='rebet'])",
                "button[data-ret='rebet']",
                "button:has-text('Rebet')",
                "button:has-text('Re-bet')",
                "text=Rebet",
            ):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click(timeout=1500)
                        await asyncio.sleep(0.35)
                        return True
                except Exception:
                    continue
            await asyncio.sleep(0.15)
        return False

    async def _read_odds(self, page: Page) -> float:
        try:
            roots = page.locator(
                "[class*='betslip'], [class*='Betslip'], [class*='bet-slip']"
            )
            if await roots.count() == 0:
                return 0.0
            t = await roots.first.inner_text(timeout=1000)
            m = re.search(r"\b(\d{1,2}\.\d{2})\b", t or "")
            if m:
                return float(m.group(1))
        except Exception:
            pass
        return 0.0

    async def _arm_until_confirm(self, page: Page, max_seconds: float = 8.0) -> bool:
        deadline = time.perf_counter() + max_seconds
        saw_place = False
        while time.perf_counter() < deadline:
            if await self._confirm_visible(page):
                return True
            if await self._click_accept_changes(page):
                await asyncio.sleep(0.35)
                continue
            if await self._click_place_bet(page):
                saw_place = True
                await asyncio.sleep(0.5)
                continue
            if saw_place:
                await asyncio.sleep(0.3)
            else:
                break
        return await self._confirm_visible(page)

    async def _hold_on_confirm(self, page: Page, watch: MatchWatch) -> bool:
        """Called only when Confirm has fully disappeared — the betslip is
        editable in this state, so setting stake directly here is fine."""
        if await self._confirm_visible(page):
            watch.confirm_armed = True
            return True

        odds = await self._read_odds(page)
        if odds > 1.01:
            watch.odds_over = odds
            watch.stake_over = self._stake_for_odds(odds)
            await self._set_stake(page, watch.stake_over)
        elif watch.stake_over > 0:
            await self._set_stake(page, watch.stake_over)

        ok = await self._arm_until_confirm(page, max_seconds=5.0)
        watch.confirm_armed = ok
        return ok

    async def _hard_cap_check_while_confirm(self, page: Page, watch: MatchWatch):
        """Called only while Confirm IS currently showing. NEVER edits the
        stake input in this state — must Cancel out first, edit, Place
        Bet, then re-arm to Confirm."""
        odds = await self._read_odds(page)
        if odds <= 1.01 or abs(odds - (watch.odds_over or 0)) <= 0.01:
            return
        new_stake = self._stake_for_odds(odds)
        if abs(new_stake - watch.stake_over) <= 0.01:
            return
        logger.info(
            f"[HARDCAP] odds moved {watch.odds_over}->{odds}, "
            f"restaking {watch.stake_over}->{new_stake}"
        )
        if await self._click_cancel(page):
            await asyncio.sleep(0.3)
            watch.odds_over = odds
            watch.stake_over = new_stake
            await self._set_stake(page, new_stake)
            await self._click_place_bet(page)
            await self._arm_until_confirm(page, max_seconds=6.0)
        else:
            logger.warning(
                "[HARDCAP] Cancel button not found — leaving current stake as-is"
            )
        watch.confirm_armed = await self._confirm_visible(page)

    # ── dedicated fast confirm-watcher (the actual speed fix) ────────────

    async def _confirm_watch_loop(self, mid: str):
        """Runs on its own fast clock (CONFIRM_WATCH_TICK), completely
        separate from the slower main watch loop. Its ONLY jobs:
          - if Confirm disappeared, get back to it as fast as possible
          - if Confirm is showing, keep checking the hard cap
        This is what makes 'go back to Confirm' near-instant instead of
        waiting on the slower score/period/suspension loop's own tick."""
        while True:
            watch = self.watches.get(mid)
            if not watch or not watch.running:
                return
            page = watch.page
            try:
                if page.is_closed():
                    return
                if await self._confirm_visible(page):
                    await self._hard_cap_check_while_confirm(page, watch)
                    watch.confirm_armed = True
                else:
                    watch.confirm_armed = False
                    ok = await self._hold_on_confirm(page, watch)
                    if not ok:
                        logger.warning(
                            f"[CONFIRM-WATCH] {mid}: could not get back to Confirm"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[CONFIRM-WATCH] {mid}: {e}")
            await asyncio.sleep(CONFIRM_WATCH_TICK)

    def _start_confirm_watch(self, mid: str, watch: MatchWatch):
        if watch.confirm_watch_task and not watch.confirm_watch_task.done():
            watch.confirm_watch_task.cancel()
        watch.confirm_watch_task = asyncio.create_task(self._confirm_watch_loop(mid))

    def _stop_confirm_watch(self, watch: MatchWatch):
        t = watch.confirm_watch_task
        watch.confirm_watch_task = None
        if t and not t.done():
            t.cancel()

    # ── verify / cashout ──────────────────────────────────────────────

    async def _verify_bet_result(self, page: Page) -> Tuple[bool, str]:
        await asyncio.sleep(1.0)
        text = (await self._page_text(page, 5000)).lower()
        for p in (
            r"bet\s*reject", r"\brejected\b", r"\bfailed\b", r"\bunavailable\b",
            r"\bsuspended\b", r"insufficient", r"odds\s*changed", r"market\s*closed",
        ):
            if re.search(p, text):
                return False, f"fail:{p}"
        for p in (
            r"bet\s*placed", r"bet\s*accepted", r"\bsuccess\b",
            r"placed\s*successfully", r"bet\s*id",
        ):
            if re.search(p, text):
                return True, f"ok:{p}"
        if not await self._confirm_visible(page):
            return True, "confirm_gone"
        return False, "confirm_still_visible"

    async def cashout_disallowed(self, page: Page, description: str = "") -> bool:
        try:
            tab_ok = False
            for sel in (
                "div.tabs-v2__tab span:has-text('Cashout')",
                "div.tabs-v2__tab:has-text('Cashout')",
                "div.label-container:has-text('Cashout')",
                "span:text-is('Cashout')",
            ):
                try:
                    loc = page.locator(sel)
                    for i in range(min(await loc.count(), 4)):
                        el = loc.nth(i)
                        if not await el.is_visible():
                            continue
                        try:
                            await el.scroll_into_view_if_needed(timeout=1200)
                        except Exception:
                            pass
                        await el.click(timeout=1500)
                        tab_ok = True
                        break
                    if tab_ok:
                        break
                except Exception:
                    continue
            if not tab_ok:
                logger.warning("[CASHOUT] tab not found")
                return False
            await asyncio.sleep(0.7)

            btn_ok = False
            for sel in (
                "button:has(span[data-cms-key='cashout'][data-cms-page='cashout'])",
                "button.af-button--primary:has-text('Cashout')",
                "button:has-text('Full Cashout')",
                "button:has-text('Cashout')",
            ):
                try:
                    loc = page.locator(sel)
                    for i in range(min(await loc.count(), 6)):
                        el = loc.nth(i)
                        if not await el.is_visible():
                            continue
                        txt = (await el.inner_text(timeout=500) or "").lower()
                        if "confirm" in txt and "cashout" not in txt:
                            continue
                        try:
                            await el.scroll_into_view_if_needed(timeout=1200)
                        except Exception:
                            pass
                        await el.click(timeout=1500)
                        btn_ok = True
                        break
                    if btn_ok:
                        break
                except Exception:
                    continue
            if not btn_ok:
                logger.warning("[CASHOUT] button not found")
                return False
            await asyncio.sleep(0.5)

            conf = page.locator("button[data-op='open_bets__cashout_confirm']")
            for _ in range(25):
                if await conf.count() > 0 and await conf.first.is_visible():
                    break
                await asyncio.sleep(0.12)
            if await conf.count() == 0 or not await conf.first.is_visible():
                conf = page.locator(
                    "button.confirm-sub.af-button--primary:has-text('Confirm')"
                )
                if await conf.count() == 0 or not await conf.first.is_visible():
                    logger.warning("[CASHOUT] confirm not found")
                    return False

            await conf.first.click(timeout=1500)
            await self._tg(f"💰 Cashout attempted {description}")
            await asyncio.sleep(0.6)
            return True
        except Exception as e:
            logger.error(f"[CASHOUT] {e}")
            return False

    # ── arm (pure code) ─────────────────────────────────────────────────

    async def ai_arm_from_plan(
        self, page: Page, plan=None, watch: Optional[MatchWatch] = None, mid: str = "",
    ) -> bool:
        if watch is None or page.is_closed():
            return False
        if await self._match_ended(page):
            await self._tg("⏹ Match ended — not arming")
            if mid:
                await self.clear_match(mid)
            return False

        h, a = await self._quick_score(page)
        if h is not None and a is not None:
            watch.home_score, watch.away_score = h, a

        period = await self._detect_period(page)
        watch.period = period
        line = float(watch.home_score + watch.away_score) + 0.5
        watch.target_line = line

        prefer = "1H" if period == "1H" else ("2H" if period == "2H" else None)

        logger.info(
            f"[ARM] score={watch.home_score}-{watch.away_score} "
            f"period={period} line={line:g} prefer={prefer or 'FT'}"
        )

        # Pause the fast confirm-watcher while we're actively re-picking
        # the market — it should not fight with what's happening here.
        self._stop_confirm_watch(watch)

        await self._clear_betslip(page)
        await asyncio.sleep(0.2)

        ok, label = await self._find_and_click_line(page, line, prefer)
        if not ok or not await self._betslip_has_over(page, line):
            await self._tg(
                f"🛑 NO VALID OVER MARKET\n{watch.home_team} vs {watch.away_team}\n"
                f"Score {watch.home_score}-{watch.away_score} | {period}\n"
                f"Needed Over {line:g}\nStopped watching.\nSend /slow for another game."
            )
            if mid:
                await self.clear_match(mid)
            watch.confirm_armed = False
            return False

        watch.active_market = label
        odds = await self._read_odds(page)
        if odds <= 1.01:
            odds = 1.5
        watch.odds_over = odds
        watch.stake_over = self._stake_for_odds(odds)

        if not await self._set_stake(page, watch.stake_over):
            await self._tg("❌ Stake failed")
            watch.confirm_armed = False
            return False

        await self._click_accept_changes(page)
        if not await self._click_place_bet(page):
            await self._tg("❌ Place Bet failed")
            watch.confirm_armed = False
            return False

        armed = await self._arm_until_confirm(page, max_seconds=8.0)
        watch.confirm_armed = armed
        if armed:
            logger.success(f"[ARM] {label} @{odds} stake={watch.stake_over} (hard-cap)")
            await self._tg(
                f"✅ ARMED\n{watch.home_team} vs {watch.away_team}\n"
                f"Score {watch.home_score}-{watch.away_score} | {period}\n"
                f"{label}\nOdds {odds} | Stake {watch.stake_over} (hard-cap)\n"
                f"Waiting for goal → Confirm"
            )
            # Kick off the dedicated fast watcher NOW that we're armed.
            if mid:
                self._start_confirm_watch(mid, watch)
        else:
            await self._tg(f"⚠️ Place Bet done but Confirm not visible\n{label}")
        return armed

    async def _try_rebet(self, page: Page, watch: MatchWatch) -> bool:
        if not await self._click_rebet(page):
            return False
        await asyncio.sleep(0.4)
        odds = await self._read_odds(page)
        if odds > 1.01:
            watch.odds_over = odds
            watch.stake_over = self._stake_for_odds(odds)
        if watch.stake_over > 0:
            await self._set_stake(page, watch.stake_over)
        await self._click_accept_changes(page)
        if not await self._click_place_bet(page):
            return False
        armed = await self._arm_until_confirm(page, max_seconds=6.0)
        watch.confirm_armed = armed
        if armed:
            await self._tg(
                f"🔁 REBET ARMED\n{watch.home_team} vs {watch.away_team}\n"
                f"{watch.active_market}\nOdds {watch.odds_over} | "
                f"Stake {watch.stake_over} (hard-cap)"
            )
        return armed

    async def _watch_loop(self, mid: str):
        """Slower loop: score/period/match-ended/suspension checks only.
        Confirm-holding is handled entirely by the dedicated fast task
        started in ai_arm_from_plan, not here."""
        watch = self.watches.get(mid)
        if not watch:
            return
        page = watch.page
        watch.running = True
        logger.info(f"[WATCH] start {mid}")
        await self.ai_arm_from_plan(page, None, watch, mid=mid)

        while watch.running:
            try:
                if page.is_closed() or mid not in self.watches:
                    break
                if await self._match_ended(page):
                    self._stop_confirm_watch(watch)
                    await self._clear_betslip(page)
                    await self._tg(f"🏁 FULL TIME — {watch.home_team} vs {watch.away_team}")
                    watch.running = False
                    break

                h, a = await self._quick_score(page)
                if h is not None and a is not None and (h + a) < watch.total_goals:
                    self._stop_confirm_watch(watch)
                    await self.cashout_disallowed(
                        page, f"{watch.home_score}-{watch.away_score}→{h}-{a}"
                    )
                    watch.home_score, watch.away_score = h, a
                    watch.confirm_armed = False
                    await self.ai_arm_from_plan(page, None, watch, mid=mid)

                period_hint = await self._detect_period(page)
                if period_hint == "HT":
                    watch.ht_seen = True
                    if not watch.ht_alerted:
                        watch.ht_alerted = True
                        self._stop_confirm_watch(watch)
                        watch.confirm_armed = False
                        await self._tg(f"⏸ HALF TIME — {watch.home_team} vs {watch.away_team}")
                if (
                    watch.ht_seen
                    and period_hint == "2H"
                    and watch.last_period_hint in ("HT", "1H")
                ):
                    watch.ht_alerted = False
                    await self._tg("🔄 2nd half — re-arming")
                    await self.ai_arm_from_plan(page, None, watch, mid=mid)

                if await self._selection_unavailable(page):
                    self._stop_confirm_watch(watch)
                    await self._clear_betslip(page)
                    watch.confirm_armed = False
                    await self._tg("⚠️ Market Unavailable — re-arming")
                    await self.ai_arm_from_plan(page, None, watch, mid=mid)

                if await self._is_suspended(page):
                    watch.last_period_hint = period_hint
                    await asyncio.sleep(1.5)
                    continue

                watch.last_period_hint = period_hint
                await asyncio.sleep(1.2)
            except Exception as e:
                logger.error(f"[WATCH] {mid}: {e}")
                await asyncio.sleep(1.5)

        self._stop_confirm_watch(watch)
        watch.running = False
        logger.info(f"[WATCH] stop {mid}")

    def start_watching(self, mid: str, page: Page, home: str, away: str, **kwargs):
        mid = str(mid)
        # Enforce exactly one active betting watch at all times.
        for other_id in list(self.watches.keys()):
            if other_id != mid:
                w = self.watches.pop(other_id)
                w.running = False
                self._stop_confirm_watch(w)
                if w.task and not w.task.done():
                    w.task.cancel()
        old = self.watches.pop(mid, None)
        if old:
            old.running = False
            self._stop_confirm_watch(old)
            if old.task and not old.task.done():
                old.task.cancel()
        h = int(kwargs.get("home_score") or 0)
        a = int(kwargs.get("away_score") or 0)
        watch = MatchWatch(page, home, away, h, a, match_id=mid)
        self.watches[mid] = watch
        watch.task = asyncio.create_task(self._watch_loop(mid))
        return watch

    def get_watch(self, mid: str) -> Optional[MatchWatch]:
        return self.watches.get(str(mid))

    async def clear_match(self, mid: str = ""):
        ids = [str(mid)] if mid else list(self.watches.keys())
        for i in ids:
            w = self.watches.pop(i, None)
            if not w:
                continue
            w.running = False
            self._stop_confirm_watch(w)
            if w.task and not w.task.done():
                w.task.cancel()
            try:
                if w.page and not w.page.is_closed():
                    await self._clear_betslip(w.page)
                    await w.page.close()
            except Exception:
                pass
        await self._tg(f"🧹 Cleared: {', '.join(ids) if ids else 'all'}")

    def stop_watching(self, mid: str):
        w = self.watches.get(str(mid))
        if w:
            w.running = False
            self._stop_confirm_watch(w)
            if w.task and not w.task.done():
                w.task.cancel()

    async def confirm_goal_click(self, mid: str) -> BetResult:
        w = self.watches.get(str(mid))
        if not w or not w.page or w.page.is_closed():
            await self._tg(f"❌ BET SKIPPED\n{mid}\npage/watch missing")
            return BetResult.FAILED
        if not w.confirm_armed:
            await self._tg(
                f"⚠️ Goal but not armed — skip\n{w.home_team} vs {w.away_team}\nNO BET PLACED"
            )
            return BetResult.ABORTED_SAFETY
        if await self._is_suspended(w.page) or await self._selection_unavailable(w.page):
            await self._tg(
                f"🔒 Goal + SportyBet locked — stop watching\n"
                f"{w.home_team} vs {w.away_team}\nNO BET PLACED"
            )
            await self.clear_match(mid)
            return BetResult.ABORTED_SAFETY

        # Stop the fast watcher before we touch Confirm ourselves —
        # it must not fight over the same click.
        self._stop_confirm_watch(w)

        if not await self._click_confirm(w.page):
            await self._tg(
                f"❌ BET NOT PLACED\n{w.home_team} vs {w.away_team}\n"
                f"{w.active_market}\nConfirm click failed"
            )
            return BetResult.FAILED

        accepted, detail = await self._verify_bet_result(w.page)
        w.confirm_armed = False

        if accepted:
            await self._tg(
                f"✅ BET ACCEPTED\n{w.home_team} vs {w.away_team}\n{w.active_market}\n"
                f"Odds {w.odds_over} | Stake {w.stake_over}\nDetail: {detail}"
            )
            await asyncio.sleep(0.6)
            if not await self._try_rebet(w.page, w):
                await self.ai_arm_from_plan(w.page, None, w, mid=mid)
            else:
                if mid:
                    self._start_confirm_watch(mid, w)
            return BetResult.SUCCESS

        await self._tg(
            f"❌ BET NOT ACCEPTED\n{w.home_team} vs {w.away_team}\n"
            f"{w.active_market}\nDetail: {detail}"
        )
        await self.ai_arm_from_plan(w.page, None, w, mid=mid)
        return BetResult.FAILED
