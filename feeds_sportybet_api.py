import asyncio
import json
import re
from datetime import datetime
from typing import Callable, Optional
from loguru import logger
import websockets
from playwright.async_api import Page


class SportyBetFeed:
    def __init__(self):
        self.matches = {}
        self.running = False
        self.callback = None
        self.page: Optional[Page] = None
        self.ws_url: Optional[str] = None
        self.connected = False
        self.alerter = None   # will be set from main

    def set_page(self, page: Page):
        self.page = page
        logger.info("SportyBetFeed received Playwright page")

    def set_alerter(self, alerter):
        self.alerter = alerter

    def _parse_played_seconds(self, value):
        try:
            if isinstance(value, (int, float)):
                return float(value)
            parts = [int(p) for p in str(value).split(':')]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
        except Exception:
            pass
        return 0.0

    def _extract_id(self, event_id):
        match = re.search(r'(\d+)$', str(event_id or ''))
        return match.group(1) if match else ''

    async def _discover_ws_url(self) -> Optional[str]:
        if not self.page:
            return None

        found = []

        def on_ws(ws):
            url = ws.url
            if any(x in url.lower() for x in ['sportybet', 'ws', 'socket', 'push', 'live']):
                found.append(url)
                logger.info(f"Captured WS candidate: {url}")

        self.page.on("websocket", on_ws)

        try:
            await self.page.goto("https://www.sportybet.com/ng/sport/football", wait_until="domcontentloaded")
            await asyncio.sleep(6)
            try:
                await self.page.click("text=Live", timeout=3000)
                await asyncio.sleep(4)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Discovery navigation error: {e}")

        try:
            self.page.remove_listener("websocket", on_ws)
        except Exception:
            pass

        if found:
            return found[0]
        return None

    async def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
            events = []

            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], list):
                    events = data["data"]
                elif "events" in data:
                    events = data["events"]
                else:
                    events = [data]
            elif isinstance(data, list):
                events = data

            for event in events:
                if not isinstance(event, dict):
                    continue

                event_id = event.get("eventId") or event.get("id") or event.get("matchId")
                home = event.get("homeTeamName") or event.get("home") or event.get("homeTeam", "")
                away = event.get("awayTeamName") or event.get("away") or event.get("awayTeam", "")

                if not event_id or not home or not away:
                    continue

                played = self._parse_played_seconds(
                    event.get("playedSeconds") or event.get("time") or event.get("played_time") or 0
                )
                status = event.get("matchStatus") or event.get("status") or event.get("period", "")

                home_score = event.get("homeScore", 0)
                away_score = event.get("awayScore", 0)

                game_score = event.get("gameScore")
                if game_score and isinstance(game_score, list) and game_score:
                    try:
                        last = str(game_score[-1])
                        h, a = last.split(":")
                        home_score, away_score = int(h), int(a)
                    except Exception:
                        pass

                match = {
                    "match_id": str(event_id),
                    "sportradar_id": self._extract_id(event_id),
                    "home_team": home,
                    "away_team": away,
                    "home_score": int(home_score or 0),
                    "away_score": int(away_score or 0),
                    "period": status,
                    "match_status": status,
                    "played_seconds": played,
                    "timestamp": datetime.now(),
                }

                self.matches[str(event_id)] = match

                if played > 0 or status.upper() in ("H1", "H2", "LIVE", "1H", "2H"):
                    logger.info(
                        f"[SportyBet WS] {home} vs {away} | {match['home_score']}-{match['away_score']} | "
                        f"{status} | {played}s"
                    )

                if self.callback:
                    await self.callback(match)

        except Exception as e:
            logger.debug(f"WS message parse error: {e}")

    async def _connect_loop(self):
        while self.running:
            try:
                if not self.ws_url:
                    logger.info("Discovering SportyBet WebSocket URL...")
                    self.ws_url = await self._discover_ws_url()

                    if not self.ws_url:
                        msg = "❌ SportyBet WebSocket NOT found. Falling back not implemented yet."
                        logger.error(msg)
                        if self.alerter:
                            await self.alerter.send(msg)
                        await asyncio.sleep(20)
                        continue

                    logger.success(f"SportyBet WebSocket URL found")
                    if self.alerter:
                        await self.alerter.send("✅ <b>SportyBet WebSocket connected successfully!</b>")

                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=10_000_000
                ) as ws:
                    self.connected = True
                    logger.success("SportyBet WebSocket connected!")

                    async for message in ws:
                        if not self.running:
                            break
                        await self._handle_message(message)

            except Exception as e:
                self.connected = False
                self.ws_url = None
                logger.warning(f"SportyBet WS disconnected: {e}")
                if self.alerter:
                    await self.alerter.send(f"⚠️ SportyBet WebSocket disconnected. Reconnecting...\n{e}")
                await asyncio.sleep(8)

    async def start(self, callback: Callable):
        self.callback = callback
        self.running = True
        self.connected = False
        logger.info("Starting SportyBet WebSocket feed...")
        await self._connect_loop()

    def stop(self):
        self.running = False
