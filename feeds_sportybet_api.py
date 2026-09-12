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


# =============================================================================
# HELPERS
# =============================================================================

def _strip_accents(text: str) -> str:

    if not text:
        return ""

    nfkd = unicodedata.normalize(
        "NFKD",
        text
    )

    return "".join(
        c
        for c in nfkd
        if not unicodedata.combining(c)
    )


def _normalize_team_name(name: str) -> str:

    if not name:
        return ""

    name = _strip_accents(
        name
    ).lower().strip()

    words = re.findall(
        r"[a-z0-9]+",
        name
    )

    filler = {
        "fc",
        "cf",
        "sc",
        "afc",
        "cd",
        "ac",
        "fk",
        "nk",
        "sk",
        "as",
        "ss",
        "rc",
        "ca",
        "cs",
        "us",
        "sd",
        "ec",
        "united",
        "city",
        "town",
        "rovers",
        "athletic",
        "sporting",
        "club",
    }

    words = [
        w
        for w in words
        if w not in filler
    ]

    if (
        len(words) > 1
        and len(words[-1]) <= 3
    ):
        words = words[:-1]

    return "".join(words)


def _match_key(
    home: str,
    away: str
) -> str:

    h = _normalize_team_name(
        home
    )

    a = _normalize_team_name(
        away
    )

    if h > a:
        h, a = a, h

    return f"{h}_vs_{a}"


# =============================================================================
# SPORTYBET FEED
# =============================================================================

class SportyBetFeed:

    def __init__(self):

        # =====================================================================
        # EXISTING STATE
        # =====================================================================

        self.matches = {}

        self.running = False

        self.callback = None

        self.page: Optional[Page] = None

        self.connected = False

        self.alerter = None

        self.sio: Optional[
            socketio.AsyncClient
        ] = None

        # =====================================================================
        # BROWSER / LOGIN STATE
        # =====================================================================

        self.playwright = None

        self.browser = None

        self.context = None

        self.phone = None

        self.password = None

        # =====================================================================
        # SOCKET SUBSCRIPTION STATE
        # =====================================================================

        self.request_id = 1

        self.subscribed_matches = set()

        self.subscription_lock = (
            asyncio.Lock()
        )

        self.subscription_task = None

    # =========================================================================
    # SETTERS
    # =========================================================================

    def set_page(
        self,
        page: Page
    ):

        self.page = page

        logger.info(
            "SportyBetFeed received Playwright page"
        )

    def set_alerter(
        self,
        alerter
    ):

        self.alerter = alerter

    def set_credentials(
        self,
        phone: str,
        password: str
    ):

        self.phone = phone

        self.password = password

    # =========================================================================
    # TIME PARSER
    # =========================================================================

    def _parse_played_seconds(
        self,
        value
    ):

        try:

            if isinstance(
                value,
                (int, float)
            ):

                return float(value)

            value = str(value)

            parts = [
                int(p)
                for p in value.split(":")
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

    # =========================================================================
    # EVENT ID
    # =========================================================================

    def _extract_id(
        self,
        event_id
    ):

        match = re.search(
            r"sr:match:(\d+)",
            str(event_id or "")
        )

        if match:
            return match.group(1)

        match = re.search(
            r"(\d+)",
            str(event_id or "")
        )

        if match:
            return match.group(1)

        return ""

    # =========================================================================
    # LOGIN
    # =========================================================================

    async def _login(self):

        if not self.page:
            return

        try:

            await self.page.goto(
                "https://www.sportybet.com/ng/",
                wait_until="domcontentloaded"
            )

            await asyncio.sleep(2)

            login_button = (
                await self.page.query_selector(
                    'button:has-text("Log In"), '
                    'a:has-text("Log In")'
                )
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

                await asyncio.sleep(
                    0.5
                )

                await self.page.wait_for_selector(
                    'input[name="psd"]',
                    state="visible",
                    timeout=10000
                )

                await self.page.fill(
                    'input[name="psd"]',
                    self.password
                )

                await asyncio.sleep(
                    0.5
                )

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

    # =========================================================================
    # ENSURE PAGE
    # =========================================================================

    async def _ensure_page_alive(
        self
    ):

        if (
            self.page
            and not self.page.is_closed()
        ):

            try:

                await self.page.evaluate(
                    "1"
                )

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

            self.browser = (
                await self.playwright.chromium.launch(
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled"
                    ]
                )
            )

            self.context = (
                await self.browser.new_context(
                    viewport={
                        "width": 412,
                        "height": 915
                    }
                )
            )

            self.page = (
                await self.context.new_page()
            )

            if (
                self.phone
                and self.password
            ):

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

    # =========================================================================
    # DISCOVER SOCKET.IO URL
    # =========================================================================

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
                        .split("?", 1)[0]
                    )

                    base = (
                        base
                        .replace(
                            "/socket.io/",
                            ""
                        )
                        .replace(
                            "/socket.io",
                            ""
                        )
                    )

                    if base.startswith(
                        "wss://"
                    ):

                        base = (
                            "https://"
                            + base[6:]
                        )

                    elif base.startswith(
                        "ws://"
                    ):

                        base = (
                            "http://"
                            + base[5:]
                        )

                    if base not in found:

                        found.append(
                            base
                        )

                        logger.info(
                            "[WS DISCOVERY] "
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
                "SportyBet Socket.IO URL discovered: "
                f"{url}"
            )

            return url

        logger.warning(
            "No SportyBet Socket.IO URL discovered."
        )

        return None

    # =========================================================================
    # BASE64 DECODER
    # =========================================================================

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

                decoded_bytes = body_str

            else:

                body_str = str(
                    body_str
                ).strip()

                # Remove accidental whitespace/newlines.
                body_str = re.sub(
                    r"\s+",
                    "",
                    body_str
                )

                padded = (
                    body_str
                    + "="
                    * (
                        -len(body_str)
                        % 4
                    )
                )

                decoded_bytes = (
                    base64.b64decode(
                        padded,
                        validate=False
                    )
                )

            text = (
                decoded_bytes
                .decode(
                    "utf-8",
                    errors="replace"
                )
            )

            logger.debug(
                "[BASE64] decoded body: "
                f"{text[:5000]}"
            )

            try:

                return json.loads(
                    text
                )

            except json.JSONDecodeError:

                logger.debug(
                    "[BASE64] decoded body "
                    "is not JSON"
                )

                return text

        except Exception as e:

            logger.debug(
                f"Body decode failed: {e}"
            )

            return None

    # =========================================================================
    # EXTRACT SUBSCRIPTION MATCHES
    # =========================================================================

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
                    "factsCenter response is not "
                    "a dictionary."
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
                    r"sr:tournament:(\d+)",
                    str(
                        tournament_value
                    )
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
                        r"sr:match:(\d+)",
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

                        event_tournament_match = (
                            re.search(
                                r"sr:tournament:(\d+)",
                                str(
                                    event_tournament_id
                                )
                            )
                        )

                        if event_tournament_match:

                            tournament_id = (
                                event_tournament_match.group(
                                    1
                                )
                            )

                    if not tournament_id:
                        continue

                    key = (
                        tournament_id,
                        match_id
                    )

                    found[key] = {
                        "match_id": match_id,
                        "tournament_id": tournament_id,
                    }

            result = list(
                found.values()
            )

            logger.info(
                "factsCenter: discovered "
                f"{len(result)} match subscriptions"
            )

            if result:

                logger.debug(
                    "factsCenter subscription sample: "
                    f"{result[:5]}"
                )

            return result

        except Exception as e:

            logger.error(
                "Failed to extract match "
                f"subscriptions: {e}"
            )

            return []

    # =========================================================================
    # FACTSCENTER — ID DISCOVERY ONLY
    # =========================================================================

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
                "factsCenter HTTP status: "
                f"{response.status_code}"
            )

            logger.info(
                "factsCenter response size: "
                f"{len(response.text)} bytes"
            )

            if response.status_code != 200:

                logger.warning(
                    "factsCenter returned HTTP "
                    f"{response.status_code}"
                )

                return []

            try:

                result = response.json()

            except Exception as e:

                logger.warning(
                    "factsCenter JSON decode failed: "
                    f"{e}"
                )

                return []

            logger.success(
                "factsCenter returned valid JSON "
                "through curl_cffi."
            )

            return result

        except Exception as e:

            logger.error(
                "factsCenter curl_cffi request failed: "
                f"{e}"
            )

            return []

    # =========================================================================
    # SUBSCRIBE TO ONE MATCH
    # =========================================================================

    async def _subscribe_match(
        self,
        match_id: str,
        tournament_id: str
    ):

        if not self.sio:
            return

        if not self.sio.connected:
            return

        subscription_key = (
            f"{tournament_id}:{match_id}"
        )

        if (
            subscription_key
            in self.subscribed_matches
        ):

            return

        request_id = self.request_id

        self.request_id += 1

        # ---------------------------------------------------------------------
        # IMPORTANT
        #
        # This matches the observed live Socket.IO topic:
        #
        # 1^30^sr:tournament:35^sr:match:72513190*status
        # ---------------------------------------------------------------------

        topic = (
            "1^30^"
            f"sr:tournament:{tournament_id}"
            f"^sr:match:{match_id}"
            "*status"
        )

        inner = {
            "topic": topic,
            "subType": "SUB",
            "pushType": "GROUP",
            "requestId": request_id,
            "productCode": 2,
        }

        payload = {
            "type": "sub",
            "data": json.dumps(
                inner,
                separators=(
                    ",",
                    ":"
                ),
                ensure_ascii=False
            ),
        }

        logger.info(
            "[SUB] "
            f"match={match_id} "
            f"tournament={tournament_id} "
            f"requestId={request_id}"
        )

        logger.info(
            f"[SUB TOPIC] {topic}"
        )

        logger.debug(
            f"[SUB PAYLOAD] {payload}"
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
                "[SUB SENT] "
                f"{tournament_id}:{match_id}"
            )

        except Exception as e:

            logger.error(
                "Subscription failed for "
                f"{match_id}: {e}"
            )

    # =========================================================================
    # SUBSCRIBE ALL
    # =========================================================================

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

                self.subscribed_matches.clear()

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
                    "live match subscription(s)."
                )

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

                    await asyncio.sleep(
                        0.05
                    )

                logger.success(
                    "Socket.IO subscriptions sent: "
                    f"{len(self.subscribed_matches)}"
                )

            except Exception as e:

                logger.exception(
                    "Error subscribing to live "
                    f"matches: {e}"
                )

    # =========================================================================
    # HANDLE SOCKET.IO DATA
    # =========================================================================

    async def _handle_event(
        self,
        data
    ):

        try:

            if data is None:
                return

            logger.debug(
                "[SOCKET.IO RAW] "
                f"type={type(data).__name__} "
                f"data={repr(data)[:5000]}"
            )

            # =================================================================
            # OUTER SOCKET.IO RET PACKET
            #
            # {
            #   "type": "ret",
            #   "data": "{\"body\":\"...\",\"topic\":\"...\"}"
            # }
            # =================================================================

            if isinstance(
                data,
                dict
            ):

                outer_type = data.get(
                    "type"
                )

                inner_data = data.get(
                    "data"
                )

                if (
                    outer_type == "ret"
                    and
                    isinstance(
                        inner_data,
                        str
                    )
                ):

                    try:

                        inner_data = json.loads(
                            inner_data
                        )

                    except json.JSONDecodeError:

                        logger.debug(
                            "[SOCKET.IO] "
                            "ret.data was not JSON"
                        )

                        return

                if isinstance(
                    inner_data,
                    dict
                ):

                    data = inner_data

            # =================================================================
            # AFTER UNWRAPPING WE EXPECT:
            #
            # {
            #     "body": "...",
            #     "topic": "...",
            #     "pushType": "GROUP"
            # }
            # =================================================================

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
                    "[SOCKET.IO] Packet has no body."
                )

                return

            logger.info(
                "[SOCKET.IO DATA] "
                f"topic={topic}"
            )

            # =================================================================
            # BASE64 DECODE
            # =================================================================

            decoded = self._decode_body(
                body_str
            )

            if decoded is None:

                logger.debug(
                    "[SOCKET.IO] "
                    "Could not decode body."
                )

                return

            # =================================================================
            # PRINT ACTUAL DECODED DATA
            # =================================================================

            if isinstance(
                decoded,
                (dict, list)
            ):

                try:

                    printable = json.dumps(
                        decoded,
                        indent=2,
                        ensure_ascii=False
                    )

                except Exception:

                    printable = repr(
                        decoded
                    )

            else:

                printable = str(
                    decoded
                )

            logger.info(
                "\n"
                + "=" * 80
                + "\n"
                + "🔥 LIVE SOCKET.IO DATA"
                + "\n"
                + "=" * 80
                + "\n"
                + f"{printable[:20000]}"
                + "\n"
                + "=" * 80
            )

            # =================================================================
            # ONLY DICTIONARY DATA CAN BE MATCH STATUS
            # =================================================================

            if not isinstance(
                decoded,
                dict
            ):

                return

            # =================================================================
            # EXTRACT MATCH ID
            # =================================================================

            match_match = re.search(
                r"sr:match:(\d+)",
                topic
            )

            if match_match:

                event_id = (
                    match_match.group(1)
                )

            else:

                event_id = self._extract_id(
                    topic
                )

            # =================================================================
            # TEAM NAMES
            # =================================================================

            home = str(
                decoded.get(
                    "fixtureHomeTeamName",
                    decoded.get(
                        "eventHomeTeamName",
                        ""
                    )
                )
                or ""
            )

            away = str(
                decoded.get(
                    "fixtureAwayTeamName",
                    decoded.get(
                        "eventAwayTeamName",
                        ""
                    )
                )
                or ""
            )

            # =================================================================
            # SCORE
            # =================================================================

            score_str = str(
                decoded.get(
                    "eventScore",
                    "0:0"
                )
                or "0:0"
            )

            home_score = 0

            away_score = 0

            try:

                parts = score_str.split(
                    ":"
                )

                if len(parts) >= 2:

                    home_score = int(
                        re.sub(
                            r"\D",
                            "",
                            parts[0]
                        )
                        or "0"
                    )

                    away_score = int(
                        re.sub(
                            r"\D",
                            "",
                            parts[1]
                        )
                        or "0"
                    )

            except Exception:
                pass

            # =================================================================
            # PLAYED TIME
            # =================================================================

            played = (
                self._parse_played_seconds(
                    decoded.get(
                        "eventPlayedTime",
                        0
                    )
                )
            )

            # =================================================================
            # STATUS
            # =================================================================

            status = decoded.get(
                "eventMatchStatus",
                decoded.get(
                    "eventStatus",
                    ""
                )
            )

            # =================================================================
            # ONLY CREATE MATCH WHEN IT LOOKS LIKE MATCH DATA
            # =================================================================

            is_match_data = (
                bool(home)
                or bool(away)
                or "eventScore" in decoded
                or "eventMatchStatus" in decoded
                or "eventPlayedTime" in decoded
                or "fixtureHomeTeamName" in decoded
                or "fixtureAwayTeamName" in decoded
            )

            if not is_match_data:

                logger.debug(
                    "[SOCKET.IO] Decoded event "
                    "does not contain recognised "
                    "match fields."
                )

                return

            # =================================================================
            # BUILD MATCH OBJECT
            # =================================================================

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

            self.matches[
                event_id
            ] = match

            key = _match_key(
                home,
                away
            )

            logger.success(
                "\n"
                "╔"
                + "═" * 78
                + "╗\n"
                "║ LIVE MATCH FROM SOCKET.IO\n"
                "╠"
                + "═" * 78
                + "╣\n"
                f"║ Match ID : {event_id}\n"
                f"║ Home     : {home}\n"
                f"║ Away     : {away}\n"
                f"║ Score    : {home_score}:{away_score}\n"
                f"║ Time     : {played}s\n"
                f"║ Status   : {status}\n"
                f"║ Key      : {key}\n"
                "╚"
                + "═" * 78
                + "╝"
            )

            # =================================================================
            # CALLBACK
            # =================================================================

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
                f"Socket.IO event handling error: {e}"
            )

    # =========================================================================
    # SOCKET.IO START
    # =========================================================================

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

                # =================================================================
                # ENSURE BROWSER
                # =================================================================

                page_ok = (
                    await self._ensure_page_alive()
                )

                if not page_ok:

                    await asyncio.sleep(
                        15
                    )

                    continue

                # =================================================================
                # DISCOVER SOCKET URL
                # =================================================================

                url = (
                    await self._discover_socket_url()
                )

                if not url:

                    msg = (
                        "Could not find "
                        "SportyBet Socket.IO URL"
                    )

                    logger.error(
                        msg
                    )

                    if self.alerter:

                        try:

                            await self.alerter.send(
                                f"❌ {msg}"
                            )

                        except Exception:
                            pass

                    await asyncio.sleep(
                        15
                    )

                    continue

                logger.info(
                    "Connecting to Socket.IO: "
                    f"{url}"
                )

                # =================================================================
                # NEW SOCKET.IO CLIENT
                # =================================================================

                self.sio = (
                    socketio.AsyncClient(
                        reconnection=True,
                        reconnection_attempts=0,
                        logger=False,
                        engineio_logger=False
                    )
                )

                self.subscribed_matches.clear()

                # =================================================================
                # CONNECT
                # =================================================================

                @self.sio.event
                async def connect():

                    self.connected = True

                    self.subscribed_matches.clear()

                    logger.success(
                        "SportyBet Socket.IO "
                        "connected successfully!"
                    )

                    if self.alerter:

                        try:

                            await self.alerter.send(
                                "✅ <b>SportyBet Socket.IO "
                                "connected successfully!</b>"
                            )

                        except Exception as e:

                            logger.debug(
                                f"Alerter error: {e}"
                            )

                    # -------------------------------------------------------------
                    # SUBSCRIBE AFTER CONNECT
                    # -------------------------------------------------------------

                    try:

                        await self._subscribe_all_live_matches()

                    except Exception as e:

                        logger.exception(
                            "Initial subscription "
                            f"failed: {e}"
                        )

                # =================================================================
                # DISCONNECT
                # =================================================================

                @self.sio.event
                async def disconnect():

                    self.connected = False

                    self.subscribed_matches.clear()

                    logger.warning(
                        "SportyBet Socket.IO disconnected"
                    )

                # =================================================================
                # CONNECT ERROR
                # =================================================================

                @self.sio.event
                async def connect_error(
                    data
                ):

                    logger.error(
                        "SportyBet Socket.IO "
                        f"connection error: {data}"
                    )

                # =================================================================
                # WILDCARD EVENT HANDLER
                #
                # IMPORTANT:
                # Socket.IO can provide multiple arguments.
                # =================================================================

                @self.sio.on("*")
                async def catch_all(
                    event,
                    *args
                ):

                    try:

                        logger.debug(
                            "[Socket.IO EVENT] "
                            f"event={event} "
                            f"args={len(args)}"
                        )

                        for raw in args:

                            logger.debug(
                                "[Socket.IO RAW EVENT] "
                                f"{repr(raw)[:10000]}"
                            )

                            await self._handle_event(
                                raw
                            )

                    except Exception as e:

                        logger.exception(
                            "catch_all parse error: "
                            f"{e}"
                        )

                # =================================================================
                # CONNECT
                #
                # DO NOT USE wait_timeout HERE.
                # Your installed python-socketio does not support it.
                # =================================================================

                await self.sio.connect(
                    url,
                    transports=[
                        "websocket"
                    ]
                )

                # =================================================================
                # WAIT
                # =================================================================

                while (
                    self.running
                    and self.sio
                    and self.sio.connected
                ):

                    await asyncio.sleep(
                        1
                    )

            except Exception as e:

                self.connected = False

                logger.exception(
                    "SportyBet Socket.IO error: "
                    f"{e}"
                )

                if self.alerter:

                    try:

                        await self.alerter.send(
                            "⚠️ SportyBet Socket.IO "
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

                if (
                    self.sio
                    and self.sio.connected
                ):

                    try:

                        await self.sio.disconnect()

                    except Exception:
                        pass

                self.sio = None

        logger.info(
            "SportyBet Socket.IO feed stopped."
        )

    # =========================================================================
    # STOP
    # =========================================================================

    def stop(self):

        self.running = False

        self.connected = False

        logger.info(
            "Stopping SportyBet Socket.IO feed...")
