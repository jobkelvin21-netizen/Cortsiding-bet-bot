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
    UNAVAILABLE = auto()
    INVALID = auto()
    MATCHES = auto()
    CHANGED = auto()


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

    # =========================================================================
    # BALANCE
    # =========================================================================

    async def fetch_balance(self, page: Page) -> Optional[float]:
        selectors = [
            '.user-balance',
            '.account-balance',
            '[data-testid="balance"]',
            '[class*="balance"]',
            'span:has-text("₦")',
        ]

        for sel in selectors:
            try:
                elem = await page.query_selector(sel)
                if not elem:
                    continue
                text = await elem.text_content()
                if not text:
                    continue
                cleaned = text.replace('₦', '').replace('NGN', '').replace(',', '').strip()
                # Extract first number
                match = re.search(r'([\d.]+)', cleaned)
                if match:
                    bal = float(match.group(1))
                    self.balance = bal
                    if self.current_account:
                        self.account_manager.update_balance(self.current_account['username'], bal)
                    return bal
            except Exception:
                continue

        logger.error("[BALANCE] Could not read balance")
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
    # ODDS READER (more flexible)
    # =========================================================================

    async def _read_odds_from_click(self, page: Page, selection_text: str) -> float:
        try:
            # Clean the team name for better matching
            clean_name = re.sub(r'\s+(FC|CF|SC|AFC|United|City)$', '', selection_text, flags=re.IGNORECASE).strip()
            keywords = [w for w in clean_name.split() if len(w) > 2]

            # Try different ways to find the selection
            candidates = [
                f'button:has-text("{selection_text}")',
                f'[class*="selection"]:has-text("{selection_text}")',
                f'div:has-text("{selection_text}")',
            ]

            # Add shorter versions
            if keywords:
                candidates.append(f'button:has-text("{keywords[0]}")')
                candidates.append(f'[class*="selection"]:has-text("{keywords[0]}")')

            for sel in candidates:
                try:
                    locator = page.locator(sel)
                    count = await locator.count()
                    if count == 0:
                        continue

                    full_text = await locator.first.text_content(timeout=2000)
                    if not full_text:
                        continue

                    # Extract odds number
                    match = re.search(r'(\d+\.\d{1,3}|\d+)', full_text.replace(',', '.'))
                    if match:
                        odds = float(match.group(1))
                        if 1.01 <= odds <= 500:
                            return odds
                except Exception:
                    continue

            # Last fallback: search any text containing the team
            try:
                locator = page.get_by_text(selection_text, exact=False)
                if await locator.count() > 0:
                    full_text = await locator.first.text_content(timeout=1500)
                    match = re.search(r'(\d+\.\d{1,3})', (full_text or '').replace(',', '.'))
                    if match:
                        odds = float(match.group(1))
                        if 1.01 <= odds <= 500:
                            return odds
            except Exception:
                pass

            logger.warning(f"[ODDS] Could not read odds for '{selection_text}'")
            return 0.0

        except Exception as e:
            logger.error(f"[ODDS] Error reading odds: {e}")
            return 0.0

    # =========================================================================
    # SCORE SAFETY CHECK
    # =========================================================================

    def _check_score_state(self, match_id: str, expected_home: int, expected_away: int):
        sb_match = self.sportybet_matches.get(match_id)

        if not sb_match:
            return ScoreState.UNAVAILABLE, None, None

        current_home = sb_match.get('home_score')
        current_away = sb_match.get('away_score')

        if current_home is None or current_away is None:
            return ScoreState.INVALID, current_home, current_away

        if current_home == expected_home and current_away == expected_away:
            return ScoreState.MATCHES, current_home, current_away

        return ScoreState.CHANGED, current_home, current_away

    async def _score_still_matches(self, match_id: str, expected_home: int, expected_away: int) -> bool:
        state, current_home, current_away = self._check_score_state(match_id, expected_home, expected_away)

        if state == ScoreState.UNAVAILABLE:
            logger.warning(f"[SAFETY] Match {match_id} not found in SportyBet feed")
            return False

        if state == ScoreState.INVALID:
            logger.warning(f"[SAFETY] Invalid score data for match {match_id}")
            return False

        if state == ScoreState.CHANGED:
            logger.warning(
                f"⚠️ SAFETY ABORT: Score changed! Expected {expected_home}-{expected_away}, "
                f"now {current_home}-{current_away}"
            )
            return False

        return True

    # =========================================================================
    # MATCH IDENTITY CHECK
    # =========================================================================

    async def _verify_match_identity(self, page: Page, expected_home: str, expected_away: str) -> bool:
        try:
            body_text = (await page.evaluate("document.body.innerText") or "").lower()

            home_ok = any(w.lower() in body_text for w in expected_home.split() if len(w) > 3)
            away_ok = any(w.lower() in body_text for w in expected_away.split() if len(w) > 3)

            if not (home_ok and away_ok):
                logger.warning(f"[IDENTITY] Page does not look like {expected_home} vs {expected_away}")
                return False
            return True
        except Exception as e:
            logger.error(f"[IDENTITY] Error: {e}")
            return False

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

        # Identity check
        if not await self._verify_match_identity(page, match.get('home_team', ''), match.get('away_team', '')):
            return BetResult.ABORTED_SAFETY

        # Score safety check
        if expected_home_score is not None and expected_away_score is not None:
            if not await self._score_still_matches(match_id, expected_home_score, expected_away_score):
                return BetResult.ABORTED_SAFETY

        # Read odds
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

        # Normal betting
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
                await asyncio.sleep(random.uniform(0.3, 0.6))
                await page.bring_to_front()

            if not skip_market_selection:
                clicked = False
                try:
                    # Try full name first
                    locator = page.get_by_text(team, exact=False)
                    if await locator.count() > 0:
                        await locator.first.click(force=True, timeout=2000)
                        clicked = True
                except Exception:
                    pass

                if not clicked:
                    # Try first significant word
                    first_word = team.split()[0] if team else ""
                    if first_word:
                        try:
                            locator = page.get_by_text(first_word, exact=False)
                            if await locator.count() > 0:
                                await locator.first.click(force=True, timeout=2000)
                                clicked = True
                        except Exception:
                            pass

                if not clicked:
                    logger.warning(f"[MARKET] Could not click selection for '{team}'")
                    return False

            # Fill stake
            stake_input = await page.query_selector(
                'input[type="number"], input[type="tel"], input[inputmode="numeric"], input[placeholder*="Stake"]'
            )
            if stake_input:
                await stake_input.fill(str(int(stake)))
            else:
                await self.fast.fast_type(page, 'input', str(int(stake)), human_delay=False)

            await asyncio.sleep(random.uniform(0.3, 0.6))

            # Click Place Bet / Accept Changes
            clicked = await self.fast.fast_click(page, 'button:has-text("Accept Changes")', human_delay=False)
            if not clicked:
                clicked = await self.fast.fast_click(page, 'button:has-text("Place Bet")', human_delay=False)

            if not clicked:
                logger.error("[BET] Submit button not found")
                await self.alerter.notify_submission_slow(match_name, "Submit button not found", 0)
                return False

            # Wait for confirmation
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
                logger.error(f"[BET] No confirmation after {elapsed:.1f}s: {e}")
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
