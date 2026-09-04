import asyncio
import sys
from datetime import datetime, timedelta
from loguru import logger

from config import Config
from auth.sportybet_login import SportyBetAuth
from feeds.flashscore_ws import FlashScoreFeed  # Changed from bet365
from feeds.sportybet_api import SportyBetFeed
from core.detector import SlowGameDetector
from core.executor import BetExecutor
from core.cashout import CashOutManager
from utils.telegram import TelegramAlerter
from utils.account_manager import AccountManager
from utils.learning import LearningEngine
from utils.security import SecurityManager

class ArbitrageBot:
    def __init__(self):
        self.security = None  # Disable security manager for now
        self.learning = LearningEngine() if Config.LEARNING_ENABLED else None
        self.account_manager = AccountManager()
        self.alerter = TelegramAlerter()
        self.auth = SportyBetAuth()
        self.flashscore = FlashScoreFeed()  # Changed from bet365
        self.sportybet = SportyBetFeed()
        self.detector = SlowGameDetector()
        self.executor = None
        self.cashout = CashOutManager(self.alerter)
        self.slow_pages = {}
        self.running = False
        self._processing = {}
        self.match_last_score = {}
        
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
            
        if Config.TEST_MODE:
            Config.TEST_END_TIME = datetime.now() + timedelta(hours=Config.TEST_DURATION_HOURS)
            logger.info(f"Test mode: {Config.TEST_DURATION_HOURS} hours")
            
    async def check_mode_switch(self):
        while self.running:
            await asyncio.sleep(60)
            if Config.TEST_MODE and Config.check_test_mode_expired():
                logger.warning("SWITCHING TO REAL MONEY")
                Config.enable_real_mode()
                await self.alerter.notify_mode_switch()
                
    async def start(self):
        await self.setup()
        
        logger.info("="*60)
        logger.info("BOT STARTING - FLASHSCORE EDITION")
        logger.info("="*60)
        
        # Get credentials and login automatically (your working code)
        print("\n" + "="*60)
        print("SPORTYBET LOGIN")
        print("="*60)
        phone = input("Phone Number: ")
        password = input("Password: ")
        print("="*60)
        
        # Launch browser and login (your working code)
        from playwright.async_api import async_playwright
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled']
        )
        
        page = await browser.new_page(
            viewport={'width': 412, 'height': 915}
        )
        
        logger.info("Opening login page...")
        await page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        # Open login form if needed
        try:
            await page.click('button:has-text("Log In"), a:has-text("Log In")', timeout=5000)
            await asyncio.sleep(1)
        except:
            pass
            
        logger.info("Logging in...")
        
        # Phone number
        await page.wait_for_selector('input[name="phone"]', state="visible", timeout=15000)
        await page.fill('input[name="phone"]', phone)
        await asyncio.sleep(0.5)
        
        # Password
        await page.wait_for_selector('input[name="psd"]', state="visible", timeout=10000)
        await page.fill('input[name="psd"]', password)
        await asyncio.sleep(0.5)
        
        # Click Login
        await page.click('button[name="logIn"]')
        await asyncio.sleep(5)
        
        logger.success("Login complete!")
        
        # Set up executor with this page
        self.auth.browser = browser
        self.auth.page = page
        
        self.executor = BetExecutor(self.alerter, self.account_manager, self.learning)
        self.executor.current_account = self.account_manager.get_active()
        
        # Get balance
        print("\nEnter your current SportyBet balance:")
        bal = float(input("Balance: "))
        self.executor.balance = bal
        
        mode = "TEST" if Config.TEST_MODE else "REAL"
        await self.alerter.notify_startup(bal, mode)
        
        # Start FlashScore WebSocket (no browser discovery needed)
        logger.info("Starting FlashScore WebSocket feed...")
        self.running = True
        
        # Start feeds
        asyncio.create_task(self.flashscore.connect(self.on_flashscore))  # Changed from bet365
        asyncio.create_task(self.sportybet.start(self.on_sportybet))
        asyncio.create_task(self.check_mode_switch())
        asyncio.create_task(self.daily_report())
        
        logger.success("Running! FlashScore WebSocket + SportyBet monitoring.")
        
        while self.running:
            await asyncio.sleep(1)
            
    async def on_flashscore(self, data):  # Changed from on_b365
        try:
            await self.detector.on_flashscore(data)  # Changed method name
            if self.detector.is_slow(data['match_id']):
                await self.handle_goal(data)
        except Exception as e:
            logger.error(f"FlashScore handler error: {e}")
            
    async def on_sportybet(self, data):  # Changed from on_sb
        try:
            await self.detector.on_sportybet(data, self.on_slow_found)
        except Exception as e:
            logger.error(f"SportyBet handler error: {e}")
        
    async def on_slow_found(self, game):
        mid = game['match_id']
        if mid in self.slow_pages:
            return
            
        logger.info(f"Monitor: {game['home_team']} vs {game['away_team']}")
        
        try:
            # Use the main page context to open new tab
            page = self.auth.page
            new_page = await page.context.new_page()
            await new_page.goto(f"{Config.SPORTYBET_BASE_URL}/ng/m/{mid}")
            await asyncio.sleep(4)
            
            try:
                await new_page.click('text=Next Goal')
                await asyncio.sleep(1.5)
            except:
                pass
                
            self.slow_pages[mid] = new_page
            self.match_last_score[mid] = (0, 0)
            
        except Exception as e:
            logger.error(f"Init error: {e}")
            
    async def handle_goal(self, data):
        mid = data['match_id']
        if mid not in self.slow_pages:
            return
            
        if self._processing.get(mid):
            return
            
        self._processing[mid] = True
        
        try:
            prev_score = self.match_last_score.get(mid, (0, 0))
            curr_score = (data['home_score'], data['away_score'])
            
            if curr_score == prev_score:
                return
                
            if data.get('var_status') or data.get('offside_flag'):
                return
                
            scoring_team = data['home_team'] if curr_score[0] > prev_score[0] else data['away_team']
            goal_num = sum(curr_score)
            
            page = self.slow_pages[mid]
            match = self.detector.get(mid)
            
            league = match.get('league', '') if match else ''
            time_str = datetime.now().strftime('%H:%M')
            
            odds = await self.get_odds(page, scoring_team)
            if odds <= 0:
                logger.error("No odds found")
                return
                
            result = await self.executor.execute(page, match, scoring_team, odds, goal_num)
            
            if result == "SWITCH":
                logger.info("Account switch requested")
            elif result is True:
                bet_id = f"{mid}_G{goal_num}_{int(time.time())}"
                stake = self.executor.calc_stake(odds)
                self.cashout.register(mid, bet_id, stake, f"Goal {goal_num}")
                asyncio.create_task(self.cashout.monitor(mid, data, page))
                
                if self.learning:
                    self.learning.record_bet(mid, league, odds, time_str, won=True)
                
            self.match_last_score[mid] = curr_score
            
        except Exception as e:
            logger.error(f"Goal handling error: {e}")
        finally:
            self._processing[mid] = False
            
    async def get_odds(self, page, team):
        try:
            return await page.evaluate(f'''
                () => {{
                    const el = document.querySelector('[data-team="{team}"] .odds');
                    return el ? parseFloat(el.textContent) : 0;
                }}
            ''')
        except:
            return 0.0
            
    async def daily_report(self):
        while self.running:
            await asyncio.sleep(86400)
            try:
                await self.alerter.send_daily_report()
                if self.learning:
                    insights = self.learning.get_insights()
                    await self.alerter.send(insights)
            except Exception as e:
                logger.error(f"Report error: {e}")

if __name__ == "__main__":
    bot = ArbitrageBot()
    
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Stopping...")
        try:
            if bot.learning:
                insights = bot.learning.get_insights()
                asyncio.run(bot.alerter.send(insights))
            asyncio.run(bot.alerter.send_daily_report())
        except:
            pass
