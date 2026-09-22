"""Bet365 WS — real football FI list; hard stop (no reconnect when stopped)."""

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

_SKIP = re.compile(
    r"terrorist|counter-terror|darts|snooker|tennis|table\s*tennis|"
    r"volleyball|basketball|hockey|ice\s*hockey|baseball|"
    r"esport|e-?sport|e-?football|efootball|virtual|simulat|"
    r"cs:?go|valorant|dota|fifa\b|ufc|boxing|mma|cricket|rugby|"
    r"afl|nfl|nba|nhl|mlb|badminton|squash|padel|handball|futsal|"
    r"mavericks|hornets|lakers|celtics|warriors|nets|knicks|"
    r"dimes|hyper|thunder|spurs|bulls|heat|suns|kings",
    re.I,
)
_CLUB = re.compile(
    r"\b(fc|cf|sc|afc|united|city|town|rovers|athletic|atletico|"
    r"real|sporting|club|bayern|psg|liverpool|chelsea|arsenal|"
    r"madrid|milan|inter|porto|benfica|ajax|dortmund|juventus)\b",
    re.I,
)


def _looks_football(na: str, ss: str = "") -> bool:
    na = (na or "").strip()
    if not na or not re.search(r"\s+v(?:s)?\.?\s+", na, re.I):
        return False
    if _SKIP.search(na):
        return False
    if re.search(r"\([^)]{1,24}\)", na):
        return False
    if "/" in na or "@" in na:
        return False
    if ss:
        try:
            h, a = map(int, ss.replace(":", "-").split("-"))
            if (h in (6, 7) and 0 <= a <= 7) or (a in (6, 7) and 0 <= h <= 7):
                return False
            if h > 10 or a > 10:
                return False
        except Exception:
            pass
    parts = re.split(r"\s+v(?:s)?\.?\s+", na, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return False
    left, right = parts[0].strip(), parts[1].strip()
    if (
        2 <= len(left.split()) <= 3
        and 2 <= len(right.split()) <= 3
        and not _CLUB.search(left)
        and not _CLUB.search(right)
    ):
        return False
    return True


def _split_teams(na: str):
    for sep in (r"\s+vs\.?\s+", r"\s+v\.?\s+"):
        parts = re.split(sep, (na or "").strip(), maxsplit=1, flags=re.I)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    return (na or "").strip(), ""


class Bet365Feed:
    def __init__(self, callback: Optional[Callable] = None):
        self.callback = callback
        self.matches: Dict[str, dict] = {}
        self.running = False
        self._want_run = False  # False = user stopped; do not reconnect
        self._last_message_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self.alerter = None
        self._page = None
        self._browser = None
        self._playwright = None
        self._last_alert_at = 0.0
        self._announced: Set[str] = set()

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_callback(self, callback: Callable):
        self.callback = callback

    async def _tg(self, text: str, min_gap: float = 15.0):
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

    async def _announce_match(self, fi: str, name: str, ss: str):
        if not self.running or not self._want_run:
            return
        if fi in self._announced or not _looks_football(name, ss):
            return
        self._announced.add(fi)
        if self.alerter:
            try:
                await self.alerter.send(
                    f"⚽ <b>FOOTBALL</b>\n"
                    f"🆔 FI=<code>{fi}</code>\n"
                    f"📋 {name}\n"
                    f"SS={ss or '?'}"
                )
            except Exception:
                pass

    def _parse_frame(self, raw: str):
        if not self.running or not self._want_run:
            return
        raw = (raw or "").replace("\x01", "").strip()
        if not raw:
            return
        parts = re.split(r"[|\x08]", raw)
        cur_fi = cur_na = cur_ss = cur_tm = None

        for part in parts:
            part = part.strip()
            if not part:
                continue
            m = NA_RE.search(part)
            if m:
                na = m.group(1).strip()
                if re.search(r"\s+v(?:s)?\.?\s+", na, re.I):
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

            if not cur_fi or not (cur_na or cur_ss):
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
                    "minute": 0,
                    "updated": 0.0,
                },
            )
            if cur_na:
                h, a = _split_teams(cur_na)
                rec["name"] = cur_na
                rec["home_team"], rec["away_team"] = h, a
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
                rec["home_score"], rec["away_score"] = h, a
                if old and old != cur_ss:
                    score_changed = True
                    logger.success(f"[GOAL] FI={mid} {old}→{cur_ss}")
            if cur_tm:
                try:
                    rec["minute"] = int(float(cur_tm))
                except Exception:
                    pass
            rec["updated"] = time.time()
            self.matches[mid] = rec
            self._last_message_at = time.time()
            if score_changed and self.callback:
                asyncio.create_task(self._safe_cb(rec))
            cur_ss = cur_na = None

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
        except Exception:
            if self._want_run:
                await self._tg("⚠️ Bet365 CDP offline — start Chrome with port 9222")
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
                        if any(x in text for x in ("NA=", "FI=", "SS=", "OV", "ID=")):
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
                await self._tg("⚠️ Bet365 403 — change Proton")
                return False

            await asyncio.sleep(2)
            for sel in ("text=Football", "text=Soccer"):
                if not self._want_run:
                    return False
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=2000)
                        await asyncio.sleep(1.5)
                        break
                except Exception:
                    pass

            self.running = True
            await self._tg("✅ Bet365 feed ON (football only)")

            while self._want_run:
                if got_403:
                    await self._tg("⚠️ Bet365 403")
                    return False
                if self._last_message_at and (
                    time.time() - self._last_message_at > WS_IDLE_SECS
                ):
                    return False  # reconnect only if still want_run
                await asyncio.sleep(1)
            return True
        except asyncio.CancelledError:
            return False
        except Exception as e:
            if self._want_run:
                logger.error(f"[BET365] {e}")
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
        logger.info("[BET365] fully stopped")

    async def start(self):
        if self._want_run and self._task and not self._task.done():
            return
        self._want_run = True
        self.running = False
        self._task = asyncio.create_task(self._supervisor())
        logger.info("[BET365] start requested")

    async def stop(self):
        """Hard stop: no frames, no announce, no reconnect."""
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
        logger.info("[BET365] stop done")
