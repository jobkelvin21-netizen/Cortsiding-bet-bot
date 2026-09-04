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
        phone = input("Phone Number: ")
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
        await self.page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # Open login form if needed
        try:
            await self.page.click('button:has-text("Log In"), a:has-text("Log In")', timeout=5000)
            await asyncio.sleep(1)
        except:
            pass

        logger.info("Logging in...")

        # Phone number field
        await self.page.wait_for_selector('input[name="phone"]', state="visible", timeout=15000)
        await self.page.fill('input[name="phone"]', phone)
        await asyncio.sleep(0.5)

        # Password field
        await self.page.wait_for_selector('input[name="psd"]', state="visible", timeout=10000)
        await self.page.fill('input[name="psd"]', password)
        await asyncio.sleep(0.5)

        # Click Login button
        await self.page.click('button[name="logIn"]')
        await asyncio.sleep(5)

        logger.success("Login complete!")

    async def get_page(self):
        return self.page

    async def close(self):
        if self.browser:
            await self.browser.close()
