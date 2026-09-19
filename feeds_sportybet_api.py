#!/usr/bin/env python3
"""
SportyBet Socket.IO / page-WS feed (push only).
Parses \~status topics for score + clock. No REST polling.
Must attach to the same logged-in Playwright context as main.
"""

import asyncio
import base64
import json
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

TOPIC_RE = re.compile(
    r"sr:match:(\d+)",
    re.I,
)
SCORE_RE = re.compile(r"(\d{1,2})\s*[:\-]\s*(\d{1,2})")


def _b64_decode(s: str) -> str:
    if not s or not isinstance(s, str):
        return ""
    pad = "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s + pad).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _parse_played_to_seconds(raw) -> float:
    if raw is None:
        return 0.0
    try:
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw).strip()
        if ":" in text:
            parts = text.split(":")
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
        return float(text)
    except Exception:
        return 0.0


def _extract_json_blobs(text: str) -> List[Any]:
    """Pull JSON objects/arrays from a Socket.IO frame string."""
    out = []
    if not text:
        return out
    # Engine.IO / Socket.IO event: 42[...]
    if text.startswith("42"):
        try:
            out.append(json.loads(text[2:]))
            return out
        except Exception:
            pass
    # fallback: find first [ or {
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                out.append(json.loads(text[i:]))
                break
            except Exception:
                continue
    return out


class SportyBetSocketFeed:
    LIVE_LIST = "https://www.sportybet.com/ng/sport/football/live_list"

    def __init__(self):
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
        # live URL if we have teams
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
        if not self.ready_event.is_set() and len(self.matches) > 0:
            self.ready_event.set()
        return rec

    def _ingest_status_payload(self, topic: str, payload: Any):
        m = TOPIC_RE.search(topic or "")
        if not m:
            return
        mid = m.group(1)

        home = away = None
        h_score = a_score = None
        played = None
        period = None
        status = None

        # payload may be list (decoded body array) or dict
        if isinstance(payload, list):
            # common pattern: mixed strings / numbers
            text_blob = " ".join(str(x) for x in payload)
            sm = SCORE_RE.search(text_blob)
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
                or payload.get("home")
            )
            away = (
                payload.get("FixtureAwayTeamName")
                or payload.get("awayTeamName")
                or payload.get("awayTeam")
                or payload.get("away")
            )
            for key in ("eventScore", "setScore", "gameScore", "score", "eventPointScore"):
                if key in payload and payload[key]:
                    sm = SCORE_RE.search(str(payload[key]))
                    if sm:
                        h_score, a_score = int(sm.group(1)), int(sm.group(2))
                        break
            gs = payload.get("eventGameScores")
            if isinstance(gs, list) and gs:
                sm = SCORE_RE.search(str(gs[0]))
                if sm:
                    h_score, a_score = int(sm.group(1)), int(sm.group(2))

            played = _parse_played_to_seconds(
                payload.get("eventPlayedTime")
                or payload.get("playedSeconds")
                or payload.get("playedTime")
                or payload.get("matchTime")
            )
            period = (
                payload.get("eventMatchPeriod")
                or payload.get("period")
                or payload.get("matchStatus")
            )
            status = payload.get("eventMatchStatus") or payload.get("eventStatus")

        fields = {}
        if home:
            fields["home_team"] = str(home).strip()
        if away:
            fields["away_team"] = str(away).strip()
        if h_score is not None and a_score is not None:
            fields["home_score"] = h_score
            fields["away_score"] = a_score
        if played is not None and played > 0:
            fields["played_seconds"] = played
            fields["minute"] = played / 60.0
        if period:
            fields["period"] = str(period)
            fields["match_status"] = str(period)
        if status is not None:
            fields["match_status"] = str(status)

        if not fields:
            # still register id from topic
            self._upsert(mid)
            return

        rec = self._upsert(mid, **fields)
        if self.callback:
            try:
                asyncio.get_running_loop().create_task(self.callback(dict(rec)))
            except RuntimeError:
                pass

    def _handle_frame_text(self, text: str):
        if not text or text in ("2", "3", "40", "41"):
            return
        blobs = _extract_json_blobs(text)
        for blob in blobs:
            # 42["data", {type, data}]
            if isinstance(blob, list) and len(blob) >= 2:
                _event, data = blob[0], blob[1]
                if not isinstance(data, dict):
                    continue
                inner = data.get("data", data)
                if isinstance(inner, str):
                    try:
                        inner = json.loads(inner)
                    except Exception:
                        # maybe pure base64 body
                        dec = _b64_decode(inner)
                        if dec:
                            try:
                                inner = json.loads(dec)
                            except Exception:
                                inner = {"body": inner}
                if not isinstance(inner, dict):
                    continue
                topic = inner.get("topic") or data.get("topic") or ""
                body = inner.get("body")
                push = (inner.get("pushType") or "").upper()

                decoded = None
                if body:
                    raw = _b64_decode(str(body))
                    if raw:
                        try:
                            decoded = json.loads(raw)
                        except Exception:
                            decoded = raw

                if "\~status" in topic or "status" in topic.lower() or push == "GROUP":
                    self._ingest_status_payload(topic, decoded if decoded is not None else inner)
                elif TOPIC_RE.search(topic):
                    # odds or other — still ensure match id exists
                    m = TOPIC_RE.search(topic)
                    if m:
                        self._upsert(m.group(1))
            elif isinstance(blob, dict):
                topic = blob.get("topic") or ""
                if topic:
                    body = blob.get("body")
                    decoded = _b64_decode(str(body)) if body else None
                    try:
                        decoded = json.loads(decoded) if decoded else blob
                    except Exception:
                        decoded = blob
                    self._ingest_status_payload(topic, decoded)

    async def attach_context(self, context, open_live_list: bool = True):
        """
        Use the SAME logged-in browser context as main.
        Opens one background tab on live_list to keep Socket.IO alive.
        """
        self._context = context
        page = await context.new_page()
        self._page = page

        def on_websocket(ws):
            url = ws.url
            if url not in self._ws_urls:
                self._ws_urls.add(url)
                logger.success(f"[SPORTY][WS OPEN] {url}")

            def on_frame(ev):
                try:
                    payload = getattr(ev, "payload", ev)
                    if isinstance(payload, (bytes, bytearray)):
                        text = payload.decode("utf-8", errors="replace")
                    else:
                        text = str(payload)
                    self._handle_frame_text(text)
                except Exception as e:
                    logger.debug(f"[SPORTY] frame err: {e}")

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

        logger.success("[SPORTY] Socket feed attached to logged-in context")
        if self.alerter:
            try:
                await self.alerter.send("✅ <b>SportyBet Socket.IO feed active</b>")
            except Exception:
                pass

    async def start(self, callback: Optional[Callable] = None):
        if callback:
            self.callback = callback
        self.running = True
        # attach_context must be called from main after login
        logger.info("[SPORTY] Socket feed started (waiting for context attach)")

    async def wait_until_ready(self, timeout: float = 20.0) -> bool:
        try:
            await asyncio.wait_for(self.ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("[SPORTY] no status pushes yet — still running")
            return False

    def stop(self):
        self.running = False
        logger.info("[SPORTY] Socket feed stopped")
