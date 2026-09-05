import asyncio
import json
import websockets
from typing import Callable, Dict, List
from datetime import datetime
from loguru import logger


class Bet365Feed:
    def __init__(self, callback: Callable):
        self.callback = callback
        self.ws_url = None
        self.running = False
        self.matches: Dict = {}
        self._retry_count = 0
        self._max_retries = 5

    async def discover_ws_url(self, page):
        """
        Use a real logged-in Playwright page to capture the actual
        WebSocket URL bet365 connects to. This is more reliable than
        guessing, since it observes the real browser's live connection.
        """
        ws_url_found = asyncio.Future()

        def on_websocket(ws):
            if "365lpodds.com" in ws.url or "zap" in ws.url:
                if not ws_url_found.done():
                    ws_url_found.set_result(ws.url)

        page.on("websocket", on_websocket)

        try:
            await page.goto("https://www.bet365.com/", timeout=30000)
            self.ws_url = await asyncio.wait_for(ws_url_found, timeout=20)
            logger.success(f"Captured real bet365 WebSocket URL: {self.ws_url}")
            return True
        except asyncio.TimeoutError:
            logger.warning("Could not capture bet365 WebSocket URL from live page")
            return False
        except Exception as e:
            logger.error(f"Error discovering ws_url: {e}")
            return False

    async def connect(self):
        """Connect to bet365 WebSocket"""
        if not self.ws_url:
            logger.warning("No ws_url available yet — cannot connect")
            return

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Origin': 'https://www.bet365.com',
            'Accept-Language': 'en-US,en;q=0.9',
        }

        try:
            logger.info(f"Connecting to: {self.ws_url}")
            async with websockets.connect(
                self.ws_url,
                extra_headers=headers,
                ping_interval=None
            ) as ws:
                logger.success("Bet365 WebSocket connected!")
                self._retry_count = 0
                await ws.send('__time,')

                while self.running:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        if message:
                            await self.process_message(message)
                    except asyncio.TimeoutError:
                        await ws.send('__time,')

        except Exception as e:
            logger.error(f"Bet365 connection error: {e}")
            self.ws_url = None
            self._retry_count += 1
            if self.running and self._retry_count <= self._max_retries:
                wait = min(5 * self._retry_count, 60)
                logger.info(f"Retrying in {wait}s (attempt {self._retry_count}/{self._max_retries})")
                await asyncio.sleep(wait)
                await self.connect()
            else:
                logger.error("Max retries reached — giving up on Bet365 feed")

    async def process_message(self, data: str):
        try:
            if data in ['pong', '__time']:
                return
            match = self.parse_match(data)
            if match and self.callback:
                await self.callback(match)
        except Exception:
            pass

    def parse_match(self, data: str) -> Dict:
        try:
            if '|' not in data:
                return None
            parts = data.split('|')
            if len(parts) < 2:
                return None
            match_id = parts[1]
            return {
                'id': match_id,
                'match_id': match_id,
                'home': 'Home',
                'away': 'Away',
                'home_team': 'Home',
                'away_team': 'Away',
                'home_score': 0,
                'away_score': 0,
                'league': 'Live',
                'timestamp': datetime.now()
            }
        except Exception:
            return None

    async def start(self):
        """Start the feed (now properly async)"""
        self.running = True
        asyncio.create_task(self.connect())

    def stop(self):
        self.running = False

    def get_matches(self) -> List[Dict]:
        return list(self.matches.values())
