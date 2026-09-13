import asyncio
import re
import time
from datetime import datetime
from typing import Dict, List, Optional
from playwright.async_api import Page
from loguru import logger
from utils.telegram import TelegramAlerter


class CashOutManager:
    def __init__(self, alerter: TelegramAlerter):
        self.alerter = alerter
        self.active_bets: Dict[str, dict] = {}  # keyed by bet_id
        self.MAX_MONITOR_SECONDS = 40 * 60
        self.PROFIT_TARGET = 1.30   # cash out when value >= 1.3x stake

    def register(self, match_id: str, bet_id: str, stake: float, description: str):
        self.active_bets[bet_id] = {
            'match_id': match_id,
            'bet_id': bet_id,
            'stake': stake,
            'description': description,
            'registered_at': datetime.now(),
        }
        logger.info(f"Cashout monitor registered: {description} (₦{stake:,.0f}) bet_id={bet_id}")

    def bets_for_match(self, match_id: str) -> List[dict]:
        return [b for b in self.active_bets.values() if b['match_id'] == match_id]

    async def get_cashout_value(self, page: Page) -> float:
        """Try multiple ways to read the current cashout value."""
        if page.is_closed():
            return 0.0

        selectors = [
            'text=/Full Cashout/i',
            'text=/Cashout/i',
            '[class*="cashout"]',
            '[class*="cash-out"]',
            'button:has-text("Cashout")',
        ]

        for sel in selectors:
            try:
                elem = await page.query_selector(sel)
                if not elem:
                    continue
                text = await elem.text_content()
                if not text:
                    continue

                # Look for a money-looking number
                match = re.search(r'([\d,]+\.?\d*)', text.replace('₦', '').replace('NGN', ''))
                if match:
                    value = float(match.group(1).replace(',', ''))
                    if value > 0:
                        return value
            except Exception:
                continue

        return 0.0

    async def execute_cashout(self, page: Page, bet_id: str) -> bool:
        if page.is_closed():
            logger.warning(f"Cannot cashout {bet_id} — page is closed")
            return False

        bet = self.active_bets.get(bet_id)
        if not bet:
            return False

        value = await self.get_cashout_value(page)
        if value <= 0:
            logger.warning(f"No cashout value available for {bet['description']}")
            return False

        # Try several possible cashout buttons
        button_selectors = [
            'button:has-text("Cashout now")',
            'button:has-text("Cash Out Now")',
            'button:has-text("Cashout")',
            'button:has-text("Full Cashout")',
            '[class*="cashout"] button',
            'button[class*="cashout"]',
        ]

        clicked = False
        for sel in button_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click(timeout=3000)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            logger.error(f"Could not find Cashout button for {bet['description']}")
            return False

        # Wait briefly for confirmation
        try:
            await page.wait_for_selector(
                'text=/success|confirmed|cashed out/i',
                timeout=6000
            )
        except Exception:
            # Even if confirmation text is not found, we still consider it attempted
            pass

        await self.alerter.notify_cashout(bet['description'], value, bet['stake'])
        logger.success(f"Cashed out {bet['description']} for ₦{value:,.0f}")
        self.active_bets.pop(bet_id, None)
        return True

    async def monitor(self, bet_id: str, match_data: dict, page: Page):
        """Continuously check cashout value and cash out when target is reached."""
        start_time = time.time()
        logger.info(f"Starting cashout monitor for bet_id={bet_id}")

        try:
            while bet_id in self.active_bets:
                await asyncio.sleep(5)

                # Safety timeout
                if time.time() - start_time > self.MAX_MONITOR_SECONDS:
                    logger.info(f"Cashout monitor timed out for bet {bet_id}")
                    self.active_bets.pop(bet_id, None)
                    break

                # Page may have been closed
                if page.is_closed():
                    logger.warning(f"Page closed while monitoring bet {bet_id} — stopping monitor")
                    self.active_bets.pop(bet_id, None)
                    break

                bet = self.active_bets.get(bet_id)
                if not bet:
                    break

                value = await self.get_cashout_value(page)
                if value <= 0:
                    continue

                profit_ratio = value / bet['stake'] if bet['stake'] > 0 else 0

                logger.debug(
                    f"[CASHOUT] {bet['description']} → value=₦{value:,.0f} "
                    f"({profit_ratio:.2f}x stake)"
                )

                if profit_ratio >= self.PROFIT_TARGET:
                    logger.info(
                        f"Profit target reached ({profit_ratio:.2f}x) — attempting cashout"
                    )
                    success = await self.execute_cashout(page, bet_id)
                    if success:
                        break

        except Exception as e:
            logger.error(f"Cashout monitor error for {bet_id}: {e}")
            self.active_bets.pop(bet_id, None)
