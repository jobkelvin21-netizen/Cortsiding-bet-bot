"""Bet365 InPlay WS — Chrome with VPN extension."""

import asyncio
import os
import re
import time
from typing import Callable, Dict, List, Optional

from loguru import logger
from playwright.async_api import async_playwright

from config import Config

SS_RE = re.compile(r"(?:^|[|;,\s])SS=(\d{1,2})\s*[-:]\s*(\d{1,2})", re.I)
ID_RE = re.compile(r"(?:^|[|;,\s])(?:ID|FI)=(\d+)", re.I)
TM_RE = re.compile(r"(?:^|[|;,\s])TM=(\d+)", re.I)
OV_ID_RE = re.compile(r"OV\d+-(\d+)_\d+_\d+U?", re.I)


class Bet365Feed:
    def __init__(self, callback: Optional[Callable] = None):
        self.callback = callback
        self.matches: Dict[str, dict] = {}
        self.running = False
        self._last_message_at: Optional[float] = None
        self._task = None
        self.alerter = None
        self._ws_url: Optional[str] = None
        self._page = None
        self._context = None

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_callback(self, callback: Callable):
        self.callback = callback

    def is_healthy(self) -> bool:
        if self._last_message_at is None:
            return False
        return (time.time() - self._last_message_at) < 45

    def get_matches(self) -> List[dict]:
        return list(self.matches.values())

    def get_match(self, match_id: str) -> Optional[dict]:
        return self.matches.get(str(match_id))

    async def _notify(self, match: dict):
        if self.callback:
            try:
                await self.callback(dict(match))
            except Exception as e:
                logger.debug(f"[BET365] callback error: {e}")

    def _extract_id(self, part: str) -> Optional[str]:
        m = ID_RE.search(part)
        if m:
            return m.group(1)
        m = OV_ID_RE.search(part)
        if m:
            return m.group(1)
        m = re.search(r"\b(\d{7,})\b", part)
        if m:
            return m.group(1)
        return None

    def _parse_frame(self, raw: str):
        raw = (raw or "").replace("\x01", "").strip()
        if not raw:
            return

        parts = re.split(r"F\|", raw)
        for part in parts:
            part = part.strip()
            if not part or part.startswith("U|") or part.startswith("CONFIG"):
                continue

            blocks = part.split(";")
            data = {}
            for b in blocks[1:]:
                if "=" not in b:
                    continue
                k, v = b.split("=", 1)
                data[k.strip().upper()] = v.strip()

            mid = data.get("ID") or data.get("FI") or self._extract_id(part)
            if not mid:
                continue
            mid = str(mid)

            rec = self.matches.get(
                mid,
                {
                    "source": "bet365",
                    "match_id": mid,
                    "home_team": "",
                    "away_team": "",
                    "home_score": 0,
                    "away_score": 0,
                    "ss": "",
                    "tm": "",
                    "minute": 0,
                    "played_seconds": 0,
                    "updated": 0.0,
                },
            )

            score_changed = False
            if "SS" in data:
                m = re.match(r"(\d+)\s*[-:]\s*(\d+)", data["SS"])
                if m:
                    h, a = int(m.group(1)), int(m.group(2))
                    old = rec.get("ss", "")
                    rec["ss"] = f"{h}-{a}"
                    rec["home_score"] = h
                    rec["away_score"] = a
                    if old and old != rec["ss"]:
                        score_changed = True
                        logger.success(f"[BET365][GOAL] id={mid} {old}→{rec['ss']}")

            if "TM" in data:
                try:
                    rec["tm"] = data["TM"]
                    rec["minute"] = int(float(data["TM"]))
                    rec["played_seconds"] = rec["minute"] * 60
                except Exception:
                    pass
            else:
                m = TM_RE.search(part)
                if m:
                    rec["tm"] = m.group(1)
                    rec["minute"] = int(m.group(1))
                    rec["played_seconds"] = rec["minute"] * 60

            for m in SS_RE.finditer(part):
                h, a = int(m.group(1)), int(m.group(2))
                ss = f"{h}-{a}"
                if rec.get("ss") != ss:
                    old = rec.get("ss", "")
                    rec["ss"] = ss
                    rec["home_score"] = h
                    rec["away_score"] = a
                    if old:
                        score_changed = True
                        logger.success(f"[BET365][GOAL] id={mid} {old}→{ss}")

            rec["updated"] = time.time()
            self.matches[mid] = rec
            self._last_message_at = time.time()

            if score_changed and self.callback:
                asyncio.create_task(self._notify(rec))

    async def _run_session(self) -> bool:
        """Launch Chrome with your existing profile (VPN included)."""
        logger.info("[BET365] Launching Chrome with your profile...")
        got_403 = False

        profile = os.path.expanduser("~/.config/google-chrome")

        async with async_playwright() as p:
            try:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=profile,
                    channel="chrome",
                    headless=False,
                    no_viewport=True,
                    locale="en-GB",
                    # FIX: tell Playwright NOT to auto-disable extensions —
                    # without this, your VPN extension gets silently disabled
                    # even though it's installed in this exact profile.
                    ignore_default_args=[
                        "--disable-extensions",
                        "--disable-component-extensions-with-background-pages",
                    ],
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--start-maximized",
                        "--disable-dev-shm-usage",
                    ],
                )
            except Exception as e:
                logger.error(f"[BET365] Launch failed: {e}")
                # FIX: this specific error almost always means your real
                # Chrome (with the profile already open) is still running.
                # Chrome locks the profile directory — only one process can
                # use it at a time.
                logger.error(
                    "[BET365] If this says 'profile in use' or similar — "
                    "close your normal Chrome browser COMPLETELY first "
                    "(check it's not just minimized — quit it entirely), "
                    "then run the bot again."
                )
                return False

            self._context = context
            page = context.pages[0] if context.pages else await context.new_page()
            self._page = page

            def on_response(resp):
                nonlocal got_403
                try:
                    if resp.status == 403:
                        got_403 = True
                        logger.warning(f"[BET365] 403 {resp.url[:80]}")
                except Exception:
                    pass

            page.on("response", on_response)

            def on_websocket(ws):
                self._ws_url = ws.url
                logger.success(f"[BET365][WS] {ws.url[:70]}")

                def on_frame(ev):
                    try:
                        payload = getattr(ev, "payload", ev)
                        text = (
                            payload.decode("utf-8", errors="ignore")
                            if isinstance(payload, (bytes, bytearray))
                            else str(payload)
                        )
                        if "SS=" in text or "TM=" in text or "ID=" in text or "FI=" in text:
                            self._parse_frame(text)
                    except Exception as e:
                        logger.debug(f"[BET365] frame: {e}")

                ws.on("framereceived", on_frame)

            page.on("websocket", on_websocket)

            try:
                resp = await page.goto(
                    Config.BET365_LIVE_URL,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                if (resp and resp.status == 403) or got_403:
                    logger.warning("[BET365] 403 — VPN not connected?")
                    await context.close()
                    return False

                await asyncio.sleep(2)
                for sel in ("text=Football", "text=Soccer"):
                    try:
                        loc = page.locator(sel)
                        if await loc.count() > 0:
                            await loc.first.click(timeout=2000)
                            await asyncio.sleep(1.5)
                            break
                    except Exception:
                        pass

                logger.success("[BET365] WS live (VPN active)")

                while self.running:
                    if got_403:
                        logger.warning("[BET365] 403 during session")
                        await context.close()
                        return False
                    idle = time.time() - (self._last_message_at or time.time())
                    if self._last_message_at and idle > 90:
                        logger.warning(f"[BET365] idle {idle:.0f}s — reconnect")
                        break
                    await asyncio.sleep(2)

                await context.close()
                return True
            except Exception as e:
                logger.error(f"[BET365] error: {e}")
                try:
                    await context.close()
                except:
                    pass
                return False

    async def _supervisor(self):
        while self.running:
            ok = await self._run_session()
            if not self.running:
                break
            if not ok:
                logger.warning("[BET365] failed — retry in 5s")
            else:
                logger.info("[BET365] reconnecting in 3s...")
            await asyncio.sleep(5.0 if not ok else 3.0)

    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._supervisor())
        logger.success("[BET365] started")

    def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
        logger.info("[BET365] stopped")
