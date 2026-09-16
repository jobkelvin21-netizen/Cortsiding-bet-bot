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
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.last_refresh_at: Optional[float] = None
        self.last_odds_fingerprint: str = ""


class BetExecutor:
    # Dynamic market patterns — numbers not hardcoded
    # H1 examples: "1st Half - 1st Goal", "1st Half - 2nd Goal", ...
    H1_GOAL_RE = re.compile(
        r'1st\s*half\s*[-–:]?\s*(\d+)(?:st|nd|rd|th)?\s*goal'
        r'|first\s*half\s*[-–:]?\s*(\d+)(?:st|nd|rd|th)?\s*goal'
        r'|1h\s*[-–:]?\s*(\d+)(?:st|nd|rd|th)?\s*goal',
        re.I,
    )
    # Full-match examples: "1st Goal", "2nd Goal", "4th Goal", "Next Goal"
    MATCH_GOAL_RE = re.compile(
        r'(?:^|\n)\s*(\d+)(?:st|nd|rd|th)?\s*goal\b'
        r'|\bnext\s*goal\b',
        re.I,
    )

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
        self.race_gaps_ms = []
        self.watches: Dict[str, MatchWatch] = {}
        self._odds_interval = getattr(Config, "ODDS_TRACK_INTERVAL", 1.5)

    # ------------------------------------------------------------------
    # Dynamic next-goal market discovery (no hardcoded 1st/2nd/3rd)
    # ------------------------------------------------------------------

    async def _list_goal_markets(self, page: Page) -> Tuple[List[str], List[str]]:
        """
        Returns (h1_markets, full_match_markets) currently visible on page.
        SportyBet labels change with score: 1st Goal, 2nd Goal, 1st Half - 2nd Goal, etc.
        """
        try:
            text = await page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
        except Exception:
            return [], []

        h1 = []
        full = []
        # Scan line by line for market headers
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line or len(line) > 80:
                continue
            low = line.lower()
            if "over/under" in low or "corner" in low or "handicap" in low:
                continue

            if self.H1_GOAL_RE.search(line):
                if line not in h1:
                    h1.append(line)
                continue

            # full match goal markets — avoid double-counting H1 lines
            if "half" in low:
                continue
            if re.search(r'\b\d+(?:st|nd|rd|th)?\s*goal\b', low) or re.search(
                r'\bnext\s*goal\b', low
            ):
                if line not in full:
                    full.append(line)

        return h1, full

    async def prepare_goal_market(self, page: Page, watch: Optional[MatchWatch] = None) -> str:
        """
        Prefer first-half next-goal market if present.
        Else full-match next-goal market.
        Numbers are discovered live from the page.
        """
        # Goals tab helps reveal markets
        try:
            tab = page.get_by_text("Goals", exact=True)
            if await tab.count() > 0:
                await tab.first.click(timeout=1500)
                await asyncio.sleep(0.5)
        except Exception:
            pass

        h1_markets, full_markets = await self._list_goal_markets(page)

        # Priority 1: any visible 1st-half * Goal market
        for label in h1_markets:
            if await self._click_market_label(page, label):
                if watch is not None:
                    watch.active_market = label
                logger.info(f"[MARKET] Using H1 goal market: {label}")
                return label

        # Priority 2: full-match * Goal / Next Goal
        for label in full_markets:
            if await self._click_market_label(page, label):
                if watch is not None:
                    watch.active_market = label
                logger.info(f"[MARKET] Using match goal market: {label}")
                return label

        # Last resort: try clicking text patterns via locator without fixed numbers
        for pattern in (
            re.compile(r'1st\s*half.*goal', re.I),
            re.compile(r'first\s*half.*goal', re.I),
            re.compile(r'\d+(?:st|nd|rd|th)?\s*goal', re.I),
            re.compile(r'next\s*goal', re.I),
        ):
            try:
                # find elements whose text matches
                handles = page.locator("div, span, h3, h4, button")
                count = min(await handles.count(), 120)
                for i in range(count):
                    el = handles.nth(i)
                    try:
                        t = (await el.text_content(timeout=300) or "").strip()
                    except Exception:
                        continue
                    if t and pattern.search(t) and len(t) < 60:
                        if "over" in t.lower() or "under" in t.lower():
                            continue
                        try:
                            await el.click(timeout=1000)
                            if watch is not None:
                                watch.active_market = t
                            logger.info(f"[MARKET] Pattern click: {t}")
                            return t
                        except Exception:
                            continue
            except Exception:
                continue

        logger.debug("[MARKET] No goal market found on page yet")
        return ""

    async def _click_market_label(self, page: Page, label: str) -> bool:
        try:
            loc = page.get_by_text(label, exact=False)
            if await loc.count() == 0:
                return False
            await loc.first.click(timeout=1500)
            await asyncio.sleep(0.4)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Watch / odds / stake
    # ------------------------------------------------------------------

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
                    if watch.page.is_closed():
                        break

                    await self.prepare_goal_market(watch.page, watch)

                    home_odds = await self._read_odds_from_click(watch.page, "Home")
                    away_odds = await self._read_odds_from_click(watch.page, "Away")
                    if home_odds <= 0:
                        home_odds = await self._read_odds_from_click(
                            watch.page, watch.home_team
                        )
                    if away_odds <= 0:
                        away_odds = await self._read_odds_from_click(
                            watch.page, watch.away_team
                        )

                    changed = False
                    if home_odds > 0:
                        old = watch.odds_by_team.get(watch.home_team)
                        watch.odds_by_team[watch.home_team] = home_odds
                        watch.stake_by_team[watch.home_team] = self.calc_stake(home_odds)
                        if old is None or abs(old - home_odds) >= 0.01:
                            changed = True
                    if away_odds > 0:
                        old = watch.odds_by_team.get(watch.away_team)
                        watch.odds_by_team[watch.away_team] = away_odds
                        watch.stake_by_team[watch.away_team] = self.calc_stake(away_odds)
                        if old is None or abs(old - away_odds) >= 0.01:
                            changed = True

                    watch.last_refresh_at = time.time()
                    if changed:
                        fp = (
                            f"H@{watch.odds_by_team.get(watch.home_team, 0):.2f}/"
                            f"{watch.stake_by_team.get(watch.home_team, 0):.0f} "
                            f"A@{watch.odds_by_team.get(watch.away_team, 0):.2f}/"
                            f"{watch.stake_by_team.get(watch.away_team, 0):.0f}"
                        )
                        if fp != watch.last_odds_fingerprint:
                            watch.last_odds_fingerprint = fp
                            logger.debug(
                                f"[WATCH] {watch.home_team} vs {watch.away_team} {fp} "
                                f"market={watch.active_market}"
                            )
                except Exception as e:
                    logger.debug(f"[WATCH] {e}")
                await asyncio.sleep(self._odds_interval)
        except asyncio.CancelledError:
            pass

    def calc_stake(self, odds: float) -> float:
        if odds <= 1.0 or not self.balance or self.balance <= 0:
            return 0
        return min(float(self.balance), float(Config.MAX_PROFIT_PER_BET) / (odds - 1))

    def can_split_by_balance(self, stake: float) -> int:
        if stake <= 0 or not self.balance:
            return 1
        return min(int(self.balance / stake), 3)

    async def fetch_balance(self, page: Page) -> Optional[float]:
        selectors = [
            '.user-balance', '.account-balance', '[data-testid="balance"]',
            '[class*="balance"]', 'span:has-text("₦")',
        ]

        async def try_one(sel):
            try:
                el = await page.query_selector(sel)
                if not el:
                    return None
                text = await el.text_content()
                if not text:
                    return None
                m = re.search(
                    r'([\d.]+)',
                    text.replace('₦', '').replace('NGN', '').replace(',', ''),
                )
                return float(m.group(1)) if m else None
            except Exception:
                return None

        for bal in await asyncio.gather(*(try_one(s) for s in selectors)):
            if bal is not None:
                self.balance = bal
                if self.current_account:
                    self.account_manager.update_balance(
                        self.current_account['username'], bal
                    )
                return bal
        return None

    async def is_dying_minutes(self, page: Page) -> bool:
        try:
            body = await page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            m = re.search(r'\b(\d{1,2}):\d{2}\s*H([12])\b', body or '', re.I)
            if m:
                return m.group(2) == '2' and int(m.group(1)) >= 35
            return False
        except Exception:
            return False

    def should_split(self, odds: float, is_dying: bool, split_count: int) -> bool:
        if split_count >= 2:
            return True
        return odds >= 10.0 and is_dying

    async def _read_odds_from_click(self, page: Page, selection_text: str) -> float:
        if not selection_text:
            return 0.0
        try:
            candidates = [
                page.get_by_role("button", name=selection_text, exact=False),
                page.locator('[class*="selection"]').filter(has_text=selection_text),
                page.get_by_text(selection_text, exact=False),
            ]
            for loc in candidates:
                try:
                    if await loc.count() == 0:
                        continue
                    txt = await loc.first.text_content(timeout=800)
                    if not txt:
                        continue
                    m = re.search(r'(\d+\.\d{1,3})', txt.replace(',', '.'))
                    if m:
                        odds = float(m.group(1))
                        if 1.01 <= odds <= 500:
                            return odds
                except Exception:
                    continue
            return 0.0
        except Exception:
            return 0.0

    async def _read_score_from_page(self, page: Page) -> Optional[tuple]:
        try:
            result = await page.evaluate(
                """() => {
                    const text = document.body ? document.body.innerText : '';
                    const lines = text.split('\\n').map(x => x.trim()).filter(Boolean).slice(0, 50);
                    const nums = [];
                    for (const ln of lines) {
                        if (/^\\d{1,2}$/.test(ln)) nums.push(parseInt(ln, 10));
                    }
                    if (nums.length >= 2 && nums[0] <= 20 && nums[1] <= 20) {
                        return [nums[0], nums[1]];
                    }
                    const m = text.match(/\\b(\\d{1,2})\\s*[-:]\\s*(\\d{1,2})\\b/);
                    if (m) return [parseInt(m[1], 10), parseInt(m[2], 10)];
                    return null;
                }"""
            )
            if result and len(result) == 2:
                h, a = int(result[0]), int(result[1])
                if 0 <= h <= 20 and 0 <= a <= 20:
                    return h, a
            return None
        except Exception:
            return None

    async def _score_still_matches(
        self, page, match_id, expected_home, expected_away, match_name
    ) -> bool:
        current = await self._read_score_from_page(page)
        if current is None:
            logger.warning(f"[SAFETY] {match_name} score unavailable")
            return False
        ch, ca = current
        if ch + ca < expected_home + expected_away:
            logger.info(
                f"[SAFETY] {match_name} page {ch}-{ca} behind "
                f"{expected_home}-{expected_away} — allow"
            )
            return True
        logger.warning(
            f"SAFETY ABORT {match_name} page {ch}-{ca} "
            f"expected {expected_home}-{expected_away}"
        )
        return False

    async def _verify_match_identity(self, page, expected_home, expected_away) -> bool:
        try:
            header_text = (
                await page.evaluate(
                    "() => document.body ? document.body.innerText.slice(0, 1800) : ''"
                )
                or ""
            ).lower()
            home_ok = any(w.lower() in header_text for w in expected_home.split() if len(w) > 3)
            away_ok = any(w.lower() in header_text for w in expected_away.split() if len(w) > 3)
            if home_ok and away_ok:
                return True
            ht = expected_home.split()[0].lower() if expected_home else ""
            at = expected_away.split()[0].lower() if expected_away else ""
            return bool(ht and at and ht in header_text and at in header_text)
        except Exception:
            return False

    def _record_race_result(self, detected_at, won, match_name, reason):
        if detected_at is None:
            return
        gap = (time.time() - detected_at) * 1000
        self.race_gaps_ms.append(gap)
        if len(self.race_gaps_ms) > 200:
            self.race_gaps_ms.pop(0)
        if won:
            self.race_wins += 1
        else:
            self.race_losses += 1
        total = self.race_wins + self.race_losses
        avg = sum(self.race_gaps_ms) / len(self.race_gaps_ms)
        logger.info(
            f"[RACE] {match_name} | {'WIN' if won else 'LOSS'} ({reason}) | "
            f"gap={gap:.0f}ms avg={avg:.0f}ms "
            f"{self.race_wins}/{total}"
        )

    async def execute(
        self,
        page: Page,
        match: dict,
        team: str,
        goal_num: int = 1,
        expected_home_score: int = None,
        expected_away_score: int = None,
        detected_at: float = None,
    ) -> BetResult:
        if self.stopped:
            return BetResult.FAILED

        match_id = match.get("match_id")
        match_name = f"{match['home_team']} vs {match['away_team']}"

        if not await self._verify_match_identity(
            page, match.get("home_team", ""), match.get("away_team", "")
        ):
            self._record_race_result(detected_at, False, match_name, "identity")
            return BetResult.ABORTED_SAFETY

        if expected_home_score is not None and expected_away_score is not None:
            if not await self._score_still_matches(
                page, match_id, expected_home_score, expected_away_score, match_name
            ):
                self._record_race_result(detected_at, False, match_name, "score_moved")
                return BetResult.ABORTED_SAFETY

        self._record_race_result(detected_at, True, match_name, "confirmed")

        await self.prepare_goal_market(page, self.get_watch(match_id))

        watch = self.get_watch(match_id)
        odds = 0.0
        if watch and team in watch.odds_by_team:
            odds = watch.odds_by_team[team]
        if odds <= 0:
            side = "Home" if team == match.get("home_team") else "Away"
            odds = await self._read_odds_from_click(page, side)
        if odds <= 0:
            odds = await self._read_odds_from_click(page, team)
        if odds <= 0:
            logger.error(f"[ODDS] none for {team}")
            return BetResult.FAILED

        self.last_odds_used = odds

        if not self.validation_bet_placed:
            self.validation_bet_placed = True
            ok = await self.run_validation_bet(page, match, team, odds, goal_num, match_name)
            if ok:
                self.validation_passed = True
                return BetResult.SUCCESS
            self.stopped = True
            return BetResult.FAILED
        if not self.validation_passed:
            return BetResult.FAILED

        if not self.balance or self.balance <= 0:
            await self.fetch_balance(page)
        stake = self.calc_stake(odds)
        if stake < 100:
            return BetResult.FAILED

        split_count = self.can_split_by_balance(stake)
        dying = await self.is_dying_minutes(page)
        if self.should_split(odds, dying, split_count):
            ok = await self.split_bet(page, match, team, odds, goal_num, split_count, dying)
            return BetResult.SUCCESS if ok else BetResult.FAILED

        await self.alerter.notify_goal_detected(match_name, team, odds, goal_num)
        ok = await self.place_single_bet(page, match, team, stake, odds, 1, goal_num, 1)
        return BetResult.SUCCESS if ok else BetResult.FAILED

    async def run_validation_bet(self, page, match, team, odds, goal_num, match_name):
        stake = Config.VALIDATION_STAKE
        await self.alerter.notify_validation_start(match_name, team, odds, goal_num, stake)
        bal = await self.fetch_balance(page)
        if bal is None:
            await self.alerter.notify_validation_step("Balance read", "FAILED")
            return False
        await self.alerter.notify_validation_step("Balance read", f"₦{bal:,.2f}")
        try:
            ok = await self.place_single_bet(page, match, team, stake, odds, 1, goal_num, 1)
        except Exception as e:
            logger.exception(e)
            ok = False
        await self.alerter.notify_validation_result(ok, match_name, team, stake, odds)
        return ok

    async def split_bet(self, page, match, team, odds, goal_num, split_count, dying):
        match_name = f"{match['home_team']} vs {match['away_team']}"
        if not self.balance:
            return False
        split_stake = self.balance / split_count
        await self.alerter.notify_goal_detected(match_name, team, odds, goal_num)
        total_bets = 0
        total_staked = 0.0
        for i in range(split_count):
            if not self.balance or self.balance < 100:
                break
            ok = await self.place_single_bet(
                page, match, team, split_stake, odds, i + 1, goal_num, split_count
            )
            if not ok:
                break
            total_bets += 1
            total_staked += split_stake
            self.balance -= split_stake
        if total_bets:
            await self.alerter.notify_stack_complete(match_name, total_bets, total_staked)
        return total_bets > 0

    async def place_single_bet(
        self, page, match, team, stake, odds, stack_num, goal_num, total_stack=1,
        skip_market_selection=False,
    ) -> bool:
        try:
            start = time.time()
            match_name = f"{match['home_team']} vs {match['away_team']}"
            await page.bring_to_front()
            await self.prepare_goal_market(page, self.get_watch(match.get("match_id")))

            if not skip_market_selection:
                labels = (
                    ["Home", team] if team == match.get("home_team")
                    else ["Away", team] if team == match.get("away_team")
                    else [team]
                )
                clicked = False
                for label in labels:
                    try:
                        loc = page.get_by_role("button", name=label, exact=False)
                        if await loc.count() > 0:
                            await loc.first.click(force=True, timeout=1200)
                            clicked = True
                            break
                    except Exception:
                        pass
                    try:
                        loc = page.get_by_text(label, exact=False)
                        if await loc.count() > 0:
                            await loc.first.click(force=True, timeout=1200)
                            clicked = True
                            break
                    except Exception:
                        pass
                if not clicked:
                    logger.warning(f"[SELECT] could not click {team}")
                    return False

            stake_input = await page.query_selector(
                'input[type="number"], input[type="tel"], input[inputmode="numeric"], '
                'input[placeholder*="Stake"], input[placeholder*="stake"]'
            )
            if stake_input:
                await stake_input.fill(str(int(stake)))
            else:
                await self.fast.fast_type(page, 'input', str(int(stake)), human_delay=False)

            clicked = await self.fast.fast_click(
                page, 'button:has-text("Confirm")', timeout=1200, human_delay=False
            )
            if not clicked:
                clicked = await self.fast.fast_click(
                    page, 'button:has-text("Accept Changes")', timeout=800, human_delay=False
                )
            if not clicked:
                clicked = await self.fast.fast_click(
                    page, 'button:has-text("Place Bet")', timeout=800, human_delay=False
                )
            if not clicked:
                await self.alerter.notify_submission_slow(match_name, "No confirm button", 0)
                return False

            try:
                await page.wait_for_selector(
                    'text=Rebet, text=Success, [class*="bet-success"], [class*="success"]',
                    timeout=Config.CONFIRMATION_TIMEOUT_MS,
                )
                elapsed = time.time() - start
                if elapsed > Config.SUBMISSION_TIMEOUT:
                    await self.alerter.notify_submission_slow(
                        match_name, "slow confirm", elapsed
                    )
                    result = await self.handle_slow(match)
                    return result in (BetResult.SUCCESS, BetResult.SWITCHED)
                self.consecutive_slow = 0
                bet_id = f"{match['match_id']}_G{goal_num}_S{stack_num}_{uuid.uuid4().hex[:8]}"
                await self.alerter.notify_bet_placed(
                    match_name, team, stake, odds, bet_id, stack_num, total_stack
                )
                with open('bets.log', 'a') as f:
                    f.write(f"{datetime.now()}: {match_name}|{team}|₦{stake}|@{odds}|{bet_id}\n")
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
            old_user = self.current_account['username'] if self.current_account else 'unknown'
            await self.alerter.notify_account_flagged(old_user)
            old, new = self.account_manager.mark_limited()
            if new:
                await self.alerter.notify_account_switched(old, new['username'])
                self.current_account = new
                self.consecutive_slow = 0
                return BetResult.SWITCHED
            self.stopped = True
            return BetResult.FAILED
        return BetResult.FAILED
