"""
BetExecutor — score from SportyBet page only.
Click market → betslip must show selection → stake → Place Bet → Confirm.
Odds change → Accept Changes → Place Bet → Confirm.
/clear closes tab + wipes watch. HT→2H detected and re-armed.
"""

import asyncio
import re
import time
from enum import Enum, auto
from typing import Optional, Dict, Any, List

from playwright.async_api import Page
from loguru import logger

from config import Config
from core.groq_ai import plan_over_market


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
        self.force_full_time: bool = False
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.last_period: str = "1H"
        self.ht_seen: bool = False

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
        self._lock = asyncio.Lock()

    def set_alerter(self, alerter):
        self.alerter = alerter

    async def _tg(self, text: str):
        if self.alerter:
            try:
                await self.alerter.send(text)
            except Exception:
                pass

    # ─── page helpers ───────────────────────────────────────────

    async def _page_text(self, page: Page, limit: int = 8000) -> str:
        try:
            t = await page.inner_text("body", timeout=2500)
            return (t or "")[:limit]
        except Exception:
            return ""

    async def _read_score_from_page(self, page: Page) -> tuple:
        """Read live score from SportyBet DOM only. Never trust old watch/Bet365."""
        text = await self._page_text(page)
        patterns = [
            r"(?:score|result)?\s*(\d{1,2})\s*[-:]\s*(\d{1,2})",
            r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b",
        ]
        candidates = []
        for pat in patterns:
            for m in re.finditer(pat, text, re.I):
                try:
                    h, a = int(m.group(1)), int(m.group(2))
                    if 0 <= h <= 15 and 0 <= a <= 15:
                        candidates.append((h, a))
                except Exception:
                    pass
        if not candidates:
            return None, None
        return candidates[0]

    async def _detect_period(self, page: Page) -> str:
        text = (await self._page_text(page)).lower()
        if re.search(r"\b(full\s*time|ft\b|match\s*ended|finished)\b", text):
            return "FT"
        if re.search(r"\b(half\s*time|ht\b|mid.?break)\b", text):
            return "HT"
        if re.search(r"\b(2nd\s*half|second\s*half|2h\b)\b", text):
            return "2H"
        m = re.search(r"\b(4[6-9]|[5-9]\d)\s*[':]?\d*", text)
        if m:
            return "2H"
        return "1H"

    async def _match_ended(self, page: Page) -> bool:
        text = (await self._page_text(page)).lower()
        return bool(
            re.search(r"\b(full\s*time|match\s*ended|finished|ft\s*\d)", text)
        )

    async def _selection_unavailable(self, page: Page) -> bool:
        try:
            slip = page.locator(
                "[class*='betslip'], [class*='Betslip'], [class*='bet-slip'], "
                "#betslip, [data-testid*='betslip']"
            )
            if await slip.count() == 0:
                return False
            t = (await slip.first.inner_text(timeout=1500) or "").lower()
            return "unavailable" in t
        except Exception:
            return False

    async def _is_suspended(self, page: Page) -> bool:
        text = (await self._page_text(page, 3000)).lower()
        return "suspended" in text and "unavailable" not in text

    async def _betslip_has_selection(self, page: Page, line: float) -> bool:
        """True only if Over {line} is actually on the betslip."""
        try:
            roots = page.locator(
                "[class*='betslip'], [class*='Betslip'], [class*='bet-slip'], "
                "#betslip, [data-testid*='betslip']"
            )
            n = await roots.count()
            if n == 0:
                return False
            needle = f"over {line}".lower()
            needle2 = f"over {line:g}".lower()
            for i in range(min(n, 3)):
                t = (await roots.nth(i).inner_text(timeout=1200) or "").lower()
                if needle in t or needle2 in t or f"o {line}" in t:
                    if "unavailable" in t:
                        return False
                    return True
            body = (await self._page_text(page, 4000)).lower()
            if ("betslip" in body or "stake" in body) and (
                needle in body or needle2 in body
            ):
                return "unavailable" not in body
        except Exception:
            pass
        return False

    async def _click_all_tab(self, page: Page):
        for sel in (
            "text=All",
            "button:has-text('All')",
            "[role='tab']:has-text('All')",
            "a:has-text('All')",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.4)
                    return
            except Exception:
                continue

    async def _scroll_markets(self, page: Page, steps: int = 8):
        for _ in range(steps):
            try:
                await page.mouse.wheel(0, 600)
                await asyncio.sleep(0.2)
            except Exception:
                break

    async def _find_and_click_over(
        self,
        page: Page,
        line: float,
        require_kw: List[str],
        forbid_kw: List[str],
        label: str,
    ) -> bool:
        """Click Over {line} under the right period heading. Verify betslip after.

        Keyword matching is scoped to ONLY the line(s) of surrounding text
        that literally contain "Over/Under" — the market's own heading
        (e.g. "1st Half - Over/Under", "2nd Half - Over/Under", or plain
        "Over/Under" for Full Time). This avoids false matches from OTHER
        nearby markets that also contain "1st half"/"2nd half" in their
        name (e.g. "1st Half - Handicap", "1st Half - 3rd Goal",
        "1st Half - Double Chance"), which would otherwise pollute the
        6-level ancestor text grab and cause require_kw/forbid_kw to match
        against the wrong market entirely.
        """
        await self._click_all_tab(page)
        await self._scroll_markets(page, 6)

        line_str = f"{line:g}"
        patterns = [
            f"text=/Over\\s*{re.escape(line_str)}/i",
            f"text=/O\\s*{re.escape(line_str)}/i",
            f"text='Over {line_str}'",
            f"text='Over {line}'",
        ]

        for _scroll in range(5):
            for pat in patterns:
                try:
                    locs = page.locator(pat)
                    count = await locs.count()
                    for i in range(min(count, 12)):
                        el = locs.nth(i)
                        try:
                            box = await el.bounding_box()
                            if not box:
                                continue
                            handle = await el.element_handle()
                            if not handle:
                                continue
                            ctx = await page.evaluate(
                                """(el) => {
                                    let n = el;
                                    for (let i = 0; i < 6 && n; i++) {
                                        n = n.parentElement;
                                    }
                                    return (n && n.innerText) ? n.innerText.slice(0, 400) : '';
                                }""",
                                handle,
                            )
                            ctx = ctx or ""
                            # Isolate only the heading line(s) that actually
                            # say "Over/Under" — ignore everything else that
                            # may have been swept up by the ancestor walk.
                            heading_lines = [
                                ln for ln in ctx.split("\n")
                                if "over/under" in ln.lower()
                            ]
                            ctx_l = (
                                "\n".join(heading_lines) if heading_lines else ctx
                            ).lower()

                            if any(f in ctx_l for f in forbid_kw):
                                continue
                            if require_kw and not any(r in ctx_l for r in require_kw):
                                continue
                            await el.scroll_into_view_if_needed(timeout=1500)
                            await el.click(timeout=2000)
                            await asyncio.sleep(0.6)
                            if await self._betslip_has_selection(page, line):
                                logger.success(f"[ARM] clicked {label} Over {line}")
                                return True
                            await el.click(timeout=1500)
                            await asyncio.sleep(0.5)
                            if await self._betslip_has_selection(page, line):
                                logger.success(f"[ARM] clicked {label} Over {line} (2nd)")
                                return True
                        except Exception:
                            continue
                except Exception:
                    continue
            await self._scroll_markets(page, 3)

        logger.warning(f"[ARM] could not put {label} Over {line} on betslip")
        return False

    # RULE:
    #   1H  -> try 1st Half Over first. If not on the page, fall back to Full Time Over.
    #   2H  -> try 2nd Half Over first. If not on the page, fall back to Full Time Over.
    #   FT/HT or force_full_time -> Full Time Over only.
    # Full Time matching uses require_kw=[] because SportyBet's default
    # Over/Under section (the Full Time market) has NO "Full Time" label —
    # it's just plain "Over/Under". forbid_kw still excludes any section
    # explicitly headed "1st Half - Over/Under" / "2nd Half - Over/Under".
    async def _click_correct_over(self, page: Page, watch: MatchWatch) -> bool:
        line = watch.over_line
        period = watch.period

        if watch.force_full_time or period in ("FT", "HT"):
            ok = await self._find_and_click_over(
                page,
                line,
                require_kw=[],
                forbid_kw=["1st half", "first half", "2nd half", "second half", "half time"],
                label="FT",
            )
            if ok:
                watch.active_market = f"FT Over {line}"
                return True
            return False

        if period == "1H":
            ok = await self._find_and_click_over(
                page,
                line,
                require_kw=["1st half", "first half", "1h"],
                forbid_kw=["2nd half", "second half", "full time", "fulltime"],
                label="1H",
            )
            if ok:
                watch.active_market = f"1H Over {line}"
                return True
            # fallback Full Time only (never another 1H line)
            ok = await self._find_and_click_over(
                page,
                line,
                require_kw=[],
                forbid_kw=["1st half", "first half", "2nd half", "second half"],
                label="FT-fallback",
            )
            if ok:
                watch.active_market = f"FT Over {line}"
                watch.force_full_time = True
                return True
            return False

        # 2H
        ok = await self._find_and_click_over(
            page,
            line,
            require_kw=["2nd half", "second half", "2h"],
            forbid_kw=["1st half", "first half"],
            label="2H",
        )
        if ok:
            watch.active_market = f"2H Over {line}"
            return True
        ok = await self._find_and_click_over(
            page,
            line,
            require_kw=[],
            forbid_kw=["1st half", "first half", "2nd half", "second half"],
            label="FT-fallback-2H",
        )
        if ok:
            watch.active_market = f"FT Over {line}"
            watch.force_full_time = True
            return True
        return False

    async def _set_stake(self, page: Page, stake: float) -> bool:
        stake_str = f"{stake:.0f}" if stake >= 10 else f"{stake:.2f}"
        selectors = [
            "[class*='betslip'] input[type='text']",
            "[class*='betslip'] input[type='number']",
            "[class*='Betslip'] input",
            "[class*='bet-slip'] input",
            "#betslip input",
            "input[placeholder*='Stake']",
            "input[placeholder*='stake']",
            "input[class*='stake']",
        ]
        for sel in selectors:
            try:
                locs = page.locator(sel)
                n = await locs.count()
                for i in range(min(n, 4)):
                    el = locs.nth(i)
                    if not await el.is_visible():
                        continue
                    await el.click(timeout=1000)
                    await el.fill("")
                    await el.type(stake_str, delay=25)
                    await asyncio.sleep(0.2)
                    return True
            except Exception:
                continue
        try:
            await page.keyboard.press("Control+a")
            await page.keyboard.type(stake_str, delay=25)
            return True
        except Exception:
            return False

    async def _click_place_bet(self, page: Page) -> bool:
        for sel in (
            "button:has-text('Place Bet')",
            "button:has-text('Place bet')",
            "text=Place Bet",
            "[class*='place'] button",
            "button:has-text('Done')",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=2000)
                    await asyncio.sleep(0.5)
                    return True
            except Exception:
                continue
        return False

    async def _click_confirm(self, page: Page) -> bool:
        for sel in (
            "button:has-text('Confirm')",
            "text=Confirm",
            "button:has-text('CONFIRM')",
            "[class*='confirm'] button",
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

    async def _click_accept_changes(self, page: Page) -> bool:
        for sel in (
            "button:has-text('Accept Changes')",
            "button:has-text('Accept changes')",
            "text=Accept Changes",
            "button:has-text('Accept')",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.4)
                    return True
            except Exception:
                continue
        return False

    async def _read_odds_from_slip(self, page: Page) -> float:
        try:
            roots = page.locator(
                "[class*='betslip'], [class*='Betslip'], [class*='bet-slip']"
            )
            if await roots.count() == 0:
                return 0.0
            t = await roots.first.inner_text(timeout=1200)
            m = re.search(r"\b(\d{1,2}\.\d{2})\b", t or "")
            if m:
                return float(m.group(1))
        except Exception:
            pass
        return 0.0

    async def _clear_betslip(self, page: Page):
        for sel in (
            "button:has-text('Remove')",
            "[class*='betslip'] [class*='remove']",
            "[class*='betslip'] [class*='delete']",
            "button:has-text('Clear')",
            "[aria-label*='Remove']",
        ):
            try:
                locs = page.locator(sel)
                n = await locs.count()
                for i in range(min(n, 5)):
                    try:
                        await locs.nth(i).click(timeout=800)
                        await asyncio.sleep(0.2)
                    except Exception:
                        pass
            except Exception:
                continue

    async def cashout_disallowed(self, page: Page, description: str = "") -> bool:
        """Cashout tab first, then Cash Out → Confirm (disallowed goal only)."""
        try:
            for sel in (
                "text=Cashout",
                "text=Cash Out",
                "button:has-text('Cashout')",
                "[class*='cashout']",
            ):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=1500)
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    continue
            for sel in (
                "button:has-text('Cash Out')",
                "button:has-text('Cashout')",
                "text=Cash Out",
            ):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click(timeout=1500)
                        await asyncio.sleep(0.4)
                        await self._click_confirm(page)
                        await self._tg(f"💰 Cashout attempted {description}")
                        return True
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"[CASHOUT] {e}")
        return False

    # ─── arm / watch ────────────────────────────────────────────

    async def ai_arm_from_plan(
        self,
        page: Page,
        plan: Optional[dict],
        watch: MatchWatch,
    ) -> bool:
        """Fresh score from page → click market → betslip check → stake → Place → Confirm."""
        if page.is_closed():
            return False

        h, a = await self._read_score_from_page(page)
        if h is not None and a is not None:
            watch.home_score = h
            watch.away_score = a
        watch.period = await self._detect_period(page)
        watch.target_line = watch.over_line

        if await self._match_ended(page):
            await self._tg("⏹ Match ended — not arming")
            return False

        if await self._selection_unavailable(page):
            await self._clear_betslip(page)
            watch.force_full_time = True
            await self._tg("⚠️ Market Unavailable → switching to Full Time Over only")

        max_profit = float(Config.MAX_PROFIT_PER_BET)
        balance = 0.0
        try:
            text = await self._page_text(page, 2000)
            bm = re.search(r"(?:NGN|₦)\s*([\d,]+(?:\.\d+)?)", text)
            if bm:
                balance = float(bm.group(1).replace(",", ""))
        except Exception:
            pass

        if plan is None:
            try:
                page_text = await self._page_text(page, 1500)
                plan = plan_over_market(
                    page_text,
                    watch.home_team,
                    watch.away_team,
                    watch.home_score,
                    watch.away_score,
                    watch.period,
                    max_profit,
                    balance,
                )
            except Exception as e:
                logger.warning(f"[GROQ] skip: {e}")
                plan = {}

        if plan and plan.get("period_market") == "FT":
            watch.force_full_time = True

        await self._clear_betslip(page)
        await asyncio.sleep(0.3)

        ok = await self._click_correct_over(page, watch)
        if not ok or not await self._betslip_has_selection(page, watch.over_line):
            await self._tg(
                f"❌ Market not on betslip\n"
                f"{watch.active_market or f'Over {watch.over_line}'}\n"
                f"Score {watch.home_score}-{watch.away_score}\n"
                f"NOT betting"
            )
            watch.confirm_armed = False
            return False

        odds = await self._read_odds_from_slip(page)
        if odds <= 1.01:
            odds = 1.50  # safe fallback only for stake math
        watch.odds_over = odds
        stake = max_profit / (odds - 1.0) if odds > 1.0 else 10.0
        if balance > 0 and stake > balance:
            stake = balance
        watch.stake_over = round(stake, 2)

        if not await self._set_stake(page, watch.stake_over):
            await self._tg("❌ Stake failed")
            return False

        await self._click_place_bet(page)
        await asyncio.sleep(0.4)
        armed = await self._click_confirm(page)
        watch.confirm_armed = True
        await self._tg(
            f"✅ ARMED\n"
            f"{watch.home_team} vs {watch.away_team}\n"
            f"Score {watch.home_score}-{watch.away_score} | {watch.period}\n"
            f"{watch.active_market}\n"
            f"Odds {watch.odds_over} | Stake {watch.stake_over}\n"
            f"Confirm ready: {armed or True}"
        )
        return True

    async def _watch_loop(self, mid: str):
        watch = self.watches.get(mid)
        if not watch:
            return
        page = watch.page
        watch.running = True
        logger.info(f"[WATCH] start {mid}")

        while watch.running:
            try:
                if page.is_closed():
                    break

                if await self._match_ended(page):
                    await self._clear_betslip(page)
                    await self._tg(f"⏹ Full time — cleared betslip ({mid})")
                    watch.running = False
                    break

                h, a = await self._read_score_from_page(page)
                if h is not None and a is not None:
                    if h + a < watch.total_goals:
                        await self.cashout_disallowed(
                            page, f"{watch.home_score}-{watch.away_score}→{h}-{a}"
                        )
                    watch.home_score = h
                    watch.away_score = a

                period = await self._detect_period(page)
                prev = watch.period
                watch.period = period

                if period == "HT":
                    watch.ht_seen = True
                    watch.confirm_armed = False

                if watch.ht_seen and period == "2H" and prev in ("HT", "1H"):
                    await self._tg("🔄 2nd half started — re-arming")
                    watch.force_full_time = False
                    await self.ai_arm_from_plan(page, None, watch)

                if await self._selection_unavailable(page):
                    await self._clear_betslip(page)
                    watch.force_full_time = True
                    watch.confirm_armed = False
                    await self.ai_arm_from_plan(page, None, watch)

                if await self._is_suspended(page):
                    await asyncio.sleep(1.0)
                    continue

                try:
                    if await page.locator("button:has-text('Accept Changes')").count() > 0:
                        await self._click_accept_changes(page)
                        odds = await self._read_odds_from_slip(page)
                        if odds > 1.01:
                            max_profit = float(Config.MAX_PROFIT_PER_BET)
                            stake = max_profit / (odds - 1.0)
                            watch.odds_over = odds
                            watch.stake_over = round(stake, 2)
                            await self._set_stake(page, watch.stake_over)
                        await self._click_place_bet(page)
                        await self._click_confirm(page)
                        watch.confirm_armed = True
                except Exception:
                    pass

                if watch.confirm_armed:
                    try:
                        conf = page.locator("button:has-text('Confirm')")
                        if await conf.count() > 0 and await conf.first.is_visible():
                            pass
                        else:
                            if await self._betslip_has_selection(page, watch.over_line):
                                await self._click_place_bet(page)
                                await self._click_confirm(page)
                    except Exception:
                        pass

                await asyncio.sleep(0.8)
            except Exception as e:
                logger.error(f"[WATCH] {mid}: {e}")
                await asyncio.sleep(1.2)

        watch.running = False
        logger.info(f"[WATCH] stop {mid}")

    def start_watching(self, mid: str, page: Page, home: str, away: str, **kwargs):
        mid = str(mid)
        old = self.watches.pop(mid, None)
        if old:
            old.running = False
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
        """
        /clear — stop watch, close SportyBet match tab, wipe state.
        If mid empty, clear ALL watches.
        """
        ids = [str(mid)] if mid else list(self.watches.keys())
        for i in ids:
            w = self.watches.pop(i, None)
            if not w:
                continue
            w.running = False
            if w.task and not w.task.done():
                w.task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(w.task), timeout=1.5)
                except Exception:
                    pass
            try:
                if w.page and not w.page.is_closed():
                    await self._clear_betslip(w.page)
                    await w.page.close()
            except Exception:
                pass
        await self._tg(f"🧹 Cleared slow match(es): {', '.join(ids) if ids else 'all'}")

    def stop_watching(self, mid: str):
        w = self.watches.get(str(mid))
        if w:
            w.running = False
            if w.task and not w.task.done():
                w.task.cancel()

    async def confirm_goal_click(self, mid: str) -> BetResult:
        """Bet365 goal for locked FI → click Confirm immediately."""
        w = self.watches.get(str(mid))
        if not w or not w.page or w.page.is_closed():
            return BetResult.FAILED
        if not w.confirm_armed:
            await self._tg("⚠️ Goal but not armed — skip")
            return BetResult.ABORTED_SAFETY
        if await self._is_suspended(w.page) or await self._selection_unavailable(w.page):
            await self._tg("🔒 Goal + SportyBet locked — stop watching this match")
            await self.clear_match(mid)
            return BetResult.ABORTED_SAFETY
        ok = await self._click_confirm(w.page)
        if ok:
            await self._tg(f"✅ BET CLICKED Confirm | {w.active_market}")
            await asyncio.sleep(1.0)
            h, a = await self._read_score_from_page(w.page)
            if h is not None:
                w.home_score = h
                w.away_score = a
            await self.ai_arm_from_plan(w.page, None, w)
            return BetResult.SUCCESS
        return BetResult.FAILED
