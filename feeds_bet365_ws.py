"""Bet365 WS — ALL LIVE MATCHES (initial dump + updates) to Telegram."""

import asyncio
import re
import time
from typing import Callable, Dict, List, Optional

from loguru import logger
from playwright.async_api import async_playwright

from config import Config

CDP_URL = "http://127.0.0.1:9222"
WS_IDLE_SECS = 90

# From working standalone
RE_FI = re.compile(r"(?:^|[|;,\s])FI=(\d{6,})", re.I)
RE_ID = re.compile(r"(?:^|[|;,\s])ID=(\d{6,})", re.I)
RE_NA = re.compile(r"(?:^|[|;,\s])NA=([^|;]+)", re.I)
RE_SS = re.compile(r"(?:^|[|;,\s])SS=(\d{1,2}\s*[-:]\s*\d{1,2})", re.I)
RE_OV = re.compile(r"OV(\d{6,})C\d+", re.I)


def _split_teams(na: str):
    for sep in (r"\s+vs\.?\s+", r"\s+v\.?\s+", r"\s+@\s+"):
        parts = re.split(sep, (na or "").strip(), maxsplit=1, flags=re.I)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    return (na or "").strip(), ""


class Bet365Feed:
    def __init__(self, callback: Optional[Callable] = None):
        self.callback = callback
        self.matches: Dict[str, dict] = {}
        self.running = False
        self._want_run = False
        self._last_message_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self.alerter = None
        self._page = None
        self._browser = None
        self._playwright = None
        self._last_alert_at = 0.0

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_callback(self, callback: Callable):
        self.callback = callback

    async def _tg(self, text: str, min_gap: float = 1.0):
        now = time.time()
        if now - self._last_alert_at < min_gap:
            return
        self._last_alert_at = now
        if self.alerter:
            try:
                await self.alerter.send(text)
            except Exception:
                pass

    def is_healthy(self) -> bool:
        if not self.running:
            return False
        if self._last_message_at is None:
            return False
        return (time.time() - self._last_message_at) < 45

    def get_matches(self) -> List[dict]:
        return list(self.matches.values())

    def get_match(self, match_id: str) -> Optional[dict]:
        return self.matches.get(str(match_id))

    async def _announce(self, fi: str, name: str, ss: str):
        """Send EVERY match to Telegram - NO FILTERING."""
        if not self.running or not self._want_run:
            return

        if self.alerter:
            try:
                await self.alerter.send(
                    f"🔴 <b>LIVE</b>\n"
                    f"🆔 <code>{fi}</code>\n"
                    f"⚽ {name}\n"
                    f"📊 {ss or '?'}"
                )
            except Exception:
                pass

    def _parse_frame(self, text: str):
        """Parse using working standalone pattern - sends ALL matches including initial dump."""
        if not self.running or not self._want_run:
            return
        if not text or len(text) < 8:
            return

        # Split rough event blocks (from standalone)
        parts = re.split(r"[|\x01\x08]", text)
        current_na = None
        current_fi = None
        current_ss = None

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Find match name
            for m in RE_NA.finditer(part):
                na = m.group(1).strip()
                if re.search(r"\bv\b|\bvs\b|@", na, re.I) or " v " in na.lower():
                    current_na = na

            # Find FI/ID
            for m in RE_FI.finditer(part):
                current_fi = m.group(1)
            for m in RE_ID.finditer(part):
                if not current_fi:
                    current_fi = m.group(1)
            for m in RE_OV.finditer(part):
                if not current_fi:
                    current_fi = m.group(1)

            # Find score
            for m in RE_SS.finditer(part):
                current_ss = re.sub(r"\s+", "", m.group(1).replace(":", "-"))

            # Commit when we have id AND (name OR score)
            if current_fi and (current_na or current_ss):
                prev = self.matches.get(current_fi, {})
                name = current_na or prev.get("name") or ""
                ss = current_ss or prev.get("ss") or ""

                if name or ss:
                    # Check if changed from previous
                    changed = prev.get("ss") != ss or prev.get("name") != name
                    
                    # ALWAYS update stored match
                    self.matches[current_fi] = {
                        "source": "bet365",
                        "match_id": current_fi,
                        "home_team": "",
                        "away_team": "",
                        "name": name,
                        "home_score": 0,
                        "away_score": 0,
                        "ss": ss,
                        "minute": 0,
                        "updated": time.time(),
                    }
                    
                    if name:
                        h, a = _split_teams(name)
                        self.matches[current_fi]["home_team"] = h
                        self.matches[current_fi]["away_team"] = a

                    # KEY: Announce on ANY change (catches initial dump!)
                    if changed:
                        display_name = name if name else f"Match {current_fi}"
                        asyncio.create_task(self._announce(current_fi, display_name, ss))
                        if name:
                            logger.info(f"[MATCH] FI={current_fi} {name}")
                        else:
                            logger.info(f"[SCORE] FI={current_fi} SS={ss} (no name yet)")
                        
                        # Callback on score changes
                        if ss and prev.get("ss") and ss != prev.get("ss") and self.callback:
                            asyncio.create_task(self._safe_cb(self.matches[current_fi]))

                # Reset for next match in same frame
                current_na = None
                current_ss = None

        self._last_message_at = time.time()

    async def _safe_cb(self, rec: dict):
        if not self.running or not self._want_run or not self.callback:
            return
        try:
            await self.callback(dict(rec))
        except Exception:
            pass

    async def _close_browser(self):
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
        except Exception:
            pass
        self._page = None
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        self._browser = None

    async def _run_session(self) -> bool:
        if not self._want_run:
            return False
        got_403 = False
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            logger.warning(f"[BET365] CDP connect failed: {e}")
            if self._want_run:
                await self._tg("⚠️ Bet365 CDP offline")
            return False
        try:
            if not self._browser.contexts:
                return False
            page = await self._browser.contexts[0].new_page()
            self._page = page

            def on_response(resp):
                nonlocal got_403
                try:
                    if resp.status == 403:
                        got_403 = True
                except Exception:
                    pass

            page.on("response", on_response)

            def on_websocket(ws):
                def on_frame(ev):
                    if not self.running or not self._want_run:
                        return
                    try:
                        payload = getattr(ev, "payload", ev)
                        text = (
                            payload.decode("utf-8", errors="ignore")
                            if isinstance(payload, (bytes, bytearray))
                            else str(payload)
                        )
                        if any(x in text for x in ("NA=", "FI=", "SS=", "OV", "EV;")):
                            self._parse_frame(text)
                    except Exception:
                        pass

                ws.on("framereceived", on_frame)

            page.on("websocket", on_websocket)
            
            url = getattr(Config, "BET365_LIVE_URL", "https://www.bet365.com/#/IP/B1")
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            if not self._want_run:
                return False
            if (resp and resp.status == 403) or got_403:
                await self._tg("⚠️ Bet365 403")
                return False
            
            await asyncio.sleep(2)
            
            # Click Football/Soccer to load in-play
            for sel in ("text=Football", "text=Soccer"):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=2000)
                        logger.info(f"[BET365] clicked {sel}")
                        await asyncio.sleep(2)
                        break
                except Exception:
                    pass

            self.running = True
            logger.success("[BET365] session UP - receiving ALL matches")
            await self._tg("✅ Bet365 ON - sending ALL live matches")
            
            while self._want_run:
                if got_403:
                    await self._tg("⚠️ Bet365 403")
                    return False
                if self._last_message_at and (
                    time.time() - self._last_message_at > WS_IDLE_SECS
                ):
                    return False
                await asyncio.sleep(1)
            return True
        except asyncio.CancelledError:
            return False
        except Exception as e:
            logger.error(f"[BET365] {e!r}")
            return False
        finally:
            self.running = False
            await self._close_browser()

    async def _supervisor(self):
        while self._want_run:
            await self._run_session()
            if not self._want_run:
                break
            await asyncio.sleep(3)
        self.running = False

    async def start(self):
        if self._want_run and self._task and not self._task.done():
            return
        self._want_run = True
        self.running = False
        self._task = asyncio.create_task(self._supervisor())

    async def stop(self):
        self._want_run = False
        self.running = False
        self.callback = None
        t = self._task
        self._task = None
        if t and not t.done():
            t.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=2.0)
            except Exception:
                pass
        await self._close_browser()
