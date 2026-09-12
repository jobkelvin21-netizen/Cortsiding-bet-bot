import asyncio
import random
import re
import time
import uuid
from enum import Enum, auto
from datetime import datetime
from typing import Optional
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
    UNAVAILABLE = auto()   # match_id not found in SportyBet feed data
    INVALID = auto()       # match found but score field unparsable
    MATCHES = auto()       # parsed score equals expected
    CHANGED = auto()       # parsed score differs from expected


class BetExecutor:
    def __init__(self, alerter: TelegramAlerter, account_manager,
                 sportybet_matches_ref: dict, learning=None):
        """
        sportybet_matches_ref: the SAME dict object as SportyBetFeed.matches
        (kept live/updated in place by the REST polling feed). The executor
        reads the live score straight from here instead of scraping the DOM.
        """
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

    # =========================================================================
    # BALANCE — never silently returns a stale value
    # =========================================================================

    async def fetch_balance(self, page: Page) -> Optional[float]:
        selectors = ['.user-balance', '.account-balance',
                     '[data-testid="balance"]', '[class*="balance"]']

        for sel in selectors:
            try:
                elem = await page.query_selector(sel)
                if not elem:
                    continue

                text = await elem.text_content()
                cleaned = (text or '').replace('₦', '').replace('NGN', '').replace(',', '').strip()

                if not cleaned:
                    logger.warning(f"[BALANCE_READ_ERROR] selector='{sel}' matched but text was empty")
                    continue

                bal = float(cleaned)
                self.balance = bal
                if self.current_account:
                    self.account_manager.update_balance(self.current_account['username'], bal)
                return bal

            except (ValueError, TypeError) as e:
                logger.warning(f"[BALANCE_READ_ERROR] selector='{sel}' unparsable: {e}")
                continue
            except Exception as e:
                logger.warning(f"[BALANCE_READ_ERROR] selector='{sel}' unexpected: {e}")
                continue

        logger.error("[BALANCE_READ_ERROR] No balance selector succeeded — balance NOT refreshed, keeping stale value flagged as unrefreshed")
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
    # MINUTE PARSING — handles normal, stoppage time, HT/FT, unexpected text
    # =========================================================================

    async def is_dying_minutes(self, page: Page) -> bool:
        try:
            time_elem = await page.query_selector(
                '[data-testid="match-time"], [class*="timer"], [class*="match-time"]'
            )
            if not time_elem:
                logger.debug("[MINUTE_PARSE] No timer element found")
                return False

            raw = (await time_elem.text_content() or '').strip()

            if not raw:
                return False

            if raw.upper() in ('HT', 'FT', 'HALFTIME', 'FULL TIME'):
                logger.debug(f"[MINUTE_PARSE] Non-numeric period: '{raw}'")
                return False

            # Handles "78", "78'", "45+2", "45+2'"
            m = re.match(r"^(\d+)(?:\+\d+)?'?$", raw)
            if not m:
                logger.warning(f"[MINUTE_PARSE] Unrecognized time format: '{raw}'")
                return False

            minute = int(m.group(1))
            return minute >= 80

        except Exception as e:
            logger.error(f"[MINUTE_PARSE] Unexpected error: {e}")
            return False

    def should_split(self, odds: float, is_dying: bool, split_count: int) -> bool:
        if split_count >= 2:
            return True
        if odds >= 10.0 and is_dying:
            return True
        return False

    # =========================================================================
    # ODDS PARSER — normalizes text, validates numeric result
    # =========================================================================

    async def _read_odds_from_click(self, page: Page, selection_text: str) -> float:
        try:
            # Use Playwright's locator API with exact text, safe against
            # special characters in team/selection names (fixes unsafe
            # selector interpolation)
            locator = page.get_by_text(selection_text, exact=False)
            count = await locator.count()

            if count == 0:
                logger.warning(f"[ODDS_PARSE] No element found for selection '{selection_text}'")
                return 0.0

            full_text = await locator.first.text_content(timeout=Config.ODDS_READ_TIMEOUT_MS)
            if not full_text:
                logger.warning(f"[ODDS_PARSE] Element for '{selection_text}' had no text")
                return 0.0

            normalized = full_text.strip().replace(',', '.')
            match = re.search(r'(\d+(?:\.\d+)?)', normalized)
            if not match:
                logger.warning(f"[ODDS_PARSE] No numeric odds found in text: '{full_text}'")
                return 0.0

            odds = float(match.group(1))
            if odds < 1.01 or odds > 1000:
                logger.warning(f"[ODDS_PARSE] Parsed odds {odds} outside sane range — rejecting")
                return 0.0

            return odds

        except Exception as e:
            logger.error(f"[ODDS_PARSE] Error reading odds for '{selection_text}': {e}")
            return 0.0

    # =========================================================================
    # SCORE SAFETY CHECK — now reads from the live SportyBet REST feed dict,
    # NOT the DOM. This is instant, reliable, and doesn't depend on any
    # score-container selector guessing.
    # =========================================================================

    def _check_score_state(self, match_id: str, expected_home: int,
                            expected_away: int) -> tuple:
        """
        Returns (ScoreState, extracted_home, extracted_away, raw_match_or_None)
        """
        sb_match = self.sportybet_matches.get(match_id)

        if not sb_match:
            logger.warning(
                f"[SCORE_SELECTOR_ERROR] match_id={match_id} not found in "
                f"live SportyBet feed data (feed may be lagging or match ended)"
            )
            return ScoreState.UNAVAILABLE, None, None, None

        current_home = sb_match.get('home_score')
        current_away = sb_match.get('away_score')

        if current_home is None or current_away is None:
            logger.warning(
                f"[SCORE_PARSE_ERROR] match_id={match_id} present but score "
                f"fields missing: {sb_match}"
            )
            return ScoreState.INVALID, current_home, current_away, sb_match

        if current_home == expected_home and current_away == expected_away:
            return ScoreState.MATCHES, current_home, current_away, sb_match

        return ScoreState.CHANGED, current_home, current_away, sb_match

    async def _verify_match_identity(self, page: Page, expected_home: str,
                                      expected_away: str) -> bool:
        """
        Confirms the currently open SportyBet page actually shows the
        expected teams before we trust anything read from it (fixes #4, #24).
        """
        try:
            body_text = await page.evaluate("document.body.innerText") or ""
        except Exception as e:
            logger.error(f"[MATCH_IDENTITY_ERROR] Could not read page body: {e}")
            return False

        home_present = expected_home.split()[0].lower() in body_text.lower() if expected_home else False
        away_present = expected_away.split()[0].lower() in body_text.lower() if expected_away else False

        if not (home_present and away_present):
            logger.warning(
                f"[MATCH_IDENTITY_ERROR] Page does not appear to show "
                f"'{expected_home}' vs '{expected_away}' — refusing to proceed"
            )
            return False

        return True

    async def _score_still_matches(self, page: Page, match_id: str,
                                    expected_home_score: int,
                                    expected_away_score: int) -> bool:
        state, current_home, current_away, sb_match = self._check_score_state(
            match_id, expected_home_score, expected_away_score
        )

        timestamp = datetime.now().isoformat()

        if state == ScoreState.UNAVAILABLE:
            logger.warning(
                f"[SCORE_DIAGNOSTIC] state=UNAVAILABLE match_id={match_id} "
                f"expected={expected_home_score}-{expected_away_score} "
                f"extracted=None source=sportybet_rest_feed time={timestamp}"
            )
            return False

        if state == ScoreState.INVALID:
            logger.warning(
                f"[SCORE_DIAGNOSTIC] state=INVALID match_id={match_id} "
                f"expected={expected_home_score}-{expected_away_score} "
                f"extracted=malformed source=sportybet_rest_feed time={timestamp}"
            )
            return False

        if state == ScoreState.CHANGED:
            logger.warning(
                f"⚠️ SAFETY ABORT: [SCORE_DIAGNOSTIC] state=CHANGED match_id={match_id} "
                f"expected={expected_home_score}-{expected_away_score} "
                f"extracted={current_home}-{current_away} "
                f"source=sportybet_rest_feed time={timestamp} "
                f"— market has moved on, not betting"
            )
            return False

        # state == MATCHES
        logger.debug(
            f"[SCORE_DIAGNOSTIC] state=MATCHES match_id={match_id} "
            f"score={current_home}-{current_away} time={timestamp}"
        )
        return True

    # =========================================================================
    # MAIN EXECUTE
    # =========================================================================

    async def execute(self, page: Page, match: dict, team: str, goal_num: int = 1,
                       expected_home_score: int = None,
                       expected_away_score: int = None) -> BetResult:
        if self.stopped:
            return BetResult.FAILED

        match_id = match.get('match_id')
        match_name = f"{match['home_team']} vs {match['away_team']}"

        # --- Safety gate #1: confirm page shows the right match ---
        identity_ok = await self._verify_match_identity(
            page, match.get('home_team', ''), match.get('away_team', '')
        )
        if not identity_ok:
            return BetResult.ABORTED_SAFETY

        # --- Safety gate #2: confirm score hasn't moved past this goal ---
        if expected_home_score is not None and expected_away_score is not None:
            still_valid = await self._score_still_matches(
                page, match_id, expected_home_score, expected_away_score
            )
            if not still_valid:
                return BetResult.ABORTED_SAFETY

        odds = await self._read_odds_from_click(page, team)
        if odds <= 0:
            logger.error(f"[ODDS_PARSE] No valid odds found for {team} on {match_name} — skipping")
            return BetResult.FAILED

        self.last_odds_used = odds
        logger.info(f"Odds confirmed: {team} @ {odds}")

        if not self.validation_bet_placed:
            self.validation_bet_placed = True
            success = await self.run_validation_bet(page, match, team, odds, goal_num, match_name)
            if success:
                self.validation_passed = True
                logger.success("✅ Validation passed — full staking now ACTIVE")
                return BetResult.SUCCESS
            else:
                self.stopped = True
                logger.error("❌ Validation failed — bot stopped placing further bets")
                return BetResult.FAILED

        if not self.validation_passed:
            return BetResult.FAILED

        fresh_balance = await self.fetch_balance(page)
        if fresh_balance is None:
            logger.error("[BALANCE_READ_ERROR] Aborting bet — could not confirm current balance")
            return BetResult.FAILED

        stake = self.calc_stake(odds)
        if stake < 100:
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
            await self.alerter.notify_validation_step("Balance read", "FAILED — could not read balance")
            return False

        await self.alerter.notify_validation_step("Balance read", f"₦{fresh_balance:,.2f} confirmed")

        error_detail = None
        try:
            success = await self.place_single_bet(
                page, match, team, stake, odds, stack_num=1, goal_num=goal_num,
                total_stack=1, skip_market_selection=True
            )
        except Exception as e:
            success = False
            error_detail = str(e)
            logger.exception(f"[VALIDATION_ERROR] {e}")

        if success:
            await self.alerter.notify_validation_result(True, match_name, team, stake, odds)
        else:
            await self.alerter.notify_validation_result(
                False, match_name, team, stake, odds,
                error_detail=error_detail or "Bet did not confirm — check market selection, stake entry, or submit button"
            )
        return success

    async def split_bet(self, page: Page, match: dict, team: str, odds: float,
                        goal_num: int, split_count: int, dying: bool) -> bool:
        match_name = f"{match['home_team']} vs {match['away_team']}"

        if not self.balance:
            logger.error("[FEED_SYNC_ERROR] No balance available for split calculation")
            return False

        split_stake = self.balance / split_count

        reason = "Balance allows" if not dying else f"High odds {odds} + dying minutes"
        logger.info(f"💰 SPLITTING ({reason}): {split_count} bets of ₦{split_stake:,.0f}")

        await self.alerter.notify_goal_detected(match_name, team, odds, goal_num)
        if dying and odds >= 10:
            await self.alerter.send(f"🚀 <b>BOOST {odds}!</b> Dying minutes - splitting bet")

        total_bets = 0
        total_staked = 0.0

        for i in range(split_count):
            # Recalculate from current balance each iteration (fixes #14)
            if not self.balance or self.balance < 100:
                break

            success = await self.place_single_bet(
                page, match, team, split_stake, odds, i + 1, goal_num, split_count
            )

            if success:
                total_bets += 1
                total_staked += split_stake
                self.balance -= split_stake
                if i < split_count - 1:
                    await asyncio.sleep(random.uniform(0.5, 1.0))
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

            if stack_num == 1 and goal_num == 1:
                await asyncio.sleep(random.uniform(0.4, 0.8))
                await page.bring_to_front()

            if not skip_market_selection:
                clicked = False
                try:
                    # Locator API instead of raw f-string CSS/text interpolation
                    # (fixes unsafe selector construction for names with quotes etc.)
                    locator = page.get_by_text(team, exact=False)
                    if await locator.count() > 0:
                        await locator.first.click(force=True, timeout=1500)
                        clicked = True
                except Exception as e:
                    logger.debug(f"[MARKET_SELECTION] locator click failed: {e}")

                if not clicked:
                    logger.warning(f"[MARKET_SELECTION] Could not click team selection for '{team}'")
                    return False

            stake_input = await page.query_selector(
                'input[type="number"], input[type="tel"], input[inputmode="numeric"]'
            )
            if stake_input:
                await stake_input.fill(str(int(stake)))
            else:
                logger.warning("[MARKET_SELECTION] No stake input found via selector, falling back to fast_type")
                await self.fast.fast_type(page, 'input', str(int(stake)), human_delay=False)

            await asyncio.sleep(random.uniform(0.4, 0.8))

            clicked = await self.fast.fast_click(
                page, 'button:has-text("Accept Changes")', human_delay=False
            )
            if not clicked:
                clicked = await self.fast.fast_click(
                    page, 'button:has-text("Place Bet")', human_delay=False
                )

            if not clicked:
                logger.error("[CONFIRMATION_TIMEOUT] Could not find submit button (Accept Changes / Place Bet)")
                await self.alerter.notify_submission_slow(match_name, "Submit button not found", 0)
                return False

            try:
                # More specific confirmation selector than a generic
                # "contains success/confirmation" text match
                await page.wait_for_selector(
                    'text=Rebet, [data-testid="bet-confirmation"], [class*="bet-success"]',
                    timeout=Config.CONFIRMATION_TIMEOUT_MS
                )
                elapsed = time.time() - start_time

                if elapsed > Config.SUBMISSION_TIMEOUT:
                    await self.alerter.notify_submission_slow(match_name, "Confirmed but slow", elapsed)
                    result = await self.handle_slow(match)
                    return result == BetResult.SUCCESS or result == BetResult.SWITCHED

                self.consecutive_slow = 0

                bet_id = f"{match['match_id']}_G{goal_num}_S{stack_num}_{uuid.uuid4().hex[:8]}"
                await self.alerter.notify_bet_placed(match_name, team, stake, odds, bet_id,
                                                     stack_num, total_stack)

                self.log_bet(match_name, team, stake, odds, bet_id)
                return True

            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"[CONFIRMATION_TIMEOUT] No confirmation seen after {elapsed:.1f}s: {e}")
                await self.alerter.notify_submission_slow(match_name, "No confirmation seen", elapsed)
                if elapsed > Config.SUBMISSION_TIMEOUT:
                    result = await self.handle_slow(match)
                    return result == BetResult.SUCCESS or result == BetResult.SWITCHED
                return False

        except Exception as e:
            logger.exception(f"[PAGE_LOAD_ERROR] Bet error: {e}")
            await self.alerter.notify_error(
                f"Bet execution error on {match.get('home_team','')} vs {match.get('away_team','')}: {e}"
            )
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
