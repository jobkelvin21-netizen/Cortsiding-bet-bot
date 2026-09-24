"""
BetExecutor — Groq plans; Playwright clicks real Over/Under only.
Scroll accumulates markets; fallback arms if page has Over lines.
Confirm ONLY in confirm_goal_click() on Bet365 goal.
Every bet result is always sent to Telegram.
"""

import asyncio
import re
import time
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
            ["2nd half", "second half"],
        )
    if pm == "2H":
        return (
            ["2nd half", "second half", "2h"],
            ["1st half", "first half"],
        )
    return (
        [],
        ["1st half", "first half", "2nd half", "second half", "half time"],
    )


_FORBIDDEN_CTX = re.compile(
    r"rest\s*of|handicap|next\s*goal|nth\s*goal|goalscorer|correct\s*score|"
    r"double\s*chance|draw\s*no\s*bet|both\s*teams|btts|odd/even|odd\s*even|"
    r"team\s*total|home\s*total|away\s*total|corners|bookings|cards",
    re.I,
)


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

    def _filter_sidebar_content(self, text: str) -> str:
        lines = text.split("\n")
        filtered = []
        skip_patterns = [
            r"^Sports\( ", r"^Games \)", r"^Live Betting\( ", r"^Scheduled \)", r"^Virtuals$",
            r"^Jackpot\( ", r"^Livescore \)", r"^Results\( ", r"^Promotions \)", r"^Sporty Loyalty$",
            r"^App\( ", r"^GMT[+-]", r"^Multi View \)", r"^Single View\( ", r"^Schedule \)",
            r"^Football\s*\(\d+\)\( ", r"^vFootball\s*\(\d+\) \)", r"^Basketball\s*\(\d+\)$",
            r"^Tennis\s*\(\d+\)\( ", r"^eFootball\s*\(\d+\) \)", r"^Table Tennis\s*\(\d+\)$",
            r"^Deposit\( ", r"^Bet History \)", r"^My Account$",
        ]
        for line in lines:
            s = line.strip()
            if not s or len(s) < 2:
                continue
            if any(re.match(p, s, re.I) for p in skip_patterns):
                continue
            filtered.append(s)
        return "\n".join(filtered)

    async def _capture_full_market_text(self, page: Page, steps: int = 14) -> str:
        await self._click_all_tab(page)
        await asyncio.sleep(0.8)

        seen_set = set()
        seen_lines: List[str] = []

        async def snapshot() -> str:
            try:
                for sel in (
                    "[class*='event-market']",
                    "[class*='EventMarket']",
                    "[class*='market-list']",
                    "[class*='MarketList']",
                    "[class*='m-market']",
                    "main",
                    "[class*='match-detail']",
                    "[class*='event-detail']",
                ):
                    try:
                        loc = page.locator(sel)
                        n = await loc.count()
                        if n == 0:
                            continue
                        parts = []
                        for i in range(min(n, 4)):
                            try:
                                t = await loc.nth(i).inner_text(timeout=1500)
                                if t and len(t) > 40:
                                    parts.append(t)
                            except Exception:
                                pass
                        if parts:
                            return self._filter_sidebar_content("\n".join(parts))
                    except Exception:
                        continue
                t = await page.inner_text("body", timeout=2500)
                return self._filter_sidebar_content(t or "")
            except Exception:
                return ""

        def absorb(blob: str):
            for line in (blob or "").split("\n"):
                line = line.strip()
                if not line or len(line) > 220:
                    continue
                key = line.lower()
                if key not in seen_set:
                    seen_set.add(key)
                    seen_lines.append(line)

        absorb(await snapshot())

        for i in range(steps):
            try:
                await page.evaluate(
                    """() => {
                        const sels = [
                            '[class*="market-list"]', '[class*="MarketList"]',
                            '[class*="event-market"]', '[class*="m-market"]',
                            'main', '[class*="match-detail"]', '[class*="content"]'
                        ];
                        for (const s of sels) {
                            for (const c of document.querySelectorAll(s)) {
                                if (c.scrollHeight > c.clientHeight + 40) {
                                    c.scrollBy(0, 450);
                                    return true;
                                }
                            }
                        }
                        window.scrollBy(0, 450);
                        return false;
                    }"""
                )
                await asyncio.sleep(0.45)
                absorb(await snapshot())
            except Exception as e:
                logger.debug(f"[SCROLL] {i}: {e}")
                break

        try:
            await page.evaluate(
                """() => {
                    window.scrollTo(0, 0);
                    document.querySelectorAll(
                        '[class*="market-list"], [class*="MarketList"], main'
                    ).forEach(c => c.scrollTop = 0);
                }"""
            )
        except Exception:
            pass

        result = "\n".join(seen_lines)
        ou = "over/under" in result.lower() or bool(
            re.search(r"\bover\s+\d+(\.\d+)?\b", result, re.I)
        )
        logger.info(
            f"[CAPTURE] lines={len(seen_lines)} len={len(result)} has_over={ou}"
        )
        return result[:10000]

    def _page_has_over_lines(self, text: str) -> List[float]:
        lines = []
        for m in re.finditer(r"\bOver\s+(\d+(?:\.\d+)?)\b", text or "", re.I):
            try:
                v = float(m.group(1))
                if v not in lines:
                    lines.append(v)
            except Exception:
                pass
        return lines

    async def _quick_score_check(self, page: Page) -> tuple:
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

        minute = None
        m = re.search(r"\b(\d{1,2})\s*'\s*(?:\+\d+)?\b", text)
        if not m:
            m = re.search(r"\b(\d{1,2})\s*:\s*\d{1,2}\b", text)
        if m:
            try:
                minute = int(m.group(1))
            except Exception:
                minute = None

        if minute is not None:
            if 1 <= minute <= 45:
                return "1H"
            if 46 <= minute <= 120:
                return "2H"

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

        has_ended = bool(
            re.search(
                r"\b(full\s*time|match\s*ended|match\s*finished|ft\s*-\s*ended)\b",
                text,
            )
        )
        if not has_ended:
            return False

        has_live = bool(
            re.search(
                r"\b(1st\s*half|2nd\s*half|first\s*half|second\s*half|live|ongoing)\b"
                r"|\b\d{1,2}\s*'\s*(?:\+\d+)?"
                r"|\b\d{1,2}\s*:\s*\d{2}\b",
                text,
            )
        )
        if has_live:
            return False

        return True

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
        try:
            roots = page.locator(
                "[class*='betslip'], [class*='Betslip'], [class*='bet-slip'], "
                "#betslip, [data-testid*='betslip']"
            )
            n = await roots.count()
            if n == 0:
                return False
            needle = f"over {line:g}".lower()
            needle2 = f"over {line}".lower()
            for i in range(min(n, 3)):
                t = (await roots.nth(i).inner_text(timeout=1200) or "").lower()
                if needle in t or needle2 in t or f"o {line:g}" in t:
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
            "a:has-text('All')",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.35)
                    return
            except Exception:
                continue

    async def _scroll_markets(self, page: Page, steps: int = 8):
        for _ in range(steps):
            try:
                await page.evaluate(
                    """() => {
                        const sels = [
                            '[class*="market-list"]', '[class*="MarketList"]',
                            '[class*="event-market"]', 'main'
                        ];
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
        await self._click_all_tab(page)
        await asyncio.sleep(0.3)

        line_str = f"{line:g}"
        patterns = [
            f"text=/^Over\\s*{re.escape(line_str)}$/i",
            f"text=/Over\\s*{re.escape(line_str)}/i",
            f"text='Over {line_str}'",
            f"text='Over {line}'",
        ]

        for _scroll in range(6):
            for pat in patterns:
                try:
                    locs = page.locator(pat)
                    count = await locs.count()
                    for i in range(min(count, 15)):
                        el = locs.nth(i)
                        try:
                            if not await el.is_visible():
                                continue
                            handle = await el.element_handle()
                            if not handle:
                                continue
                            ctx = await page.evaluate(
                                """(el) => {
                                    let n = el;
                                    let best = '';
                                    for (let i = 0; i < 8 && n; i++) {
                                        n = n.parentElement;
                                        if (n && n.innerText) {
                                            const t = n.innerText.slice(0, 600);
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

                            is_ou_context = bool(
                                re.search(
                                    r"over\s*/\s*under|over/under|match\s*goals|"
                                    r"total\s*goals|goals\s*over|goal\s*line",
                                    ctx_l,
                                )
                            )
                            has_over_btn = bool(
                                re.search(
                                    rf"\bover\s*{re.escape(line_str)}\b", ctx_l
                                )
                            )
                            if not is_ou_context and not has_over_btn:
                                continue

                            if require_kw and not any(r in ctx_l for r in require_kw):
                                if label in ("1H", "2H"):
                                    continue

                            await el.scroll_into_view_if_needed(timeout=1500)
                            await el.click(timeout=2000)
                            await asyncio.sleep(0.55)
                            if await self._betslip_has_selection(page, line):
                                logger.success(f"[ARM] clicked {label} Over {line}")
                                return True
                            await el.click(timeout=1500)
                            await asyncio.sleep(0.45)
                            if await self._betslip_has_selection(page, line):
                                logger.success(
                                    f"[ARM] clicked {label} Over {line} (2nd)"
                                )
                                return True
                        except Exception:
                            continue
                except Exception:
                    continue
            await self._scroll_markets(page, 3)

        logger.warning(f"[ARM] could not put {label} Over {line} on betslip")
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
                    await el.type(stake_str, delay=20)
                    await asyncio.sleep(0.15)
                    return True
            except Exception:
                continue
        try:
            await page.keyboard.press("Control+a")
            await page.keyboard.type(stake_str, delay=20)
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
                    await asyncio.sleep(0.45)
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
            "button:has-text('Accept')",
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.35)
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
                        await locs.nth(i).click(timeout=700)
                        await asyncio.sleep(0.15)
                    except Exception:
                        pass
            except Exception:
                continue

    async def _verify_bet_result(self, page: Page) -> Tuple[bool, str]:
        """
        After Confirm click — decide accepted vs not.
        Always used so Telegram always gets a result.
        Returns (accepted: bool, detail: str).
        """
        await asyncio.sleep(1.2)
        text = ""
        try:
            text = (await self._page_text(page, 5000)).lower()
        except Exception:
            text = ""

        # Explicit failure signals
        fail_patterns = [
            r"\bbet\s*reject",
            r"\brejected\b",
            r"\bfailed\b",
            r"\berror\b",
            r"\bunavailable\b",
            r"\bsuspended\b",
            r"\binsufficient\b",
            r"\bnot\s*enough\b",
            r"\bodds\s*changed\b",
            r"\baccept\s*changes\b",
            r"\bstake\s*too\b",
            r"\bminimum\s*stake\b",
            r"\bmaximum\s*stake\b",
            r"\bmarket\s*closed\b",
            r"\bbetting\s*closed\b",
        ]
        for p in fail_patterns:
            if re.search(p, text):
                return False, f"page shows: {p}"

        # Explicit success signals
        ok_patterns = [
            r"\bbet\s*placed\b",
            r"\bbet\s*accepted\b",
            r"\bsuccess\b",
            r"\bplaced\s*successfully\b",
            r"\byour\s*bet\b",
            r"\bbet\s*id\b",
            r"\bconfirmed\b",
        ]
        for p in ok_patterns:
            if re.search(p, text):
                return True, f"page shows success ({p})"

        # Confirm gone + no failure text → treat as accepted
        try:
            still_confirm = await self._confirm_visible(page)
        except Exception:
            still_confirm = False

        if not still_confirm:
            # Betslip empty or no longer showing the Over selection is a good sign
            return True, "Confirm gone — treated as accepted"

        # Still on Confirm / dialog → not accepted
        return False, "Confirm still visible — not accepted"

    async def _maybe_accept_changes(self, page: Page, watch: MatchWatch):
        try:
            if await page.locator("button:has-text('Accept Changes')").count() > 0:
                await self._click_accept_changes(page)
                odds = await self._read_odds_from_slip(page)
                if odds > 1.01:
                    max_profit = float(Config.MAX_PROFIT_PER_BET)
                    stake = max_profit / (odds - 1.0)
                    watch.odds_over = odds
                    watch.stake_over = round(max(10.0, stake), 2)
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
                "[class*='cashout']",
            ):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=1500)
                        await asyncio.sleep(0.45)
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
                        await asyncio.sleep(0.35)
                        await self._click_confirm(page)
                        await self._tg(f"💰 Cashout attempted {description}")
                        return True
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"[CASHOUT] {e}")
        return False

    # ─── arm ───────────────────────────────────────────────────

    async def _finish_arm(
        self,
        page: Page,
        watch: MatchWatch,
        line: float,
        pm: str,
        label_prefix: str,
    ) -> bool:
        require_kw, forbid_kw = _require_forbid_for_period_market(pm)
        candidate_label = f"{label_prefix} Over {line:g}"

        if not _is_allowed_market_label(candidate_label):
            await self._tg(f"🛑 SAFETY BLOCK\n{candidate_label}\nNOT betting")
            return False

        await self._clear_betslip(page)
        await asyncio.sleep(0.25)

        clicked = await self._find_and_click_over(
            page, line, require_kw, forbid_kw, label_prefix
        )
        if not clicked and pm in ("1H", "2H"):
            clicked = await self._find_and_click_over(
                page,
                line,
                [],
                ["1st half", "first half", "2nd half", "second half"],
                "FT",
            )
            if clicked:
                label_prefix = "FT"
                candidate_label = f"FT Over {line:g}"
                pm = "FT"

        if not clicked or not await self._betslip_has_selection(page, line):
            await self._tg(
                f"❌ Market not on betslip\n"
                f"{candidate_label}\n"
                f"Score {watch.home_score}-{watch.away_score}\n"
                f"NOT betting"
            )
            watch.confirm_armed = False
            return False

        watch.active_market = candidate_label
        watch.target_line = line
        watch.period = pm

        odds = await self._read_odds_from_slip(page)
        if odds <= 1.01:
            odds = 1.50
        watch.odds_over = odds
        max_profit = float(Config.MAX_PROFIT_PER_BET)
        stake = max_profit / (odds - 1.0) if odds > 1.0 else 10.0
        watch.stake_over = round(max(10.0, stake), 2)

        if not await self._set_stake(page, watch.stake_over):
            await self._tg("❌ Stake failed")
            watch.confirm_armed = False
            return False

        await self._click_accept_changes(page)
        if not await self._click_place_bet(page):
            await self._tg("❌ Place Bet failed")
            watch.confirm_armed = False
            return False

        await asyncio.sleep(0.4)
        armed = await self._confirm_visible(page)
        watch.confirm_armed = armed

        if armed:
            logger.success(
                f"[ARM] {watch.active_market} stake={watch.stake_over} @{watch.odds_over}"
            )
            await self._tg(
                f"✅ ARMED\n"
                f"{watch.home_team} vs {watch.away_team}\n"
                f"Score {watch.home_score}-{watch.away_score} | {pm}\n"
                f"{watch.active_market}\n"
                f"Odds {watch.odds_over} | Stake {watch.stake_over}\n"
                f"Waiting for goal → Confirm"
            )
        else:
            await self._tg(
                f"⚠️ Placed but Confirm not visible\n{watch.active_market}"
            )
        return armed

    async def ai_arm_from_plan(
        self,
        page: Page,
        plan: Optional[dict],
        watch: MatchWatch,
    ) -> bool:
        if page.is_closed():
            return False

        if await self._match_ended(page):
            await self._tg("⏹ Match ended — not arming")
            return False

        text = await self._capture_full_market_text(page)
        over_lines = self._page_has_over_lines(text)
        logger.info(f"[ARM] over lines on page: {over_lines}")

        h, a = await self._quick_score_check(page)
        if h is not None and a is not None:
            watch.home_score, watch.away_score = h, a

        period = await self._detect_period(page)
        watch.period = period

        balance = 0.0
        try:
            quick = await self._page_text(page, 2000)
            bm = re.search(r"(?:NGN|₦)\s*([\d,]+(?:\.\d+)?)", quick)
            if bm:
                balance = float(bm.group(1).replace(",", ""))
        except Exception:
            pass

        max_profit = float(Config.MAX_PROFIT_PER_BET)

        if plan is None:
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
                logger.warning(f"[GROQ] {e}")
                plan = {"market_found": False, "error": str(e)}

        if plan.get("market_found"):
            try:
                sh = int(plan.get("score_home"))
                sa = int(plan.get("score_away"))
                if 0 <= sh <= 15 and 0 <= sa <= 15:
                    watch.home_score, watch.away_score = sh, sa
            except Exception:
                pass
            pm = (plan.get("period_market") or "FT").upper()
            try:
                line = float(plan.get("line"))
            except Exception:
                line = watch.over_line
            if over_lines and line not in over_lines:
                target = watch.over_line
                line = target if target in over_lines else over_lines[0]
            label_prefix = {"1H": "1H", "2H": "2H"}.get(pm, "FT")
            return await self._finish_arm(page, watch, line, pm, label_prefix)

        if over_lines:
            line = watch.over_line
            if line not in over_lines:
                above = [x for x in over_lines if x >= line]
                line = min(above) if above else max(over_lines)
            pm = period if period in ("1H", "2H") else "FT"
            if period == "HT":
                pm = "FT"
            label_prefix = {"1H": "1H", "2H": "2H"}.get(pm, "FT")
            logger.warning(
                f"[ARM] Groq empty — fallback {label_prefix} Over {line} from page"
            )
            await self._tg(
                f"⚠️ Groq had no plan — using page Over {line:g} ({label_prefix})"
            )
            return await self._finish_arm(page, watch, line, pm, label_prefix)

        msg = plan.get("error") or plan.get("reason") or "no valid market"
        logger.warning(f"[AI ARM] {msg}")
        await self._tg(
            f"⚠️ Arm failed: {msg}\n"
            f"{watch.home_team} vs {watch.away_team}\n"
            f"NOT betting"
        )
        watch.confirm_armed = False
        return False

    # ─── watch loop ────────────────────────────────────────────

    async def _watch_loop(self, mid: str):
        watch = self.watches.get(mid)
        if not watch:
            return
        page = watch.page
        watch.running = True
        logger.info(f"[WATCH] start {mid}")

        await self.ai_arm_from_plan(page, None, watch)

        while watch.running:
            try:
                if page.is_closed():
                    break

                if await self._match_ended(page):
                    await self._clear_betslip(page)
                    await self._tg(
                        f"🏁 FULL TIME — {watch.home_team} vs {watch.away_team}\n"
                        f"Send /slow for a new match."
                    )
                    watch.running = False
                    break

                h, a = await self._quick_score_check(page)
                if h is not None and a is not None and (h + a) < watch.total_goals:
                    await self.cashout_disallowed(
                        page, f"{watch.home_score}-{watch.away_score}→{h}-{a}"
                    )
                    watch.home_score, watch.away_score = h, a
                    watch.confirm_armed = False
                    await self.ai_arm_from_plan(page, None, watch)

                period_hint = await self._detect_period(page)

                if period_hint == "HT":
                    watch.ht_seen = True
                    watch.confirm_armed = False
                    if not watch.ht_alerted:
                        watch.ht_alerted = True
                        await self._tg(
                            f"⏸ HALF TIME — {watch.home_team} vs {watch.away_team}\n"
                            f"Will auto re-arm on 2nd half."
                        )

                if (
                    watch.ht_seen
                    and period_hint == "2H"
                    and watch.last_period_hint in ("HT", "1H")
                ):
                    watch.ht_alerted = False
                    await self._tg("🔄 2nd half started — re-arming")
                    await self.ai_arm_from_plan(page, None, watch)

                if await self._selection_unavailable(page):
                    await self._clear_betslip(page)
                    watch.confirm_armed = False
                    await self._tg("⚠️ Market Unavailable — re-arming")
                    await self.ai_arm_from_plan(page, None, watch)

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
                    await asyncio.wait_for(asyncio.shield(w.task), timeout=1.5)
                except Exception:
                    pass
            try:
                if w.page and not w.page.is_closed():
                    await self._clear_betslip(w.page)
                    await w.page.close()
            except Exception:
                pass
        await self._tg(
            f"🧹 Cleared slow match(es): {', '.join(ids) if ids else 'all'}"
        )

    def stop_watching(self, mid: str):
        w = self.watches.get(str(mid))
        if w:
            w.running = False
            if w.task and not w.task.done():
                w.task.cancel()

    async def confirm_goal_click(self, mid: str) -> BetResult:
        """
        Bet365 goal → click Confirm → ALWAYS verify + Telegram result.
        Never silent.
        """
        w = self.watches.get(str(mid))
        if not w or not w.page or w.page.is_closed():
            await self._tg(
                f"❌ BET SKIPPED\nMatch {mid}\nReason: page/watch missing"
            )
            return BetResult.FAILED

        if not w.confirm_armed:
            await self._tg(
                f"⚠️ Goal but not armed — skip\n"
                f"{w.home_team} vs {w.away_team}\n"
                f"NO BET PLACED"
            )
            return BetResult.ABORTED_SAFETY

        if await self._is_suspended(w.page) or await self._selection_unavailable(
            w.page
        ):
            await self._tg(
                f"🔒 Goal + SportyBet locked — stop watching\n"
                f"{w.home_team} vs {w.away_team}\n"
                f"NO BET PLACED"
            )
            await self.clear_match(mid)
            return BetResult.ABORTED_SAFETY

        ok_click = await self._click_confirm(w.page)
        if not ok_click:
            await self._tg(
                f"❌ BET NOT PLACED\n"
                f"{w.home_team} vs {w.away_team}\n"
                f"{w.active_market}\n"
                f"Odds {w.odds_over} | Stake {w.stake_over}\n"
                f"Reason: Confirm button click failed"
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
            await asyncio.sleep(1.0)
            await self.ai_arm_from_plan(w.page, None, w)
            return BetResult.SUCCESS

        await self._tg(
            f"❌ BET NOT ACCEPTED\n"
            f"{w.home_team} vs {w.away_team}\n"
            f"{w.active_market}\n"
            f"Odds {w.odds_over} | Stake {w.stake_over}\n"
            f"Detail: {detail}"
        )
        # Try re-arm so we can attempt next opportunity
        await asyncio.sleep(0.8)
        await self.ai_arm_from_plan(w.page, None, w)
        return BetResult.FAILED
