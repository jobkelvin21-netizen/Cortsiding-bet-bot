import asyncio
import re
import unicodedata
from datetime import datetime
from typing import Callable, Optional

from loguru import logger
from curl_cffi import requests


# =============================================================================
# HELPERS
# =============================================================================

def _strip_accents(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _normalize_team_name(name: str) -> str:
    if not name:
        return ""
    name = _strip_accents(name).lower().strip()
    words = re.findall(r"[a-z0-9]+", name)
    filler = {
        "fc", "cf", "sc", "afc", "cd", "ac", "fk", "nk", "sk", "as", "ss",
        "rc", "ca", "cs", "us", "sd", "ec",
        "united", "city", "town", "rovers", "athletic", "sporting", "club",
    }
    words = [w for w in words if w not in filler]
    if len(words) > 1 and len(words[-1]) <= 3:
        words = words[:-1]
    return "".join(words)


def _match_key(home: str, away: str) -> str:
    h = _normalize_team_name(home)
    a = _normalize_team_name(away)
    if h > a:
        h, a = a, h
    return f"{h}_vs_{a}"


# =============================================================================
# SPORTYBET FEED — REST POLLING ONLY
#
# We don't need push-based live scores from SportyBet's own socket for
# anything anymore: Polymarket drives goal detection, and the executor's
# safety check reads the score directly off the live page DOM right before
# betting. All this feed needs to provide is: which matches are live right
# now, with their team names and IDs, so MatchLinker can link a Polymarket
# goal to the correct SportyBet match/page.
# =============================================================================

class SportyBetFeed:

    FACTS_CENTER_URL = (
        "https://www.sportybet.com/api/ng/"
        "factsCenter/liveOrPrematchEvents?sportId=sr:sport:1"
    )

    def __init__(self, poll_interval: float = 5.0):
        self.matches = {}
        self.running = False
        self.callback = None
        self.alerter = None
        self.poll_interval = poll_interval

        # Kept for compatibility with main.py's set_page/set_credentials calls
        # — no longer used for login/browser purposes, since REST polling
        # doesn't require a browser session at all.
        self.page = None
        self.phone = None
        self.password = None

    def set_page(self, page):
        # No-op now — kept so main.py doesn't need changing.
        self.page = page

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_credentials(self, phone: str, password: str):
        # No-op now — REST endpoint needs no login.
        self.phone = phone
        self.password = password

    def _extract_id(self, event_id):
        match = re.search(r"sr:match:(\d+)", str(event_id or ""))
        if match:
            return match.group(1)
        match = re.search(r"(\d+)", str(event_id or ""))
        if match:
            return match.group(1)
        return ""

    def _parse_matches(self, obj) -> dict:
        """Parse the factsCenter response into our standard match dict shape."""
        parsed = {}

        if not isinstance(obj, dict):
            logger.warning("factsCenter response is not a dictionary.")
            return parsed

        tournaments = obj.get("data", [])
        if not isinstance(tournaments, list):
            logger.warning("factsCenter 'data' is not a list.")
            return parsed

        for tournament in tournaments:
            if not isinstance(tournament, dict):
                continue

            tournament_name = tournament.get("name", "")
            events = tournament.get("events", [])
            if not isinstance(events, list):
                continue

            for event in events:
                if not isinstance(event, dict):
                    continue

                event_id_raw = event.get("eventId", "")
                match_id = self._extract_id(event_id_raw)
                if not match_id:
                    continue

                home = event.get("homeTeamName", "")
                away = event.get("awayTeamName", "")
                if not home or not away:
                    continue

                # Live score, if present (setScore is typically "H:A")
                home_score, away_score = 0, 0
                set_score = str(event.get("setScore", "") or "")
                sm = re.match(r"^(\d+):(\d+)$", set_score)
                if sm:
                    home_score, away_score = int(sm.group(1)), int(sm.group(2))

                played_seconds = 0.0
                played_raw = event.get("playedSeconds")
                if played_raw is not None:
                    try:
                        played_seconds = float(played_raw)
                    except (TypeError, ValueError):
                        pass

                status = event.get("matchStatus", "")

                match = {
                    "match_id": match_id,
                    "sportradar_id": match_id,
                    "home_team": home,
                    "away_team": away,
                    "home_score": home_score,
                    "away_score": away_score,
                    "period": status,
                    "match_status": status,
                    "played_seconds": played_seconds,
                    "tournament_name": tournament_name,
                    "timestamp": datetime.now(),
                }

                parsed[match_id] = match

        return parsed

    async def _fetch_once(self) -> dict:
        try:
            response = await asyncio.to_thread(
                requests.get, self.FACTS_CENTER_URL, impersonate="chrome", timeout=15
            )

            if response.status_code != 200:
                logger.warning(f"factsCenter returned HTTP {response.status_code}")
                return {}

            try:
                data = response.json()
            except Exception as e:
                logger.warning(f"factsCenter JSON decode failed: {e}")
                return {}

            return self._parse_matches(data)

        except Exception as e:
            logger.error(f"factsCenter request failed: {e}")
            return {}

    async def start(self, callback: Callable):
        self.callback = callback
        self.running = True

        logger.info(f"Starting SportyBet REST polling feed (every {self.poll_interval}s)...")

        consecutive_failures = 0

        while self.running:
            try:
                new_matches = await self._fetch_once()

                if not new_matches:
                    consecutive_failures += 1
                    if consecutive_failures == 3:
                        logger.warning("SportyBet factsCenter returned empty 3 times in a row.")
                        if self.alerter:
                            try:
                                await self.alerter.send(
                                    "⚠️ SportyBet factsCenter has returned no data "
                                    "for 3 consecutive polls."
                                )
                            except Exception:
                                pass
                else:
                    consecutive_failures = 0

                    if not self.matches:
                        logger.success(f"SportyBet: {len(new_matches)} live matches loaded.")
                        if self.alerter:
                            try:
                                await self.alerter.send(
                                    f"✅ <b>SportyBet feed active</b> — {len(new_matches)} live matches loaded."
                                )
                            except Exception:
                                pass

                    self.matches = new_matches

                    if self.callback:
                        for match in new_matches.values():
                            try:
                                await self.callback(match)
                            except Exception as e:
                                logger.debug(f"SportyBet callback error: {e}")

                await asyncio.sleep(self.poll_interval)

            except Exception as e:
                logger.error(f"SportyBet polling loop error: {e}")
                await asyncio.sleep(self.poll_interval)

        logger.info("SportyBet REST polling feed stopped.")

    def stop(self):
        self.running = False
        logger.info("Stopping SportyBet REST polling feed...")
