import asyncio
from playwright.async_api import async_playwright
from loguru import logger

class SportyBetAuth:
    def __init__(self):
        self.browser = None
        self.page = None
        
    async def init(self):
        # Get credentials
        print("\n" + "="*60)
        print("SPORTYBET LOGIN")
        print("="*60)
        username = input("Phone/Username: ")
        password = input("Password: ")
        print("="*60)
        
        # Launch browser
        playwright = await async_playwright().start()
        self.browser = await playwright.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled']
        )
        
        self.page = await self.browser.new_page(
            viewport={'width': 412, 'height': 915}
        )
        
        # Login
        logger.info("Opening login page...")
        await self.page.goto("https://www.sportybet.com/ng/login")
        await asyncio.sleep(3)
        
        logger.info("Logging in...")
        await self.page.fill('input[name="username"]', username)
        await asyncio.sleep(0.5)
        await self.page.fill('input[name="password"]', password)
        await asyncio.sleep(0.5)
        await self.page.click('button[type="submit"]')
        await asyncio.sleep(5)
        
        logger.success("Login complete!")
        
    async def get_page(self):
        return self.page
        
    async def close(self):
        if self.browser:
            await self.browser.close()
