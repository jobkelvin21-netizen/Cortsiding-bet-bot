# feeds/bet365_ws.py
import asyncio
import time
import os
from datetime import datetime
from typing import Callable, Optional
from loguru import logger
from playwright.async_api import async_playwright

RAW_FRAMES_LOG = "bet365_all_frames.log"
USEFUL_FRAMES_LOG = "bet365_useful_frames.log"


class Bet365Feed:
    def __init__(self, callback: Callable = None):
        self.callback = callback
        self.running = False
        self.last_message_at = None
        self.playwright = None
        self.browser = None
        self.page = None
        self._loop = None

    async def _setup_page(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled']
        )
        self.page = await self.browser.new_page(viewport={'width': 1280, 'height': 800})

        logger.info("Opening Bet365...")
        await self.page.goto("https://www.bet365.com/#/IP/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        # Try to make sure we are on In-Play Football
        try:
            # Click Football if visible
            football = await self.page.query_selector('div:has-text("Football"), span:has-text("Football"), a:has-text("Football")')
            if football:
                await football.click()
                logger.success("Clicked Football")
                await asyncio.sleep(4)
        except Exception as e:
            logger.warning(f"Could not click Football: {e}")

        # Extra wait for live data to start flowing
        logger.info("Waiting for live data to load...")
        await asyncio.sleep(8)

    def _on_frame(self, payload: str):
        self.last_message_at = time.time()
        length = len(payload)

        # Always save everything
        try:
            with open(RAW_FRAMES_LOG, "a", encoding="utf-8", errors="ignore") as f:
                f.write(f"{datetime.now()} | LEN={length} | FULL={payload}\n")
        except Exception:
            pass

        # Only keep longer messages (more likely to contain match info)
        if length >= 60:
            try:
                with open(USEFUL_FRAMES_LOG, "a", encoding="utf-8", errors="ignore") as f:
                    f.write(f"{datetime.now()} | LEN={length} | {payload}\n")
                logger.info(f"[USEFUL] LEN={length} → {payload[:120]}...")
            except Exception:
                pass

    async def _run_once(self):
        try:
            await self._setup_page()

            def on_websocket(ws):
                logger.debug(f"WebSocket opened: {ws.url}")

                def on_framereceived(payload):
                    if isinstance(payload, bytes):
                        try:
                            payload = payload.decode("utf-8", errors="ignore")
                        except Exception:
                            return
                    self._on_frame(payload)

                ws.on("framereceived", on_framereceived)

            self.page.on("websocket", on_websocket)

            logger.success("Bet365 collector running. Waiting for useful frames...")
            logger.info("Now watching. Leave it running for 1-2 minutes.")

            # Keep alive
            while self.running:
                await asyncio.sleep(5)
                if self.page.is_closed():
                    logger.warning("Page closed")
                    break

                # If no messages for too long, restart
                if self.last_message_at and (time.time() - self.last_message_at > 60):
                    logger.warning("No messages for 60s, reconnecting...")
                    break

        except Exception as e:
            logger.error(f"Collector error: {e}")
        finally:
            if self.browser:
                try:
                    await self.browser.close()
                except Exception:
                    pass

    async def start(self):
        self.running = True
        self._loop = asyncio.get_event_loop()
        logger.info("Starting improved Bet365 collector...")
        while self.running:
            await self._run_once()
            if self.running:
                logger.info("Restarting collector in 5 seconds...")
                await asyncio.sleep(5)

    def stop(self):
        self.running = False
