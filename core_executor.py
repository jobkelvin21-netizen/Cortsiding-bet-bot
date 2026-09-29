"""
core/executor.py — pure code, no AI.

Simple flow: read SportyBet's score ONCE, pick the matching Over market,
arm it, then just stay on Confirm. Confirm is clicked ONLY by
confirm_goal_click (a Bet365 goal). Nothing re-checks the score or
re-picks the market on its own — only a genuine change on SportyBet's own
page (suspended / unavailable / half changes) triggers a re-arm.
"""

import asyncio
import re
import time
from enum import Enum, auto
from typing import Dict, Optional

from loguru import logger
from playwright.async_api import Page

from config import Config

CONFIRM_WATCH_TICK = 0.15  # fast loop: is Confirm up? if not, get back to it

SECTION_LABEL = {
    "1H": "1st Half - Over/Under",
    "2H": "2nd Half - Over/Under",
    "FT": "Over/Under",
}

# Captured selectors
SEL_CONFIRM = "button:has(span[data-cms-key='confirm']):not([data-op*='cashout'])"
SEL_PLACE = ("button[data-op='desktop-betslip-place-bet-button'], "
             "button:has(span[data-cms-key='place_bet'])")
SEL_ACCEPT = "button:has(span[data-cms-key='accept_changes'])"
SEL_REBET = "button[data-ret='rebet'], button:has(span[data-cms-key='rebet'])"
SEL_STAKE = ("div.m-betslips input.m-input, input.m-input[placeholder*='min'], "
             "#j_betslip input[type='text']")
SEL_CASHOUT_BTN = "button:has(span[data-cms-key='cashout'][data-cms-page='cashout'])"
SEL_CASHOUT_CONFIRM = "button[data-op='open_bets__cashout_confirm']"

JS_STATE = """() => {
  const h = document.querySelector('div.sr-lmt-plus-scb__result-team.srm-team1');
  const a = document.querySelector('div.sr-lmt-plus-scb__result-team.srm-team2');
  if (!h || !a) return null;
  let box = h;
  for (let i = 0; i < 3 && box.parentElement; i++) box = box.parentElement;
  return {h: (h.innerText || '').trim(), a: (a.innerText || '').trim(),
          box: box.innerText || ''};
}"""


def _xq(s: str) -> str:
    return f'"{s}"' if '"' not in s else f"'{s}'"


def parse_period(box: str) -> str:
    """Period from the scoreboard box text. Anything unclear -> UNK, which
    only ever bets Full Time."""
    if re.search(r"\b1st\b", box, re.I):
        return "1H"
    if re.search(r"\b2nd\b", box, re.I):
        return "2H"
    if re.search(r"\bHT\b|half\s*-?\s*time", box, re.I):
        return "HT"
    return "UNK"


class BetResult(Enum):
    SUCCESS = auto()
    FAILED = auto()
    ABORTED_SAFETY = auto()


class MatchWatch:
    def __init__(self, page: Page, home_team: str, away_team: str,
                 home_score: int = 0, away_score: int = 0, match_id: str = ""):
        self.page = page
        self.home_team = home_team or ""
        self.away_team = away_team or ""
        self.home_score = int(home_score)
        self.away_score = int(away_score)
        self.match_id = str(match_id or "")
        self.odds_over = 0.0
        self.stake_over = 0.0
        self.active_market = ""
        self.confirm_armed = False
        self.period = "UNK"
        self.task: Optional[asyncio.Task] = None
        self.confirm_watch_task: Optional[asyncio.Task] = None
        self.running = False
        self.last_period_hint = "UNK"
        self.ht_seen = False
        self.ht_alerted = False
        self.ht_total: Optional[int] = None
        self.armed_key: Optional[str] = None
        self.armed_line: Optional[float] = None

    @property
    def total_goals(self) -> int:
        return int(self.home_score) + int(self.away_score)


class BetExecutor:
    def __init__(self, alerter=None):
        self.alerter = alerter
        self.watches: Dict[str, MatchWatch] = {}
        self.balance = 0.0
        self.current_account = None

    def set_alerter(self, alerter):
        self.alerter = alerter

    def _tg(self, text: str):
        if self.alerter:
            asyncio.create_task(self._send(text))

    async def _send(self, text: str):
        try:
            await self.alerter.send(text)
        except Exception:
            pass

    # ── page state ────────────────────────────────────────────────────
    async def _page_text(self, page: Page, limit: int = 4000) -> str:
        try:
            return (await page.inner_text("body", timeout=2000) or "")[:limit]
        except Exception:
            return ""

    async def _read_state(self, page: Page) -> Optional[dict]:
        try:
            raw = await page.evaluate(JS_STATE)
        except Exception:
            return None
        if not raw:
            return None
        h = int(raw["h"]) if raw["h"].isdigit() else None
        a = int(raw["a"]) if raw["a"].isdigit() else None
        box = raw["box"] or ""
        return {"h": h, "a": a,
                "total": (h + a) if h is not None and a is not None else None,
                "period": parse_period(box)}

    async def detect_period(self, page: Page) -> str:
        st = await self._read_state(page)
        return st["period"] if st else "UNK"

    async def _match_ended(self, page: Page) -> bool:
        text = (await self._page_text(page, 4000)).lower()
        if not re.search(r"\b(match\s*ended|match\s*finished)\b", text):
            return False
        if re.search(r"\b(1st\s*half|2nd\s*half|ongoing)\b|\b\d{1,2}\s*'", text):
            return False
        return True

    async def _selection_unavailable(self, page: Page) -> bool:
        try:
            slip = page.locator("div.m-betslips, #j_betslip")
            if await slip.count() == 0:
                return False
            t = (await slip.first.inner_text(timeout=1000) or "").lower()
            return "unavailable" in t
        except Exception:
            return False

    async def _is_suspended(self, page: Page) -> bool:
        t = (await self._page_text(page, 3000)).lower()
        return "suspended" in t and "unavailable" not in t

    # ── slip / clicking ───────────────────────────────────────────────
    async def _click_all_tab(self, page: Page):
        for loc in (
            page.locator("div.m-nav-item").get_by_text("All", exact=True),
            page.locator("[role='tab']").get_by_text("All", exact=True),
        ):
            try:
                if await loc.count() > 0:
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.3)
                    return
            except Exception:
                continue

    async def _clear_betslip(self, page: Page):
        try:
            for _ in range(10):
                loc = page.locator("i.m-icon-delete").first
                if not await loc.is_visible():
                    break
                await loc.click(timeout=800)
                await asyncio.sleep(0.12)
        except Exception:
            pass

    async def _click_over_in_section(self, page: Page, heading: str, over_text: str) -> bool:
        xs = _xq(heading)
        headings = page.locator(f"xpath=//*[normalize-space(text())={xs}]")
        try:
            n = await headings.count()
            for h in range(n):
                el = headings.nth(h)
                for level in range(1, 9):
                    container = el.locator(f"xpath=ancestor::*[{level}]")
                    if await container.count() == 0:
                        break
                    same = container.locator(f"xpath=.//*[normalize-space(text())={xs}]")
                    if await same.count() != 1:
                        break
                    hits = container.get_by_text(over_text, exact=True)
                    if await hits.count() > 0 and await hits.first.is_visible():
                        await hits.first.scroll_into_view_if_needed(timeout=1500)
                        await hits.first.click(timeout=2000)
                        return True
        except Exception as e:
            logger.warning(f"[ARM] click {heading} {over_text}: {e}")
        return False

    async def _betslip_has_over(self, page: Page, line: float) -> bool:
        try:
            slip = page.locator("div.m-betslips, #j_betslip")
            if await slip.count() == 0:
                return False
            t = (await slip.first.inner_text(timeout=1000) or "").lower()
            needle = f"over {line:g}".lower()
            return needle in t and "unavailable" not in t
        except Exception:
            return False

    async def _wait_betslip_has_over(self, page: Page, line: float, timeout: float = 2.5) -> bool:
        """Poll, never blind-double-click — a re-click can un-select a bet
        that already worked, just slow to render."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if await self._betslip_has_over(page, line):
                return True
            await asyncio.sleep(0.15)
        return False

    def _stake_for_odds(self, odds: float) -> float:
        if odds <= 1.01:
            odds = 1.5
        max_profit = float(Config.MAX_PROFIT_PER_BET)
        stake = max(10.0, max_profit / (odds - 1.0))
        if self.balance and self.balance > 0:
            stake = min(stake, float(self.balance))
        return round(stake, 2)

    async def _set_stake(self, page: Page, stake: float) -> bool:
        stake_str = f"{stake:.0f}" if stake >= 10 else f"{stake:.2f}"
        try:
            loc = page.locator(SEL_STAKE).first
            if not await loc.is_visible():
                return False
            await loc.click(timeout=800)
            await loc.fill("")
            typer = getattr(loc, "press_sequentially", None) or loc.type
            await typer(stake_str, delay=15)
            return True
        except Exception:
            return False

    async def _click_place_bet(self, page: Page) -> bool:
        try:
            loc = page.locator(SEL_PLACE).first
            if await loc.is_visible():
                await loc.click(timeout=1500)
                await asyncio.sleep(0.3)
                return True
        except Exception:
            pass
        return False

    async def _click_confirm(self, page: Page, timeout: int = 700) -> bool:
        """The ONLY function allowed to click Confirm for a goal."""
        try:
            await page.locator(SEL_CONFIRM).first.click(timeout=timeout)
            return True
        except Exception:
            return False

    async def _confirm_visible(self, page: Page) -> bool:
        try:
            return await page.locator(SEL_CONFIRM).first.is_visible()
        except Exception:
            return False

    async def _click_accept_changes(self, page: Page) -> bool:
        try:
            loc = page.locator(SEL_ACCEPT).first
            if await loc.is_visible():
                await loc.click(timeout=1200)
                await asyncio.sleep(0.25)
                return True
        except Exception:
            pass
        return False

    async def _click_rebet(self, page: Page) -> bool:
        """Short poll — the button can take a moment to render after a bet
        is confirmed."""
        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline:
            try:
                loc = page.locator(SEL_REBET).first
                if await loc.is_visible():
                    await loc.click(timeout=1500)
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.15)
        return False

    async def _read_odds(self, page: Page) -> float:
        try:
            slip = page.locator("div.m-betslips, #j_betslip").first
            t = await slip.inner_text(timeout=1000)
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
                continue
            if await self._click_place_bet(page):
                saw_place = True
                continue
            await asyncio.sleep(0.25 if saw_place else 0.5)
        return await self._confirm_visible(page)

    # ── stay-on-Confirm: dedicated fast loop, no re-scoring ──────────────
    async def _confirm_watch_loop(self, mid: str):
        while True:
            w = self.watches.get(mid)
            if not w or not w.running or w.page.is_closed():
                return
            try:
                if await self._confirm_visible(w.page):
                    w.confirm_armed = True
                else:
                    w.confirm_armed = False
                    ok = await self._arm_until_confirm(w.page, 5.0)
                    w.confirm_armed = ok
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[CONFIRM-WATCH] {mid}: {e}")
            await asyncio.sleep(CONFIRM_WATCH_TICK)

    def _start_confirm_watch(self, mid: str, w: MatchWatch):
        self._stop_confirm_watch(w)
        w.confirm_watch_task = asyncio.create_task(self._confirm_watch_loop(mid))

    def _stop_confirm_watch(self, w: MatchWatch):
        t, w.confirm_watch_task = w.confirm_watch_task, None
        if t and not t.done():
            t.cancel()

    # ── arm: read score ONCE, pick market, stay armed ────────────────────
    async def ai_arm_from_plan(self, page: Page, plan=None,
                               watch: Optional[MatchWatch] = None, mid: str = "") -> bool:
        if watch is None or page.is_closed():
            return False
        mid = str(mid or watch.match_id)
        self._stop_confirm_watch(watch)

        if await self._match_ended(page):
            self._tg(f"⏹ Match ended — not arming\n{watch.home_team} vs {watch.away_team}")
            await self.clear_match(mid)
            return False

        st = await self._read_state(page)
        if st and st["total"] is not None:
            watch.home_score, watch.away_score = st["h"], st["a"]
        period = st["period"] if st else "UNK"
        watch.period = period
        total = watch.total_goals

        if period == "HT" and watch.ht_total is None:
            watch.ht_total = total

        # STRICT period rules: 1H -> 1H then FT; 2H -> 2H then FT; else FT only.
        if period == "1H":
            plan_list = [("1H", SECTION_LABEL["1H"], total + 0.5)]
        elif period == "2H" and watch.ht_total is not None and total >= watch.ht_total:
            plan_list = [("2H", SECTION_LABEL["2H"], (total - watch.ht_total) + 0.5)]
        else:
            plan_list = []
        plan_list.append(("FT", SECTION_LABEL["FT"], total + 0.5))

        logger.info(f"[ARM] score={watch.home_score}-{watch.away_score} "
                    f"period={period} plan={[(k, ln) for k, _, ln in plan_list]}")

        await self._clear_betslip(page)
        await self._click_all_tab(page)

        chosen = None
        for key, heading, line in plan_list:
            over_text = f"Over {line:g}"
            if await self._click_over_in_section(page, heading, over_text):
                if await self._wait_betslip_has_over(page, line):
                    chosen = (key, line)
                    break
            await self._clear_betslip(page)

        if chosen is None:
            self._tg(f"🛑 NO VALID OVER MARKET\n{watch.home_team} vs {watch.away_team}\n"
                     f"Score {watch.home_score}-{watch.away_score} | {period}\n"
                     f"Stopped watching.\nSend /slow for another game.")
            await self.clear_match(mid)
            return False

        key, line = chosen
        watch.armed_key, watch.armed_line = key, line
        watch.active_market = f"{key} Over {line:g}"

        odds = await self._read_odds(page)
        if odds <= 1.01:
            odds = 1.5
        watch.odds_over = odds
        watch.stake_over = self._stake_for_odds(odds)

        if not await self._set_stake(page, watch.stake_over):
            self._tg("❌ Stake failed")
            return False
        await self._click_accept_changes(page)
        if not await self._click_place_bet(page):
            self._tg("❌ Place Bet failed")
            return False

        armed = await self._arm_until_confirm(page, 8.0)
        watch.confirm_armed = armed
        if armed:
            logger.success(f"[ARM] {watch.active_market} @{odds} stake={watch.stake_over}")
            self._tg(f"✅ ARMED\n{watch.home_team} vs {watch.away_team}\n"
                     f"Score {watch.home_score}-{watch.away_score} | {period}\n"
                     f"{watch.active_market}\nOdds {odds} | Stake {watch.stake_over}\n"
                     f"Waiting for goal → Confirm")
            self._start_confirm_watch(mid, watch)
        else:
            self._tg(f"⚠️ Place Bet done but Confirm not visible\n{watch.active_market}")
        return armed

    # ── after Confirm ────────────────────────────────────────────────
    async def _try_rebet(self, page: Page, watch: MatchWatch) -> bool:
        if not await self._click_rebet(page):
            return False
        odds = await self._read_odds(page)
        if odds > 1.01:
            watch.odds_over = odds
            watch.stake_over = self._stake_for_odds(odds)
        if watch.stake_over > 0:
            await self._set_stake(page, watch.stake_over)
        await self._click_accept_changes(page)
        if not await self._click_place_bet(page):
            return False
        armed = await self._arm_until_confirm(page, 6.0)
        watch.confirm_armed = armed
        if armed:
            self._tg(f"🔁 REBET ARMED\n{watch.active_market}\nOdds {watch.odds_over} | "
                     f"Stake {watch.stake_over}")
        return armed

    async def confirm_goal_click(self, mid: str) -> BetResult:
        w = self.watches.get(str(mid))
        if not w or w.page.is_closed():
            self._tg(f"❌ BET SKIPPED\n{mid}\npage/watch missing")
            return BetResult.FAILED
        if not w.confirm_armed:
            self._tg(f"⚠️ Goal but not armed — skip\n{w.home_team} vs {w.away_team}\nNO BET PLACED")
            return BetResult.ABORTED_SAFETY
        if await self._is_suspended(w.page) or await self._selection_unavailable(w.page):
            self._tg(f"🔒 Goal + SportyBet locked — stop watching\n"
                     f"{w.home_team} vs {w.away_team}\nNO BET PLACED")
            await self.clear_match(mid)
            return BetResult.ABORTED_SAFETY

        self._stop_confirm_watch(w)

        clicked = await self._click_confirm(w.page)
        if not clicked:
            self._tg(f"❌ BET NOT PLACED — Confirm click failed\n"
                     f"{w.home_team} vs {w.away_team}\n{w.active_market}")
            return BetResult.FAILED

        await asyncio.sleep(0.6)
        w.confirm_armed = False
        self._tg(f"✅ BET CONFIRMED\n{w.home_team} vs {w.away_team}\n{w.active_market}\n"
                 f"Odds {w.odds_over} | Stake {w.stake_over}")

        if not await self._try_rebet(w.page, w):
            await self.ai_arm_from_plan(w.page, None, w, mid=mid)
        else:
            if mid in self.watches:
                self._start_confirm_watch(mid, w)
        return BetResult.SUCCESS

    # ── VAR / disallowed goal: cash out ──────────────────────────────
    async def cashout_disallowed(self, page: Page, description: str = "") -> bool:
        try:
            tab = page.locator("div.tabs-v2__tab").filter(
                has_text=re.compile(r"^\s*Cashout")).first
            if not await tab.is_visible():
                return False
            await tab.click(timeout=1500)
            await asyncio.sleep(0.5)

            btn = page.locator(SEL_CASHOUT_BTN).first
            if not await btn.is_visible():
                return False
            await btn.click(timeout=1500)

            conf = page.locator(SEL_CASHOUT_CONFIRM).first
            for _ in range(20):
                if await conf.is_visible():
                    await conf.click(timeout=1500)
                    self._tg(f"💰 Cashout attempted {description}")
                    return True
                await asyncio.sleep(0.12)
            return False
        except Exception as e:
            logger.error(f"[CASHOUT] {e}")
            return False

    # ── watch loop: only reacts to SportyBet's own page changes ──────────
    async def _watch_loop(self, mid: str):
        w = self.watches.get(mid)
        if not w:
            return
        page = w.page
        w.running = True
        logger.info(f"[WATCH] start {mid}")
        await self.ai_arm_from_plan(page, None, w, mid=mid)

        while w.running:
            try:
                if page.is_closed() or mid not in self.watches:
                    break
                if await self._match_ended(page):
                    self._stop_confirm_watch(w)
                    self._tg(f"🏁 FULL TIME — {w.home_team} vs {w.away_team}")
                    w.running = False
                    break

                st = await self._read_state(page)
                total = st["total"] if st else None
                if total is not None and total < w.total_goals:
                    self._stop_confirm_watch(w)
                    await self.cashout_disallowed(
                        page, f"{w.home_score}-{w.away_score}→{st['h']}-{st['a']}")
                    w.home_score, w.away_score = st["h"], st["a"]
                    w.confirm_armed = False
                    await self.ai_arm_from_plan(page, None, w, mid=mid)

                period = st["period"] if st else "UNK"
                if period == "HT":
                    w.ht_seen = True
                    if not w.ht_alerted:
                        w.ht_alerted = True
                        self._stop_confirm_watch(w)
                        w.confirm_armed = False
                        self._tg(f"⏸ HALF TIME — {w.home_team} vs {w.away_team}")
                if (w.ht_seen and period == "2H"
                        and w.last_period_hint in ("HT", "1H")):
                    w.ht_alerted = False
                    self._tg("🔄 2nd half — re-arming")
                    await self.ai_arm_from_plan(page, None, w, mid=mid)

                if await self._selection_unavailable(page):
                    self._stop_confirm_watch(w)
                    self._tg("⚠️ Market Unavailable — re-arming")
                    w.confirm_armed = False
                    await self.ai_arm_from_plan(page, None, w, mid=mid)
                elif await self._is_suspended(page):
                    pass  # wait, don't re-arm on a temporary suspension
                elif (w.confirm_armed
                      and (w.confirm_watch_task is None or w.confirm_watch_task.done())):
                    self._start_confirm_watch(mid, w)

                if period != "UNK":
                    w.last_period_hint = period
                await asyncio.sleep(1.2)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[WATCH] {mid}: {e}")
                await asyncio.sleep(1.5)

        self._stop_confirm_watch(w)
        w.running = False
        logger.info(f"[WATCH] stop {mid}")

    # ── public API ────────────────────────────────────────────────────
    def start_watching(self, mid: str, page: Page, home: str, away: str, **kwargs):
        mid = str(mid)
        for oid in list(self.watches.keys()):   # one match at a time, always
            old = self.watches.pop(oid)
            old.running = False
            self._stop_confirm_watch(old)
            if old.task and not old.task.done():
                old.task.cancel()
        h = int(kwargs.get("home_score") or 0)
        a = int(kwargs.get("away_score") or 0)
        w = MatchWatch(page, home, away, h, a, match_id=mid)
        self.watches[mid] = w
        w.task = asyncio.create_task(self._watch_loop(mid))
        return w

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
                if not w.page.is_closed():
                    await self._clear_betslip(w.page)
                    await w.page.close()
            except Exception:
                pass
        self._tg(f"🧹 Cleared: {', '.join(ids) if ids else 'all'}")

    async def park_match(self, mid: str):
        """Stop betting on a match but keep its page open (half-time
        parking, for the switch-back flow)."""
        w = self.watches.pop(str(mid), None)
        if not w:
            return None
        w.running = False
        self._stop_confirm_watch(w)
        if w.task and not w.task.done():
            w.task.cancel()
        try:
            if not w.page.is_closed():
                await self._clear_betslip(w.page)
        except Exception:
            pass
        return w.page

    def stop_watching(self, mid: str):
        w = self.watches.get(str(mid))
        if w:
            w.running = False
            self._stop_confirm_watch(w)
            if w.task and not w.task.done():
                w.task.cancel()
