#!/usr/bin/env python3
"""
SportyBet push feed via page WebSocket (Socket.IO style frames).
Filename: feeds/sportybet_api.py  — import: from feeds.sportybet_api import SportyBetFeed
No REST polling for score/clock.
"""

import asyncio
import base64
import json
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

TOPIC_RE = re.compile(r"sr:match:(\d+)", re.I)
SCORE_RE = re.compile(r"(\d{1,2})\s*[:\-]\s*(\d{1,2})")


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


class SportyBetFeed:
    LIVE_LIST = "https://www.sportybet.com/ng/sport/football/live_list"

    def __init__(self, poll_interval: float = 1.5):
        # poll_interval kept only so old main.py constructors don't crash
        self.matches: Dict[str, dict] = {}
        self.running = False
        self.callback: Optional[Callable] = None
        self.alerter = None
        self._page = None
        self._context = None
        self.last_message_at: Optional[float] = None
        self.ready_event = asyncio.Event()
        self._ws_urls = set()

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_callback(self, callback: Callable):
        self.callback = callback

    def is_healthy(self) -> bool:
        if self.last_message_at is None:
            return False
        return (time.time() - self.last_message_at) < 40

    def get_matches(self) -> List[dict]:
        return list(self.matches.values())

    def get_match(self, match_id: str) -> Optional[dict]:
        return self.matches.get(str(match_id))

    def _upsert(self, match_id: str, **fields):
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
        if rec.get("home_team") and rec.get("away_team") and not rec.get("live_url"):
            h = re.sub(r"[^a-zA-Z0-9]+", "_", rec["home_team"]).strip("_")
            a = re.sub(r"[^a-zA-Z0-9]+", "_", rec["away_team"]).strip("_")
            rec["live_url"] = (
                f"https://www.sportybet.com/ng/sport/football/live/"
                f"World/League/{h}_vs_{a}/sr:match:{mid}"
            )
        rec["timestamp"] = datetime.now()
        rec["updated"] = time.time()
        self.matches[mid] = rec
        self.last_message_at = time.time()
        if not self.ready_event.is_set():
            self.ready_event.set()
        return rec

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
        logger.info(
            f"[SPORTY][PUSH] {rec.get('home_team','?')} "
            f"{rec.get('home_score')}-{rec.get('away_score')} "
            f"{rec.get('away_team','?')} min={rec.get('minute',0):.0f} "
            f"id={mid}"
        )
        if self.callback:
            try:
                asyncio.get_running_loop().create_task(self.callback(dict(rec)))
            except RuntimeError:
                pass

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
                if dec:
                    try:
                        inner = json.loads(dec)
                    except Exception:
                        return
        if not isinstance(inner, dict):
            return

        topic = inner.get("topic") or payload.get("topic") or ""
        body = inner.get("body")
        decoded = None
        if body:
            raw = _b64_decode(str(body))
            if raw:
                try:
                    decoded = json.loads(raw)
                except Exception:
                    decoded = raw

        if "\~status" in topic or "status" in topic.lower():
            self._ingest_status(topic, decoded if decoded is not None else inner)
        elif TOPIC_RE.search(topic):
            m = TOPIC_RE.search(topic)
            if m:
                self._upsert(m.group(1))

    async def attach_context(self, context, open_live_list: bool = True):
        """Same logged-in Chrome context as main — one tab holds the socket."""
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

        logger.success("[SPORTY] Socket feed on logged-in context")
        if self.alerter:
            try:
                await self.alerter.send("✅ <b>SportyBet Socket feed active</b>")
            except Exception:
                pass

    async def start(self, callback: Optional[Callable] = None):
        if callback:
            self.callback = callback
        self.running = True
        logger.info("[SPORTY] feed started (Socket push — attach_context after login)")

    async def wait_until_ready(self, timeout: float = 25.0) -> bool:
        try:
            await asyncio.wait_for(self.ready_event.wait(), timeout=timeout)
            logger.success(f"[SPORTY] ready — {len(self.matches)} matches")
            return True
        except asyncio.TimeoutError:
            logger.warning("[SPORTY] no pushes yet — still listening")
            return False

    def stop(self):
        self.running = False
