import asyncio
import sys
import time
from datetime import datetime
from loguru import logger

from config import Config
from auth_sportybet_login import SportyBetAuth
from feeds.bet365_ws import Bet365Feed
from feeds.sportybet_api import SportyBetFeed
from core.detector import SlowGameDetector
from core.executor import BetExecutor
from core.cashout import CashOutManager
from utils.telegram import TelegramAlerter
from utils.account_manager import AccountManager


class ArbitrageBot:
    def __init__(self):
        self.account_manager = AccountManager()
        self.alerter = TelegramAlerter()
        self.auth = SportyBetAuth()
        self.bet365 = Bet365Feed(self.on_bet365)
        self.sportybet = SportyBetFeed()
        self.detector = SlowGameDetector()
        self.executor = None
        self.cashout = CashOutManager(self.alerter)
        self.slow_pages = {}
        self.running = False
        self._processing = {}
        self.match_last_score = {}
        self.browser = None

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
        logger.info("BOT STARTING - POLYMARKET + SPORTYBET SOCKET.IO")
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
        except:
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

        # Important: pass page, alerter, and credentials to SportyBet feed
        self.sportybet.set_page(page)
        self.sportybet.set_alerter(self.alerter)
        self.sportybet.set_credentials(phone, password)  # NEW: enables auto-relogin on relaunch

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

        asyncio.create_task(self.bet365.start())
        asyncio.create_task(self.sportybet.start(self.on_sb))
        asyncio.create_task(self.daily_report())

        logger.success("Bot running!")

        while self.running:
            await asyncio.sleep(1)

    async def on_bet365(self, data):
        try:
            await self.detector.on_bet365(data, self._background_slow_found)
        except Exception as e:
            logger.error(f"Fast feed handler error: {e}")

    async def on_sb(self, data):
        try:
            await self.detector.on_sportybet(data, self._background_slow_found)
            match_id = data.get('match_id')
            if match_id and self.detector.is_slow(match_id):
                asyncio.create_task(self.handle_goal(data))
        except Exception as e:
            logger.error(f"SportyBet handler error: {e}")

    async def _background_slow_found(self, game):
        asyncio.create_task(self.on_slow_found(game))

    async def on_slow_found(self, game):
        mid = game['match_id']
        if mid in self.slow_pages:
            return

        logger.info(f"Monitor: {game['home_team']} vs {game['away_team']} (lag: {game.get('gap_seconds', 0):.1f}s)")
        await self.alerter.notify_slow_match_found(game['home_team'], game['away_team'], game.get('gap_seconds', 0))

        try:
            new_context = await self.browser.new_context(viewport={'width': 412, 'height': 915})
            new_page = await new_context.new_page()
            await new_page.goto(f"{Config.SPORTYBET_BASE_URL}/ng/m/{mid}")
            await asyncio.sleep(4)

            try:
                await new_page.click('text=Next Goal')
                await asyncio.sleep(1.5)
            except:
                pass

            self.slow_pages[mid] = new_page
            self.match_last_score[mid] = game.get('_last_score', (0, 0))

        except Exception as e:
            logger.error(f"Init error for {mid}: {e}")

    async def handle_goal(self, data):
        mid = data['match_id']
        if mid not in self.slow_pages:
            return

        if self._processing.get(mid):
            return

        self._processing[mid] = True

        try:
            prev_score = self.match_last_score.get(mid, (0, 0))
            curr_score = (data.get('home_score', 0), data.get('away_score', 0))

            if curr_score == prev_score:
                return

            scoring_team = data['home_team'] if curr_score[0] > prev_score[0] else data['away_team']
            goal_num = sum(curr_score)

            page = self.slow_pages[mid]
            match = self.detector.get(mid)

            result = await self.executor.execute(page, match, scoring_team, goal_num)

            if result == "SWITCH":
                logger.info("Account switch requested")
            elif result is True:
                bet_id = f"{mid}_G{goal_num}_{int(time.time())}"
                stake = (Config.VALIDATION_STAKE if not self.executor.validation_passed
                         else self.executor.calc_stake(self.executor.last_odds_used))
                self.cashout.register(mid, bet_id, stake, f"Goal {goal_num}")
                asyncio.create_task(self.cashout.monitor(mid, data, page))

            self.match_last_score[mid] = curr_score

        except Exception as e:
            logger.error(f"Goal handling error: {e}")
        finally:
            self._processing[mid] = False

    async def daily_report(self):
        while self.running:
            await asyncio.sleep(86400)
            try:
                await self.alerter.send_daily_report()
            except Exception as e:
                logger.error(f"Report error: {e}")


if __name__ == "__main__":
    bot = ArbitrageBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Stopping...")
        try:
            asyncio.run(bot.alerter.send_daily_report())
        except:
            pass
