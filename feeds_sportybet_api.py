import asyncio
import re
from datetime import datetime
from typing import Callable

from curl_cffi import requests
from loguru import logger


class SportyBetFeed:
    FACTS_CENTER_URL = (
        "https://www.sportybet.com/api/ng/"
        "factsCenter/liveOrPrematchEvents?sportId=sr:sport:1"
    )

    def __init__(self, poll_interval: float = 1.5):
        self.matches = {}
        self.running = False
        self.callback = None
        self.alerter = None
        self.poll_interval = poll_interval

        self.page = None
        self.phone = None
        self.password = None

        self.last_success_at = None
        self.last_error_at = None

        self.ready_event = asyncio.Event()
        self._refresh_lock = asyncio.Lock()

    def set_page(self, page):
        self.page = page

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_credentials(self, phone: str, password: str):
        self.phone = phone
        self.password = password

    @staticmethod
    def _extract_id(event_id):
        text = str(event_id or "")
        match = re.search(r"sr:match:(\d+)", text)
        if match:
            return match.group(1)
        match = re.search(r"(\d+)", text)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _full_event_id(event_id_raw, match_id: str) -> str:
        text = str(event_id_raw or "").strip()
        if text.startswith("sr:match:"):
            return text
        if match_id:
            return f"sr:match:{match_id}"
        return text

    @staticmethod
    def _parse_score(value):
        if value is None:
            return 0, 0
        text = str(value).strip()
        if not text:
            return 0, 0
        match = re.match(r"^\s*(\d+)\s*[:\-]\s*(\d+)\s*$", text)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 0, 0

    @staticmethod
    def _slug(text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r"[^a-zA-Z0-9]+", "_", text)
        return text.strip("_") or "Unknown"

    def _parse_matches(self, obj) -> dict:
        parsed = {}

        if not isinstance(obj, dict):
            logger.warning("SportyBet factsCenter response is not a dictionary.")
            return parsed

        tournaments = obj.get("data", [])
        if not isinstance(tournaments, list):
            logger.warning("SportyBet factsCenter 'data' is not a list.")
            return parsed

        for tournament in tournaments:
            if not isinstance(tournament, dict):
                continue

            tournament_name = str(
                tournament.get("name")
                or tournament.get("tournamentName")
                or ""
            ).strip()

            # Country / category for URL path segment
            country = str(
                tournament.get("categoryName")
                or tournament.get("category")
                or tournament.get("country")
                or tournament.get("regionName")
                or ""
            ).strip()
            if not country and tournament_name:
                # fallback: first word of tournament (often wrong — prefer API fields)
                country = "World"

            events = tournament.get("events", [])
            if not isinstance(events, list):
                continue

            for event in events:
                if not isinstance(event, dict):
                    continue

                event_id_raw = event.get("eventId") or event.get("id") or ""
                match_id = self._extract_id(event_id_raw)
                if not match_id:
                    continue

                home = str(
                    event.get("homeTeamName") or event.get("homeTeam") or ""
                ).strip()
                away = str(
                    event.get("awayTeamName") or event.get("awayTeam") or ""
                ).strip()
                if not home or not away:
                    continue

                # Event-level country override if present
                ev_country = str(
                    event.get("categoryName")
                    or event.get("category")
                    or event.get("country")
                    or ""
                ).strip()
                use_country = ev_country or country or "World"

                score_value = event.get("setScore") or event.get("gameScore") or ""
                home_score, away_score = self._parse_score(score_value)

                played_seconds = 0.0
                played_raw = event.get("playedSeconds")
                if played_raw is not None:
                    try:
                        if isinstance(played_raw, str) and ":" in played_raw:
                            pieces = played_raw.split(":")
                            if len(pieces) == 2:
                                played_seconds = float(pieces[0]) * 60 + float(pieces[1])
                            else:
                                played_seconds = float(played_raw)
                        else:
                            played_seconds = float(played_raw)
                    except (TypeError, ValueError):
                        played_seconds = 0.0

                match_status = str(
                    event.get("matchStatus")
                    or event.get("period")
                    or event.get("status")
                    or ""
                ).strip()

                full_eid = self._full_event_id(event_id_raw, match_id)
                league_slug = self._slug(tournament_name or "League")
                country_slug = self._slug(use_country)
                teams_slug = f"{self._slug(home)}_vs_{self._slug(away)}"

                # Canonical desktop live URL (matches your working browser URL)
                live_path = f"{country_slug}/{league_slug}/{teams_slug}/{full_eid}"
                live_url = (
                    f"https://www.sportybet.com/ng/sport/football/live/{live_path}"
                )

                match = {
                    "source": "sportybet",
                    "match_id": match_id,
                    "sportradar_id": match_id,
                    "event_id": full_eid,
                    "home_team": home,
                    "away_team": away,
                    "home_score": home_score,
                    "away_score": away_score,
                    "period": match_status,
                    "match_status": match_status,
                    "played_seconds": played_seconds,
                    "tournament_name": tournament_name,
                    "league": tournament_name,
                    "country": use_country,
                    "live_url": live_url,
                    "timestamp": datetime.now(),
                }

                parsed[match_id] = match

        return parsed

    async def _fetch_once(self) -> dict:
        try:
            response = await asyncio.to_thread(
                requests.get, self.FACTS_CENTER_URL, impersonate="chrome", timeout=10
            )

            if response.status_code != 200:
                logger.warning(
                    f"SportyBet factsCenter returned HTTP {response.status_code}"
                )
                self.last_error_at = datetime.now()
                return {}

            try:
                data = response.json()
            except Exception as e:
                logger.warning(f"SportyBet factsCenter JSON decode failed: {e}")
                self.last_error_at = datetime.now()
                return {}

            parsed = self._parse_matches(data)
            self.last_success_at = datetime.now()
            return parsed

        except Exception as e:
            self.last_error_at = datetime.now()
            logger.error(f"SportyBet factsCenter request failed: {e}")
            return {}

    async def force_refresh(self) -> bool:
        if self._refresh_lock.locked():
            async with self._refresh_lock:
                return True

        async with self._refresh_lock:
            new_matches = await self._fetch_once()
            if new_matches:
                self.matches.clear()
                self.matches.update(new_matches)
                return True
            return False

    async def start(self, callback: Callable):
        if self.running:
            return

        self.callback = callback
        self.running = True

        logger.info(
            f"Starting SportyBet REST polling feed (every {self.poll_interval}s)..."
        )

        consecutive_failures = 0
        first_success = True

        while self.running:
            try:
                async with self._refresh_lock:
                    new_matches = await self._fetch_once()

                    if self.last_success_at is not None:
                        if not self.ready_event.is_set():
                            self.ready_event.set()
                        self.matches.clear()
                        self.matches.update(new_matches)

                if not new_matches:
                    consecutive_failures += 1
                    if consecutive_failures == 3:
                        logger.warning(
                            "SportyBet factsCenter returned no matches for 3 consecutive polls."
                        )
                        if self.alerter:
                            try:
                                await self.alerter.send(
                                    "⚠️ SportyBet factsCenter has returned no live match data "
                                    "for 3 consecutive polls."
                                )
                            except Exception:
                                pass
                else:
                    consecutive_failures = 0
                    if first_success:
                        logger.success(
                            f"SportyBet: {len(new_matches)} live matches loaded."
                        )
                        first_success = False
                        if self.alerter:
                            try:
                                await self.alerter.send(
                                    f"✅ <b>SportyBet feed active</b> — "
                                    f"{len(new_matches)} live matches loaded."
                                )
                            except Exception:
                                pass

                if self.callback:
                    try:
                        await self.callback(
                            {
                                "type": "snapshot",
                                "count": len(self.matches),
                                "matches": self.matches,
                            }
                        )
                    except Exception as e:
                        logger.debug(f"SportyBet callback error: {e}")

                await asyncio.sleep(self.poll_interval)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"SportyBet polling loop error: {e}")
                await asyncio.sleep(self.poll_interval)

        logger.info("SportyBet REST polling feed stopped.")

    def stop(self):
        self.running = False
        logger.info("Stopping SportyBet REST polling feed...")

    async def wait_until_ready(self, timeout: float = 15.0):
        try:
            await asyncio.wait_for(self.ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
