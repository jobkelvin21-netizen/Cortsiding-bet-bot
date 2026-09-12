import asyncio
import re
import json
import base64
import unicodedata
from datetime import datetime
from typing import Callable, Optional

from loguru import logger
import socketio
from playwright.async_api import Page, async_playwright
from curl_cffi import requests


def _strip_accents(text: str) -> str:
    if not text:
        return ''

    nfkd = unicodedata.normalize('NFKD', text)

    return ''.join(
        c for c in nfkd
        if not unicodedata.combining(c)
    )


def _normalize_team_name(name: str) -> str:
    if not name:
        return ''

    name = _strip_accents(name).lower().strip()

    words = re.findall(
        r'[a-z0-9]+',
        name
    )

    filler = {
        'fc', 'cf', 'sc', 'afc', 'cd', 'ac',
        'fk', 'nk', 'sk', 'as', 'ss', 'rc',
        'ca', 'cs', 'us', 'sd', 'ec',
        'united', 'city', 'town', 'rovers',
        'athletic', 'sporting', 'club'
    }

    words = [
        w for w in words
        if w not in filler
    ]

    if len(words) > 1 and len(words[-1]) <= 3:
        words = words[:-1]

    return ''.join(words)


def _match_key(home: str, away: str) -> str:
    h = _normalize_team_name(home)
    a = _normalize_team_name(away)

    if h > a:
        h, a = a, h

    return f"{h}_vs_{a}"


class SportyBetFeed:

    def __init__(self):

        # =========================================================
        # EXISTING STATE
        # =========================================================

        self.matches = {}
        self.running = False
        self.callback = None

        self.page: Optional[Page] = None

        self.connected = False

        self.alerter = None

        self.sio: Optional[
            socketio.AsyncClient
        ] = None

        # =========================================================
        # BROWSER / LOGIN STATE
        # =========================================================

        self.playwright = None
        self.browser = None
        self.context = None

        self.phone = None
        self.password = None

        # =========================================================
        # NEW SOCKET SUBSCRIPTION STATE
        # =========================================================

        self.request_id = 1

        self.subscribed_matches = set()

        self.subscription_lock = asyncio.Lock()

        # Used to prevent duplicate subscription jobs
        self.subscription_task = None

    # =============================================================
    # EXISTING SETTERS
    # =============================================================

    def set_page(self, page: Page):

        self.page = page

        logger.info(
            "SportyBetFeed received Playwright page"
        )

    def set_alerter(self, alerter):

        self.alerter = alerter

    def set_credentials(
        self,
        phone: str,
        password: str
    ):

        self.phone = phone
        self.password = password

    # =============================================================
    # TIME PARSER
    # =============================================================

    def _parse_played_seconds(self, value):

        try:

            if isinstance(
                value,
                (int, float)
            ):
                return float(value)

            parts = [
                int(p)
                for p in str(value).split(':')
            ]

            if len(parts) == 2:

                return (
                    parts[0] * 60
                    + parts[1]
                )

            if len(parts) == 3:

                return (
                    parts[0] * 3600
                    + parts[1] * 60
                    + parts[2]
                )

        except Exception:
            pass

        return 0.0

    # =============================================================
    # EVENT ID
    # =============================================================

    def _extract_id(self, event_id):

        match = re.search(
            r'(\d+)$',
            str(event_id or '')
        )

        return (
            match.group(1)
            if match
            else ''
        )

    # =============================================================
    # LOGIN
    # =============================================================

    async def _login(self):

        """
        Perform login on self.page using stored credentials.
        """

        if not self.page:
            return

        try:

            await self.page.goto(
                "https://www.sportybet.com/ng/",
                wait_until="domcontentloaded"
            )

            await asyncio.sleep(2)

            login_button = await self.page.query_selector(
                'button:has-text("Log In"), '
                'a:has-text("Log In")'
            )

            if login_button:

                logger.info(
                    "Not logged in yet. Logging in..."
                )

                await login_button.click()

                await asyncio.sleep(1)

                await self.page.wait_for_selector(
                    'input[name="phone"]',
                    state="visible",
                    timeout=10000
                )

                await self.page.fill(
                    'input[name="phone"]',
                    self.phone
                )

                await asyncio.sleep(0.5)

                await self.page.wait_for_selector(
                    'input[name="psd"]',
                    state="visible",
                    timeout=10000
                )

                await self.page.fill(
                    'input[name="psd"]',
                    self.password
                )

                await asyncio.sleep(0.5)

                await self.page.click(
                    'button[name="logIn"]',
                    timeout=10000
                )

                await asyncio.sleep(5)

                logger.success(
                    "Login complete!"
                )

            else:

                logger.success(
                    "Already logged in. Continuing..."
                )

        except Exception as e:

            logger.error(
                f"Login failed during relaunch: {e}"
            )

    # =============================================================
    # CHECK PAGE
    # =============================================================

    async def _ensure_page_alive(self):

        """
        Check if self.page is still usable.

        If dead, launch a completely new browser,
        create a new page and login again.
        """

        if (
            self.page
            and not self.page.is_closed()
        ):

            try:

                await self.page.evaluate("1")

                return True

            except Exception:
                pass

        logger.warning(
            "Browser page is dead or closed. "
            "Relaunching a fresh browser..."
        )

        try:

            if self.playwright is None:

                self.playwright = (
                    await async_playwright().start()
                )

            # Close old browser if necessary
            if self.browser:

                try:
                    await self.browser.close()
                except Exception:
                    pass

            self.browser = await self.playwright.chromium.launch(
                headless=False,
                args=[
                    '--disable-blink-features=AutomationControlled'
                ]
            )

            self.context = await self.browser.new_context(
                viewport={
                    'width': 412,
                    'height': 915
                }
            )

            self.page = await self.context.new_page()

            if self.phone and self.password:

                await self._login()

            else:

                logger.warning(
                    "No stored credentials — "
                    "relaunched page may be logged out."
                )

            logger.success(
                "Fresh browser/page relaunched."
            )

            return True

        except Exception as e:

            logger.error(
                f"Failed to relaunch browser: {e}"
            )

            return False

    # =============================================================
    # SOCKET.IO URL DISCOVERY
    # =============================================================

    async def _discover_socket_url(
        self
    ) -> Optional[str]:

        """
        Automatically discover the current SportyBet
        Socket.IO server from the browser.

        This is intentionally done every connection cycle
        so if SportyBet changes the Socket.IO host, we
        discover the new host automatically.
        """

        if not self.page:
            return None

        found = []

        def on_ws(ws):

            try:

                url = ws.url

                logger.debug(
                    f"[WS DISCOVERY] WebSocket: {url}"
                )

                if (
                    "sportybet" in url.lower()
                    and "socket.io" in url.lower()
                ):

                    base = (
                        url
                        .split("?")[0]
                        .replace(
                            "/socket.io/",
                            ""
                        )
                        .replace(
                            "/socket.io",
                            ""
                        )
                    )

                    if base.startswith("wss://"):

                        base = (
                            "https://"
                            + base[6:]
                        )

                    elif base.startswith("ws://"):

                        base = (
                            "http://"
                            + base[5:]
                        )

                    if base not in found:

                        found.append(base)

                        logger.info(
                            f"[WS DISCOVERY] "
                            f"Captured Socket.IO candidate: "
                            f"{base}"
                        )

            except Exception as e:

                logger.debug(
                    f"WebSocket discovery error: {e}"
                )

        self.page.on(
            "websocket",
            on_ws
        )

        try:

            logger.info(
                "Opening SportyBet football live page "
                "to discover Socket.IO..."
            )

            await self.page.goto(
                "https://www.sportybet.com/ng/sport/football",
                wait_until="domcontentloaded",
                timeout=30000
            )

            await asyncio.sleep(6)

            # Try Live button.
            try:

                await self.page.click(
                    "text=Live",
                    timeout=3000
                )

                await asyncio.sleep(4)

            except Exception:
                pass

        except Exception as e:

            logger.warning(
                f"Discovery error: {e}"
            )

        finally:

            try:

                self.page.remove_listener(
                    "websocket",
                    on_ws
                )

            except Exception:
                pass

        if found:

            # The first discovered SportyBet Socket.IO
            # server is normally the one used by the page.
            url = found[0]

            logger.success(
                f"SportyBet Socket.IO URL discovered: "
                f"{url}"
            )

            return url

        logger.warning(
            "No SportyBet Socket.IO URL discovered."
        )

        return None

    # =============================================================
    # BASE64 DECODER
    # =============================================================

    def _decode_body(
        self,
        body_str
    ):

        try:

            padded = (
                body_str
                + "=" * (-len(body_str) % 4)
            )

            decoded_bytes = base64.b64decode(
                padded
            )

            text = decoded_bytes.decode(
                "utf-8"
            )

            return json.loads(text)

        except Exception as e:

            logger.debug(
                f"Body decode failed: {e}"
            )

            return None

    # =============================================================
    # FIND MATCHES INSIDE FACTSCENTER RESPONSE
    # =============================================================

    def _extract_subscription_matches(
        self,
        obj
    ):
        """
        SportyBet's factsCenter response can change structure.

        Instead of assuming one exact JSON layout, recursively
        search the entire response for:

            sr:match:XXXXXXXX

        and its nearest tournament ID.

        Returns:

            [
                {
                    "match_id": "...",
                    "tournament_id": "..."
                }
            ]
        """

        found = {}

        def walk(
            value,
            tournament_id=None
        ):

            if isinstance(value, dict):

                # -------------------------------------------------
                # Look for tournament ID
                # -------------------------------------------------

                current_tournament = (
                    tournament_id
                )

                for key, val in value.items():

                    key_lower = str(
                        key
                    ).lower()

                    if (
                        "tournament" in key_lower
                        and isinstance(
                            val,
                            (str, int)
                        )
                    ):

                        text = str(val)

                        match = re.search(
                            r'sr:tournament:(\d+)',
                            text
                        )

                        if match:

                            current_tournament = (
                                match.group(1)
                            )

                        elif text.isdigit():

                            current_tournament = text

                # -------------------------------------------------
                # Search every value
                # -------------------------------------------------

                for key, val in value.items():

                    # Check strings for match IDs
                    if isinstance(
                        val,
                        str
                    ):

                        matches = re.findall(
                            r'sr:match:(\d+)',
                            val
                        )

                        for match_id in matches:

                            if current_tournament:

                                found[
                                    (
                                        match_id,
                                        current_tournament
                                    )
                                ] = {
                                    "match_id": match_id,
                                    "tournament_id": (
                                        current_tournament
                                    )
                                }

                    walk(
                        val,
                        current_tournament
                    )

            elif isinstance(value, list):

                for item in value:

                    walk(
                        item,
                        tournament_id
                    )

            elif isinstance(
                value,
                str
            ):

                matches = re.findall(
                    r'sr:match:(\d+)',
                    value
                )

                for match_id in matches:

                    if tournament_id:

                        found[
                            (
                                match_id,
                                tournament_id
                            )
                        ] = {
                            "match_id": match_id,
                            "tournament_id": tournament_id
                        }

        walk(obj)

        result = list(
            found.values()
        )

        logger.info(
            f"factsCenter: discovered "
            f"{len(result)} match subscriptions"
        )

        return result

    # =============================================================
    # GET CURRENT LIVE MATCHES
    # =============================================================

    async def _get_live_matches_for_subscription(
        self
    ):

        """
        REST is used ONLY to discover which matches exist.

        It is NOT used as the live data feed.

        curl_cffi is used here because the SportyBet
        endpoint returns valid JSON through curl_cffi,
        while Playwright's API request returned HTTP 202.
        """

        url = (
            "https://www.sportybet.com/api/ng/"
            "factsCenter/liveOrPrematchEvents"
            "?sportId=sr:sport:1"
        )

        try:

            logger.info(
                "Requesting SportyBet factsCenter "
                "using curl_cffi..."
            )

            response = await asyncio.to_thread(
                requests.get,
                url,
                impersonate="chrome",
                timeout=20
            )

            logger.info(
                f"factsCenter HTTP status: "
                f"{response.status_code}"
            )

            logger.info(
                f"factsCenter content type: "
                f"{response.headers.get('content-type', '')}"
            )

            logger.info(
                f"factsCenter response size: "
                f"{len(response.text)} bytes"
            )

            if response.status_code != 200:

                logger.warning(
                    f"factsCenter returned HTTP "
                    f"{response.status_code}"
                )

                logger.debug(
                    f"factsCenter response: "
                    f"{response.text[:3000]}"
                )

                return []

            try:

                result = response.json()

            except Exception as e:

                logger.warning(
                    f"factsCenter JSON decode failed: "
                    f"{e}"
                )

                logger.debug(
                    f"factsCenter response: "
                    f"{response.text[:3000]}"
                )

                return []

            logger.success(
                "factsCenter returned valid JSON "
                "through curl_cffi."
            )

            return result

        except Exception as e:

            logger.error(
                f"factsCenter curl_cffi request failed: "
                f"{e}"
            )

            return []

    # =============================================================
    # SUBSCRIBE TO ONE MATCH
    # =============================================================

    async def _subscribe_match(
        self,
        match_id: str,
        tournament_id: str
    ):

        """
        Send the same type of Socket.IO SUB message
        observed from the SportyBet browser.

        Example:

        topic =
        \\1^52^sr:tournament:402^sr:match:72151136^status

        subType = SUB
        pushType = GROUP
        productCode = 7
        """

        if not self.sio:
            return

        if not self.sio.connected:
            return

        subscription_key = (
            f"{tournament_id}:{match_id}"
        )

        # Avoid subscribing to the same match twice
        # during the same connection.
        if subscription_key in self.subscribed_matches:

            return

        request_id = self.request_id

        self.request_id += 1

        topic = (
            "\\1^52^"
            f"sr:tournament:{tournament_id}"
            "^"
            f"sr:match:{match_id}"
            "^status"
        )

        inner = {
            "topic": topic,
            "subType": "SUB",
            "pushType": "GROUP",
            "requestId": request_id,
            "productCode": 7,
        }

        payload = {
            "type": "sub",
            "data": json.dumps(
                inner,
                separators=(",", ":")
            )
        }

        logger.info(
            f"[SUB] match={match_id} "
            f"tournament={tournament_id} "
            f"requestId={request_id}"
        )

        logger.debug(
            f"[SUB] payload={payload}"
        )

        try:

            # This creates:
            #
            # 42["data",{
            #   "type":"sub",
            #   "data":"{...}"
            # }]
            #
            # which is the structure observed from
            # the SportyBet browser.

            await self.sio.emit(
                "data",
                payload
            )

            self.subscribed_matches.add(
                subscription_key
            )

        except Exception as e:

            logger.error(
                f"Subscription failed for "
                f"{match_id}: {e}"
            )

    # =============================================================
    # SUBSCRIBE TO ALL CURRENT LIVE MATCHES
    # =============================================================

    async def _subscribe_all_live_matches(
        self
    ):

        """
        Discover current live matches using factsCenter,
        then subscribe to their STATUS streams through
        Socket.IO.

        This function is called after every successful
        Socket.IO connection.
        """

        if not self.sio:
            return

        if not self.sio.connected:

            logger.warning(
                "Cannot subscribe because Socket.IO "
                "is not connected."
            )

            return

        async with self.subscription_lock:

            try:

                logger.info(
                    "Preparing SportyBet live "
                    "Socket.IO subscriptions..."
                )

                # New Socket.IO connection means we need
                # fresh subscriptions.
                self.subscribed_matches.clear()

                # Get current matches
                response = (
                    await self._get_live_matches_for_subscription()
                )

                if not response:

                    logger.warning(
                        "factsCenter returned no data."
                    )

                    return

                # Extract sr:match + tournament
                matches = (
                    self._extract_subscription_matches(
                        response
                    )
                )

                if not matches:

                    logger.warning(
                        "No match subscriptions found "
                        "in factsCenter response."
                    )

                    return

                logger.success(
                    f"Found {len(matches)} "
                    f"live match subscription(s)."
                )

                # Send subscriptions
                for item in matches:

                    if not self.running:
                        break

                    if not self.sio.connected:
                        break

                    await self._subscribe_match(
                        match_id=item[
                            "match_id"
                        ],
                        tournament_id=item[
                            "tournament_id"
                        ]
                    )

                    # Small delay to reproduce browser-like
                    # subscription pacing.
                    await asyncio.sleep(
                        0.05
                    )

                logger.success(
                    f"Socket.IO subscriptions sent: "
                    f"{len(self.subscribed_matches)}"
                )

            except Exception as e:

                logger.error(
                    f"Error subscribing to live "
                    f"matches: {e}"
                )

    # =============================================================
    # HANDLE SOCKET.IO EVENT
    # =============================================================

    async def _handle_event(
        self,
        data
    ):

        """
        Process incoming Socket.IO data.

        Your original event parsing is kept here.
        """

        try:

            if not isinstance(
                data,
                dict
            ):
                return

            topic = data.get(
                "topic",
                ""
            )

            body_str = data.get(
                "body"
            )

            if not body_str:
                return

            decoded = self._decode_body(
                body_str
            )

            if not decoded:
                return

            # =====================================================
            # LIVE MATCH STATUS
            # =====================================================

            if (
                isinstance(decoded, dict)
                and
                "fixtureHomeTeamName" in decoded
            ):

                event_id = (
                    self._extract_id(topic)
                )

                home = decoded.get(
                    "fixtureHomeTeamName",
                    ""
                )

                away = decoded.get(
                    "fixtureAwayTeamName",
                    ""
                )

                played = (
                    self._parse_played_seconds(
                        decoded.get(
                            "eventPlayedTime",
                            0
                        )
                    )
                )

                status = decoded.get(
                    "eventMatchStatus",
                    ""
                )

                home_score = 0
                away_score = 0

                score_str = decoded.get(
                    "eventScore",
                    "0:0"
                )

                try:

                    h, a = score_str.split(
                        ":"
                    )

                    home_score = int(h)
                    away_score = int(a)

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

                self.matches[
                    event_id
                ] = match

                key = _match_key(
                    home,
                    away
                )

                logger.info(
                    f"[LIVE] "
                    f"{home} vs {away} "
                    f"-> key={key} "
                    f"score={home_score}:{away_score} "
                    f"played={played} "
                    f"status={status}"
                )

                if self.callback:

                    await self.callback(
                        match
                    )

            # =====================================================
            # OTHER DATA
            # =====================================================

            elif isinstance(
                decoded,
                list
            ):

                logger.debug(
                    f"[SportyBet WS] "
                    f"Odds update on topic "
                    f"{topic}: "
                    f"{decoded[:3]}..."
                )

        except Exception as e:

            logger.debug(
                f"Event handling error: {e}"
            )

    # =============================================================
    # SOCKET.IO START
    # =============================================================

    async def start(
        self,
        callback: Callable
    ):

        self.callback = callback

        self.running = True

        self.connected = False

        logger.info(
            "Starting SportyBet Socket.IO feed..."
        )

        while self.running:

            try:

                # =================================================
                # MAKE SURE BROWSER EXISTS
                # =================================================

                page_ok = (
                    await self._ensure_page_alive()
                )

                if not page_ok:

                    await asyncio.sleep(
                        15
                    )

                    continue

                # =================================================
                # DISCOVER SOCKET.IO URL
                # =================================================

                url = (
                    await self._discover_socket_url()
                )

                if not url:

                    msg = (
                        "❌ Could not find "
                        "SportyBet Socket.IO URL"
                    )

                    logger.error(msg)

                    if self.alerter:

                        await self.alerter.send(
                            msg
                        )

                    await asyncio.sleep(
                        15
                    )

                    continue

                logger.info(
                    f"Connecting to Socket.IO: "
                    f"{url}"
                )

                # =================================================
                # CREATE NEW SOCKET.IO CLIENT
                # =================================================

                self.sio = socketio.AsyncClient(
                    reconnection=True,
                    reconnection_attempts=0,
                    logger=False,
                    engineio_logger=False
                )

                # New connection = new subscriptions
                self.subscribed_matches.clear()

                # =================================================
                # CONNECT EVENT
                # =================================================

                @self.sio.event
                async def connect():

                    self.connected = True

                    # Reset subscriptions because this is
                    # a new Socket.IO connection.
                    self.subscribed_matches.clear()

                    logger.success(
                        "SportyBet Socket.IO "
                        "connected successfully!"
                    )

                    if self.alerter:

                        try:

                            await self.alerter.send(
                                "✅ <b>SportyBet WebSocket "
                                "(Socket.IO) connected "
                                "successfully!</b>"
                            )

                        except Exception as e:

                            logger.debug(
                                f"Alerter error: {e}"
                            )

                    # =================================================
                    # THIS IS THE IMPORTANT PART
                    #
                    # Browser-style flow:
                    #
                    # CONNECT
                    #   ↓
                    # factsCenter
                    #   ↓
                    # get match IDs
                    #   ↓
                    # send SUB through Socket.IO
                    # =================================================

                    try:

                        await self._subscribe_all_live_matches()

                    except Exception as e:

                        logger.error(
                            f"Initial subscription "
                            f"failed: {e}"
                        )

                # =================================================
                # DISCONNECT EVENT
                # =================================================

                @self.sio.event
                async def disconnect():

                    self.connected = False

                    self.subscribed_matches.clear()

                    logger.warning(
                        "SportyBet Socket.IO disconnected"
                    )

                # =================================================
                # CATCH ALL SOCKET.IO EVENTS
                # =================================================

                @self.sio.on('*')
                async def catch_all(
                    event,
                    raw
                ):

                    try:

                        logger.debug(
                            f"[Socket.IO] "
                            f"event={event}"
                        )

                        # Some Socket.IO responses can arrive
                        # wrapped in a data string.
                        if (
                            isinstance(
                                raw,
                                dict
                            )
                            and
                            "data" in raw
                            and
                            isinstance(
                                raw["data"],
                                str
                            )
                        ):

                            try:

                                inner = json.loads(
                                    raw["data"]
                                )

                                # Handle normal SportyBet
                                # topic/body message.
                                if isinstance(
                                    inner,
                                    dict
                                ):

                                    await self._handle_event(
                                        inner
                                    )

                            except json.JSONDecodeError:

                                await self._handle_event(
                                    raw
                                )

                        else:

                            await self._handle_event(
                                raw
                            )

                    except Exception as e:

                        logger.debug(
                            f"catch_all parse error: "
                            f"{e}"
                        )

                # =================================================
                # CONNECT
                # =================================================

                await self.sio.connect(
                    url,
                    transports=[
                        'websocket'
                    ]
                )

                # =================================================
                # WAIT WHILE CONNECTED
                # =================================================

                while (
                    self.running
                    and
                    self.sio
                    and
                    self.sio.connected
                ):

                    await asyncio.sleep(
                        1
                    )

            except Exception as e:

                self.connected = False

                logger.error(
                    f"SportyBet Socket.IO error: "
                    f"{e}"
                )

                if self.alerter:

                    try:

                        await self.alerter.send(
                            f"⚠️ SportyBet Socket.IO "
                            f"error: {e}"
                        )

                    except Exception:
                        pass

                await asyncio.sleep(
                    8
                )

            finally:

                self.connected = False

                self.subscribed_matches.clear()

                # =================================================
                # CLOSE OLD SOCKET CLEANLY
                # =================================================

                if (
                    self.sio
                    and
                    self.sio.connected
                ):

                    try:

                        await self.sio.disconnect()

                    except Exception:
                        pass

                self.sio = None

                # =================================================
                # IMPORTANT:
                #
                # When the loop starts again, it will:
                #
                # 1. Check browser
                # 2. Discover Socket.IO URL AGAIN
                # 3. Connect to the NEW URL
                # 4. Call factsCenter
                # 5. Subscribe again
                #
                # So a changed SportyBet Socket.IO host
                # is automatically handled.
                # =================================================

        logger.info(
            "SportyBet Socket.IO feed stopped."
        )

    # =============================================================
    # STOP
    # =============================================================

    def stop(self):

        self.running = False

        self.connected = False

        logger.info(
            "Stopping SportyBet Socket.IO feed..."
        )
