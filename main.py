import asyncio
import sys
import time
from loguru import logger

from config import Config
from auth_sportybet_login import SportyBetAuth
from feeds.polymarket_ws import PolymarketFeed
from feeds.sportybet_api import SportyBetFeed
from core.match_linker import MatchLinker
from core.executor import BetExecutor
from core.cashout import CashOutManager
from utils.telegram import TelegramAlerter
from utils.account_manager import AccountManager


class ArbitrageBot:
    def __init__(self):
        self.account_manager = AccountManager()
        self.alerter = TelegramAlerter()
        self.auth = SportyBetAuth()

        self.sportybet = SportyBetFeed()
        self.polymarket = PolymarketFeed(self._noop_callback)

        self.linker = MatchLinker(self.sportybet.matches)
        self.linker.set_goal_callback(self.on_goal)

        self.executor = None
        self.cashout = CashOutManager(self.alerter)
        self.slow_pages = {}
        self.running = False
        self._processing = {}
        self.browser = None

    async def _noop_callback(self, match):
        pass

    async def setup(self):
        if not self.account_manager.accounts:
            print("No accounts! Add one:")
            u = input("Username: ")
            p = input("Password: ")
            ph = input("Phone: ")
            self.account_manager.add_account(u, p, ph)

        acc = self.account_manager.get_active()
        if not acc:
            logger.error("No active account")
            sys.exit(1)

    async def start(self):
        await self.setup()

        logger.info("=" * 60)
        logger.info("BOT STARTING - POLYMARKET + SPORTYBET")
        logger.info("=" * 60)

        print("\n" + "=" * 60)
        print("SPORTYBET LOGIN")
        print("=" * 60)
        phone = input("Phone Number: ")
        password = input("Password: ")
        print("=" * 60)

        from playwright.async_api import async_playwright
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled']
        )
        self.browser = browser

        page = await browser.new_page(viewport={'width': 412, 'height': 915})

        logger.info("Opening SportyBet login...")
        await page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        try:
            await page.click('button:has-text("Log In"), a:has-text("Log In")', timeout=5000)
            await asyncio.sleep(1)
        except Exception:
            pass

        logger.info("Logging in...")
        await page.wait_for_selector('input[name="phone"]', state="visible", timeout=15000)
        await page.fill('input[name="phone"]', phone)
        await asyncio.sleep(0.5)

        await page.wait_for_selector('input[name="psd"]', state="visible", timeout=10000)
        await page.fill('input[name="psd"]', password)
        await asyncio.sleep(0.5)

        await page.click('button[name="logIn"]')
        await asyncio.sleep(5)

        logger.success("SportyBet login complete!")

        self.sportybet.set_page(page)
        self.sportybet.set_alerter(self.alerter)
        self.sportybet.set_credentials(phone, password)

        self.auth.browser = browser
        self.auth.page = page

        self.executor = BetExecutor(self.alerter, self.account_manager, None)
        self.executor.current_account = self.account_manager.get_active()

        print("\nEnter your current SportyBet balance:")
        bal = float(input("Balance: "))
        self.executor.balance = bal

        await self.alerter.notify_startup(bal)

        logger.info("Starting feeds...")
        self.running = True

        self.polymarket.callback = self.linker.on_polymarket

        await self.polymarket.start()
        asyncio.create_task(self.sportybet.start(self.on_sportybet_update))

        logger.success("Bot running! Watching Polymarket for goals, betting on SportyBet.")

        while self.running:
            await asyncio.sleep(1)

    async def on_sportybet_update(self, data):
        pass

    async def on_goal(self, goal_data: dict):
        try:
            mid = goal_data['match_id']

            if mid not in self.slow_pages:
                await self.open_match_page(goal_data)

            if self._processing.get(mid):
                return
            self._processing[mid] = True

            try:
                page = self.slow_pages.get(mid)
                if not page:
                    logger.warning(f"No open SportyBet page for {mid} — cannot bet")
                    return

                match = {
                    'match_id': mid,
                    'home_team': goal_data['home_team'],
                    'away_team': goal_data['away_team'],
                }

                result = await self.executor.execute(
                    page, match, goal_data['scoring_team'], goal_data['goal_num'],
                    expected_home_score=goal_data['home_score'],
                    expected_away_score=goal_data['away_score'],
                )

                if result == "SWITCH":
                    logger.info("Account switch requested")
                elif result is True:
                    bet_id = f"{mid}_G{goal_data['goal_num']}_{int(time.time())}"
                    stake = (Config.VALIDATION_STAKE if not self.executor.validation_passed
                             else self.executor.calc_stake(self.executor.last_odds_used))
                    self.cashout.register(mid, bet_id, stake, f"Goal {goal_data['goal_num']}")
                    asyncio.create_task(self.cashout.monitor(bet_id, goal_data, page))

            finally:
                self._processing[mid] = False

        except Exception as e:
            logger.error(f"Goal handling error: {e}")

    async def open_match_page(self, goal_data: dict):
        mid = goal_data['match_id']
        try:
            new_context = await self.browser.new_context(viewport={'width': 412, 'height': 915})
            new_page = await new_context.new_page()
            await new_page.goto(f"{Config.SPORTYBET_BASE_URL}/ng/m/{mid}")
            await asyncio.sleep(4)

            try:
                await new_page.click('text=Next Goal')
                await asyncio.sleep(1.5)
            except Exception:
                pass

            self.slow_pages[mid] = new_page
            await self.alerter.notify_slow_match_found(
                goal_data['home_team'], goal_data['away_team'], 0
            )

        except Exception as e:
            logger.error(f"Page open error for {mid}: {e}")


if __name__ == "__main__":
    bot = ArbitrageBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Stopping...")
        try:
            asyncio.run(bot.alerter.send_daily_report())
        except Exception:
            pass
