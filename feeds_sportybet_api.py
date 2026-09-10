import asyncio
import re
from datetime import datetime
from typing import Callable, Optional
from loguru import logger
import socketio
from playwright.async_api import Page


class SportyBetFeed:
    def __init__(self):
        self.matches = {}
        self.running = False
        self.callback = None
        self.page: Optional[Page] = None
        self.connected = False
        self.alerter = None
        self.sio: Optional[socketio.AsyncClient] = None

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

    async def _discover_socket_url(self) -> Optional[str]:
        """Find the Socket.IO URL from the browser"""
        if not self.page:
            return None

        found = []

        def on_ws(ws):
            url = ws.url
            if "sportybet" in url.lower() and "socket.io" in url.lower():
                # Convert to the base Socket.IO URL
                base = url.split("?")[0].replace("/socket.io/", "").replace("/socket.io", "")
                if base.startswith("wss://"):
                    base = "https://" + base[6:]
                elif base.startswith("ws://"):
                    base = "http://" + base[5:]
                found.append(base)
                logger.info(f"Captured Socket.IO candidate: {base}")

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
            logger.warning(f"Discovery error: {e}")

        try:
            self.page.remove_listener("websocket", on_ws)
        except Exception:
            pass

        if found:
            return found[0]
        return None

    async def _handle_event(self, data):
        """Process incoming Socket.IO data"""
        try:
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

                if played > 0 or str(status).upper() in ("H1", "H2", "LIVE", "1H", "2H"):
                    logger.info(
                        f"[SportyBet WS] {home} vs {away} | "
                        f"{match['home_score']}-{match['away_score']} | {status} | {played}s"
                    )

                if self.callback:
                    await self.callback(match)

        except Exception as e:
            logger.debug(f"Event handling error: {e}")

    async def start(self, callback: Callable):
        self.callback = callback
        self.running = True
        self.connected = False

        logger.info("Starting SportyBet Socket.IO feed...")

        while self.running:
            try:
                url = await self._discover_socket_url()
                if not url:
                    msg = "❌ Could not find SportyBet Socket.IO URL"
                    logger.error(msg)
                    if self.alerter:
                        await self.alerter.send(msg)
                    await asyncio.sleep(15)
                    continue

                logger.info(f"Connecting to Socket.IO: {url}")

                self.sio = socketio.AsyncClient(
                    reconnection=True,
                    reconnection_attempts=0,
                    logger=False,
                    engineio_logger=False
                )

                @self.sio.event
                async def connect():
                    self.connected = True
                    logger.success("SportyBet Socket.IO connected successfully!")
                    if self.alerter:
                        await self.alerter.send("✅ <b>SportyBet WebSocket (Socket.IO) connected successfully!</b>")

                @self.sio.event
                async def disconnect():
                    self.connected = False
                    logger.warning("SportyBet Socket.IO disconnected")

                # Listen to common event names SportyBet might use
                @self.sio.on('*')
                async def catch_all(event, data):
                    await self._handle_event(data)

                await self.sio.connect(url, transports=['websocket'], wait_timeout=15)

                # Keep the connection alive
                while self.running and self.sio.connected:
                    await asyncio.sleep(1)

            except Exception as e:
                self.connected = False
                logger.error(f"SportyBet Socket.IO error: {e}")
                if self.alerter:
                    await self.alerter.send(f"⚠️ SportyBet Socket.IO error: {e}")
                await asyncio.sleep(8)
            finally:
                if self.sio and self.sio.connected:
                    await self.sio.disconnect()

    def stop(self):
        self.running = False
