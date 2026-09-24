"""
BetExecutor — Groq decides market/line; Playwright only executes.
Deep scroll + up to 3 full scan attempts before NO VALID OVER MARKET.
No pure-DOM market picking. Confirm ONLY on Bet365 goal for locked match.
Every bet result always sent to Telegram. Rebet after accept if still open.
"""

import asyncio
import re
from enum import Enum, auto
from typing import Optional, Dict, Any, List, Tuple

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
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.last_period_hint: str = "1H"
        self.ht_seen: bool = False
        self.ht_alerted: bool = False

    @property
    def total_goals(self) -> int:
        return int(self.home_score) + int(self.away_score)

    @property
    def over_line(self) -> float:
        return float(self.total_goals) + 0.5


_ALLOWED_MARKET_RE = re.compile(r"^(1H|2H|FT) Over \d+(\.\d+)?$")


def _is_allowed_market_label(label: str) -> bool:
    return bool(_ALLOWED_MARKET_RE.match((label or "").strip()))


def _require_forbid_for_period_market(pm: str):
    pm = (pm or "FT").upper()
    if pm == "1H":
        return (
            ["1st half", "first half", "1h"],
            ["2nd half", "second half", "rest of"],
        )
    if pm == "2H":
        return (
            ["2nd half", "second half", "2h"],
            ["1st half", "first half", "rest of"],
        )
    return (
        [],
        ["1st half", "first half", "2nd half", "second half", "half time", "rest of"],
    )


_FORBIDDEN_CTX = re.compile(
    r"rest\s*of|handicap|asian|next\s*goal|nth\s*goal|goalscorer|correct\s*score|"
    r"double\s*chance|draw\s*no\s*bet|both\s*teams|btts|odd/even|odd\s*even|"
    r"gg/ng|team\s*total|home\s*total|away\s*total|corners|bookings|cards",
    re.I,
)


class BetExecutor:
    def __init__(self, alerter=None):
        self.alerter = alerter
        self.watches: Dict[str, MatchWatch] = {}

    def set_alerter(self, alerter):
        self.alerter = alerter

    async def _tg(self, text: str):
        if self.alerter:
            try:
                await self.alerter.send(text)
            except Exception:
                pass

    async def _page_text(self, page: Page, limit: int = 8000) -> str:
        try:
            t = await page.inner_text("body", timeout=2500)
            return (t or "")[:limit]
        except Exception:
            return ""

    def _filter_sidebar(self, text: str) -> str:
        skip = [
            r"^Sports\( ", r"^Games \)", r"^Live Betting\( ", r"^Scheduled \)", r"^Virtuals$",
            r"^Jackpot\( ", r"^Livescore \)", r"^Results\( ", r"^Promotions \)",
            r"^Deposit\( ", r"^Bet History \)", r"^My Account$",
            r"^Football\s*\(\d+\)\( ", r"^eFootball\s*\(\d+\) \)",
        ]
        out = []
        for line in (text or "").split("\n"):
            s = line.strip()
            if not s or len(s) < 2:
                continue
            if any(re.match(p, s, re.I) for p in skip):
                continue
            out.append(s)
        return "\n".join(out)

    async def _capture_full_market_text(self, page: Page, steps: int = 22) -> str:
        """Deep scroll All tab and accumulate every market line for Groq."""
        await self._click_all_tab(page)
        await asyncio.sleep(0.7)

        seen = set()
        lines: List[str] = []

        async def snap() -> str:
            try:
                for sel in (
                    "[class*='event-market']",
                    "[class*='market-list']",
                    "[class*='MarketList']",
                    "main",
                    "[class*='match-detail']",
                ):
                    loc = page.locator(sel)
                    if await loc.count() == 0:
                        continue
                    parts = []
                    for i in range(min(await loc.count(), 5)):
                        try:
                            t = await loc.nth(i).inner_text(timeout=1500)
                            if t and len(t) > 40:
                                parts.append(t)
                        except Exception:
                            pass
                    if parts:
                        return self._filter_sidebar("\n".join(parts))
                return self._filter_sidebar(
                    await page.inner_text("body", timeout=2500) or ""
                )
            except Exception:
                return ""

        def absorb(blob: str):
            for line in (blob or "").split("\n"):
                line = line.strip()
                if not line or len(line) > 220:
                    continue
                k = line.lower()
                if k not in seen:
                    seen.add(k)
                    lines.append(line)

        absorb(await snap())
        for i in range(steps):
            try:
                await page.evaluate(
                    """() => {
                        const sels = [
                            '[class*="market-list"]','[class*="MarketList"]',
                            '[class*="event-market"]','main'
                        ];
                        for (const s of sels) {
                            for (const c of document.querySelectorAll(s)) {
                                if (c.scrollHeight > c.clientHeight + 40) {
                                    c.scrollBy(0, 480);
                                    return;
                                }
                            }
                        }
                        window.scrollBy(0, 480);
                    }"""
                )
                await asyncio.sleep(0.5)
                absorb(await snap())
            except Exception:
                break

        try:
            await page.evaluate(
                """() => {
                    window.scrollTo(0,0);
                    document.querySelectorAll(
                        '[class*="market-list"],[class*="MarketList"],main'
                    ).forEach(c => c.scrollTop = 0);
                }"""
            )
        except Exception:
            pass

        result = "\n".join(lines)
        has_ou = bool(
            re.search(
                r"1st\s*half\s*-?\s*over/under|2nd\s*half\s*-?\s*over/under|"
                r"\bover/under\b|\bover\s+\d+(\.\d+)?\b",
                result,
                re.I,
            )
        )
        logger.info(
            f"[CAPTURE] lines={len(lines)} len={len(result)} has_over_section={has_ou}"
        )
        return result[:10000]

    async def _quick_score(self, page: Page) -> tuple:
        text = await self._page_text(page, 3000)
        for pat in (
            r"\b(\d{1,2})\s*:\s*(\d{1,2})\b",
            r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b",
        ):
            for m in re.finditer(pat, text):
                try:
                    h, a = int(m.group(1)), int(m.group(2))
                    if 0 <= h <= 15 and 0 <= a <= 15:
                        return h, a
                except Exception:
                    continue
        return None, None

    async def _detect_period(self, page: Page) -> str:
        text = (await self._page_text(page, 4000)).lower()
        m = re.search(r"\b(\d{1,2})\s*'\s*(?:\+\d+)?\b", text)
        if not m:
            m = re.search(r"\b(\d{1,2})\s*:\s*\d{1,2}\b", text)
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

    async def _match_ended(self, page: Page) -> bool:
        text = (await self._page_text(page, 4000)).lower()
        if not re.search(
            r"\b(full\s*time|match\s*ended|match\s*finished)\b", text
        ):
            return False
        if re.search(
            r"\b(1st\s*half|2nd\s*half|live|ongoing)\b|\b\d{1,2}\s*'",
            text,
        ):
            return False
        return True

    async def _selection_unavailable(self, page: Page) -> bool:
        try:
            slip = page.locator(
                "[class*='betslip'], [class*='Betslip'], "
                "[class*='bet-slip'], #betslip"
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

    async def _betslip_has_over(self, page: Page, line: float) -> bool:
        try:
            roots = page.locator(
                "[class*='betslip'], [class*='Betslip'], "
                "[class*='bet-slip'], #betslip"
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

    async def _confirm_visible(self, page: Page) -> bool:
        try:
            loc = page.locator("button:has-text('Confirm')")
            return await loc.count() > 0 and await loc.first.is_visible()
        except Exception:
            return False

    async def _click_all_tab(self, page: Page):
        for sel in (
            "text=All",
            "button:has-text('All')",
            "[role='tab']:has-text('All')",
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
                await page.evaluate(
                    """() => {
                        const sels = ['[class*="market-list"]','main'];
                        for (const s of sels) {
                            for (const c of document.querySelectorAll(s)) {
                                if (c.scrollHeight > c.clientHeight + 40) {
                                    c.scrollBy(0, 500);
                                    return;
                                }
                            }
                        }
                        window.scrollBy(0, 500);
                    }"""
                )
                await asyncio.sleep(0.25)
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
        """Click Over {line} under real Over/Under header — many scroll passes."""
        await self._click_all_tab(page)
        await asyncio.sleep(0.3)
        line_str = f"{line:g}"
        patterns = [
            f"text=/^Over\\s*{re.escape(line_str)}$/i",
            f"text=/Over\\s*{re.escape(line_str)}/i",
            f"text='Over {line_str}'",
            f"text='Over {line}'",
        ]

        for _pass in range(10):
            for pat in patterns:
                try:
                    locs = page.locator(pat)
                    count = await locs.count()
                    for i in range(min(count, 18)):
                        el = locs.nth(i)
                        try:
                            if not await el.is_visible():
                                continue
                            handle = await el.element_handle()
                            if not handle:
                                continue
                            ctx = await page.evaluate(
                                """(el) => {
                                    let n = el, best = '';
                                    for (let i = 0; i < 8 && n; i++) {
                                        n = n.parentElement;
                                        if (n && n.innerText) {
                                            const t = n.innerText.slice(0, 700);
                                            if (t.length > best.length) best = t;
                                        }
                                    }
                                    return best;
                                }""",
                                handle,
                            )
                            ctx_l = (ctx or "").lower()
                            if _FORBIDDEN_CTX.search(ctx_l):
                                continue
                            if any(f in ctx_l for f in forbid_kw):
                                continue
                            if not re.search(
                                r"over\s*/\s*under|over/under|"
                                r"1st\s*half.*over|2nd\s*half.*over",
                                ctx_l,
                            ):
                                if "over" not in ctx_l:
                                    continue
                            if require_kw and not any(r in ctx_l for r in require_kw):
                                if label in ("1H", "2H"):
                                    continue
                            await el.scroll_into_view_if_needed(timeout=1500)
                            await el.click(timeout=2000)
                            await asyncio.sleep(0.55)
                            if await self._betslip_has_over(page, line):
                                logger.success(f"[ARM] clicked {label} Over {line}")
                                return True
                            await el.click(timeout=1200)
                            await asyncio.sleep(0.45)
                            if await self._betslip_has_over(page, line):
                                logger.success(
                                    f"[ARM] clicked {label} Over {line} (2nd)"
                                )
                                return True
                        except Exception:
                            continue
                except Exception:
                    continue
            await self._scroll_markets(page, 4)

        logger.warning(f"[ARM] could not put {label} Over {line} on betslip")
        return False

    async def _set_stake(self, page: Page, stake: float) -> bool:
        stake_str = f"{stake:.0f}" if stake >= 10 else f"{stake:.2f}"
        for sel in (
            "[class*='betslip'] input[type='text']",
            "[class*='betslip'] input[type='number']",
            "[class*='Betslip'] input",
            "#betslip input",
            "input[placeholder*='Stake' i]",
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
        try:
            await page.keyboard.press("Control+a")
            await page.keyboard.type(stake_str, delay=18)
            return True
        except Exception:
            return False

    async def _click_place_bet(self, page: Page) -> bool:
        for sel in (
            "button:has-text('Place Bet')",
            "button:has-text('Place bet')",
            "text=Place Bet",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=2000)
                    await asyncio.sleep(0.4)
                    return True
            except Exception:
                continue
        return False

    async def _click_confirm(self, page: Page) -> bool:
        for sel in (
            "button:has-text('Confirm')",
            "text=Confirm",
            "button:has-text('CONFIRM')",
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
            "button:has-text('Accept Changes')",
            "button:has-text('Accept changes')",
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

    async def _click_rebet(self, page: Page) -> bool:
        for sel in (
            "button:has-text('Rebet')",
            "button:has-text('Re-bet')",
            "text=Rebet",
            "text=Re-bet",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.5)
                    return True
            except Exception:
                continue
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

    async def _clear_betslip(self, page: Page):
        for sel in (
            "button:has-text('Remove')",
            "[class*='betslip'] [class*='remove']",
            "button:has-text('Clear')",
        ):
            try:
                locs = page.locator(sel)
                for i in range(min(await locs.count(), 5)):
                    try:
                        await locs.nth(i).click(timeout=600)
                        await asyncio.sleep(0.12)
                    except Exception:
                        pass
            except Exception:
                continue

    async def _verify_bet_result(self, page: Page) -> Tuple[bool, str]:
        await asyncio.sleep(1.1)
        text = (await self._page_text(page, 5000)).lower()
        for p in (
            r"bet\s*reject",
            r"\brejected\b",
            r"\bfailed\b",
            r"\bunavailable\b",
            r"\bsuspended\b",
            r"insufficient",
            r"odds\s*changed",
            r"accept\s*changes",
            r"market\s*closed",
            r"betting\s*closed",
        ):
            if re.search(p, text):
                return False, f"fail:{p}"
        for p in (
            r"bet\s*placed",
            r"bet\s*accepted",
            r"\bsuccess\b",
            r"placed\s*successfully",
            r"bet\s*id",
        ):
            if re.search(p, text):
                return True, f"ok:{p}"
        if not await self._confirm_visible(page):
            return True, "confirm_gone"
        return False, "confirm_still_visible"

    async def _maybe_accept_changes(self, page: Page, watch: MatchWatch):
        try:
            if await page.locator("button:has-text('Accept Changes')").count() > 0:
                await self._click_accept_changes(page)
                odds = await self._read_odds(page)
                if odds > 1.01:
                    max_profit = float(Config.MAX_PROFIT_PER_BET)
                    stake = max(10.0, max_profit / (odds - 1.0))
                    watch.odds_over = odds
                    watch.stake_over = round(stake, 2)
                    await self._set_stake(page, watch.stake_over)
                watch.confirm_armed = await self._confirm_visible(page)
        except Exception:
            pass

    async def cashout_disallowed(self, page: Page, description: str = "") -> bool:
        try:
            for sel in (
                "text=Cashout",
                "text=Cash Out",
                "button:has-text('Cashout')",
            ):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=1500)
                        await asyncio.sleep(0.4)
                        break
                except Exception:
                    continue
            for sel in (
                "button:has-text('Cash Out')",
                "button:has-text('Cashout')",
            ):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click(timeout=1500)
                        await asyncio.sleep(0.3)
                        await self._click_confirm(page)
                        await self._tg(f"💰 Cashout attempted {description}")
                        return True
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"[CASHOUT] {e}")
        return False

    async def _stop_no_market(self, mid: str, watch: MatchWatch, reason: str):
        await self._tg(
            f"🛑 NO VALID OVER MARKET\n"
            f"{watch.home_team} vs {watch.away_team}\n"
            f"Score {watch.home_score}-{watch.away_score} | {watch.period}\n"
            f"Reason: {reason}\n"
            f"(Tried 3 full scroll+scan cycles)\n"
            f"Stopped watching this match.\n"
            f"Send /slow for another game."
        )
        await self.clear_match(mid)

    async def _finish_arm(
        self,
        page: Page,
        watch: MatchWatch,
        line: float,
        pm: str,
        label_prefix: str,
    ) -> bool:
        require_kw, forbid_kw = _require_forbid_for_period_market(pm)
        candidate = f"{label_prefix} Over {line:g}"
        if not _is_allowed_market_label(candidate):
            await self._tg(f"🛑 SAFETY BLOCK\n{candidate}\nNOT betting")
            return False

        await self._clear_betslip(page)
        await asyncio.sleep(0.2)

        clicked = await self._find_and_click_over(
            page, line, require_kw, forbid_kw, label_prefix
        )
        if not clicked and pm in ("1H", "2H"):
            clicked = await self._find_and_click_over(
                page,
                line,
                [],
                [
                    "1st half",
                    "first half",
                    "2nd half",
                    "second half",
                    "rest of",
                ],
                "FT",
            )
            if clicked:
                label_prefix, pm = "FT", "FT"
                candidate = f"FT Over {line:g}"

        if not clicked or not await self._betslip_has_over(page, line):
            return False

        watch.active_market = candidate
        watch.target_line = line
        watch.period = pm

        odds = await self._read_odds(page)
        if odds <= 1.01:
            odds = 1.5
        watch.odds_over = odds
        max_profit = float(Config.MAX_PROFIT_PER_BET)
        watch.stake_over = round(max(10.0, max_profit / (odds - 1.0)), 2)

        if not await self._set_stake(page, watch.stake_over):
            await self._tg("❌ Stake failed")
            return False
        await self._click_accept_changes(page)
        if not await self._click_place_bet(page):
            await self._tg("❌ Place Bet failed")
            return False

        await asyncio.sleep(0.4)
        armed = await self._confirm_visible(page)
        watch.confirm_armed = armed
        if armed:
            logger.success(f"[ARM] {candidate} @{odds} stake={watch.stake_over}")
            await self._tg(
                f"✅ ARMED\n"
                f"{watch.home_team} vs {watch.away_team}\n"
                f"Score {watch.home_score}-{watch.away_score} | {pm}\n"
                f"{candidate}\n"
                f"Odds {odds} | Stake {watch.stake_over}\n"
                f"Waiting for goal → Confirm"
            )
        else:
            await self._tg(
                f"⚠️ Place Bet done but Confirm not visible\n{candidate}"
            )
        return armed

    async def ai_arm_from_plan(
        self,
        page: Page,
        plan: Optional[dict],
        watch: MatchWatch,
        mid: str = "",
    ) -> bool:
        """Deep scan up to 3 full cycles before NO VALID / stop watching."""
        if page.is_closed():
            return False
        if await self._match_ended(page):
            await self._tg("⏹ Match ended — not arming")
            if mid:
                await self.clear_match(mid)
            return False

        max_profit = float(Config.MAX_PROFIT_PER_BET)
        last_reason = "no valid Over market"

        for attempt in range(1, 4):
            await self._click_all_tab(page)
            await asyncio.sleep(0.5)
            text = await self._capture_full_market_text(page, steps=22)

            h, a = await self._quick_score(page)
            if h is not None and a is not None:
                watch.home_score, watch.away_score = h, a
            period = await self._detect_period(page)
            watch.period = period

            balance = 0.0
            try:
                bm = re.search(
                    r"(?:NGN|₦)\s*([\d,]+(?:\.\d+)?)",
                    await self._page_text(page, 2000),
                )
                if bm:
                    balance = float(bm.group(1).replace(",", ""))
            except Exception:
                pass

            logger.info(
                f"[ARM] attempt {attempt}/3 period={period} "
                f"score={watch.home_score}-{watch.away_score} "
                f"text_len={len(text)}"
            )

            try:
                plan = plan_over_market(
                    text,
                    watch.home_team,
                    watch.away_team,
                    watch.home_score,
                    watch.away_score,
                    period,
                    max_profit,
                    balance,
                )
            except Exception as e:
                plan = {"market_found": False, "error": str(e)}

            if not plan.get("market_found"):
                last_reason = (
                    plan.get("error") or plan.get("reason") or "Groq found no Over"
                )
                logger.warning(f"[ARM] attempt {attempt}: {last_reason}")
                await self._scroll_markets(page, 10)
                await asyncio.sleep(0.9)
                continue

            try:
                sh, sa = int(plan["score_home"]), int(plan["score_away"])
                if 0 <= sh <= 15 and 0 <= sa <= 15:
                    watch.home_score, watch.away_score = sh, sa
            except Exception:
                pass

            pm = (plan.get("period_market") or "FT").upper()
            line = float(watch.home_score + watch.away_score) + 0.5
            label = {"1H": "1H", "2H": "2H"}.get(pm, "FT")

            ok = await self._finish_arm(page, watch, line, pm, label)
            if ok:
                return True

            last_reason = f"Could not click {label} Over {line:g} onto betslip"
            logger.warning(f"[ARM] attempt {attempt}: {last_reason}")
            await self._clear_betslip(page)
            await self._scroll_markets(page, 10)
            await asyncio.sleep(0.9)

        if mid:
            await self._stop_no_market(mid, watch, last_reason)
        else:
            await self._tg(
                f"⚠️ Arm failed after 3 full scans\n"
                f"{watch.home_team} vs {watch.away_team}\n{last_reason}"
            )
        watch.confirm_armed = False
        return False

    async def _try_rebet(self, page: Page, watch: MatchWatch) -> bool:
        if not await self._click_rebet(page):
            return False
        await asyncio.sleep(0.4)
        if watch.stake_over > 0:
            await self._set_stake(page, watch.stake_over)
        await self._click_accept_changes(page)
        if not await self._click_place_bet(page):
            return False
        await asyncio.sleep(0.4)
        armed = await self._confirm_visible(page)
        watch.confirm_armed = armed
        if armed:
            await self._tg(
                f"🔁 REBET ARMED\n"
                f"{watch.home_team} vs {watch.away_team}\n"
                f"{watch.active_market}\n"
                f"Odds {watch.odds_over} | Stake {watch.stake_over}"
            )
        return armed

    async def _watch_loop(self, mid: str):
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
                    await self._clear_betslip(page)
                    await self._tg(
                        f"🏁 FULL TIME — {watch.home_team} vs {watch.away_team}\n"
                        f"Send /slow for a new match."
                    )
                    watch.running = False
                    break

                h, a = await self._quick_score(page)
                if h is not None and a is not None and (h + a) < watch.total_goals:
                    await self.cashout_disallowed(
                        page, f"{watch.home_score}-{watch.away_score}→{h}-{a}"
                    )
                    watch.home_score, watch.away_score = h, a
                    watch.confirm_armed = False
                    await self.ai_arm_from_plan(page, None, watch, mid=mid)

                period_hint = await self._detect_period(page)
                if period_hint == "HT":
                    watch.ht_seen = True
                    watch.confirm_armed = False
                    if not watch.ht_alerted:
                        watch.ht_alerted = True
                        await self._tg(
                            f"⏸ HALF TIME — {watch.home_team} vs {watch.away_team}\n"
                            f"Will re-arm on 2nd half."
                        )
                if (
                    watch.ht_seen
                    and period_hint == "2H"
                    and watch.last_period_hint in ("HT", "1H")
                ):
                    watch.ht_alerted = False
                    await self._tg("🔄 2nd half — re-arming")
                    await self.ai_arm_from_plan(page, None, watch, mid=mid)

                if await self._selection_unavailable(page):
                    await self._clear_betslip(page)
                    watch.confirm_armed = False
                    await self._tg("⚠️ Market Unavailable — re-arming via Groq")
                    await self.ai_arm_from_plan(page, None, watch, mid=mid)

                if await self._is_suspended(page):
                    watch.last_period_hint = period_hint
                    await asyncio.sleep(1.5)
                    continue

                await self._maybe_accept_changes(page, watch)
                watch.last_period_hint = period_hint
                await asyncio.sleep(2.0)
            except Exception as e:
                logger.error(f"[WATCH] {mid}: {e}")
                await asyncio.sleep(1.5)

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
        ids = [str(mid)] if mid else list(self.watches.keys())
        for i in ids:
            w = self.watches.pop(i, None)
            if not w:
                continue
            w.running = False
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
            if w.task and not w.task.done():
                w.task.cancel()

    async def confirm_goal_click(self, mid: str) -> BetResult:
        w = self.watches.get(str(mid))
        if not w or not w.page or w.page.is_closed():
            await self._tg(f"❌ BET SKIPPED\n{mid}\npage/watch missing")
            return BetResult.FAILED
        if not w.confirm_armed:
            await self._tg(
                f"⚠️ Goal but not armed — skip\n"
                f"{w.home_team} vs {w.away_team}\nNO BET PLACED"
            )
            return BetResult.ABORTED_SAFETY
        if await self._is_suspended(w.page) or await self._selection_unavailable(
            w.page
        ):
            await self._tg(
                f"🔒 Goal + SportyBet locked — stop watching\n"
                f"{w.home_team} vs {w.away_team}\nNO BET PLACED"
            )
            await self.clear_match(mid)
            return BetResult.ABORTED_SAFETY

        if not await self._click_confirm(w.page):
            await self._tg(
                f"❌ BET NOT PLACED\n"
                f"{w.home_team} vs {w.away_team}\n"
                f"{w.active_market}\nConfirm click failed"
            )
            return BetResult.FAILED

        accepted, detail = await self._verify_bet_result(w.page)
        w.confirm_armed = False

        if accepted:
            await self._tg(
                f"✅ BET ACCEPTED\n"
                f"{w.home_team} vs {w.away_team}\n"
                f"{w.active_market}\n"
                f"Odds {w.odds_over} | Stake {w.stake_over}\n"
                f"Detail: {detail}"
            )
            await asyncio.sleep(0.6)
            if not await self._try_rebet(w.page, w):
                await self.ai_arm_from_plan(w.page, None, w, mid=mid)
            return BetResult.SUCCESS

        await self._tg(
            f"❌ BET NOT ACCEPTED\n"
            f"{w.home_team} vs {w.away_team}\n"
            f"{w.active_market}\n"
            f"Detail: {detail}"
        )
        await self.ai_arm_from_plan(w.page, None, w, mid=mid)
        return BetResult.FAILED
