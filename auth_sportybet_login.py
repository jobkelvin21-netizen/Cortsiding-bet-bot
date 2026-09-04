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
        
        # Launch browser
        self.browser = await playwright.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled']
        )
        
        # Create context
        self.context = await self.browser.new_context(
            viewport={'width': 412, 'height': 915},
            user_agent='Mozilla/5.0 (Linux; Android 13; SM-G998B)'
        )
        
        # Create page
        self.page = await self.context.new_page()
        
        # Go to SportyBet
        logger.info("Opening SportyBet...")
        await self.page.goto("https://www.sportybet.com/ng/")
        
        # Wait for manual login
        print("\n" + "="*60)
        print("1. Login to SportyBet in the browser")
        print("2. Press ENTER here when logged in")
        print("="*60)
        input()
        
        # Save session
        await self.context.storage_state(path=str(STORAGE_FILE))
        logger.success("Login saved!")
        
    async def get_page(self):
        return self.page
        
    async def close(self):
        if self.browser:
            await self.browser.close()
