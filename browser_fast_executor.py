import asyncio
import random
from playwright.async_api import Page

class FastExecutor:
    @staticmethod
    async def delay(min_sec=0.3, max_sec=0.8):
        await asyncio.sleep(random.uniform(min_sec, max_sec))
    
    @staticmethod
    async def fast_click(page: Page, selector: str):
        try:
            await asyncio.sleep(random.uniform(0.1, 0.3))
            await page.click(selector, force=True)
            return True
        except:
            try:
                await page.evaluate(f'document.querySelector("{selector}")?.click()')
                return True
            except:
                return False
    
    @staticmethod
    async def fast_type(page: Page, selector: str, text: str):
        try:
            await page.click(selector)
            await asyncio.sleep(0.15)
            for char in str(text):
                await page.keyboard.press(char)
                await asyncio.sleep(random.uniform(0.03, 0.05))
            return True
        except:
            try:
                await page.fill(selector, str(text))
                return True
            except:
                return False
    
    @staticmethod
    async def scroll_to(page: Page, selector: str):
        try:
            await page.evaluate(f'document.querySelector("{selector}")?.scrollIntoView({{behavior: "auto", block: "center"}})')
            await asyncio.sleep(0.3)
            return True
        except:
            return False
