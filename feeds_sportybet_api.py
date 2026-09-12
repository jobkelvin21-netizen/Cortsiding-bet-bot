import asyncio
import re
from datetime import datetime
from typing import Callable

from curl_cffi import requests
from loguru import logger


class SportyBetFeed:
    """
    SportyBet live-match REST feed.

    This feed is ONLY responsible for discovering live SportyBet matches.

    It does not:
    - place bets
    - open browser pages
    - perform goal detection
    - depend on SportyBet browser login
    """

    FACTS_CENTER_URL = (
        "https://www.sportybet.com/api/ng/"
        "factsCenter/liveOrPrematchEvents?sportId=sr:sport:1"
    )

    def __init__(self, poll_interval: float = 2.0):
        self.matches = {}

        self.running = False
        self.callback = None
        self.alerter = None

        self.poll_interval = poll_interval

        # Compatibility with existing main.py.
        self.page = None
        self.phone = None
        self.password = None

        self.last_success_at = None
        self.last_error_at = None

        # Set after the first successful HTTP response, even if the response
        # contains zero live matches.
        self.ready_event = asyncio.Event()

    # =========================================================================
    # COMPATIBILITY
    # =========================================================================

    def set_page(self, page):
        self.page = page

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_credentials(self, phone: str, password: str):
        self.phone = phone
        self.password = password

    # =========================================================================
    # ID
    # =========================================================================

    @staticmethod
    def _extract_id(event_id):
        text = str(event_id or "")

        match = re.search(
            r"sr:match:(\d+)",
            text
        )

        if match:
            return match.group(1)

        match = re.search(
            r"(\d+)",
            text
        )

        if match:
            return match.group(1)

        return ""

    # =========================================================================
    # SCORE
    # =========================================================================

    @staticmethod
    def _parse_score(value):
        """
        SportyBet normally provides football score as:

            0:0
            1:0
            2:1

        We also tolerate '-' and whitespace.
        """

        if value is None:
            return 0, 0

        text = str(value).strip()

        if not text:
            return 0, 0

        # Most common SportyBet format.
        match = re.match(
            r"^\s*(\d+)\s*[:\-]\s*(\d+)\s*$",
            text
        )

        if match:
            return (
                int(match.group(1)),
                int(match.group(2)),
            )

        return 0, 0

    # =========================================================================
    # PARSER
    # =========================================================================

    def _parse_matches(self, obj) -> dict:
        parsed = {}

        if not isinstance(obj, dict):
            logger.warning(
                "SportyBet factsCenter response is not a dictionary."
            )
            return parsed

        tournaments = obj.get("data", [])

        if not isinstance(tournaments, list):
            logger.warning(
                "SportyBet factsCenter 'data' is not a list."
            )
            return parsed

        for tournament in tournaments:

            if not isinstance(tournament, dict):
                continue

            tournament_name = str(
                tournament.get("name", "")
            ).strip()

            events = tournament.get(
                "events",
                []
            )

            if not isinstance(events, list):
                continue

            for event in events:

                if not isinstance(event, dict):
                    continue

                event_id_raw = (
                    event.get("eventId")
                    or event.get("id")
                    or ""
                )

                match_id = self._extract_id(
                    event_id_raw
                )

                if not match_id:
                    continue

                home = str(
                    event.get("homeTeamName", "")
                    or event.get("homeTeam", "")
                ).strip()

                away = str(
                    event.get("awayTeamName", "")
                    or event.get("awayTeam", "")
                ).strip()

                if not home or not away:
                    continue

                # -------------------------------------------------------------
                # Score
                # -------------------------------------------------------------

                score_value = (
                    event.get("setScore")
                    or event.get("gameScore")
                    or ""
                )

                home_score, away_score = (
                    self._parse_score(score_value)
                )

                # -------------------------------------------------------------
                # Played seconds
                # -------------------------------------------------------------

                played_seconds = 0.0

                played_raw = event.get(
                    "playedSeconds"
                )

                if played_raw is not None:

                    try:
                        if isinstance(
                            played_raw,
                            str
                        ) and ":" in played_raw:

                            pieces = played_raw.split(":")

                            if len(pieces) == 2:
                                minutes = float(
                                    pieces[0]
                                )
                                seconds = float(
                                    pieces[1]
                                )

                                played_seconds = (
                                    minutes * 60
                                    + seconds
                                )
                            else:
                                played_seconds = float(
                                    played_raw
                                )

                        else:
                            played_seconds = float(
                                played_raw
                            )

                    except (
                        TypeError,
                        ValueError,
                    ):
                        played_seconds = 0.0

                # -------------------------------------------------------------
                # Status
                # -------------------------------------------------------------

                match_status = (
                    event.get("matchStatus")
                    or event.get("period")
                    or event.get("status")
                    or ""
                )

                match_status = str(
                    match_status
                ).strip()

                # -------------------------------------------------------------
                # Keep complete useful structure.
                # -------------------------------------------------------------

                match = {
                    "source": "sportybet",

                    "match_id": match_id,
                    "sportradar_id": match_id,

                    "event_id": event_id_raw,

                    "home_team": home,
                    "away_team": away,

                    "home_score": home_score,
                    "away_score": away_score,

                    "period": match_status,
                    "match_status": match_status,

                    "played_seconds": played_seconds,

                    "tournament_name": tournament_name,

                    "timestamp": datetime.now(),
                }

                parsed[match_id] = match

        return parsed

    # =========================================================================
    # HTTP
    # =========================================================================

    async def _fetch_once(self) -> dict:

        try:

            response = await asyncio.to_thread(
                requests.get,
                self.FACTS_CENTER_URL,
                impersonate="chrome",
                timeout=15,
            )

            if response.status_code != 200:

                logger.warning(
                    f"SportyBet factsCenter returned "
                    f"HTTP {response.status_code}"
                )

                self.last_error_at = datetime.now()

                return {}

            try:
                data = response.json()

            except Exception as e:

                logger.warning(
                    f"SportyBet factsCenter JSON decode failed: {e}"
                )

                self.last_error_at = datetime.now()

                return {}

            parsed = self._parse_matches(data)

            self.last_success_at = datetime.now()

            return parsed

        except Exception as e:

            self.last_error_at = datetime.now()

            logger.error(
                f"SportyBet factsCenter request failed: {e}"
            )

            return {}

    # =========================================================================
    # START
    # =========================================================================

    async def start(self, callback: Callable):

        if self.running:
            return

        self.callback = callback
        self.running = True

        logger.info(
            "Starting SportyBet REST polling feed "
            f"(every {self.poll_interval}s)..."
        )

        consecutive_failures = 0
        first_success = True

        while self.running:

            try:

                new_matches = await self._fetch_once()

                # A successful HTTP/JSON response is considered readiness,
                # even if there are currently no matches.
                if self.last_success_at is not None:

                    if not self.ready_event.is_set():
                        self.ready_event.set()

                if not new_matches:

                    consecutive_failures += 1

                    if consecutive_failures == 3:

                        logger.warning(
                            "SportyBet factsCenter returned no matches "
                            "for 3 consecutive polls."
                        )

                        if self.alerter:

                            try:
                                await self.alerter.send(
                                    "⚠️ SportyBet factsCenter has returned "
                                    "no live match data for 3 consecutive polls."
                                )

                            except Exception:
                                pass

                    # IMPORTANT:
                    #
                    # If the API successfully returns an empty list, replace
                    # the old dictionary with {}. This removes stale matches.
                    #
                    # If the request itself failed, _fetch_once also returns
                    # {}, which means we could temporarily clear matches.
                    # To avoid destroying a valid snapshot during transient
                    # HTTP failures, only replace here when we know the
                    # response was successful.
                    #
                    if self.last_success_at is not None:
                        self.matches = {}

                else:

                    consecutive_failures = 0

                    old_count = len(
                        self.matches
                    )

                    self.matches = new_matches

                    if first_success:

                        logger.success(
                            f"SportyBet: "
                            f"{len(new_matches)} live matches loaded."
                        )

                        first_success = False

                        if self.alerter:

                            try:
                                await self.alerter.send(
                                    "✅ <b>SportyBet feed active</b> — "
                                    f"{len(new_matches)} live matches loaded."
                                )

                            except Exception:
                                pass

                    elif old_count == 0:

                        logger.info(
                            f"SportyBet: "
                            f"{len(new_matches)} live matches available."
                        )

                    # ---------------------------------------------------------
                    # IMPORTANT:
                    #
                    # Notify MatchLinker immediately after replacing the
                    # snapshot. Do NOT wait for the next 2-second reconciliation
                    # cycle.
                    # ---------------------------------------------------------

                    if self.callback:

                        try:
                            await self.callback(
                                {
                                    "type": "snapshot",
                                    "count": len(new_matches),
                                    "matches": new_matches,
                                }
                            )

                        except Exception as e:

                            logger.debug(
                                f"SportyBet callback error: {e}"
                            )

                await asyncio.sleep(
                    self.poll_interval
                )

            except asyncio.CancelledError:
                raise

            except Exception as e:

                logger.error(
                    f"SportyBet polling loop error: {e}"
                )

                await asyncio.sleep(
                    self.poll_interval
                )

        logger.info(
            "SportyBet REST polling feed stopped."
        )

    # =========================================================================
    # STOP
    # =========================================================================

    def stop(self):

        self.running = False

        logger.info(
            "Stopping SportyBet REST polling feed..."
        )

    # =========================================================================
    # READY
    # =========================================================================

    async def wait_until_ready(self, timeout: float = 15.0):

        try:

            await asyncio.wait_for(
                self.ready_event.wait(),
                timeout=timeout,
            )

            return True

        except asyncio.TimeoutError:

            return False
