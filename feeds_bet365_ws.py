# feeds/bet365_ws.py
import asyncio
import re
import time
from datetime import datetime
from typing import Callable, Optional
from loguru import logger
from playwright.async_api import async_playwright

from proxies import ProxyRotator

RAW_LOG = "bet365_all_frames.log"
PARSED_LOG = "bet365_parsed.log"


class Bet365Feed:
    def __init__(self, callback: Callable = None):
        self.callback = callback
        self.running = False
        self.matches = {}
        self.last_message_at = None
        self.proxy_rotator = ProxyRotator()
        self.playwright = None
        self.browser = None
        self.page = None
        self._loop = None

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
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = await browser.new_page(viewport={"width": 1280, "height": 900})

            await page.goto("https://www.bet365.com/#/IP/B1", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)

            title = await page.title()
            body = await page.evaluate("document.body.innerText.slice(0, 500)")
            if any(x in body.lower() for x in ["blocked", "sorry, you have been blocked", "access denied"]):
                logger.warning(f"Proxy #{idx} BLOCKED")
                self.proxy_rotator.mark_bad(idx)
                await browser.close()
                return None, None

            # Force Football
            try:
                await page.click('text=Football', timeout=5000)
                await asyncio.sleep(3)
            except Exception:
                pass

            logger.success(f"Proxy #{idx} on In-Play Football")
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

    def _try_parse(self, payload: str) -> Optional[dict]:
        """Last attempt parser for common Bet365 patterns"""
        try:
            # Pattern 1: SS=score style
            # Example fragments often look like: SS=1-0; or SS=2-1;
            scores = re.findall(r'SS=(\d+)-(\d+)', payload)
            if not scores:
                scores = re.findall(r'(\d+)-(\d+)', payload)

            # Very rough team name extraction (best effort)
            teams = re.findall(r'([A-Z][A-Za-z0-9\s\.\-]{2,25})', payload)

            if scores and len(teams) >= 2:
                home_score, away_score = int(scores[0][0]), int(scores[0][1])
                home = teams[0].strip()
                away = teams[1].strip()

                # Basic cleaning
                if len(home) < 3 or len(away) < 3:
                    return None

                return {
                    "home_team": home,
                    "away_team": away,
                    "home_score": home_score,
                    "away_score": away_score,
                    "period": "",
                    "source": "bet365",
                    "raw": payload[:200]
                }
        except Exception:
            pass
        return None

    def _on_frame(self, payload):
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8", errors="ignore")
            except Exception:
                return

        self.last_message_at = time.time()
        length = len(payload)

        # Log almost everything
        try:
            with open(RAW_LOG, "a", encoding="utf-8", errors="ignore") as f:
                f.write(f"{datetime.now()} | LEN={length} | {payload}\n")
        except Exception:
            pass

        # Only try to parse longer messages
        if length < 40:
            return

        match = self._try_parse(payload)
        if match:
            key = f"{match['home_team']}_{match['away_team']}"
            self.matches[key] = match

            msg = f"[BET365 LIVE] {match['home_team']} {match['home_score']}-{match['away_score']} {match['away_team']}"
            logger.success(msg)

            try:
                with open(PARSED_LOG, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now()} | {msg} | {match.get('raw')}\n")
            except Exception:
                pass

            if self.callback and self._loop:
                asyncio.run_coroutine_threadsafe(self.callback(match), self._loop)

    async def _run_once(self):
        browser, page = await self._launch_with_proxy()
        if not page:
            return

        self.browser = browser
        self.page = page

        def on_ws(ws):
            logger.debug(f"WS opened: {ws.url}")

            def on_framereceived(payload):
                self._on_frame(payload)

            ws.on("framereceived", on_framereceived)

        page.on("websocket", on_ws)

        logger.success("Listening for live football events...")
        try:
            while self.running:
                await asyncio.sleep(4)
                if page.is_closed():
                    break
                if self.last_message_at and (time.time() - self.last_message_at > 45):
                    logger.warning("Stalled — reconnecting")
                    break
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    async def start(self):
        self.running = True
        self._loop = asyncio.get_event_loop()
        asyncio.create_task(self._run_loop())
        logger.info("Bet365 final attempt feed started")

    async def _run_loop(self):
        while self.running:
            await self._run_once()
            if self.running:
                await asyncio.sleep(4)

    def stop(self):
        self.running = False

    def get_matches(self):
        return list(self.matches.values())
