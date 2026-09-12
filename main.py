import asyncio
import sys
import time
from datetime import datetime
from loguru import logger

from config import Config
from auth_sportybet_login import SportyBetAuth
from feeds.bet365_ws import Bet365Feed
from feeds.sportybet_api import SportyBetFeed
from core.detector import GoalMatchDetector
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
        self.detector = GoalMatchDetector()
        self.executor = None
        self.cashout = CashOutManager(self.alerter)
        self.match_pages = {}
        self.running = False
        self._processing = {}
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

        logger.info("="*60)
        logger.info("BOT STARTING - POLYMARKET (FAST) + SPORTYBET")
        logger.info("MATCH-PAIRING + GOAL-EVENT MODE")
        logger.info("="*60)

        print("\n" + "="*60)
        print("SPORTYBET LOGIN")
        print("="*60)
        phone = input("Phone Number: ")
        password = input("Password: ")
        print("="*60)

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

        self.auth.browser = browser
        self.auth.page = page

        try:
            self.sportybet.set_page(page)
        except AttributeError:
            pass
        try:
            self.sportybet.set_alerter(self.alerter)
        except AttributeError:
            pass

        self.executor = BetExecutor(self.alerter, self.account_manager, None)
        self.executor.current_account = self.account_manager.get_active()

        print("\nEnter your current SportyBet balance:")
        bal = float(input("Balance: "))
        self.executor.balance = bal

        await self.alerter.notify_startup(bal)

        logger.info("Starting feeds...")
        self.running = True

        try:
            asyncio.create_task(self.bet365.start())
            asyncio.create_task(self.sportybet.start(self.on_sb))
        except Exception as e:
            logger.warning(f"Feed error: {e}")

        asyncio.create_task(self.daily_report())

        logger.success("Bot running! Watching for matched games + goals.")

        while self.running:
            await asyncio.sleep(1)

    async def on_bet365(self, data):
        try:
            await self.detector.on_bet365(
                data,
                matched_callback=self._on_match_confirmed,
                goal_callback=self._on_goal_from_fast_feed
            )
        except Exception as e:
            logger.error(f"Fast feed handler error: {e}")

    async def on_sb(self, data):
        try:
            await self.detector.on_sportybet(data, matched_callback=self._on_match_confirmed)
        except Exception as e:
            logger.error(f"SportyBet handler error: {e}")

    async def _on_match_confirmed(self, rec):
        """Fires once, the moment a match is confirmed live on BOTH feeds."""
        asyncio.create_task(
            self.alerter.notify_match_matched(rec.home_team, rec.away_team)
        )
        asyncio.create_task(self._ensure_page_open(rec))

    async def _ensure_page_open(self, rec):
        key = rec.key
        if key in self.match_pages or not rec.sb_event_id:
            return
        try:
            new_context = await self.browser.new_context(viewport={'width': 412, 'height': 915})
            new_page = await new_context.new_page()
            await new_page.goto(f"{Config.SPORTYBET_BASE_URL}/ng/m/{rec.sb_event_id}")
            await asyncio.sleep(4)
            try:
                await new_page.click('text=Next Goal')
                await asyncio.sleep(1.5)
            except Exception:
                pass
            self.match_pages[key] = new_page
            logger.info(f"Monitoring page opened: {rec.home_team} vs {rec.away_team}")
        except Exception as e:
            logger.error(f"Could not open monitoring page for {rec.home_team} vs {rec.away_team}: {e}")

    async def _on_goal_from_fast_feed(self, rec, scoring_side, prev_score, new_score):
        key = rec.key
        if key not in self.match_pages:
            return
        if self._processing.get(key):
            return
        asyncio.create_task(self._handle_goal(rec, scoring_side, prev_score, new_score))

    async def _handle_goal(self, rec, scoring_side, prev_score, new_score):
        key = rec.key
        self._processing[key] = True

        try:
            goals_before = sum(prev_score)
            if not self.detector.is_market_still_valid(key, goals_before):
                logger.warning(
                    f"🚫 SKIPPING bet on {rec.home_team} vs {rec.away_team} — "
                    f"SportyBet already reflects this goal (market has moved on)"
                )
                await self.alerter.notify_market_skipped(rec.home_team, rec.away_team)
                return

            scoring_team = rec.home_team if scoring_side == 'home' else rec.away_team
            goal_num = sum(new_score)

            page = self.match_pages[key]
            match = {
                'match_id': rec.sb_event_id or key,
                'home_team': rec.home_team,
                'away_team': rec.away_team,
            }

            result = await self.executor.execute(page, match, scoring_team, goal_num)

            if result == "SWITCH":
                logger.info("Account switch requested")
            elif result is True:
                bet_id = f"{rec.sb_event_id}_G{goal_num}_{int(time.time())}"
                stake = (Config.VALIDATION_STAKE if not self.executor.validation_passed
                         else self.executor.calc_stake(self.executor.last_odds_used))
                self.cashout.register(rec.sb_event_id, bet_id, stake, f"Goal {goal_num}")
                asyncio.create_task(self.cashout.monitor(rec.sb_event_id, match, page))

        except Exception as e:
            logger.error(f"Goal handling error: {e}")
        finally:
            self._processing[key] = False

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
        except Exception:
            pass
