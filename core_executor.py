import asyncio
import random
import re
import time
from datetime import datetime
from playwright.async_api import Page
from loguru import logger

from config import Config
from browser.fast_executor import FastExecutor
from utils.telegram import TelegramAlerter


class BetExecutor:
    def __init__(self, alerter: TelegramAlerter, account_manager, learning=None):
        self.alerter = alerter
        self.account_manager = account_manager
        self.learning = learning
        self.balance = 0
        self.consecutive_slow = 0
        self.stopped = False
        self.fast = FastExecutor()
        self.current_account = None
        self.match_goal_count = {}
        self.last_odds_used = 0.0

        self.validation_bet_placed = False
        self.validation_passed = False

    async def fetch_balance(self, page: Page) -> float:
        try:
            for sel in ['.user-balance', '.account-balance', '[class*="balance"]']:
                elem = await page.query_selector(sel)
                if elem:
                    text = await elem.text_content()
                    bal = float(text.replace('₦', '').replace('NGN', '').replace(',', '').strip())
                    self.balance = bal
                    if self.current_account:
                        self.account_manager.update_balance(self.current_account['username'], bal)
                    return bal
        except Exception:
            pass
        return self.balance

    def calc_stake(self, odds: float) -> float:
        if odds <= 1.0 or self.balance <= 0:
            return 0
        stake_for_profit = Config.MAX_PROFIT_PER_BET / (odds - 1)
        return min(self.balance, stake_for_profit)

    def can_split_by_balance(self, stake: float) -> int:
        if stake <= 0:
            return 1
        max_splits = int(self.balance / stake)
        return min(max_splits, 3)

    async def is_dying_minutes(self, page: Page) -> bool:
        try:
            time_elem = await page.query_selector('[class*="timer"], [class*="match-time"]')
            if time_elem:
                time_text = await time_elem.text_content()
                clean = time_text.split('+')[0].split(':')[0].strip("'")
                minute = int(clean)
                return minute >= 80
        except Exception:
            pass
        return False

    def should_split(self, odds: float, is_dying: bool, split_count: int) -> bool:
        if split_count >= 2:
            return True
        if odds >= 10.0 and is_dying:
            return True
        return False

    async def _read_odds_from_click(self, page: Page, selection_text: str) -> float:
        """
        Reads odds from the clickable element text (e.g. "Home @1.85")
        """
        try:
            # Try multiple ways to find the element
            elem = await page.query_selector(f'text="{selection_text}"')
            if not elem:
                elem = await page.query_selector(f'text={selection_text}')
            if not elem:
                return 0.0

            full_text = await elem.text_content()
            match = re.search(r'(\d+\.\d+)', full_text or '')
            if match:
                return float(match.group(1))
        except Exception as e:
            logger.debug(f"Odds read error: {e}")
        return 0.0

    async def execute(self, page: Page, match: dict, team: str, goal_num: int = 1) -> bool:
        if self.stopped:
            return False

        match_name = f"{match['home_team']} vs {match['away_team']}"

        try:
            await self.fast.scroll_to(page, 'text=Next Goal')
            await self.fast.fast_click(page, 'text=Next Goal')
            await asyncio.sleep(0.4)
        except Exception:
            pass

        odds = await self._read_odds_from_click(page, team)
        if odds <= 0:
            logger.error(f"No odds found for {team} on {match_name} — skipping this goal event")
            return False

        self.last_odds_used = odds
        logger.info(f"Odds confirmed: {team} @ {odds}")

        # Validation bet (first slow match only)
        if not self.validation_bet_placed:
            self.validation_bet_placed = True
            success = await self.run_validation_bet(page, match, team, odds, goal_num, match_name)
            if success:
                self.validation_passed = True
                logger.success("✅ Validation passed — full staking now ACTIVE")
            else:
                self.stopped = True
                logger.error("❌ Validation failed — bot stopped placing further bets")
            return success

        if not self.validation_passed:
            return False

        self.balance = await self.fetch_balance(page)
        stake = self.calc_stake(odds)

        if stake < 100:
            return False

        split_count = self.can_split_by_balance(stake)
        dying = await self.is_dying_minutes(page)

        if self.should_split(odds, dying, split_count):
            return await self.split_bet(page, match, team, odds, goal_num, split_count, dying)
        else:
            await self.alerter.notify_goal_detected(match_name, team, odds, goal_num)
            return await self.place_single_bet(page, match, team, stake, odds, 1, goal_num, total_stack=1)

    async def run_validation_bet(self, page: Page, match: dict, team: str, odds: float,
                                  goal_num: int, match_name: str) -> bool:
        stake = Config.VALIDATION_STAKE
        logger.info(f"🔬 VALIDATION BET: ₦{stake} on {team} @ {odds}")
        await self.alerter.notify_validation_start(match_name, team, odds, goal_num, stake)

        self.balance = await self.fetch_balance(page)
        await self.alerter.notify_validation_step("Balance read", f"₦{self.balance:,.2f} confirmed")

        error_detail = None
        try:
            success = await self.place_single_bet(
                page, match, team, stake, odds, stack_num=1, goal_num=goal_num,
                total_stack=1, skip_market_selection=True
            )
        except Exception as e:
            success = False
            error_detail = str(e)

        if success:
            await self.alerter.notify_validation_result(True, match_name, team, stake, odds)
        else:
            await self.alerter.notify_validation_result(
                False, match_name, team, stake, odds,
                error_detail=error_detail or "Bet did not confirm — check market selection, stake entry, or submit button"
            )
        return success

    async def split_bet(self, page: Page, match: dict, team: str, odds: float,
                        goal_num: int, split_count: int, dying: bool):
        match_name = f"{match['home_team']} vs {match['away_team']}"
        split_stake = self.balance / split_count

        reason = "Balance allows" if not dying else f"High odds {odds} + dying minutes"
        logger.info(f"💰 SPLITTING ({reason}): {split_count} bets of ₦{split_stake:,.0f}")

        await self.alerter.notify_goal_detected(match_name, team, odds, goal_num)
        if dying and odds >= 10:
            await self.alerter.send(f"🚀 <b>BOOST {odds}!</b> Dying minutes - splitting bet")

        total_bets = 0
        total_staked = 0

        for i in range(split_count):
            if self.balance < 100:
                break

            success = await self.place_single_bet(
                page, match, team, split_stake, odds, i+1, goal_num, split_count
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
                # Try multiple selectors for better reliability
                selectors = [
                    f'text="{team}"',
                    f'text={team}',
                    f'button:has-text("{team}")',
                    f'div:has-text("{team}")',
                ]
                for selector in selectors:
                    try:
                        if await self.fast.fast_click(page, selector):
                            clicked = True
                            break
                    except Exception:
                        continue

                if not clicked:
                    logger.warning(f"Could not click team selection for {team}")
                    return False

            # Fill stake
            stake_input = await page.query_selector(
                'input[type="number"], input[type="tel"], input[inputmode="numeric"]'
            )
            if stake_input:
                await stake_input.fill(str(int(stake)))
            else:
                await self.fast.fast_type(page, 'input', str(int(stake)))

            await asyncio.sleep(random.uniform(0.4, 0.8))

            # Submit bet
            clicked = await self.fast.fast_click(page, 'button:has-text("Accept Changes")')
            if not clicked:
                clicked = await self.fast.fast_click(page, 'button:has-text("Place Bet")')

            if not clicked:
                logger.error("Could not find submit button (Accept Changes / Place Bet)")
                await self.alerter.notify_submission_slow(match_name, "Submit button not found", 0)
                return False

            try:
                await page.wait_for_selector(
                    'text=Rebet, [class*="success"], [class*="confirmation"]',
                    timeout=5000
                )
                elapsed = time.time() - start_time

                if elapsed > Config.SUBMISSION_TIMEOUT:
                    await self.alerter.notify_submission_slow(match_name, "Confirmed but slow", elapsed)
                    return await self.handle_slow(match)

                self.consecutive_slow = 0

                bet_id = f"{match['match_id']}_G{goal_num}_S{stack_num}_{int(time.time())}"
                await self.alerter.notify_bet_placed(match_name, team, stake, odds, bet_id,
                                                     stack_num, total_stack)

                self.log_bet(match_name, team, stake, odds,
                            f"{match['match_id']}_G{goal_num}_S{stack_num}")
                return True

            except Exception:
                elapsed = time.time() - start_time
                await self.alerter.notify_submission_slow(match_name, "No confirmation seen", elapsed)
                if elapsed > Config.SUBMISSION_TIMEOUT:
                    return await self.handle_slow(match)
                return False

        except Exception as e:
            logger.error(f"Bet error: {e}")
            await self.alerter.notify_error(
                f"Bet execution error on {match.get('home_team','')} vs {match.get('away_team','')}: {e}"
            )
            return False

    async def handle_slow(self, match):
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
                return "SWITCH"
            else:
                self.stopped = True
                await self.alerter.notify_error("No accounts left!")

        return False

    def log_bet(self, match, team, stake, odds, bet_id):
        with open('bets.log', 'a') as f:
            f.write(f"{datetime.now()}: {match}|{team}|₦{stake}|@{odds}|{bet_id}\n")
