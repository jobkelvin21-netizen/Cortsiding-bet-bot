#!/usr/bin/env python3
"""
SportyBet hybrid feed:
  - REST factsCenter → match IDs, teams, live URLs, baseline score/clock
  - Page Socket.IO  → live score/clock pushes (when available)
Import: from feeds.sportybet_api import SportyBetFeed
"""

import asyncio
import base64
import json
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from curl_cffi import requests
from loguru import logger

TOPIC_RE = re.compile(r"sr:match:(\d+)", re.I)
SCORE_RE = re.compile(r"(\d{1,2})\s*[:\-]\s*(\d{1,2})")

# Avoid any backslash-tilde in source (SyntaxWarning fix)
STATUS_TAG = "\~" + "status"


def _b64_decode(s: str) -> str:
    if not s or not isinstance(s, str):
        return ""
    pad = "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s + pad).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _played_to_seconds(raw) -> float:
    if raw is None:
        return 0.0
    try:
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw).strip()
        if ":" in text:
            a, b = text.split(":", 1)
            return float(a) * 60 + float(b)
        return float(text)
    except Exception:
        return 0.0


def _slug(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "").strip())
    return text.strip("_") or "Unknown"


class SportyBetFeed:
    FACTS_CENTER_URL = (
        "https://www.sportybet.com/api/ng/"
        "factsCenter/liveOrPrematchEvents?sportId=sr:sport:1"
    )
    LIVE_LIST = "https://www.sportybet.com/ng/sport/football/live_list"

    def __init__(self, poll_interval: float = 1.5):
        self.matches: Dict[str, dict] = {}
        self.running = False
        self.callback: Optional[Callable] = None
        self.alerter = None
        self.poll_interval = float(poll_interval)

        self._page = None
        self._context = None
        self._ws_urls = set()

        self.last_message_at: Optional[float] = None
        self.last_rest_at: Optional[float] = None
        self.ready_event = asyncio.Event()
        self._refresh_lock = asyncio.Lock()

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_callback(self, callback: Callable):
        self.callback = callback

    def is_healthy(self) -> bool:
        """Healthy if REST or Socket updated recently."""
        now = time.time()
        rest_ok = self.last_rest_at is not None and (now - self.last_rest_at) < 20
        sock_ok = self.last_message_at is not None and (now - self.last_message_at) < 40
        return rest_ok or sock_ok

    def get_matches(self) -> List[dict]:
        return list(self.matches.values())

    def get_match(self, match_id: str) -> Optional[dict]:
        return self.matches.get(str(match_id))

    # ------------------------------------------------------------------
    # Shared upsert
    # ------------------------------------------------------------------
    def _upsert(self, match_id: str, **fields) -> dict:
        mid = str(match_id)
        rec = self.matches.get(mid) or {
            "source": "sportybet",
            "match_id": mid,
            "sportradar_id": mid,
            "event_id": f"sr:match:{mid}",
            "home_team": "",
            "away_team": "",
            "home_score": 0,
            "away_score": 0,
            "played_seconds": 0.0,
            "minute": 0.0,
            "period": "",
            "match_status": "",
            "tournament_name": "",
            "league": "",
            "country": "World",
            "live_url": "",
            "timestamp": datetime.now(),
        }
        for k, v in fields.items():
            if v is not None and v != "":
                rec[k] = v

        # Build live URL when we have teams + id
        if rec.get("home_team") and rec.get("away_team"):
            country = _slug(rec.get("country") or "World")
            league = _slug(rec.get("league") or rec.get("tournament_name") or "League")
            teams = f"{_slug(rec['home_team'])}_vs_{_slug(rec['away_team'])}"
            eid = rec.get("event_id") or f"sr:match:{mid}"
            rec["live_url"] = (
                f"https://www.sportybet.com/ng/sport/football/live/"
                f"{country}/{league}/{teams}/{eid}"
            )

        rec["timestamp"] = datetime.now()
        rec["updated"] = time.time()
        self.matches[mid] = rec
        if not self.ready_event.is_set():
            self.ready_event.set()
        return rec

    # ------------------------------------------------------------------
    # REST — match IDs + live links + baseline clock/score
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_id(event_id) -> str:
        text = str(event_id or "")
        m = re.search(r"sr:match:(\d+)", text)
        if m:
            return m.group(1)
        m = re.search(r"(\d+)", text)
        return m.group(1) if m else ""

    def _parse_rest(self, obj: dict) -> dict:
        parsed = {}
        if not isinstance(obj, dict):
            return parsed
        tournaments = obj.get("data", [])
        if not isinstance(tournaments, list):
            return parsed

        for tournament in tournaments:
            if not isinstance(tournament, dict):
                continue
            tournament_name = str(
                tournament.get("name") or tournament.get("tournamentName") or ""
            ).strip()
            country = str(
                tournament.get("categoryName")
                or tournament.get("category")
                or tournament.get("country")
                or "World"
            ).strip() or "World"

            events = tournament.get("events") or []
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

                score_value = event.get("setScore") or event.get("gameScore") or ""
                h_score, a_score = 0, 0
                sm = SCORE_RE.search(str(score_value))
                if sm:
                    h_score, a_score = int(sm.group(1)), int(sm.group(2))

                played = _played_to_seconds(event.get("playedSeconds"))
                status = str(
                    event.get("matchStatus") or event.get("period") or ""
                ).strip()
                full_eid = (
                    event_id_raw
                    if str(event_id_raw).startswith("sr:match:")
                    else f"sr:match:{match_id}"
                )

                parsed[match_id] = {
                    "match_id": match_id,
                    "event_id": full_eid,
                    "home_team": home,
                    "away_team": away,
                    "home_score": h_score,
                    "away_score": a_score,
                    "played_seconds": played,
                    "minute": played / 60.0 if played else 0.0,
                    "period": status,
                    "match_status": status,
                    "tournament_name": tournament_name,
                    "league": tournament_name,
                    "country": country,
                }
        return parsed

    async def _fetch_rest_once(self) -> dict:
        try:
            response = await asyncio.to_thread(
                requests.get,
                self.FACTS_CENTER_URL,
                impersonate="chrome",
                timeout=10,
            )
            if response.status_code != 200:
                logger.warning(f"[SPORTY][REST] HTTP {response.status_code}")
                return {}
            data = response.json()
            parsed = self._parse_rest(data)
            self.last_rest_at = time.time()
            return parsed
        except Exception as e:
            logger.error(f"[SPORTY][REST] {e}")
            return {}

    async def _rest_loop(self):
        first = True
        while self.running:
            try:
                async with self._refresh_lock:
                    new_matches = await self._fetch_rest_once()
                    if new_matches:
                        # Merge REST into store (do not wipe socket-only fields blindly)
                        for mid, fields in new_matches.items():
                            self._upsert(mid, **fields)
                        if first:
                            first = False
                            logger.success(
                                f"[SPORTY][REST] {len(new_matches)} live matches + IDs + URLs"
                            )
                            if self.alerter:
                                try:
                                    await self.alerter.send(
                                        f"✅ <b>SportyBet REST</b> — "
                                        f"{len(new_matches)} matches (IDs + live links)"
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
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[SPORTY][REST] loop: {e}")
            await asyncio.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # Socket — live pushes (score/clock) when page streams them
    # ------------------------------------------------------------------
    def _ingest_status(self, topic: str, payload: Any):
        m = TOPIC_RE.search(topic or "")
        if not m:
            return
        mid = m.group(1)
        home = away = None
        h_score = a_score = None
        played = 0.0
        period = None

        if isinstance(payload, list):
            blob = " ".join(str(x) for x in payload)
            sm = SCORE_RE.search(blob)
            if sm:
                h_score, a_score = int(sm.group(1)), int(sm.group(2))
            for item in payload:
                if isinstance(item, dict):
                    payload = item
                    break

        if isinstance(payload, dict):
            home = (
                payload.get("FixtureHomeTeamName")
                or payload.get("homeTeamName")
                or payload.get("homeTeam")
            )
            away = (
                payload.get("FixtureAwayTeamName")
                or payload.get("awayTeamName")
                or payload.get("awayTeam")
            )
            for key in ("eventScore", "setScore", "gameScore", "score"):
                if payload.get(key):
                    sm = SCORE_RE.search(str(payload[key]))
                    if sm:
                        h_score, a_score = int(sm.group(1)), int(sm.group(2))
                        break
            gs = payload.get("eventGameScores")
            if isinstance(gs, list) and gs:
                sm = SCORE_RE.search(str(gs[0]))
                if sm:
                    h_score, a_score = int(sm.group(1)), int(sm.group(2))
            played = _played_to_seconds(
                payload.get("eventPlayedTime")
                or payload.get("playedSeconds")
                or payload.get("playedTime")
            )
            period = payload.get("eventMatchPeriod") or payload.get("period")

        fields = {}
        if home:
            fields["home_team"] = str(home).strip()
        if away:
            fields["away_team"] = str(away).strip()
        if h_score is not None and a_score is not None:
            fields["home_score"] = h_score
            fields["away_score"] = a_score
        if played > 0:
            fields["played_seconds"] = played
            fields["minute"] = played / 60.0
        if period:
            fields["period"] = str(period)
            fields["match_status"] = str(period)

        rec = self._upsert(mid, **fields) if fields else self._upsert(mid)
        self.last_message_at = time.time()
        logger.info(
            f"[SPORTY][SOCK] {rec.get('home_team', '?')} "
            f"{rec.get('home_score')}-{rec.get('away_score')} "
            f"{rec.get('away_team', '?')} min={rec.get('minute', 0):.0f} id={mid}"
        )

    def _handle_text(self, text: str):
        if not text or text in ("2", "3", "40", "41"):
            return
        data = None
        if text.startswith("42"):
            try:
                data = json.loads(text[2:])
            except Exception:
                return
        else:
            try:
                i = text.find("[")
                if i >= 0:
                    data = json.loads(text[i:])
            except Exception:
                return

        if not isinstance(data, list) or len(data) < 2:
            return
        payload = data[1]
        if not isinstance(payload, dict):
            return

        inner = payload.get("data", payload)
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except Exception:
                dec = _b64_decode(inner)
                if not dec:
                    return
                try:
                    inner = json.loads(dec)
                except Exception:
                    return
        if not isinstance(inner, dict):
            return

        topic = str(inner.get("topic") or payload.get("topic") or "")
        body = inner.get("body")
        decoded = None
        if body:
            raw = _b64_decode(str(body))
            if raw:
                try:
                    decoded = json.loads(raw)
                except Exception:
                    decoded = raw

        # STATUS_TAG = "\~status" built without backslash in source
        if STATUS_TAG in topic or "status" in topic.lower():
            self._ingest_status(topic, decoded if decoded is not None else inner)
        else:
            m = TOPIC_RE.search(topic)
            if m:
                self._upsert(m.group(1))

    async def attach_context(self, context, open_live_list: bool = True):
        """Attach Socket listener on the same logged-in browser context."""
        self._context = context
        page = await context.new_page()
        self._page = page

        def on_websocket(ws):
            if ws.url not in self._ws_urls:
                self._ws_urls.add(ws.url)
                logger.success(f"[SPORTY][WS OPEN] {ws.url}")

            def on_frame(ev):
                try:
                    payload = getattr(ev, "payload", ev)
                    text = (
                        payload.decode("utf-8", errors="replace")
                        if isinstance(payload, (bytes, bytearray))
                        else str(payload)
                    )
                    self._handle_text(text)
                except Exception as e:
                    logger.debug(f"[SPORTY] frame: {e}")

            ws.on("framereceived", on_frame)

        page.on("websocket", on_websocket)

        if open_live_list:
            await page.goto(self.LIVE_LIST, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2.5)
            for sel in (
                'button:has-text("Accept")',
                'button:has-text("OK")',
                'button:has-text("Got it")',
            ):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=600)
                except Exception:
                    pass

        logger.success("[SPORTY] Socket attached (REST still supplies IDs + URLs)")
        if self.alerter:
            try:
                await self.alerter.send(
                    "✅ <b>SportyBet Socket attached</b>\n"
                    "REST = match IDs + live links\n"
                    "Socket = live score/clock when pushed"
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    async def start(self, callback: Optional[Callable] = None):
        if callback:
            self.callback = callback
        if self.running:
            return
        self.running = True
        asyncio.create_task(self._rest_loop())
        logger.info(
            f"[SPORTY] started — REST every {self.poll_interval}s + Socket when attached"
        )

    async def wait_until_ready(self, timeout: float = 20.0) -> bool:
        try:
            await asyncio.wait_for(self.ready_event.wait(), timeout=timeout)
            logger.success(f"[SPORTY] ready — {len(self.matches)} matches")
            return True
        except asyncio.TimeoutError:
            logger.warning("[SPORTY] not ready yet")
            return False

    def stop(self):
        self.running = False
