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
        # SOCKET SUBSCRIPTION STATE
        # =========================================================

        self.request_id = 1

        self.subscribed_matches = set()

        self.subscription_lock = asyncio.Lock()

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
            r'sr:match:(\d+)',
            str(event_id or '')
        )

        if match:
            return match.group(1)

        match = re.search(
            r'(\d+)',
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

        if not body_str:
            return None

        try:

            if isinstance(
                body_str,
                bytes
            ):

                raw = body_str

            else:

                value = str(
                    body_str
                ).strip()

                value = re.sub(
                    r'\s+',
                    '',
                    value
                )

                value += '=' * (
                    -len(value) % 4
                )

                try:

                    raw = base64.b64decode(
                        value,
                        validate=False
                    )

                except Exception:

                    raw = base64.urlsafe_b64decode(
                        value
                    )

            text = raw.decode(
                "utf-8"
            ).strip()

            return json.loads(
                text
            )

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

        found = {}

        try:

            if not isinstance(
                obj,
                dict
            ):

                logger.warning(
                    "factsCenter response is not a dictionary."
                )

                return []

            tournaments = obj.get(
                "data",
                []
            )

            if not isinstance(
                tournaments,
                list
            ):

                logger.warning(
                    "factsCenter 'data' is not a list."
                )

                return []

            for tournament in tournaments:

                if not isinstance(
                    tournament,
                    dict
                ):
                    continue

                tournament_id = ""

                tournament_value = (
                    tournament.get(
                        "id",
                        ""
                    )
                )

                tournament_match = re.search(
                    r'sr:tournament:(\d+)',
                    str(tournament_value)
                )

                if tournament_match:

                    tournament_id = (
                        tournament_match.group(1)
                    )

                events = tournament.get(
                    "events",
                    []
                )

                if not isinstance(
                    events,
                    list
                ):
                    continue

                for event in events:

                    if not isinstance(
                        event,
                        dict
                    ):
                        continue

                    event_id = event.get(
                        "eventId",
                        ""
                    )

                    match_id_match = re.search(
                        r'sr:match:(\d+)',
                        str(event_id)
                    )

                    if not match_id_match:
                        continue

                    match_id = (
                        match_id_match.group(1)
                    )

                    event_tournament = (
                        event.get(
                            "tournament"
                        )
                    )

                    if isinstance(
                        event_tournament,
                        dict
                    ):

                        event_tournament_id = (
                            event_tournament.get(
                                "id",
                                ""
                            )
                        )

                        event_tournament_match = re.search(
                            r'sr:tournament:(\d+)',
                            str(event_tournament_id)
                        )

                        if event_tournament_match:

                            tournament_id = (
                                event_tournament_match.group(1)
                            )

                    if not tournament_id:
                        continue

                    key = (
                        tournament_id,
                        match_id
                    )

                    found[key] = {
                        "match_id": match_id,
                        "tournament_id": tournament_id
                    }

            result = list(
                found.values()
            )

            logger.info(
                f"factsCenter: discovered "
                f"{len(result)} match subscriptions"
            )

            if result:

                logger.debug(
                    f"factsCenter subscription sample: "
                    f"{result[:5]}"
                )

            return result

        except Exception as e:

            logger.error(
                f"Failed to extract match subscriptions: "
                f"{e}"
            )

            return []

    # =============================================================
    # GET CURRENT LIVE MATCHES
    # =============================================================

    async def _get_live_matches_for_subscription(
        self
    ):

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

        if not self.sio:
            return False

        if not self.sio.connected:
            return False

        subscription_key = (
            f"{tournament_id}:{match_id}"
        )

        if subscription_key in self.subscribed_matches:
            return True

        request_id = self.request_id

        self.request_id += 1

        # =========================================================
        # IMPORTANT:
        #
        # This is the exact topic structure from the browser
        # traffic you captured.
        # =========================================================

        topic = (
            "\\1\\1\\1\\\\52"
            f"^sr:tournament:{tournament_id}"
            f"^sr:match:{match_id}"
            "*status"
        )

        inner = {
            "topic": topic,
            "subType": "SUB",
            "pushType": "GROUP",
            "requestId": str(request_id),
            "productCode": 2,
        }

        payload = {
            "type": "sub",
            "data": json.dumps(
                inner,
                separators=(",", ":"),
                ensure_ascii=False
            )
        }

        logger.info(
            f"[SUB] match={match_id} "
            f"tournament={tournament_id} "
            f"requestId={request_id}"
        )

        logger.info(
            f"[SUB TOPIC] {repr(topic)}"
        )

        logger.debug(
            f"[SUB PAYLOAD] "
            f"{json.dumps(payload, ensure_ascii=False)}"
        )

        try:

            await self.sio.emit(
                "data",
                payload
            )

            self.subscribed_matches.add(
                subscription_key
            )

            logger.success(
                f"[SUB SENT] "
                f"{tournament_id}:{match_id}"
            )

            return True

        except Exception as e:

            logger.error(
                f"[SUB FAILED] "
                f"{tournament_id}:{match_id}: {e}"
            )

            return False

    # =============================================================
    # SUBSCRIBE TO ALL CURRENT LIVE MATCHES
    # =============================================================

    async def _subscribe_all_live_matches(
        self
    ):

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

                response = (
                    await self._get_live_matches_for_subscription()
                )

                if not response:

                    logger.warning(
                        "factsCenter returned no data."
                    )

                    return

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

                sent = 0

                for item in matches:

                    if not self.running:
                        break

                    if not self.sio.connected:
                        break

                    result = await self._subscribe_match(
                        match_id=item["match_id"],
                        tournament_id=item["tournament_id"]
                    )

                    if result:
                        sent += 1

                    await asyncio.sleep(
                        0.05
                    )

                logger.success(
                    f"Socket.IO subscriptions sent: "
                    f"{sent}"
                )

            except Exception as e:

                logger.exception(
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

        try:

            logger.debug(
                f"[SOCKET RAW] "
                f"type={type(data).__name__}"
            )

            # -----------------------------------------------------
            # Actual packet observed:
            #
            # {
            #   "type": "ret",
            #   "data": "{\"body\":\"...\",\"topic\":\"...\"}"
            # }
            # -----------------------------------------------------

            if isinstance(
                data,
                dict
            ):

                packet_type = data.get(
                    "type"
                )

                inner = data.get(
                    "data"
                )

                logger.debug(
                    f"[SOCKET PACKET TYPE] "
                    f"{packet_type}"
                )

                if isinstance(
                    inner,
                    str
                ):

                    try:

                        inner = json.loads(
                            inner
                        )

                    except json.JSONDecodeError:

                        logger.debug(
                            "[SOCKET] "
                            "Nested data was not JSON."
                        )

                        return

                if isinstance(
                    inner,
                    dict
                ):

                    data = inner

            if not isinstance(
                data,
                dict
            ):

                return

            topic = str(
                data.get(
                    "topic",
                    ""
                )
            )

            body_str = data.get(
                "body"
            )

            if not body_str:

                logger.debug(
                    f"[SOCKET] No body. "
                    f"topic={topic}"
                )

                return

            logger.info(
                f"[SOCKET RET] topic={topic}"
            )

            # -----------------------------------------------------
            # DECODE BODY
            # -----------------------------------------------------

            decoded = self._decode_body(
                body_str
            )

            if decoded is None:

                if isinstance(
                    body_str,
                    str
                ):

                    try:

                        decoded = json.loads(
                            body_str
                        )

                    except Exception:

                        logger.debug(
                            f"[SOCKET] "
                            f"Unable to decode body: "
                            f"{body_str[:500]}"
                        )

                        return

                else:

                    return

            # -----------------------------------------------------
            # PRINT ACTUAL SOCKET DATA
            # -----------------------------------------------------

            logger.info(
                "=" * 80
            )

            logger.info(
                "[SOCKET LIVE DATA]"
            )

            logger.info(
                f"TOPIC: {topic}"
            )

            try:

                logger.info(
                    json.dumps(
                        decoded,
                        ensure_ascii=False,
                        indent=2
                    )[:15000]
                )

            except Exception:

                logger.info(
                    repr(decoded)[:15000]
                )

            logger.info(
                "=" * 80
            )

            if not isinstance(
                decoded,
                dict
            ):

                return

            # -----------------------------------------------------
            # DETECT MATCH DATA
            # -----------------------------------------------------

            is_match_payload = any(
                key in decoded
                for key in (
                    "eventScore",
                    "eventPlayedTime",
                    "eventMatchStatus",
                    "fixtureHomeTeamName",
                    "fixtureAwayTeamName",
                    "eventMatchPeriod"
                )
            )

            if not is_match_payload:

                return

            # -----------------------------------------------------
            # MATCH ID
            # -----------------------------------------------------

            match_id_match = re.search(
                r'sr:match:(\d+)',
                topic
            )

            if match_id_match:

                event_id = (
                    match_id_match.group(1)
                )

            else:

                event_id = (
                    str(
                        decoded.get(
                            "matchId",
                            decoded.get(
                                "eventId",
                                ""
                            )
                        )
                    )
                )

                event_id = self._extract_id(
                    event_id
                )

            # -----------------------------------------------------
            # TEAMS
            # -----------------------------------------------------

            home = decoded.get(
                "fixtureHomeTeamName",
                decoded.get(
                    "homeTeamName",
                    ""
                )
            )

            away = decoded.get(
                "fixtureAwayTeamName",
                decoded.get(
                    "awayTeamName",
                    ""
                )
            )

            # -----------------------------------------------------
            # SCORE
            # -----------------------------------------------------

            score = decoded.get(
                "eventScore",
                "0:0"
            )

            home_score = 0
            away_score = 0

            if isinstance(
                score,
                str
            ):

                score_match = re.search(
                    r'(\d+)\s*:\s*(\d+)',
                    score
                )

                if score_match:

                    home_score = int(
                        score_match.group(1)
                    )

                    away_score = int(
                        score_match.group(2)
                    )

            elif isinstance(
                score,
                dict
            ):

                home_score = int(
                    score.get(
                        "home",
                        score.get(
                            "homeScore",
                            0
                        )
                    )
                    or 0
                )

                away_score = int(
                    score.get(
                        "away",
                        score.get(
                            "awayScore",
                            0
                        )
                    )
                    or 0
                )

            # -----------------------------------------------------
            # PLAYED TIME
            # -----------------------------------------------------

            played = (
                self._parse_played_seconds(
                    decoded.get(
                        "eventPlayedTime",
                        0
                    )
                )
            )

            # -----------------------------------------------------
            # STATUS
            # -----------------------------------------------------

            status = decoded.get(
                "eventMatchStatus",
                decoded.get(
                    "eventMatchPeriod",
                    ""
                )
            )

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
                "topic": topic,
                "raw": decoded,
            }

            if event_id:

                self.matches[
                    event_id
                ] = match

            key = _match_key(
                home,
                away
            )

            logger.success(
                f"[LIVE MATCH] "
                f"{home} vs {away} "
                f"| key={key} "
                f"| score={home_score}:{away_score} "
                f"| played={played}s "
                f"| status={status} "
                f"| id={event_id}"
            )

            if self.callback:

                try:

                    await self.callback(
                        match
                    )

                except Exception as e:

                    logger.error(
                        f"Match callback failed: {e}"
                    )

        except Exception as e:

            logger.exception(
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

                    logger.error(
                        msg
                    )

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
                # GET BROWSER USER AGENT
                # =================================================

                headers = {}

                try:

                    user_agent = await self.page.evaluate(
                        "() => navigator.userAgent"
                    )

                    if user_agent:

                        headers[
                            "User-Agent"
                        ] = user_agent

                except Exception as e:

                    logger.debug(
                        f"Could not obtain browser "
                        f"user-agent: {e}"
                    )

                headers[
                    "Origin"
                ] = "https://www.sportybet.com"

                headers[
                    "Referer"
                ] = "https://www.sportybet.com/ng/"

                # =================================================
                # GET BROWSER COOKIES
                # =================================================

                try:

                    cookies = await self.context.cookies()

                    cookie_header = "; ".join(
                        f"{cookie['name']}={cookie['value']}"
                        for cookie in cookies
                        if cookie.get("name")
                    )

                    if cookie_header:

                        headers[
                            "Cookie"
                        ] = cookie_header

                        logger.info(
                            f"Using {len(cookies)} "
                            f"browser cookies for "
                            f"Socket.IO."
                        )

                    else:

                        logger.warning(
                            "No browser cookies found."
                        )

                except Exception as e:

                    logger.debug(
                        f"Could not read browser "
                        f"cookies: {e}"
                    )

                # =================================================
                # CREATE NEW SOCKET.IO CLIENT
                # =================================================

                self.sio = socketio.AsyncClient(
                    reconnection=False,
                    logger=False,
                    engineio_logger=False
                )

                self.subscribed_matches.clear()

                # =================================================
                # CONNECT EVENT
                # =================================================

                @self.sio.event
                async def connect():

                    self.connected = True

                    self.request_id = 1

                    self.subscribed_matches.clear()

                    logger.success(
                        "========================================"
                    )

                    logger.success(
                        "SPORTYBET SOCKET.IO CONNECTED"
                    )

                    logger.success(
                        "========================================"
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

                    # Give Socket.IO a short moment before SUB.
                    await asyncio.sleep(
                        0.5
                    )

                    try:

                        await self._subscribe_all_live_matches()

                    except Exception as e:

                        logger.exception(
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
                        "SportyBet Socket.IO "
                        "disconnected"
                    )

                # =================================================
                # CONNECT ERROR
                # =================================================

                @self.sio.event
                async def connect_error(
                    data
                ):

                    self.connected = False

                    logger.error(
                        f"Socket.IO connect_error: "
                        f"{data}"
                    )

                # =================================================
                # ACTUAL DATA EVENT
                # =================================================

                @self.sio.on("data")
                async def on_data(
                    data
                ):

                    logger.info(
                        f"[Socket.IO DATA] "
                        f"type={type(data).__name__}"
                    )

                    try:

                        await self._handle_event(
                            data
                        )

                    except Exception as e:

                        logger.exception(
                            f"data event processing "
                            f"failed: {e}"
                        )

                # =================================================
                # CONNECT
                # =================================================

                logger.info(
                    "Connecting to SportyBet "
                    "Socket.IO..."
                )

                await self.sio.connect(
                    url,
                    transports=[
                        "websocket"
                    ],
                    headers=headers,
                    wait_timeout=20
                )

                logger.success(
                    "Socket.IO connection established."
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

                logger.exception(
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

                if self.sio:

                    try:

                        if self.sio.connected:

                            await self.sio.disconnect()

                    except Exception as e:

                        logger.debug(
                            f"Socket.IO cleanup error: {e}"
                        )

                self.sio = None

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
