"""Bet365 WS — attach to your Chrome (Proton extension) via CDP. No own browser."""

import asyncio
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

CDP_URL = "http://127.0.0.1:9222"


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
        self._browser = None
        self._playwright = None

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
        logger.info(f"[BET365] connecting CDP {CDP_URL}")
        got_403 = False

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            logger.error(
                f"[BET365] CDP connect failed: {e}\n"
                "Start Chrome first:\n"
                '  google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-bot-debug"'
            )
            return False

        try:
            if not self._browser.contexts:
                logger.error("[BET365] no browser context on CDP")
                return False
            context = self._browser.contexts[0]
            page = await context.new_page()
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

            url = getattr(Config, "BET365_LIVE_URL", "https://www.bet365.com/#/IP/B1")
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if (resp and resp.status == 403) or got_403:
                logger.warning("[BET365] 403 — is Proton extension connected?")
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

            logger.success("[BET365] WS live via your Chrome + Proton")

            while self.running:
                if got_403:
                    return False
                idle = time.time() - (self._last_message_at or time.time())
                if self._last_message_at and idle > 90:
                    logger.warning(f"[BET365] idle {idle:.0f}s — reconnect")
                    break
                await asyncio.sleep(2)
            return True
        except Exception as e:
            logger.error(f"[BET365] session: {e}")
            return False
        finally:
            try:
                if self._page and not self._page.is_closed():
                    await self._page.close()
            except Exception:
                pass
            try:
                if self._playwright:
                    await self._playwright.stop()
            except Exception:
                pass
            self._page = None
            self._browser = None
            self._playwright = None

    async def _supervisor(self):
        while self.running:
            ok = await self._run_session()
            if not self.running:
                break
            wait = 5.0 if not ok else 3.0
            logger.info(f"[BET365] retry in {wait}s")
            await asyncio.sleep(wait)

    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._supervisor())
        logger.success("[BET365] started (CDP → your Chrome)")

    def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
        logger.info("[BET365] stopped")
