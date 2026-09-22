"""Bet365 WS via CDP — push football FI+name to Telegram; goals by FI."""

import asyncio
import re
import time
from typing import Callable, Dict, List, Optional, Set

from loguru import logger
from playwright.async_api import async_playwright

from config import Config

CDP_URL = "http://127.0.0.1:9222"
WS_IDLE_SECS = 90

SS_RE = re.compile(r"(?:^|[|;,\s])SS=(\d{1,2})\s*[-:]\s*(\d{1,2})", re.I)
FI_RE = re.compile(r"(?:^|[|;,\s])FI=(\d{6,})", re.I)
ID_RE = re.compile(r"(?:^|[|;,\s])ID=(\d{6,})", re.I)
NA_RE = re.compile(r"(?:^|[|;,\s])NA=([^|;]+)", re.I)
TM_RE = re.compile(r"(?:^|[|;,\s])TM=(\d+)", re.I)
OV_RE = re.compile(r"OV(\d{6,})C\d+", re.I)

# Skip obvious non-football noise
_SKIP = re.compile(
    r"terrorist|counter-terror|darts|snooker|tennis|table tennis|"
    r"volleyball|basketball|hockey|baseball|esport|cs:?go|valorant|"
    r"league of legends|dota|ufc|boxing|mma|cricket|rugby",
    re.I,
)


def _looks_football(na: str, ss: str = "") -> bool:
    na = (na or "").strip()
    if not na or not re.search(r"\s+v(?:s)?\.?\s+|\s+@\s+", na, re.I):
        return False
    if _SKIP.search(na):
        return False
    if ss:
        try:
            h, a = map(int, ss.replace(":", "-").split("-"))
            if h > 15 or a > 15:  # unlikely football
                return False
        except Exception:
            pass
    return True


def _split_teams(na: str):
    na = (na or "").strip()
    for sep in (r"\s+vs\.?\s+", r"\s+v\.?\s+", r"\s+@\s+"):
        parts = re.split(sep, na, maxsplit=1, flags=re.I)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    return na, ""


class Bet365Feed:
    def __init__(self, callback: Optional[Callable] = None):
        self.callback = callback
        self.matches: Dict[str, dict] = {}
        self.running = False
        self._last_message_at: Optional[float] = None
        self._task = None
        self.alerter = None
        self._ws_url = None
        self._page = None
        self._browser = None
        self._playwright = None
        self._last_alert_at = 0.0
        self._announced: Set[str] = set()  # FI already sent to Telegram

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_callback(self, callback: Callable):
        self.callback = callback

    async def _alert(self, text: str, min_gap: float = 25.0):
        now = time.time()
        if now - self._last_alert_at < min_gap:
            return
        self._last_alert_at = now
        logger.warning(text)
        if self.alerter:
            try:
                await self.alerter.send(text)
            except Exception:
                pass

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
                logger.debug(f"[BET365] callback: {e}")

    async def _announce_match(self, fi: str, name: str, ss: str):
        if fi in self._announced:
            return
        if not _looks_football(name, ss):
            return
        self._announced.add(fi)
        msg = (
            f"⚽ <b>FOOTBALL</b>\n"
            f"🆔 FI=<code>{fi}</code>\n"
            f"📋 {name}\n"
            f"SS={ss or '?'}"
        )
        logger.info(f"[BET365] announce FI={fi} {name}")
        if self.alerter:
            try:
                await self.alerter.send(msg)
            except Exception:
                pass

    def _parse_frame(self, raw: str):
        raw = (raw or "").replace("\x01", "").strip()
        if not raw:
            return

        parts = re.split(r"[|\x08]", raw)
        cur_fi = None
        cur_na = None
        cur_ss = None
        cur_tm = None

        for part in parts:
            part = part.strip()
            if not part:
                continue

            m = NA_RE.search(part)
            if m:
                na = m.group(1).strip()
                if re.search(r"\bv\b|\bvs\b|@", na, re.I) or " v " in na.lower():
                    cur_na = na

            m = FI_RE.search(part)
            if m:
                cur_fi = m.group(1)
            else:
                m = ID_RE.search(part)
                if m and re.fullmatch(r"\d{6,}", m.group(1)):
                    cur_fi = m.group(1)
                else:
                    m = OV_RE.search(part)
                    if m:
                        cur_fi = m.group(1)

            m = SS_RE.search(part)
            if m:
                cur_ss = f"{int(m.group(1))}-{int(m.group(2))}"

            m = TM_RE.search(part)
            if m:
                cur_tm = m.group(1)

            if not cur_fi:
                continue
            if not (cur_na or cur_ss):
                continue

            mid = str(cur_fi)
            rec = self.matches.get(
                mid,
                {
                    "source": "bet365",
                    "match_id": mid,
                    "home_team": "",
                    "away_team": "",
                    "name": "",
                    "home_score": 0,
                    "away_score": 0,
                    "ss": "",
                    "tm": "",
                    "minute": 0,
                    "updated": 0.0,
                },
            )

            if cur_na:
                h, a = _split_teams(cur_na)
                rec["name"] = cur_na
                if h:
                    rec["home_team"] = h
                if a:
                    rec["away_team"] = a
                # Announce football once when we first get a name
                asyncio.create_task(
                    self._announce_match(mid, cur_na, cur_ss or rec.get("ss") or "")
                )

            score_changed = False
            if cur_ss:
                old = rec.get("ss") or ""
                try:
                    h, a = map(int, cur_ss.split("-"))
                except Exception:
                    h = a = 0
                rec["ss"] = cur_ss
                rec["home_score"] = h
                rec["away_score"] = a
                if old and old != cur_ss:
                    score_changed = True
                    logger.success(
                        f"[BET365][GOAL] FI={mid} {old}→{cur_ss} {rec.get('name') or ''}"
                    )

            if cur_tm:
                try:
                    rec["tm"] = cur_tm
                    rec["minute"] = int(float(cur_tm))
                except Exception:
                    pass

            rec["updated"] = time.time()
            self.matches[mid] = rec
            self._last_message_at = time.time()

            if score_changed and self.callback:
                asyncio.create_task(self._notify(rec))

            cur_ss = None
            cur_na = None

    async def _run_session(self) -> bool:
        logger.info(f"[BET365] CDP {CDP_URL}")
        got_403 = False
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            await self._alert(
                "⚠️ <b>Bet365 CDP offline</b>\n"
                "<code>google-chrome --remote-debugging-port=9222 "
                '--user-data-dir="$HOME/chrome-bot-debug"</code>'
            )
            return False

        try:
            if not self._browser.contexts:
                await self._alert("⚠️ Bet365 CDP: no context")
                return False
            context = self._browser.contexts[0]
            page = await context.new_page()
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
                        if any(x in text for x in ("NA=", "FI=", "SS=", "OV", "ID=")):
                            self._parse_frame(text)
                    except Exception as e:
                        logger.debug(f"[BET365] frame: {e}")

                ws.on("framereceived", on_frame)

            page.on("websocket", on_websocket)

            url = getattr(Config, "BET365_LIVE_URL", "https://www.bet365.com/#/IP/B1")
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if (resp and resp.status == 403) or got_403:
                await self._alert("⚠️ <b>Bet365 403</b> — change Proton location")
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

            logger.success("[BET365] WS live")
            if self.alerter:
                try:
                    await self.alerter.send("✅ Bet365 WS connected — football FI list → Telegram")
                except Exception:
                    pass

            while self.running:
                if got_403:
                    await self._alert("⚠️ Bet365 403 — change VPN")
                    return False
                if self._last_message_at:
                    idle = time.time() - self._last_message_at
                    if idle > WS_IDLE_SECS:
                        await self._alert(f"⚠️ Bet365 silent {int(idle)}s — reconnect")
                        return False
                await asyncio.sleep(2)
            return True
        except Exception as e:
            await self._alert(f"⚠️ Bet365 session: {e}")
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
            await asyncio.sleep(4.0 if not ok else 2.0)

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
