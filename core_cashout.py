import asyncio
import re
from datetime import datetime
from typing import Dict
from playwright.async_api import Page
from loguru import logger
from utils.telegram import TelegramAlerter


class CashOutManager:
    def __init__(self, alerter: TelegramAlerter):
        self.alerter = alerter
        self.active_bets: Dict[str, dict] = {}

    def register(self, match_id: str, bet_id: str, stake: float, description: str):
        self.active_bets[match_id] = {
            'bet_id': bet_id,
            'stake': stake,
            'description': description,
            'registered_at': datetime.now(),
        }
        logger.info(f"Cashout monitor registered: {description} (₦{stake:,.0f})")

    async def get_cashout_value(self, page: Page) -> float:
        try:
            elem = await page.query_selector('text=/Full Cashout/i')
            if elem:
                text = await elem.text_content()
                match = re.search(r'[\d,]+\.?\d*', text)
                if match:
                    return float(match.group(0).replace(',', ''))
        except Exception as e:
            logger.debug(f"Cashout value read error: {e}")
        return 0.0

    async def execute_cashout(self, page: Page, match_id: str) -> bool:
        try:
            bet = self.active_bets.get(match_id)
            if not bet:
                return False

            value = await self.get_cashout_value(page)
            if value <= 0:
                logger.warning(f"No cashout value available for {bet['description']}")
                return False

            await page.click('button:has-text("Cashout now")', timeout=3000)

            try:
                await page.wait_for_selector('text=/success|confirmed/i', timeout=5000)
            except Exception:
                pass

            await self.alerter.notify_cashout(bet['description'], value, bet['stake'])
            logger.success(f"Cashed out {bet['description']} for ₦{value:,.0f}")
            del self.active_bets[match_id]
            return True

        except Exception as e:
            logger.error(f"Cashout execution error: {e}")
            return False

    async def monitor(self, match_id: str, match_data: dict, page: Page):
        try:
            while match_id in self.active_bets:
                await asyncio.sleep(5)

                bet = self.active_bets.get(match_id)
                if not bet:
                    break

                value = await self.get_cashout_value(page)
                if value <= 0:
                    continue

                profit_ratio = value / bet['stake'] if bet['stake'] > 0 else 0

                if profit_ratio >= 1.3:
                    await self.execute_cashout(page, match_id)
                    break

        except Exception as e:
            logger.error(f"Cashout monitor error: {e}")
