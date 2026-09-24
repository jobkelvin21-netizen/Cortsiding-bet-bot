"""
BetExecutor — Groq decides the market + reads the score. Playwright only
scrolls, finds the exact button Groq chose, clicks it, sets stake, and
arms up to Confirm. Confirm itself is ONLY ever clicked in
confirm_goal_click(), in response to a Bet365 goal event.

STRICT MARKET RULE (enforced independently of Groq, as a safety net):
Only these three market shapes may ever be clicked:
  - "1st Half - Over/Under"  (1H)
  - "2nd Half - Over/Under"  (2H)
  - plain "Over/Under" with no half label (Full Time)
A candidate is rejected outright if no nearby heading literally contains
"over/under" — this blocks things like "Rest of Match Total Goals",
Handicap, 3rd Goal, Double Chance, etc. even if a button on the page
happens to say "Over {line}". If no valid market is found, the bot does
not bet at all.

Arming happens ONCE per state change (initial /slow lock, HT→2H
transition, market-unavailable recovery, VAR/disallowed-goal recovery,
and once after each confirmed bet to set up the next line) — not on a
continuous loop.
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


# Final allow-list check: active_market must be EXACTLY one of these
# shapes before any stake is ever placed. "1H Over 1.5", "2H Over 2.5",
# "FT Over 0.5" — nothing else passes, no matter what Groq or the DOM
# search produced.
_ALLOWED_MARKET_RE = re.compile(r"^(1H|2H|FT) Over \d+(\.\d+)?$")


def _is_allowed_market_label(label: str) -> bool:
    return bool(_ALLOWED_MARKET_RE.match((label or "").strip()))


def _require_forbid_for_period_market(pm: str):
    """Turns Groq's chosen period_market into the require/forbid keyword
    lists used by the strict DOM click-scoping in _find_and_click_over."""
    pm = (pm or "FT").upper()
    if pm == "1H":
        return (
            ["1st half", "first half", "1h"],
            ["2nd half", "second half", "full time", "fulltime"],
        )
    if pm == "2H":
        return (
            ["2nd half", "second half", "2h"],
            ["1st half", "first half"],
        )
    # FT / anything else
    return (
        [],
        ["1st half", "first half", "2nd half", "second half", "half time"],
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

    async def _capture_full_market_text(self, page: Page, steps: int = 10) -> str:
        """SportyBet's markets panel is virtualized — only what's scrolled
        into view exists in the DOM at any instant. Scroll step by step
        and accumulate every unique line seen along the way, so Groq gets
        the FULL market list (1H/2H/FT Over-Under lines included), not
        just whatever happened to be on screen at one instant.
        Scrolls back to the top afterward so the later click-search phase
        starts from the top of the page again."""
        await self._click_all_tab(page)

        seen_lines: List[str] = []
        seen_set = set()

        async def snapshot():
            try:
                t = await page.inner_text("body", timeout=2500)
            except Exception:
                t = ""
            for line in (t or "").split("\n"):
                line = line.strip()
                if line and line not in seen_set:
                    seen_set.add(line)
                    seen_lines.append(line)

        await snapshot()
        for i in range(steps):
            try:
                await page.mouse.wheel(0, 500 + i * 30)
                await asyncio.sleep(0.25)
            except Exception:
                break
            await snapshot()

        try:
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass

        return "\n".join(seen_lines)[:9000]

    async def _quick_score_check(self, page: Page) -> tuple:
        """Cheap, local-only score read — used ONLY as a trigger for
        VAR/disallowed-goal detection in the watch loop (comparing against
        watch.total_goals, which Groq owns/updates). This does NOT decide
        which market to bet on; that's exclusively Groq's job via
        ai_arm_from_plan."""
        text = await self._page_text(page, 3000)

        colon_pattern = re.compile(r"\b(\d{1,2})\s*:\s*(\d{1,2})\b")
        for m in colon_pattern.finditer(text):
            try:
                h, a = int(m.group(1)), int(m.group(2))
                if 0 <= h <= 15 and 0 <= a <= 15:
                    return h, a
            except Exception:
                continue

        dash_pattern = re.compile(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b")
        for m in dash_pattern.finditer(text):
            try:
                h, a = int(m.group(1)), int(m.group(2))
                if 0 <= h <= 15 and 0 <= a <= 15:
                    return h, a
            except Exception:
                continue

        return None, None

    async def _detect_period(self, page: Page) -> str:
        """Cheap regex heuristic used ONLY to decide WHEN to trigger a
        re-arm (HT alert, HT→2H transition) in the watch loop. Groq does
        its own, more careful period read (via _normalize_period inside
        groq_ai.py) every time ai_arm_from_plan actually runs."""
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
        """Click Over {line} — but ONLY if the button sits under a heading
        that literally contains "over/under". This is a strict allow-list:

          - Walk up to 6 ancestor levels from the candidate button.
          - Find the line(s) of that ancestor's text that contain
            "over/under".
          - If NO such line exists nearby, REJECT the candidate outright
            (no fallback to raw surrounding text — that was the bug that
            let markets like "Rest of Match Total Goals" get clicked just
            because a button on the page happened to say "Over 0.5").
          - Only once a genuine "over/under" heading is found do
            require_kw / forbid_kw (from Groq's chosen period_market) get
            checked against THAT heading text.
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

                            heading_lines = [
                                ln for ln in ctx.split("\n")
                                if "over/under" in ln.lower()
                            ]
                            if not heading_lines:
                                continue

                            ctx_l = "\n".join(heading_lines).lower()

                            if "rest of" in ctx_l:
                                continue
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

    async def _maybe_accept_changes(self, page: Page, watch: MatchWatch):
        """Lightweight maintenance only — if odds moved while we're sitting
        armed, accept the change and refresh the stake amount. This does
        NOT click Place Bet or Confirm and does NOT re-select the market;
        it just keeps the existing (Groq-chosen) selection valid."""
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

    # ─── arm (Groq decides, Playwright executes) ───────────────

    async def ai_arm_from_plan(
        self,
        page: Page,
        plan: Optional[dict],
        watch: MatchWatch,
    ) -> bool:
        """Groq reads the full (scrolled) market text + the live score,
        decides the period/market/line. Playwright then finds and clicks
        EXACTLY that market (strict heading check still applies as a
        safety net), sets stake, clicks Accept Changes / Place Bet, and
        stops once Confirm is visible. Confirm is NEVER clicked here."""
        if page.is_closed():
            return False

        if await self._match_ended(page):
            await self._tg("⏹ Match ended — not arming")
            return False

        text = await self._capture_full_market_text(page)

        # DEBUG: Log what was captured
        logger.info(f"[GROQ DEBUG] Text length: {len(text)}")
        logger.info(f"[GROQ DEBUG] Contains 'Over/Under': {'over/under' in text.lower()}")
        logger.info(f"[GROQ DEBUG] First 800 chars:\n{text[:800]}")
        
        # Save to file for inspection
        try:
            with open(f"debug_{watch.match_id}.txt", "w", encoding="utf-8") as f:
                f.write(text)
            logger.info(f"[GROQ DEBUG] Saved full text to debug_{watch.match_id}.txt")
        except Exception as e:
            logger.error(f"[GROQ DEBUG] Failed to save file: {e}")

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
                    watch.period,
                    max_profit,
                    balance,
                )
            except Exception as e:
                logger.warning(f"[GROQ] error: {e}")
                plan = {"market_found": False, "error": str(e)}

        if not plan.get("market_found"):
            msg = plan.get("error") or plan.get("reason") or "no valid market"
            logger.warning(f"[AI ARM] {msg}")
            await self._tg(
                f"⚠️ Arm failed: {msg}\n"
                f"{watch.home_team} vs {watch.away_team}\n"
                f"NOT betting"
            )
            watch.confirm_armed = False
            return False

        # Groq read the score — trust it (already sanity-checked in
        # groq_ai.py against 0-15 range and cross-verified via regex hint).
        sh = plan.get("score_home")
        sa = plan.get("score_away")
        try:
            if sh is not None and sa is not None:
                watch.home_score, watch.away_score = int(sh), int(sa)
        except Exception:
            pass

        pm = (plan.get("period_market") or "FT").upper()
        try:
            line = float(plan.get("line"))
        except Exception:
            await self._tg("⚠️ Arm failed: Groq returned an invalid line")
            watch.confirm_armed = False
            return False

        label_prefix = {"1H": "1H", "2H": "2H"}.get(pm, "FT")
        candidate_label = f"{label_prefix} Over {line:g}"

        # Safety check BEFORE we even try to click anything.
        if not _is_allowed_market_label(candidate_label):
            logger.error(f"[SAFETY] blocked pre-click label: {candidate_label!r}")
            await self._tg(
                f"🛑 SAFETY BLOCK (pre-click)\n{candidate_label!r}\nNOT betting."
            )
            watch.confirm_armed = False
            return False

        await self._clear_betslip(page)
        await asyncio.sleep(0.3)

        require_kw, forbid_kw = _require_forbid_for_period_market(pm)
        clicked = await self._find_and_click_over(
            page, line, require_kw, forbid_kw, label_prefix
        )
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

        # Final safety check AFTER clicking, before any stake is placed —
        # independent of Groq, independent of the DOM search above.
        if not _is_allowed_market_label(watch.active_market):
            logger.error(f"[SAFETY] blocked post-click label: {watch.active_market!r}")
            await self._tg(
                f"🛑 SAFETY BLOCK (post-click)\n{watch.active_market!r}\nNOT betting."
            )
            await self._clear_betslip(page)
            watch.confirm_armed = False
            watch.active_market = ""
            return False

        odds = await self._read_odds_from_slip(page)
        if odds <= 1.01:
            try:
                odds = float(plan.get("odds") or 1.5)
            except Exception:
                odds = 1.5
        watch.odds_over = odds
        stake = max_profit / (odds - 1.0) if odds > 1.0 else 10.0
        if balance > 0 and stake > balance:
            stake = balance
        watch.stake_over = round(max(10.0, stake), 2)

        if not await self._set_stake(page, watch.stake_over):
            await self._tg("❌ Stake failed")
            watch.confirm_armed = False
            return False

        await self._click_accept_changes(page)  # harmless if not present
        if not await self._click_place_bet(page):
            await self._tg("❌ Place Bet click failed")
            watch.confirm_armed = False
            return False

        await asyncio.sleep(0.4)
        armed = await self._confirm_visible(page)
        watch.confirm_armed = armed

        if armed:
            logger.success(
                f"[AI ARM] {watch.active_market} stake={watch.stake_over:.0f} @{watch.odds_over}"
            )
            await self._tg(
                f"✅ ARMED\n"
                f"{watch.home_team} vs {watch.away_team}\n"
                f"Score {watch.home_score}-{watch.away_score} | {pm}\n"
                f"{watch.active_market}\n"
                f"Odds {watch.odds_over} | Stake {watch.stake_over}\n"
                f"Waiting for goal → will click Confirm"
            )
        else:
            await self._tg(
                f"⚠️ Placed but Confirm not visible\n{watch.active_market}"
            )
        return armed

    # ─── watch loop (cheap heuristics only, no continuous re-arming) ───

    async def _watch_loop(self, mid: str):
        watch = self.watches.get(mid)
        if not watch:
            return
        page = watch.page
        watch.running = True
        logger.info(f"[WATCH] start {mid}")

        # Arm once at the start.
        await self.ai_arm_from_plan(page, None, watch)

        while watch.running:
            try:
                if page.is_closed():
                    break

                if await self._match_ended(page):
                    await self._clear_betslip(page)
                    await self._tg(
                        f"🏁 FULL TIME — {watch.home_team} vs {watch.away_team}\n"
                        f"Final score {watch.home_score}-{watch.away_score}\n"
                        f"Betslip cleared, no longer watching this match.\n"
                        f"Send /slow for a new match."
                    )
                    watch.running = False
                    break

                # Cheap VAR/disallowed-goal guard — comparison only,
                # not a market decision.
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
                            f"Score {watch.home_score}-{watch.away_score}\n"
                            f"Not armed during HT. You can send a new /slow now if you want.\n"
                            f"Will auto re-arm when 2nd half kicks off."
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

                # Odds-changed maintenance only — never clicks Confirm.
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
        """Bet365 goal for locked FI → the ONLY thing this does is click
        Confirm. No market selection, no DOM search, no Groq call here."""
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
            await self._tg(
                f"✅ BET PLACED (goal)\n"
                f"{w.home_team} vs {w.away_team}\n"
                f"{w.active_market}\n"
                f"Odds {w.odds_over} | Stake {w.stake_over}"
            )
            w.confirm_armed = False
            await asyncio.sleep(1.0)
            # One Groq call to arm the NEXT line — not continuous.
            await self.ai_arm_from_plan(w.page, None, w)
            return BetResult.SUCCESS

        await self._tg("❌ Confirm click failed")
        return BetResult.FAILED
