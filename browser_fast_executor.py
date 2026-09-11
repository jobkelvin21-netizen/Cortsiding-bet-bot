import asyncio
import random
from playwright.async_api import Page


class FastExecutor:
    @staticmethod
    async def delay(min_sec=0.3, max_sec=0.8):
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    @staticmethod
    async def fast_click(page: Page, selector: str, timeout: int = 1500, human_delay: bool = True):
        """
        FIX: added explicit timeout (was defaulting to Playwright's 30s,
        meaning one missed selector could hang for 30 real seconds — fatal
        against a 3-second detection window). human_delay=False skips the
        randomized pause entirely for the live-betting hot path, where
        speed matters more than appearing human.
        """
        try:
            if human_delay:
                await asyncio.sleep(random.uniform(0.05, 0.15))
            await page.click(selector, force=True, timeout=timeout)
            return True
        except Exception:
            try:
                clicked = await page.evaluate(
                    f'() => {{ const el = document.querySelector("{selector}"); '
                    f'if (el) {{ el.click(); return true; }} return false; }}'
                )
                return bool(clicked)
            except Exception:
                return False

    @staticmethod
    async def fast_type(page: Page, selector: str, text: str, timeout: int = 1500, human_delay: bool = True):
        try:
            await page.click(selector, timeout=timeout)
            if human_delay:
                await asyncio.sleep(0.1)
                for char in str(text):
                    await page.keyboard.press(char)
                    await asyncio.sleep(random.uniform(0.02, 0.03))
            else:
                await page.fill(selector, str(text))
            return True
        except Exception:
            try:
                await page.fill(selector, str(text))
                return True
            except Exception:
                return False

    @staticmethod
    async def scroll_to(page: Page, selector: str):
        try:
            await page.evaluate(
                f'document.querySelector("{selector}")?.scrollIntoView({{behavior: "auto", block: "center"}})'
            )
            await asyncio.sleep(0.15)
            return True
        except Exception:
            return False
