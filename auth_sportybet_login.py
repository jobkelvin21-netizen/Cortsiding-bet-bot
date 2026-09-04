import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright
from loguru import logger

STORAGE_FILE = Path.home() / '.betbot_storage.json'

class SportyBetAuth:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        
    async def init(self):
        playwright = await async_playwright().start()
        
        launch_opts = {
            'headless': False,
            'args': ['--disable-blink-features=AutomationControlled', '--window-size=500,900']
        }
            
        self.browser = await playwright.chromium.launch(**launch_opts)
        
        ctx_opts = {
            'viewport': {'width': 412, 'height': 915},
            'user_agent': 'Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36',
            'locale': 'en-NG',
            'timezone_id': 'Africa/Lagos',
        }
        
        if STORAGE_FILE.exists():
            ctx_opts['storage_state'] = str(STORAGE_FILE)
            
        self.context = await self.browser.new_context(**ctx_opts)
        
        # Open page - YOU control everything
        self.page = await self.context.new_page()
        await self.page.goto("https://www.sportybet.com/ng/")
        
        print("\n" + "="*60)
        print("BROWSER OPENED")
        print("="*60)
        print("1. Login to SportyBet manually")
        print("2. Navigate to any page (Home, Live, etc.)")
        print("3. Make sure you're fully logged in")
        print("4. Then press ENTER here to continue")
        print("="*60)
        
        input("\nPress ENTER when ready...")
        
        # Save session
        await self.context.storage_state(path=str(STORAGE_FILE))
        logger.success("Session saved!")
        
    async def get_page(self):
        return self.page
        
    async def close(self):
        if self.browser:
            await self.browser.close()
