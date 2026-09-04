import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright
from loguru import logger
from config import Config

CREDENTIALS_FILE = Path.home() / '.betbot_creds.json'
STORAGE_FILE = Path.home() / '.betbot_storage.json'

class SportyBetAuth:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self.logged_in = False
        
    async def init(self):
        playwright = await async_playwright().start()
        
        launch_opts = {
            'headless': False,
            'args': ['--disable-blink-features=AutomationControlled', '--window-size=500,900']
        }
        if Config.PROXY:
            launch_opts['proxy'] = {'server': Config.PROXY}
            
        self.browser = await playwright.chromium.launch(**launch_opts)
        
        ctx_opts = {
            'viewport': {'width': 412, 'height': 915},
            'user_agent': 'Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36',
            'locale': 'en-NG',
            'timezone_id': 'Africa/Lagos',
            'geolocation': {'latitude': 6.5244, 'longitude': 3.3792},
        }
        
        if STORAGE_FILE.exists():
            ctx_opts['storage_state'] = str(STORAGE_FILE)
            
        self.context = await self.browser.new_context(**ctx_opts)
        await self.context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); window.chrome = { runtime: {} };")
        
        self.page = await self.context.new_page()
        await self.page.goto(f"{Config.SPORTYBET_BASE_URL}/ng/")
        await asyncio.sleep(3)
        
        balance = await self.page.query_selector('.user-balance, .account-balance')
        if balance:
            logger.success("Already logged in!")
            self.logged_in = True
            return
            
        await self.manual_login()
        
    async def manual_login(self):
        logger.info("Please login...")
        await self.page.goto(f"{Config.SPORTYBET_BASE_URL}/ng/")
        await asyncio.sleep(2)

        try:
            await self.page.click('text=Log In')
        except Exception:
            pass
        
        if CREDENTIALS_FILE.exists():
            try:
                creds = json.loads(CREDENTIALS_FILE.read_text())
                await self.page.fill('input[name="username"]', creds.get('username', ''))
                await self.page.fill('input[name="password"]', creds.get('password', ''))
            except:
                pass
                
        print("\n" + "="*50)
        print("LOGIN IN BROWSER, THEN PRESS ENTER")
        print("="*50)
        input()
        
        try:
            pass  # Skipping automated check — user confirms login manually
            save = input("Save credentials? (y/n): ").lower()
            if save == 'y':
                user = await self.page.evaluate('() => document.querySelector(".username")?.textContent || ""')
                pwd = input("Password: ")
                CREDENTIALS_FILE.write_text(json.dumps({'username': user, 'password': pwd}))
            await self.context.storage_state(path=str(STORAGE_FILE))
            self.logged_in = True
            logger.success("Login saved!")
        except Exception as e:
            logger.error(f"Login failed: {e}")
            raise
            
    async def get_page(self):
        return self.page if self.logged_in else None
        
    async def close(self):
        if self.browser:
            await self.browser.close()
