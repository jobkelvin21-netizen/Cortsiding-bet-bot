import asyncio
import json
from datetime import datetime
from typing import Callable, Dict, Optional
from loguru import logger

class Bet365Feed:
    def __init__(self):
        self.ws_url = None
        self.matches: Dict[str, dict] = {}
        self.running = False
        self.callback = None

    async def auto_discover(self, page):
        logger.info("Discovering bet365 WebSocket...")
        ws_urls = []

        def handle_ws(ws):
            url = ws.url
            ws_urls.append(url)
            logger.debug(f"WebSocket detected: {url[:80]}...")

        page.on("websocket", handle_ws)

        try:
            # Step 1: Go to homepage first
            await page.goto("https://www.bet365.com/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # Step 2: Go to In-Play
            await page.goto("https://www.bet365.com/#/IP/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(8)  # Give it more time to open WebSockets

            # Step 3: Scroll a bit to trigger more connections
            try:
                await page.mouse.wheel(0, 500)
                await asyncio.sleep(2)
            except:
                pass

        except Exception as e:
            logger.warning(f"Navigation error during discovery: {e}")

        # Filter good WebSocket URLs
        good_urls = []
        for url in ws_urls:
            url_lower = url.lower()
            if "bet365" in url_lower and any(x in url_lower for x in ["sport", "match", "live", "push", "score", "ip"]):
                good_urls.append(url)

        if good_urls:
            # Prefer the longest / most complete looking URL
            self.ws_url = max(good_urls, key=len)
            logger.success(f"Found WebSocket: {self.ws_url[:70]}...")
            return True

        # Fallback: take any bet365 websocket if available
        if ws_urls:
            self.ws_url = ws_urls[0]
            logger.warning(f"Using fallback WebSocket: {self.ws_url[:70]}...")
            return True

        logger.error("No WebSocket URL found")
        return False

    async def connect(self, callback: Callable):
        self.callback = callback
        self.running = True

        if not self.ws_url:
            logger.error("No WebSocket URL!")
            return

        while self.running:
            try:
                import websockets
                async with websockets.connect(
                    self.ws_url,
                    extra_headers={'Origin': 'https://www.bet365.com'},
                    ping_interval=20
                ) as ws:
                    logger.info("Connected to bet365")

                    for sub in [
                        '{"type":"sub","ch":"matches"}',
                        '{"type":"sub","ch":"scores"}',
                        '1'
                    ]:
                        await ws.send(sub)
                        await asyncio.sleep(0.5)

                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            await self.parse(msg)
                        except asyncio.TimeoutError:
                            await ws.send('2')
            except Exception as e:
                logger.error(f"WS error: {e}")
                await asyncio.sleep(5)

    async def parse(self, raw):
        try:
            data = None
            if isinstance(raw, str):
                try:
                    data = json.loads(raw)
                except:
                    parts = raw.split('|')
                    if len(parts) >= 3:
                        data = {'type': parts[0], 'mid': parts[1], 'data': parts[2:]}
            else:
                data = {'type': 'binary', 'raw': raw.hex()[:100]}

            if not data:
                return

            match = self.extract(data)
            if match:
                self.matches[match['match_id']] = match
                if self.callback:
                    await self.callback(match)
        except Exception as e:
            logger.debug(f"Parse: {e}")

    def extract(self, data: dict) -> Optional[dict]:
        try:
            mid = str(data.get('mid') or data.get('matchId') or data.get('id') or '')
            if not mid:
                return None

            score = data.get('score') or data.get('sc') or '0-0'
            if isinstance(score, str) and '-' in score:
                h, a = map(int, score.split('-'))
            else:
                h = data.get('homeScore', data.get('hs', 0))
                a = data.get('awayScore', data.get('as', 0))

            league = str(data.get('league') or data.get('tournament') or data.get('comp', ''))
            league_lower = league.lower()
            major = ['premier league', 'la liga', 'serie a', 'bundesliga', 'ligue 1', 'champions league', 'europa']
            if any(m in league_lower for m in major):
                return None

            return {
                'match_id': mid,
                'home_team': str(data.get('home') or data.get('homeTeam') or data.get('ht', '')),
                'away_team': str(data.get('away') or data.get('awayTeam') or data.get('at', '')),
                'home_score': h,
                'away_score': a,
                'league': league,
                'timestamp': datetime.now(),
                'var_status': data.get('var'),
                'offside_flag': 'offside' in str(data).lower(),
                'is_lower': True
            }
        except:
            return None

    def stop(self):
        self.running = False
