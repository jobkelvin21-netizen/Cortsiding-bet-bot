import asyncio
import re
import json
import base64
from datetime import datetime
from typing import Callable, Optional
from loguru import logger
import socketio
from playwright.async_api import Page, async_playwright


class SportyBetFeed:
    def __init__(self):
        self.matches = {}
        self.running = False
        self.callback = None
        self.page: Optional[Page] = None
        self.connected = False
        self.alerter = None
        self.sio: Optional[socketio.AsyncClient] = None

        # For auto-relaunch/re-login
        self.playwright = None
        self.browser = None
        self.context = None
        self.phone = None
        self.password = None

    def set_page(self, page: Page):
        self.page = page
        logger.info("SportyBetFeed received Playwright page")

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_credentials(self, phone: str, password: str):
        self.phone = phone
        self.password = password

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

    async def _login(self):
        """Perform login on self.page using stored credentials."""
        try:
            await self.page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded")
            await asyncio.sleep(2)

            login_button = await self.page.query_selector('button:has-text("Log In"), a:has-text("Log In")')

            if login_button:
                logger.info("Not logged in yet. Logging in...")
                await login_button.click()
                await asyncio.sleep(1)

                await self.page.wait_for_selector('input[name="phone"]', state="visible", timeout=0)
                await self.page.fill('input[name="phone"]', self.phone)
                await asyncio.sleep(0.5)

                await self.page.wait_for_selector('input[name="psd"]', state="visible", timeout=0)
                await self.page.fill('input[name="psd"]', self.password)
                await asyncio.sleep(0.5)

                await self.page.click('button[name="logIn"]', timeout=0)
                await asyncio.sleep(5)
                logger.success("Login complete!")
            else:
                logger.success("Already logged in. Continuing...")

        except Exception as e:
            logger.error(f"Login failed during relaunch: {e}")

    async def _ensure_page_alive(self):
        """Check if self.page is still usable; if not, relaunch a fresh browser+page and re-login."""
        if self.page and not self.page.is_closed():
            try:
                await self.page.evaluate("1")
                return True
            except Exception:
                pass

        logger.warning("Browser page is dead or closed. Relaunching a fresh browser...")

        try:
            if self.playwright is None:
                self.playwright = await async_playwright().start()

            self.browser = await self.playwright.chromium.launch(
                headless=False,
                args=['--disable-blink-features=AutomationControlled']
            )
            self.context = await self.browser.new_context(viewport={'width': 412, 'height': 915})
            self.page = await self.context.new_page()

            if self.phone and self.password:
                await self._login()
            else:
                logger.warning("No stored credentials — relaunched page may be logged out.")

            logger.success("Fresh browser/page relaunched.")
            return True

        except Exception as e:
            logger.error(f"Failed to relaunch browser: {e}")
            return False

    async def _discover_socket_url(self) -> Optional[str]:
        """Find the Socket.IO URL from the browser"""
        if not self.page:
            return None

        found = []

        def on_ws(ws):
            url = ws.url
            if "sportybet" in url.lower() and "socket.io" in url.lower():
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

    def _decode_body(self, body_str):
        try:
            padded = body_str + "=" * (-len(body_str) % 4)
            decoded_bytes = base64.b64decode(padded)
            text = decoded_bytes.decode("utf-8")
            return json.loads(text)
        except Exception as e:
            logger.debug(f"Body decode failed: {e}")
            return None

    async def _handle_event(self, data):
        """Process incoming Socket.IO data (real SportyBet format: topic + base64 body)"""
        try:
            if not isinstance(data, dict):
                return

            topic = data.get("topic", "")
            body_str = data.get("body")

            if not body_str:
                return

            decoded = self._decode_body(body_str)
            if not decoded:
                return

            if isinstance(decoded, dict) and "fixtureHomeTeamName" in decoded:
                event_id = self._extract_id(topic)
                home = decoded.get("fixtureHomeTeamName", "")
                away = decoded.get("fixtureAwayTeamName", "")
                played = self._parse_played_seconds(decoded.get("eventPlayedTime", 0))
                status = decoded.get("eventMatchStatus", "")

                home_score, away_score = 0, 0
                score_str = decoded.get("eventScore", "0:0")
                try:
                    h, a = score_str.split(":")
                    home_score, away_score = int(h), int(a)
                except Exception:
                    pass

                match = {
                    "match_id": event_id,
                    "sportradar_id": event_id,
                    "home_team": home,
                    "away_team": away,
                    "home_score": home_score,
                    "away_score": away_score,
                    "period": status,
                    "match_status": status,
                    "played_seconds": played,
                    "timestamp": datetime.now(),
                }

                self.matches[event_id] = match

                logger.info(
                    f"[SportyBet WS] {home} vs {away} | "
                    f"{match['home_score']}-{match['away_score']} | {status} | {played}s"
                )

                if self.callback:
                    await self.callback(match)

            elif isinstance(decoded, list):
                logger.debug(f"[SportyBet WS] Odds update on topic {topic}: {decoded[:3]}...")

        except Exception as e:
            logger.debug(f"Event handling error: {e}")

    async def start(self, callback: Callable):
        self.callback = callback
        self.running = True
        self.connected = False

        logger.info("Starting SportyBet Socket.IO feed...")

        while self.running:
            try:
                page_ok = await self._ensure_page_alive()
                if not page_ok:
                    await asyncio.sleep(15)
                    continue

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

                @self.sio.on('*')
                async def catch_all(event, raw):
                    try:
                        if isinstance(raw, dict) and "data" in raw and isinstance(raw["data"], str):
                            inner = json.loads(raw["data"])
                            await self._handle_event(inner)
                        else:
                            await self._handle_event(raw)
                    except Exception as e:
                        logger.debug(f"catch_all parse error: {e}")

                await self.sio.connect(url, transports=['websocket'])

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
