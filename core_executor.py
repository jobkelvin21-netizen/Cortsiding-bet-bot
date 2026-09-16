import asyncio
import random
import re
import time
import uuid
from enum import Enum, auto
from datetime import datetime
from typing import Optional, Dict
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


class ScoreState(Enum):
    UNAVAILABLE = auto()
    INVALID = auto()
    MATCHES = auto()
    CHANGED = auto()


class MatchWatch:
    """
    Background state tracked continuously for one pre-opened match page.
    Holds the most recently known odds for each selection, kept fresh by
    a background loop — so at goal-time, execute() only needs to CONFIRM
    the cached values are still valid, not read them from scratch.
    """
    def __init__(self, page: Page, home_team: str, away_team: str):
        self.page = page
        self.home_team = home_team
        self.away_team = away_team
        self.odds_by_team: Dict[str, float] = {}
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.last_refresh_at: Optional[float] = None


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
        self.match_goal_count = {}
        self.last_odds_used = 0.0

        self.validation_bet_placed = False
        self.validation_passed = False

        # Race-timing stats
        self.race_wins = 0
        self.race_losses = 0
        self.race_gaps_ms = []

        # NEW: background match watchers — keyed by match_id, always
        # tracking odds in the background so goal-time is confirm-only
        self.watches: Dict[str, MatchWatch] = {}

    # =========================================================================
    # BACKGROUND WATCH — always-on odds tracking per pre-opened match page
    # =========================================================================

    def start_watching(self, match_id: str, page: Page, home_team: str, away_team: str):
        """Call this the moment a match page is pre-opened (on link), so
        odds tracking starts immediately, ahead of any goal."""
        if match_id in self.watches:
            return

        watch = MatchWatch(page, home_team, away_team)
        watch.running = True
        watch.task = asyncio.create_task(self._watch_loop(match_id, watch))
        self.watches[match_id] = watch
        logger.debug(f"[WATCH] Started background tracking for {home_team} vs {away_team}")

    def stop_watching(self, match_id: str):
        watch = self.watches.pop(match_id, None)
        if watch:
            watch.running = False
            if watch.task:
                watch.task.cancel()
            logger.debug(f"[WATCH] Stopped tracking match_id={match_id}")

    async def _watch_loop(self, match_id: str, watch: MatchWatch):
        try:
            while watch.running:
                try:
                    home_odds = await self._read_odds_from_click(watch.page, watch.home_team)
                    away_odds = await self._read_odds_from_click(watch.page, watch.away_team)

                    if home_odds > 0:
                        watch.odds_by_team[watch.home_team] = home_odds
                    if away_odds > 0:
                        watch.odds_by_team[watch.away_team] = away_odds

                    watch.last_refresh_at = time.time()

                except Exception as e:
                    logger.debug(f"[WATCH] Odds refresh error for {watch.home_team} vs {watch.away_team}: {e}")

                await asyncio.sleep(Config.ODDS_TRACK_INTERVAL)
        except asyncio.CancelledError:
            pass

    def get_watch(self, match_id: str) -> Optional[MatchWatch]:
        return self.watches.get(match_id)

    # =========================================================================
    # BALANCE — runs all selectors concurrently, never silently stale
    # =========================================================================

    async def fetch_balance(self, page: Page) -> Optional[float]:
        selectors = [
            '.user-balance',
            '.account-balance',
            '[data-testid="balance"]',
            '[class*="balance"]',
            'span:has-text("₦")',
        ]

        async def try_selector(sel):
            try:
                elem = await page.query_selector(sel)
                if not elem:
                    return None
                text = await elem.text_content()
                if not text:
                    return None
                cleaned = text.replace('₦', '').replace('NGN', '').replace(',', '').strip()
                match = re.search(r'([\d.]+)', cleaned)
                if match:
                    return float(match.group(1))
                return None
            except Exception:
                return None

        results = await asyncio.gather(*(try_selector(sel) for sel in selectors))

        for bal in results:
            if bal is not None:
                self.balance = bal
                if self.current_account:
                    self.account_manager.update_balance(self.current_account['username'], bal)
                return bal

        logger.error("[BALANCE_READ_ERROR] No balance selector succeeded — balance NOT refreshed")
        return None

    def calc_stake(self, odds: float) -> float:
        if odds <= 1.0 or not self.balance or self.balance <= 0:
            return 0
        stake_for_profit = Config.MAX_PROFIT_PER_BET / (odds - 1)
        return min(self.balance, stake_for_profit)

    def can_split_by_balance(self, stake: float) -> int:
        if stake <= 0 or not self.balance:
            return 1
        max_splits = int(self.balance / stake)
        return min(max_splits, 3)

    # =========================================================================
    # MINUTE CHECK
    # =========================================================================

    async def is_dying_minutes(self, page: Page) -> bool:
        try:
            time_elem = await page.query_selector(
                '[data-testid="match-time"], [class*="timer"], [class*="match-time"], [class*="time"]'
            )
            if not time_elem:
                return False

            raw = (await time_elem.text_content() or '').strip().upper()

            if raw in ('HT', 'FT', 'HALFTIME', 'FULL TIME', 'HALF TIME'):
                return False

            m = re.match(r'^(\d+)', raw)
            if m:
                minute = int(m.group(1))
                return minute >= 80
            return False
        except Exception:
            return False

    def should_split(self, odds: float, is_dying: bool, split_count: int) -> bool:
        if split_count >= 2:
            return True
        if odds >= 10.0 and is_dying:
            return True
        return False

    # =========================================================================
    # ODDS READER — narrow selector first, safe (locator-based) construction,
    # broad fallback only if needed
    # =========================================================================

    async def _read_odds_from_click(self, page: Page, selection_text: str) -> float:
        if not selection_text:
            return 0.0

        try:
            clean_name = re.sub(r'\s+(FC|CF|SC|AFC|United|City)$', '', selection_text, flags=re.IGNORECASE).strip()
            keywords = [w for w in clean_name.split() if len(w) > 2]

            # Locator API used throughout — no raw f-string CSS interpolation
            # of arbitrary team names, safe against quotes/special characters
            candidates = [
                page.get_by_role("button", name=selection_text, exact=False),
                page.locator('[class*="selection"]').filter(has_text=selection_text),
            ]

            if keywords:
                candidates.append(page.get_by_role("button", name=keywords[0], exact=False))
                candidates.append(page.locator('[class*="selection"]').filter(has_text=keywords[0]))

            for locator in candidates:
                try:
                    count = await locator.count()
                    if count == 0:
                        continue

                    full_text = await locator.first.text_content(timeout=1500)
                    if not full_text:
                        continue

                    match = re.search(r'(\d+\.\d{1,3}|\d+)', full_text.replace(',', '.'))
                    if match:
                        odds = float(match.group(1))
                        if 1.01 <= odds <= 500:
                            return odds
                except Exception as e:
                    logger.debug(f"[ODDS_PARSE] Candidate selector failed for '{selection_text}': {e}")
                    continue

            # Broad fallback, last resort only
            try:
                locator = page.get_by_text(selection_text, exact=False)
                if await locator.count() > 0:
                    full_text = await locator.first.text_content(timeout=1200)
                    match = re.search(r'(\d+\.\d{1,3})', (full_text or '').replace(',', '.'))
                    if match:
                        odds = float(match.group(1))
                        if 1.01 <= odds <= 500:
                            return odds
            except Exception as e:
                logger.debug(f"[ODDS_PARSE] Broad fallback failed for '{selection_text}': {e}")

            return 0.0

        except Exception as e:
            logger.error(f"[ODDS_PARSE] Error reading odds for '{selection_text}': {e}")
            return 0.0

    # =========================================================================
    # SCORE SAFETY CHECK — DOM-based, reading the pre-opened page directly,
    # not the REST feed (removes REST-staleness from the hot path entirely)
    # =========================================================================

    async def _read_score_from_page(self, page: Page) -> Optional[tuple]:
        try:
            score_elem = await page.query_selector(
                '[data-testid="match-score"], [class*="match-score"], [class*="score-board"]'
            )
            if not score_elem:
                return None

            text = (await score_elem.text_content() or "").strip()
            m = re.match(r'^\s*(\d+)\s*[-:]\s*(\d+)\s*$', text)
            if not m:
                return None

            return int(m.group(1)), int(m.group(2))
        except Exception as e:
            logger.debug(f"[SCORE_SELECTOR_ERROR] DOM score read failed: {e}")
            return None

    async def _score_still_matches(self, page: Page, match_id: str,
                                    expected_home: int, expected_away: int,
                                    match_name: str) -> bool:
        current = await self._read_score_from_page(page)

        if current is None:
            logger.warning(f"[SAFETY] {match_name} (match_id={match_id}) — score element unavailable on page")
            return False

        current_home, current_away = current
        expected_total = expected_home + expected_away
        current_total = current_home + current_away

        if current_total < expected_total:
            logger.info(
                f"[SAFETY] {match_name} — page still shows {current_home}-{current_away} "
                f"vs expected {expected_home}-{expected_away}. Allowing bet."
            )
            return True

        logger.warning(
            f"⚠️ SAFETY ABORT: {match_name} — page score is {current_home}-{current_away} "
            f"(expected {expected_home}-{expected_away}). Market already updated."
        )
        return False

    # =========================================================================
    # MATCH IDENTITY CHECK — targeted header read, not whole-page scan
    # =========================================================================

    async def _verify_match_identity(self, page: Page, expected_home: str, expected_away: str) -> bool:
        try:
            header = await page.query_selector(
                '[data-testid="match-header"], [class*="match-header"], h1, h2'
            )
            if header:
                header_text = (await header.text_content() or "").lower()
            else:
                header_text = (await page.evaluate("document.body.innerText") or "").lower()

            home_ok = any(w.lower() in header_text for w in expected_home.split() if len(w) > 3)
            away_ok = any(w.lower() in header_text for w in expected_away.split() if len(w) > 3)

            if not (home_ok and away_ok):
                logger.warning(f"[IDENTITY] {expected_home} vs {expected_away} — page does not appear to show this match")
                return False
            return True
        except Exception as e:
            logger.error(f"[IDENTITY] Error checking {expected_home} vs {expected_away}: {e}")
            return False

    # =========================================================================
    # RACE TIMING
    # =========================================================================

    def _record_race_result(self, detected_at: Optional[float], won: bool,
                             match_name: str, reason: str):
        if detected_at is None:
            return

        gap_ms = (time.time() - detected_at) * 1000
        self.race_gaps_ms.append(gap_ms)
        if len(self.race_gaps_ms) > 200:
            self.race_gaps_ms.pop(0)

        if won:
            self.race_wins += 1
        else:
            self.race_losses += 1

        total = self.race_wins + self.race_losses
        win_rate = (self.race_wins / total * 100) if total else 0.0
        avg_gap = sum(self.race_gaps_ms) / len(self.race_gaps_ms)

        result_label = "WIN" if won else "LOSS"
        logger.info(
            f"[RACE] {match_name} | result={result_label} ({reason}) | "
            f"gap={gap_ms:.0f}ms | avg_gap={avg_gap:.0f}ms | "
            f"win_rate={win_rate:.1f}% ({self.race_wins}/{total})"
        )

    # =========================================================================
    # MAIN EXECUTE — always-confirm design: checks pre-tracked state,
    # confirms it's still valid at the exact goal moment, then bets.
    # No fresh odds/score computation happens here unless the watch
    # cache is missing (fallback path only).
    # =========================================================================

    async def execute(self, page: Page, match: dict, team: str, goal_num: int = 1,
                      expected_home_score: int = None,
                      expected_away_score: int = None,
                      detected_at: float = None) -> BetResult:

        if self.stopped:
            return BetResult.FAILED

        match_id = match.get('match_id')
        match_name = f"{match['home_team']} vs {match['away_team']}"

        # --- CONFIRM #1: identity ---
        if not await self._verify_match_identity(page, match.get('home_team', ''), match.get('away_team', '')):
            self._record_race_result(detected_at, won=False, match_name=match_name, reason="identity_mismatch")
            return BetResult.ABORTED_SAFETY

        # --- CONFIRM #2: score hasn't moved past this goal ---
        if expected_home_score is not None and expected_away_score is not None:
            if not await self._score_still_matches(page, match_id, expected_home_score, expected_away_score, match_name):
                self._record_race_result(detected_at, won=False, match_name=match_name, reason="score_moved")
                return BetResult.ABORTED_SAFETY

        # Passed both confirmations — this is a won race
        self._record_race_result(detected_at, won=True, match_name=match_name, reason="confirmed")

        # --- CONFIRM #3: use pre-tracked odds if available, else read fresh (fallback only) ---
        watch = self.get_watch(match_id)
        odds = 0.0
        if watch and team in watch.odds_by_team:
            odds = watch.odds_by_team[team]
            logger.debug(f"[CONFIRM] Using pre-tracked odds for {team}: {odds}")
        else:
            logger.debug(f"[CONFIRM] No pre-tracked odds for {team} — reading fresh (fallback)")
            odds = await self._read_odds_from_click(page, team)

        if odds <= 0:
            logger.error(f"[ODDS] No valid odds found for {team} on {match_name}")
            return BetResult.FAILED

        self.last_odds_used = odds
        logger.info(f"Odds confirmed: {team} @ {odds}")

        # Validation bet (first bet only)
        if not self.validation_bet_placed:
            self.validation_bet_placed = True
            success = await self.run_validation_bet(page, match, team, odds, goal_num, match_name)
            if success:
                self.validation_passed = True
                logger.success("✅ Validation passed — full staking now ACTIVE")
                return BetResult.SUCCESS
            else:
                self.stopped = True
                logger.error("❌ Validation failed — bot stopped")
                return BetResult.FAILED

        if not self.validation_passed:
            return BetResult.FAILED

        fresh_balance = await self.fetch_balance(page)
        if fresh_balance is None:
            logger.error("[BALANCE] Aborting — could not read balance")
            return BetResult.FAILED

        stake = self.calc_stake(odds)
        if stake < 100:
            logger.warning(f"Stake too low: {stake}")
            return BetResult.FAILED

        split_count = self.can_split_by_balance(stake)
        dying = await self.is_dying_minutes(page)

        if self.should_split(odds, dying, split_count):
            success = await self.split_bet(page, match, team, odds, goal_num, split_count, dying)
            return BetResult.SUCCESS if success else BetResult.FAILED
        else:
            await self.alerter.notify_goal_detected(match_name, team, odds, goal_num)
            success = await self.place_single_bet(page, match, team, stake, odds, 1, goal_num, total_stack=1)
            return BetResult.SUCCESS if success else BetResult.FAILED

    async def run_validation_bet(self, page: Page, match: dict, team: str, odds: float,
                                 goal_num: int, match_name: str) -> bool:
        stake = Config.VALIDATION_STAKE
        logger.info(f"🔬 VALIDATION BET: ₦{stake} on {team} @ {odds}")
        await self.alerter.notify_validation_start(match_name, team, odds, goal_num, stake)

        fresh_balance = await self.fetch_balance(page)
        if fresh_balance is None:
            await self.alerter.notify_validation_step("Balance read", "FAILED")
            return False

        await self.alerter.notify_validation_step("Balance read", f"₦{fresh_balance:,.2f}")

        try:
            success = await self.place_single_bet(
                page, match, team, stake, odds, stack_num=1, goal_num=goal_num,
                total_stack=1, skip_market_selection=False
            )
        except Exception as e:
            success = False
            logger.exception(f"[VALIDATION] {e}")

        if success:
            await self.alerter.notify_validation_result(True, match_name, team, stake, odds)
        else:
            await self.alerter.notify_validation_result(False, match_name, team, stake, odds)
        return success

    async def split_bet(self, page: Page, match: dict, team: str, odds: float,
                        goal_num: int, split_count: int, dying: bool) -> bool:
        match_name = f"{match['home_team']} vs {match['away_team']}"

        if not self.balance:
            return False

        split_stake = self.balance / split_count
        reason = "Balance allows" if not dying else f"High odds {odds} + dying minutes"
        logger.info(f"💰 SPLITTING ({reason}): {split_count} bets of ₦{split_stake:,.0f}")

        await self.alerter.notify_goal_detected(match_name, team, odds, goal_num)

        total_bets = 0
        total_staked = 0.0

        for i in range(split_count):
            if not self.balance or self.balance < 100:
                break

            success = await self.place_single_bet(
                page, match, team, split_stake, odds, i + 1, goal_num, split_count
            )

            if success:
                total_bets += 1
                total_staked += split_stake
                self.balance -= split_stake
            else:
                break

        if total_bets > 0:
            await self.alerter.notify_stack_complete(match_name, total_bets, total_staked)

        return total_bets > 0

    async def place_single_bet(self, page: Page, match: dict, team: str, stake: float,
                               odds: float, stack_num: int, goal_num: int, total_stack: int = 1,
                               skip_market_selection: bool = False) -> bool:
        try:
            start_time = time.time()
            match_name = f"{match['home_team']} vs {match['away_team']}"

            # Artificial "human-like" delays removed — speed prioritized
            await page.bring_to_front()

            if not skip_market_selection:
                clicked = False
                try:
                    locator = page.get_by_role("button", name=team, exact=False)
                    if await locator.count() > 0:
                        await locator.first.click(force=True, timeout=1500)
                        clicked = True
                except Exception as e:
                    logger.debug(f"[MARKET_SELECTION] Role-based click failed: {e}")

                if not clicked:
                    try:
                        locator = page.get_by_text(team, exact=False)
                        if await locator.count() > 0:
                            await locator.first.click(force=True, timeout=1500)
                            clicked = True
                    except Exception as e:
                        logger.debug(f"[MARKET_SELECTION] Text-based click failed: {e}")

                if not clicked:
                    first_word = team.split()[0] if team else ""
                    if first_word:
                        try:
                            locator = page.get_by_text(first_word, exact=False)
                            if await locator.count() > 0:
                                await locator.first.click(force=True, timeout=1500)
                                clicked = True
                        except Exception as e:
                            logger.debug(f"[MARKET_SELECTION] First-word click failed: {e}")

                if not clicked:
                    logger.warning(f"[MARKET_SELECTION] Could not click selection for '{team}'")
                    return False

            stake_input = await page.query_selector(
                'input[type="number"], input[type="tel"], input[inputmode="numeric"], input[placeholder*="Stake"]'
            )
            if stake_input:
                await stake_input.fill(str(int(stake)))
            else:
                await self.fast.fast_type(page, 'input', str(int(stake)), human_delay=False)

            clicked = await self.fast.fast_click(page, 'button:has-text("Accept Changes")', human_delay=False)
            if not clicked:
                clicked = await self.fast.fast_click(page, 'button:has-text("Place Bet")', human_delay=False)

            if not clicked:
                logger.error("[CONFIRMATION_TIMEOUT] Submit button not found")
                await self.alerter.notify_submission_slow(match_name, "Submit button not found", 0)
                return False

            try:
                await page.wait_for_selector(
                    'text=Rebet, text=Success, [data-testid="bet-confirmation"], [class*="bet-success"], [class*="success"]',
                    timeout=Config.CONFIRMATION_TIMEOUT_MS
                )
                elapsed = time.time() - start_time

                if elapsed > Config.SUBMISSION_TIMEOUT:
                    await self.alerter.notify_submission_slow(match_name, "Confirmed but slow", elapsed)
                    result = await self.handle_slow(match)
                    return result in (BetResult.SUCCESS, BetResult.SWITCHED)

                self.consecutive_slow = 0

                bet_id = f"{match['match_id']}_G{goal_num}_S{stack_num}_{uuid.uuid4().hex[:8]}"
                await self.alerter.notify_bet_placed(match_name, team, stake, odds, bet_id, stack_num, total_stack)
                self.log_bet(match_name, team, stake, odds, bet_id)
                return True

            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"[CONFIRMATION_TIMEOUT] No confirmation after {elapsed:.1f}s: {e}")
                await self.alerter.notify_submission_slow(match_name, "No confirmation", elapsed)
                if elapsed > Config.SUBMISSION_TIMEOUT:
                    result = await self.handle_slow(match)
                    return result in (BetResult.SUCCESS, BetResult.SWITCHED)
                return False

        except Exception as e:
            logger.exception(f"[BET] Error: {e}")
            await self.alerter.notify_error(f"Bet error on {match.get('home_team')} vs {match.get('away_team')}: {e}")
            return False

    async def handle_slow(self, match) -> BetResult:
        self.consecutive_slow += 1
        logger.warning(f"Slow #{self.consecutive_slow}")

        if self.consecutive_slow >= Config.MAX_CONSECUTIVE_SLOW:
            old_user = self.current_account['username'] if self.current_account else 'unknown'
            await self.alerter.notify_account_flagged(old_user)

            old, new = self.account_manager.mark_limited()
            if new:
                await self.alerter.notify_account_switched(old, new['username'])
                self.current_account = new
                self.consecutive_slow = 0
                return BetResult.SWITCHED
            else:
                self.stopped = True
                await self.alerter.notify_error("No accounts left!")
                return BetResult.FAILED

        return BetResult.FAILED

    def log_bet(self, match, team, stake, odds, bet_id):
        with open('bets.log', 'a') as f:
            f.write(f"{datetime.now()}: {match}|{team}|₦{stake}|@{odds}|{bet_id}\n")
