import asyncio
import re
import time
import uuid
from enum import Enum, auto
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Any

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
        self.home_score = int(home_score)
        self.away_score = int(away_score)

        self.odds_over: float = 0.0
        self.stake_over: float = 0.0
        self.active_market: str = ""
        self.target_line: float = 0.5
        self.confirm_armed: bool = False
        self.market_notified: bool = False
        self.period: str = "1H"  # "1H" | "2H" | "unknown"

        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.last_refresh_at: Optional[float] = None
        self.last_market_check_at: float = 0.0
        self.last_no_market_log_at: float = 0.0

    @property
    def total_goals(self) -> int:
        return self.home_score + self.away_score

    @property
    def over_line(self) -> float:
        return float(self.total_goals) + 0.5


class BetExecutor:
    SUCCESS_PATTERNS = (
        "Submission Successful",
        "Bet Successful",
    )

    MARKET_REVALIDATE_INTERVAL = 10.0
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
        self._odds_interval = float(getattr(Config, "ODDS_TRACK_INTERVAL", 1.2))
        self.max_rebet = int(getattr(Config, "MAX_STACK_PER_GOAL", 3))
        self.rebet_delay = float(getattr(Config, "MIN_STACK_DELAY", 0.8))

    # ------------------------------------------------------------------
    def calc_stake(self, odds: float) -> float:
        """Stake so profit equals MAX_PROFIT_PER_BET hard cap."""
        if odds <= 1.0 or not self.balance or self.balance <= 0:
            return 0.0
        target = float(Config.MAX_PROFIT_PER_BET) / (odds - 1.0)
        if self.balance < target:
            return float(self.balance)
        return float(target)

    def profit_at(self, stake: float, odds: float) -> float:
        if odds <= 1.0 or stake <= 0:
            return 0.0
        return stake * (odds - 1.0)

    def _line_str(self, line: float) -> str:
        return f"{line:.1f}"

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
            if not url or url.startswith("about:"):
                return False
            return True
        except Exception:
            return False

    async def _dismiss_popups(self, page: Page) -> None:
        for sel in (
            'button:has-text("OK")',
            'button:has-text("Got it")',
            'button:has-text("Close")',
            'button:has-text("I understand")',
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=400)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Score once from SportyBet feed
    # ------------------------------------------------------------------

    def _coerce_score_pair(self, obj: Any) -> Optional[Tuple[int, int]]:
        if obj is None:
            return None
        if isinstance(obj, (list, tuple)) and len(obj) >= 2:
            try:
                h, a = int(obj[0]), int(obj[1])
                if 0 <= h <= 20 and 0 <= a <= 20:
                    return h, a
            except Exception:
                return None
        if isinstance(obj, dict):
            for hk, ak in (
                ("home", "away"),
                ("homeScore", "awayScore"),
                ("home_score", "away_score"),
                ("h", "a"),
            ):
                if hk in obj and ak in obj:
                    try:
                        h, a = int(obj[hk]), int(obj[ak])
                        if 0 <= h <= 20 and 0 <= a <= 20:
                            return h, a
                    except Exception:
                        pass
        if isinstance(obj, str):
            m = re.search(r"(\d{1,2})\s*[:\-]\s*(\d{1,2})", obj)
            if m:
                h, a = int(m.group(1)), int(m.group(2))
                if 0 <= h <= 20 and 0 <= a <= 20:
                    return h, a
        return None

    def _parse_score_from_match(self, match: dict) -> Tuple[int, int]:
        if not match:
            return 0, 0
        for hk, ak in (
            ("home_score", "away_score"),
            ("homeScore", "awayScore"),
            ("score_home", "score_away"),
            ("homeGoals", "awayGoals"),
        ):
            if hk in match and ak in match:
                try:
                    h, a = int(match[hk]), int(match[ak])
                    if 0 <= h <= 20 and 0 <= a <= 20:
                        return h, a
                except Exception:
                    pass
        for key in ("score", "current_score", "liveScore", "result", "setScore"):
            pair = self._coerce_score_pair(match.get(key))
            if pair:
                return pair
        for key in ("event", "match", "data", "fixture"):
            nested = match.get(key)
            if isinstance(nested, dict):
                pair = self._parse_score_from_match(nested)
                if pair != (0, 0):
                    return pair
        return 0, 0

    def _resolve_initial_score(
        self, match_id: str, match: Optional[dict]
    ) -> Tuple[int, int]:
        try:
            ref = self.sportybet_matches or {}
            live = ref.get(match_id) or ref.get(str(match_id))
            if live:
                h, a = self._parse_score_from_match(live)
                logger.info(f"[SCORE_REST] feed ref {match_id} → {h}-{a}")
                return h, a
        except Exception as e:
            logger.debug(f"[SCORE_REST] feed ref: {e}")

        if match:
            h, a = self._parse_score_from_match(match)
            logger.info(
                f"[SCORE_REST] match dict {match_id} → {h}-{a} "
                f"keys={list(match.keys())[:12]}"
            )
            return h, a

        logger.warning(f"[SCORE_REST] no score for {match_id} — 0-0")
        return 0, 0

    # ------------------------------------------------------------------
    async def wait_match_details_ready(
        self, page: Page, home: str = "", away: str = "", timeout_s: float = 18.0
    ) -> bool:
        if not await self._page_alive(page):
            return False
        deadline = time.time() + timeout_s
        home_tok = (home.split()[0] if home else "").strip()
        away_tok = (away.split()[0] if away else "").strip()

        while time.time() < deadline:
            if not await self._page_alive(page):
                return False
            try:
                await self._dismiss_popups(page)
                url = (page.url or "").lower()
                all_tab = page.get_by_text(re.compile(r"^\s*All\s*$"))
                has_tabs = (await all_tab.count()) > 0
                has_teams = True
                if home_tok and away_tok:
                    h = page.get_by_text(home_tok, exact=False)
                    a = page.get_by_text(away_tok, exact=False)
                    has_teams = (await h.count()) > 0 and (await a.count()) > 0
                on_list = "live_list" in url
                if has_tabs and has_teams and not on_list:
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
        if not await self._page_alive(page):
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
                    await page.wait_for_timeout(280)
                    return True
            except Exception:
                continue
        return False

    async def _scroll_markets_panel(self, page: Page, amount: int = 420) -> None:
        try:
            await page.evaluate(
                """(amount) => {
                    const nodes = Array.from(document.querySelectorAll(
                        'div, section, main, [class*="market"], [class*="Market"],' +
                        ' [class*="scroll"], [class*="content"]'
                    ));
                    let best = null, bestOverflow = 0;
                    for (const el of nodes) {
                        const sh = el.scrollHeight || 0;
                        const ch = el.clientHeight || 0;
                        if (sh > ch + 80) {
                            const overflow = sh - ch;
                            const rect = el.getBoundingClientRect();
                            if (rect.width < 200) continue;
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
        except Exception:
            try:
                await page.mouse.wheel(0, amount)
            except Exception:
                pass

    async def _detect_period(self, page: Page) -> str:
        """Return '1H', '2H', or 'unknown' from page text / clock."""
        try:
            body = await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 4000)"
            )
            low = (body or "").lower()
            if re.search(r"\b2nd\s*half\b|\bsecond\s*half\b|\bht\b|\bhalf\s*time\b", low):
                if re.search(r"\b(4[6-9]|[5-9]\d)\s*[':]|\b2nd\b", low):
                    return "2H"
            if re.search(r"\b1st\s*half\b|\bfirst\s*half\b", low):
                return "1H"
            m = re.search(r"\b(\d{1,2})\s*[:']\s*\d{0,2}", low)
            if m:
                minute = int(m.group(1))
                if minute <= 45:
                    return "1H"
                if minute >= 46:
                    return "2H"
            m2 = re.search(r"\b(\d{1,3})\s*'", low)
            if m2:
                minute = int(m2.group(1))
                if minute <= 45:
                    return "1H"
                if minute >= 46:
                    return "2H"
        except Exception:
            pass
        return "unknown"

    # ------------------------------------------------------------------
    # OVER market
    # Priority by period:
    #   1H → 1st Half Over/Under Over {line}
    #   2H → 2nd Half Over/Under Over {line}
    #   else / missing → full match Over/Under Over {line}
    # Never team O/U. Line = total + 0.5 (not hardcoded).
    # ------------------------------------------------------------------

    async def prepare_over_market(self, page: Page, watch: MatchWatch) -> str:
        if not await self._page_alive(page):
            return ""

        line = watch.over_line
        line_s = self._line_str(line)
        watch.target_line = line
        now = time.time()

        watch.period = await self._detect_period(page)

        if watch.active_market and (now - watch.last_market_check_at) < self.MARKET_REVALIDATE_INTERVAL:
            try:
                slip = await self._betslip_text(page)
                if "over" in slip and line_s in slip:
                    return watch.active_market
            except Exception:
                pass
            watch.active_market = ""

        await self.wait_match_details_ready(
            page, watch.home_team, watch.away_team, timeout_s=5.0
        )
        await self.open_all_tab(page)

        if watch.period == "2H":
            section_order = [
                ("2H", re.compile(r"2nd\s*Half\s*[-–]?\s*Over/?Under", re.I), f"2nd Half Over {line_s}"),
                ("FULL", re.compile(r"(?:^|\b)Over/?Under\b", re.I), f"Over {line_s}"),
            ]
        else:
            section_order = [
                ("1H", re.compile(r"1st\s*Half\s*[-–]?\s*Over/?Under", re.I), f"1st Half Over {line_s}"),
                ("FULL", re.compile(r"(?:^|\b)Over/?Under\b", re.I), f"Over {line_s}"),
            ]

        async def try_click_over(prefer_half: str) -> Optional[str]:
            over_loc = page.get_by_text(
                re.compile(rf"^\s*Over\s*{re.escape(line_s)}\s*$", re.I)
            )
            try:
                n = min(await over_loc.count(), 24)
            except Exception:
                n = 0

            for i in range(n):
                el = over_loc.nth(i)
                try:
                    ctx = await el.evaluate(
                        """(node) => {
                            let t = '';
                            let cur = node;
                            for (let i = 0; i < 9 && cur; i++) {
                                t = (cur.innerText || cur.textContent || '') + '\\n' + t;
                                cur = cur.parentElement;
                            }
                            return t.slice(0, 600);
                        }"""
                    )
                    ctx = re.sub(r"\s+", " ", (ctx or "")).strip()
                    low = ctx.lower()

                    # Reject team-specific O/U
                    if re.search(
                        r"(?:2nd\s*half\s*[-–]?\s*)?(?:al[-\s]?tai|bukiryah|[a-z]{3,})\s+over/?under",
                        low,
                    ):
                        if not re.search(
                            r"(?:1st|2nd)\s*half\s*[-–]?\s*over/?under", low
                        ):
                            continue
                        if re.search(
                            r"(?:1st|2nd)\s*half\s*[-–]\s*[a-z0-9].{0,20}over/?under",
                            low,
                        ):
                            continue

                    is_1h = bool(re.search(r"1st\s*half\s*[-–]?\s*over/?under", low))
                    is_2h = bool(re.search(r"2nd\s*half\s*[-–]?\s*over/?under", low))
                    is_full = (
                        bool(re.search(r"(?:^|\b)over/?under\b", low))
                        and not is_1h
                        and not is_2h
                    )

                    if prefer_half == "1H" and not is_1h:
                        continue
                    if prefer_half == "2H" and not is_2h:
                        continue
                    if prefer_half == "FULL" and not is_full:
                        if is_1h or is_2h:
                            continue

                    await el.scroll_into_view_if_needed(timeout=900)
                    await el.click(timeout=1400)
                    await page.wait_for_timeout(280)

                    slip_txt = await self._betslip_text(page)
                    if "over" not in slip_txt:
                        continue
                    if "under" in slip_txt and "over" not in slip_txt:
                        await self._clear_betslip_selection(page)
                        continue

                    if is_1h or "1st half" in slip_txt:
                        return f"1st Half Over {line_s}"
                    if is_2h or "2nd half" in slip_txt:
                        return f"2nd Half Over {line_s}"
                    return f"Over {line_s}"
                except Exception:
                    continue
            return None

        for step in range(14):
            for key, _pat, _label in section_order:
                found = await try_click_over(key)
                if found:
                    watch.active_market = found
                    watch.last_market_check_at = time.time()
                    logger.info(
                        f"[MARKET] LOCATED {found} | period={watch.period} "
                        f"tracked={watch.home_score}-{watch.away_score} "
                        f"total={watch.total_goals} line={line_s}"
                    )
                    if not watch.market_notified:
                        watch.market_notified = True
                        try:
                            await self.alerter.send_message(
                                f"✅ MARKET FOUND\n"
                                f"{watch.home_team} vs {watch.away_team}\n"
                                f"Score: {watch.home_score}-{watch.away_score} | period={watch.period}\n"
                                f"Armed: <b>{found}</b>\n"
                                f"Next goal pays this Over line."
                            )
                        except Exception as e:
                            logger.debug(f"telegram market notify: {e}")
                    return found

            await self._scroll_markets_panel(page, amount=400 + step * 35)
            await page.wait_for_timeout(150)

        if (now - watch.last_no_market_log_at) > self.NO_MARKET_LOG_INTERVAL:
            logger.warning(
                f"[MARKET] Over {line_s} NOT found | period={watch.period} "
                f"tracked={watch.home_score}-{watch.away_score} — skip, keep watching"
            )
            watch.last_no_market_log_at = now
        watch.active_market = ""
        return ""

    async def _betslip_text(self, page: Page) -> str:
        try:
            slip = page.locator(
                '[class*="betslip"], [class*="Betslip"], [class*="bet-slip"]'
            )
            if await slip.count() > 0:
                return ((await slip.first.text_content()) or "").lower()
        except Exception:
            pass
        return ""

    async def _clear_betslip_selection(self, page: Page) -> None:
        """Remove via Cancel/X — never re-tap market."""
        for sel in (
            'button:has-text("Cancel")',
            '[class*="betslip"] button:has-text("×")',
            '[class*="betslip"] [class*="close"]',
            '[class*="Betslip"] [class*="remove"]',
            'button[aria-label*="remove" i]',
            'button[aria-label*="close" i]',
        ):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=500)
                    await page.wait_for_timeout(150)
            except Exception:
                pass
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _memory_score_ok(
        self,
        watch: MatchWatch,
        expected_home: int,
        expected_away: int,
        match_name: str,
    ) -> bool:
        th, ta = watch.home_score, watch.away_score
        eh, ea = int(expected_home), int(expected_away)
        logger.info(
            f"[SCORE_MEM] {match_name} tracked={th}-{ta} poly={eh}-{ea} "
            f"need Over {watch.over_line}"
        )
        if eh + ea != th + ta + 1:
            logger.warning(
                f"[SAFETY] ABORT {match_name} poly {eh}-{ea} vs tracked {th}-{ta} "
                f"(need exactly +1 goal)"
            )
            return False
        logger.info(
            f"[SAFETY] ALLOW {match_name} {th}-{ta}→{eh}-{ea} "
            f"(Over {watch.over_line} should pay)"
        )
        return True

    def advance_tracked_score(self, match_id: str, home: int, away: int) -> None:
        watch = self.watches.get(str(match_id))
        if not watch:
            return
        watch.home_score = int(home)
        watch.away_score = int(away)
        watch.active_market = ""
        watch.confirm_armed = False
        watch.market_notified = False
        watch.odds_over = 0.0
        logger.info(
            f"[SCORE_MEM] advanced → {home}-{away} next Over {watch.over_line}"
        )

    async def _verify_match_identity(
        self, page: Page, expected_home: str, expected_away: str
    ) -> bool:
        if not await self._page_alive(page):
            return False
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
    async def _read_over_odds_from_slip(self, page: Page) -> float:
        txt = await self._betslip_text(page)
        m = re.search(r"over[^\d]{0,20}(\d+\.\d{1,3})", txt.replace(",", "."))
        if not m:
            m = re.search(r"(\d+\.\d{1,3})", txt.replace(",", "."))
        if m:
            odds = float(m.group(1))
            if 1.01 <= odds <= 500:
                return odds
        return 0.0

    async def _read_stake_from_slip(self, page: Page) -> float:
        txt = await self._betslip_text(page)
        m = re.search(r"(?:stake|total\s*stake|ngn)\s*[₦ngn]*\s*([\d,]+)", txt, re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except Exception:
                pass
        return 0.0

    async def fetch_balance(self, page: Page) -> Optional[float]:
        if not await self._page_alive(page):
            return None
        for sel in (
            ".user-balance",
            ".account-balance",
            '[data-testid="balance"]',
            '[class*="balance"]',
            'span:has-text("₦")',
            'span:has-text("NGN")',
        ):
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

    async def _set_stake_input(self, page: Page, stake: float) -> bool:
        box = page.locator(
            'input[type="number"], input[type="tel"], input[inputmode="numeric"], '
            'input[placeholder*="Stake" i], input[placeholder*="stake" i], '
            'input[placeholder*="Amount" i], input[placeholder*="Total" i]'
        )
        try:
            if await box.count() > 0:
                el = box.first
                await el.click(timeout=700)
                await el.fill("")
                await el.fill(str(int(stake)))
                await page.wait_for_timeout(120)
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

    async def _click_accept_changes(self, page: Page) -> bool:
        btn = page.get_by_role(
            "button", name=re.compile(r"Accept\s*Changes", re.I)
        )
        try:
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click(timeout=1000)
                await page.wait_for_timeout(200)
                return True
        except Exception:
            pass
        try:
            loc = page.get_by_text(re.compile(r"Accept\s*Changes", re.I))
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=1000)
                await page.wait_for_timeout(200)
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

    async def _confirm_visible(self, page: Page) -> bool:
        btn = page.get_by_role("button", name=re.compile(r"^\s*Confirm\s*$", re.I))
        try:
            return (await btn.count()) > 0 and await btn.first.is_visible()
        except Exception:
            return False

    async def _cancel_confirm(self, page: Page) -> None:
        cancel = page.get_by_role("button", name=re.compile(r"^\s*Cancel\s*$", re.I))
        try:
            if await cancel.count() > 0 and await cancel.first.is_visible():
                await cancel.first.click(timeout=600)
                await page.wait_for_timeout(200)
        except Exception:
            pass

    async def _wait_bet_success(self, page: Page, timeout_ms: int = 8000) -> bool:
        for text in self.SUCCESS_PATTERNS:
            try:
                await page.wait_for_selector(f"text={text}", timeout=timeout_ms // 2)
                return True
            except Exception:
                continue
        return False

    async def _arm_over(self, page: Page, watch: MatchWatch, stake: float) -> bool:
        """
        Video flow:
        Cancel confirm if open → ensure Over on slip → stake →
        Accept Changes if needed → Place Bet → stay on Confirm.
        """
        if await self._confirm_visible(page):
            await self._cancel_confirm(page)

        label = await self.prepare_over_market(page, watch)
        if not label:
            watch.confirm_armed = False
            return False

        odds = await self._read_over_odds_from_slip(page)
        if odds > 0:
            watch.odds_over = odds
            stake = self.calc_stake(odds)
            if stake < 10:
                watch.confirm_armed = False
                return False

        if not await self._set_stake_input(page, stake):
            logger.debug("[ARM] stake failed")
            watch.confirm_armed = False
            return False

        await self._click_accept_changes(page)

        if not await self._click_place_bet(page):
            await self._click_accept_changes(page)
            if not await self._click_place_bet(page):
                logger.debug("[ARM] Place Bet failed")
                watch.confirm_armed = False
                return False

        await page.wait_for_timeout(280)
        ok = await self._confirm_visible(page)
        watch.confirm_armed = ok
        watch.stake_over = stake
        if ok:
            logger.info(
                f"[CONFIRM] ARMED {label} stake={stake:.0f} odds={watch.odds_over} "
                f"profit_cap={Config.MAX_PROFIT_PER_BET}"
            )
            try:
                await self.alerter.send_message(
                    f"🟢 ON CONFIRM\n"
                    f"{watch.home_team} vs {watch.away_team}\n"
                    f"{label} @ {watch.odds_over}\n"
                    f"Stake ₦{stake:,.0f} (hard cap ₦{Config.MAX_PROFIT_PER_BET:,.0f})\n"
                    f"Waiting goal alert…"
                )
            except Exception:
                pass
        else:
            logger.warning(f"[CONFIRM] NOT visible after Place Bet ({label})")
        return ok

    def _stake_exceeds_hard_cap(self, stake: float, odds: float) -> bool:
        """True if this stake at these odds would profit above hard cap (+small tolerance)."""
        if odds <= 1.0 or stake <= 0:
            return False
        cap = float(Config.MAX_PROFIT_PER_BET)
        return self.profit_at(stake, odds) > cap * 1.02

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
        match_id = str(match_id)
        if match_id in self.watches:
            return

        h, a = self._resolve_initial_score(match_id, match)
        if (home_score, away_score) != (0, 0) and (h, a) == (0, 0):
            h, a = int(home_score), int(away_score)

        watch = MatchWatch(page, home_team, away_team, home_score=h, away_score=a)
        watch.running = True
        watch.task = asyncio.create_task(self._watch_loop(match_id, watch))
        self.watches[match_id] = watch
        logger.info(
            f"[WATCH] started {home_team} vs {away_team} "
            f"REST {h}-{a} → target Over {watch.over_line}"
        )

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

    async def _watch_loop(self, match_id: str, watch: MatchWatch):
        try:
            while watch.running:
                try:
                    if not await self._page_alive(watch.page):
                        logger.warning(f"[WATCH] page dead {match_id} — stop")
                        watch.running = False
                        break

                    label = await self.prepare_over_market(watch.page, watch)

                    if watch.last_refresh_at is None or (
                        time.time() - watch.last_refresh_at
                    ) > 8:
                        await self.fetch_balance(watch.page)

                    if label:
                        odds = await self._read_over_odds_from_slip(watch.page)
                        if odds <= 0:
                            odds = watch.odds_over
                        if odds > 0:
                            old_odds = watch.odds_over
                            watch.odds_over = odds
                            ideal_stake = self.calc_stake(odds)
                            watch.stake_over = ideal_stake
                            odds_changed = old_odds > 0 and abs(old_odds - odds) >= 0.01
                        else:
                            odds_changed = False
                            ideal_stake = self.calc_stake(watch.odds_over)
                            watch.stake_over = ideal_stake

                        confirm_up = await self._confirm_visible(watch.page)

                        need_rearm = False
                        if not confirm_up or not watch.confirm_armed:
                            need_rearm = True
                        elif odds_changed:
                            need_rearm = True
                        elif ideal_stake >= 10 and watch.odds_over > 1:
                            current_stake = await self._read_stake_from_slip(watch.page)
                            if current_stake <= 0:
                                current_stake = watch.stake_over
                            if self._stake_exceeds_hard_cap(
                                current_stake, watch.odds_over
                            ):
                                logger.info(
                                    f"[HARD_CAP] stake {current_stake:.0f} @ {watch.odds_over} "
                                    f"exceeds cap {Config.MAX_PROFIT_PER_BET} — re-arm"
                                )
                                need_rearm = True
                            elif abs(current_stake - ideal_stake) / max(ideal_stake, 1) > 0.05:
                                need_rearm = True

                        if need_rearm and ideal_stake >= 10:
                            await self._arm_over(
                                watch.page, watch, ideal_stake
                            )

                    watch.last_refresh_at = time.time()
                except Exception as e:
                    logger.debug(f"[WATCH] {e}")
                await asyncio.sleep(self._odds_interval)
        except asyncio.CancelledError:
            pass

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
        await self._click_accept_changes(page)
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
        team: str = "",
        goal_num: int = 1,
        expected_home_score: Optional[int] = None,
        expected_away_score: Optional[int] = None,
        detected_at: Optional[float] = None,
    ) -> BetResult:
        if self.stopped or not await self._page_alive(page):
            return BetResult.FAILED

        match_id = str(match.get("match_id") or "")
        home_team = match.get("home_team", "")
        away_team = match.get("away_team", "")
        match_name = f"{home_team} vs {away_team}"

        watch = self.get_watch(match_id)
        if watch is None:
            self.start_watching(
                match_id, page, home_team, away_team, match=match
            )
            watch = self.get_watch(match_id)

        if not await self._verify_match_identity(page, home_team, away_team):
            self._record_race_result(detected_at, False, match_name, "identity")
            return BetResult.ABORTED_SAFETY

        if (
            watch
            and expected_home_score is not None
            and expected_away_score is not None
        ):
            if not self._memory_score_ok(
                watch, expected_home_score, expected_away_score, match_name
            ):
                self._record_race_result(detected_at, False, match_name, "score_moved")
                return BetResult.ABORTED_SAFETY

        if not watch or not watch.active_market:
            label = await self.prepare_over_market(page, watch)
            if not label:
                logger.warning(f"[EXECUTE] no Over {watch.over_line} — abort")
                self._record_race_result(
                    detected_at, False, match_name, "no_over_market"
                )
                return BetResult.ABORTED_SAFETY

        self._record_race_result(detected_at, True, match_name, "confirmed")

        bal = await self.fetch_balance(page)
        if bal is None and (not self.balance or self.balance <= 0):
            logger.error("[BALANCE] cannot read")
            return BetResult.FAILED

        odds = watch.odds_over if watch else 0.0
        if odds <= 0:
            odds = await self._read_over_odds_from_slip(page)
        if odds <= 0:
            logger.error("[ODDS] no Over odds")
            return BetResult.FAILED

        self.last_odds_used = odds
        stake = self.calc_stake(odds)
        if stake < 10:
            return BetResult.FAILED

        if not self.validation_bet_placed:
            self.validation_bet_placed = True
            ok = await self.run_validation_bet(
                page, match, odds, goal_num, match_name, watch
            )
            if ok:
                self.validation_passed = True
                if expected_home_score is not None and expected_away_score is not None:
                    self.advance_tracked_score(
                        match_id, expected_home_score, expected_away_score
                    )
                await self._rebet_loop(
                    page, match_name, f"Over {watch.over_line}", goal_num
                )
                return BetResult.SUCCESS
            self.stopped = True
            return BetResult.FAILED

        if not self.validation_passed:
            return BetResult.FAILED

        await self.alerter.notify_goal_detected(
            match_name, f"Over {watch.over_line}", odds, goal_num
        )
        ok = await self.place_single_bet(
            page, match, stake, odds, 1, goal_num, 1, watch
        )
        if ok:
            if expected_home_score is not None and expected_away_score is not None:
                self.advance_tracked_score(
                    match_id, expected_home_score, expected_away_score
                )
            await self._rebet_loop(
                page, match_name, f"Over {watch.over_line}", goal_num
            )
            return BetResult.SUCCESS
        return BetResult.FAILED

    async def run_validation_bet(
        self, page, match, odds, goal_num, match_name, watch: MatchWatch
    ) -> bool:
        await self.fetch_balance(page)
        stake = min(
            float(Config.VALIDATION_STAKE),
            float(self.balance or Config.VALIDATION_STAKE),
        )
        await self.alerter.notify_validation_start(
            match_name, f"Over {watch.over_line}", odds, goal_num, stake
        )
        if self.balance is None:
            await self.alerter.notify_validation_step("Balance read", "FAILED")
            return False
        await self.alerter.notify_validation_step(
            "Balance read", f"₦{self.balance:,.2f}"
        )
        try:
            ok = await self.place_single_bet(
                page, match, stake, odds, 1, goal_num, 1, watch
            )
        except Exception as e:
            logger.exception(e)
            ok = False
        await self.alerter.notify_validation_result(
            ok, match_name, f"Over {watch.over_line}", stake, odds
        )
        return ok

    async def place_single_bet(
        self,
        page,
        match,
        stake,
        odds,
        stack_num,
        goal_num,
        total_stack,
        watch: MatchWatch,
    ) -> bool:
        try:
            if not await self._page_alive(page):
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

            if watch.confirm_armed and await self._confirm_visible(page):
                if not await self._click_confirm_dialog(page):
                    if not await self._arm_over(page, watch, stake):
                        await self.alerter.notify_submission_slow(
                            match_name, "No confirm", 0
                        )
                        return False
                    if not await self._click_confirm_dialog(page):
                        return False
            else:
                if not await self._arm_over(page, watch, stake):
                    await self.alerter.notify_submission_slow(
                        match_name, "Could not arm Over", 0
                    )
                    return False
                if not await self._click_confirm_dialog(page):
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
                    f"{match.get('match_id')}_O{watch.over_line}_S{stack_num}_"
                    f"{uuid.uuid4().hex[:8]}"
                )
                await self.alerter.notify_bet_placed(
                    match_name,
                    f"Over {watch.over_line}",
                    stake,
                    odds,
                    bet_id,
                    stack_num,
                    total_stack,
                )
                with open("bets.log", "a", encoding="utf-8") as f:
                    f.write(
                        f"{datetime.now()}: {match_name}|Over {watch.over_line}|"
                        f"₦{stake}|@{odds}|{bet_id}\n"
                    )
                if self.balance:
                    self.balance = max(0.0, self.balance - stake)
                watch.confirm_armed = False
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
