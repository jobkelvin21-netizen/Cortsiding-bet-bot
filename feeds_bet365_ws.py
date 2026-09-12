# feeds/bet365_ws.py
import asyncio
import json
import re
import time
import base64
import os
from datetime import datetime
from typing import Callable, Optional
from loguru import logger
from playwright.async_api import async_playwright

from proxies import ProxyRotator

RAW_FRAMES_LOG = "bet365_all_frames.log"
USEFUL_FRAMES_LOG = "bet365_useful_frames.log"
FOOTBALL_FRAMES_LOG = "bet365_football_frames.log"


def try_base64_decode(payload: str):
    try:
        padded = payload + "=" * (-len(payload) % 4)
        decoded_bytes = base64.b64decode(padded, validate=True)
        text = decoded_bytes.decode("utf-8")
        return json.loads(text)
    except Exception:
        return None


def is_football_related(payload: str) -> bool:
    """Simple filter to keep only messages that look football-related"""
    payload_lower = payload.lower()
    football_keywords = [
        "football", "soccer", "ovInPlay", "ovsf", "goal", "corner",
        "yellow", "red card", "penalty", "ht", "ft", "1h", "2h",
        "ss=", "sc=", "pa;", "ev;", "c1", "sportid=1"
    ]
    return any(k in payload_lower for k in football_keywords) or len(payload) > 80


class Bet365Feed:
    def __init__(self, callback: Callable = None):
        self.callback = callback
        self.running = False
        self.matches = {}
        self.last_message_at = None
        self.proxy_rotator = ProxyRotator()
        self._loop = None
        self.playwright = None
        self.browser = None
        self.page = None

    async def _launch_with_proxy(self):
        proxy, idx = self.proxy_rotator.get_next()

        if proxy is None:
            logger.warning("All proxies exhausted. Waiting 60s...")
            await asyncio.sleep(60)
            self.proxy_rotator.reset()
            proxy, idx = self.proxy_rotator.get_next()
            if proxy is None:
                return None, None

        logger.info(f"Trying proxy #{idx}: {proxy['server']}")

        browser = None
        try:
            if self.playwright is None:
                self.playwright = await async_playwright().start()

            browser = await self.playwright.chromium.launch(
                headless=False,
                proxy={
                    "server": proxy["server"],
                    "username": proxy["username"],
                    "password": proxy["password"],
                },
                args=['--disable-blink-features=AutomationControlled'],
            )
            page = await browser.new_page(viewport={'width': 1280, 'height': 800})

            # Go directly to In-Play
            await page.goto("https://www.bet365.com/#/IP/", wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(4)

            title = await page.title()
            body_text = await page.evaluate("document.body.innerText.slice(0, 400)")

            blocked_signs = ["blocked", "sorry, you have been blocked", "access denied", "attention required"]
            if any(sign in body_text.lower() for sign in blocked_signs) or any(sign in title.lower() for sign in blocked_signs):
                logger.warning(f"Proxy #{idx} got BLOCKED")
                self.proxy_rotator.mark_bad(idx)
                await browser.close()
                return None, None

            # Try to click Football
            try:
                football_btn = await page.query_selector(
                    'div:has-text("Football"), span:has-text("Football"), a:has-text("Football")'
                )
                if football_btn:
                    await football_btn.click()
                    logger.success("Clicked Football")
                    await asyncio.sleep(4)
            except Exception as e:
                logger.warning(f"Could not click Football: {e}")

            logger.success(f"Proxy #{idx} reached Bet365 In-Play Football")
            return browser, page

        except Exception as e:
            logger.warning(f"Proxy #{idx} failed: {e}")
            self.proxy_rotator.mark_bad(idx)
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            return None, None

    def _on_ws_frame(self, payload):
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8", errors="ignore")
            except Exception:
                return

        self.last_message_at = time.time()
        length = len(payload)

        # Log everything
        try:
            with open(RAW_FRAMES_LOG, "a", encoding="utf-8", errors="ignore") as f:
                f.write(f"{datetime.now()} | LEN={length} | FULL={payload}\n")
        except Exception:
            pass

        # Only keep football-related or longer messages
        if not is_football_related(payload):
            return

        # Log useful football frames
        try:
            with open(FOOTBALL_FRAMES_LOG, "a", encoding="utf-8", errors="ignore") as f:
                f.write(f"{datetime.now()} | LEN={length} | {payload}\n")
            logger.info(f"[FOOTBALL FRAME] LEN={length}")
        except Exception:
            pass

        # Also keep the useful log
        if length >= 60:
            try:
                with open(USEFUL_FRAMES_LOG, "a", encoding="utf-8", errors="ignore") as f:
                    f.write(f"{datetime.now()} | LEN={length} | {payload}\n")
            except Exception:
                pass

    async def _run_once(self):
        browser, page = await self._launch_with_proxy()
        if not page:
            return

        self.browser = browser
        self.page = page

        def on_ws(ws):
            logger.debug(f"[BET365] WebSocket opened: {ws.url}")

            def on_frame(payload):
                self._on_ws_frame(payload)

            ws.on("framereceived", on_frame)

        page.on("websocket", on_ws)

        try:
            logger.success("Watching Live Football only...")
            while self.running:
                await asyncio.sleep(5)
                if page.is_closed():
                    break
                if self.last_message_at and (time.time() - self.last_message_at > 50):
                    logger.warning("No messages for 50s — reconnecting")
                    break
        except Exception as e:
            logger.error(f"Session error: {e}")
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    async def start(self):
        self.running = True
        self._loop = asyncio.get_event_loop()
        asyncio.create_task(self._run_loop())
        logger.info("Bet365 Football-only feed starting...")

    async def _run_loop(self):
        while self.running:
            await self._run_once()
            if self.running:
                await asyncio.sleep(5)

    def stop(self):
        self.running = False

    def get_matches(self):
        return list(self.matches.values())
