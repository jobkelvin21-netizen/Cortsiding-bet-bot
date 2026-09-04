import asyncio
from playwright.async_api import async_playwright

class SportyBetAuth:
    def __init__(self):
        self.browser = None
        self.page = None
        
    async def init(self):
        playwright = await async_playwright().start()
        
        self.browser = await playwright.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled']
        )
        
        self.page = await self.browser.new_page(
            viewport={'width': 412, 'height': 915}
        )
        
        # Open SportyBet
        print("Opening browser...")
        await self.page.goto("https://www.sportybet.com")
        
        print("\n" + "="*60)
        print("1. Login to SportyBet")
        print("2. When you see your balance, press ENTER")
        print("="*60)
        
        input()  # Wait for you
        
        print("Logged in! Starting bot...")
        
    async def get_page(self):
        return self.page
        
    async def close(self):
        if self.browser:
            await self.browser.close()
