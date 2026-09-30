"""Bet365 WS — same tab only.
Idle → fast goto clean #/IP/B1 (commit + short data wait).
No hard reload. No Telegram spam (backoff after fail).
"""

import asyncio
import re
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from loguru import logger
from playwright.async_api import async_playwright

from config import Config

CDP_URL = "http://127.0.0.1:9222"
DEFAULT_LIVE = "https://www.bet365.com/#/IP/B1"

KEEPALIVE_SECS = 25
STUCK_SECS = 25
FAIL_BACKOFF_SECS = 180
MAX_FAILS_THEN_LONG_PAUSE = 3
LONG_PAUSE_SECS = 600
IDLE_CHECK_TICK = 0.4
DATA_WAIT_SECS = 8              # fast: was 20s

RE_FI = re.compile(r"(?:^|[|;,\s])FI=(\d{6,})", re.I)
RE_ID = re.compile(r"(?:^|[|;,\s])ID=(\d{6,})", re.I)
RE_NA = re.compile(r"(?:^|[|;,\s])NA=([^|;]+)", re.I)
RE_SS = re.compile(r"(?:^|[|;,\s])SS=(\d{1,2}\s*[-:]\s*\d{1,2})", re.I)
RE_OV = re.compile(r"OV(\d{6,})C\d+", re.I)

_OBVIOUS_NON_FOOTBALL = re.compile(
    r"\btennis\b|\bvolleyball\b|\bbasketball\b|\btable\s*tennis\b|"
    r"\bdarts\b|\bsnooker\b|\bbadminton\b|\bice\s*hockey\b|"
    r"\brugby\b|\bcricket\b|\bbaseball\b|\bboxing\b|\bmma\b|"
    r"\besports?\b|\be-?football\b|\bvirtual\b|\bfifa\s*\d{2,}\b|\bpes\s*\d{2,}\b|"
    r"\bgt\s*league\b|\bh2h\s*gg\b|\bliga\s*pro\b",
    re.I,
)


def _split_teams(na: str):
    for sep in (r"\s+vs\.?\s+", r"\s+v\.?\s+", r"\s+@\s+"):
        parts = re.split(sep, (na or "").strip(), maxsplit=1, flags=re.I)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    return (na or "").strip(), ""


def _is_probably_football(name: str) -> bool:
    if not name:
        return False
    if not re.search(r"\s+v(?:s)?\.?\s+|\s+@\s+", name, re.I):
        return False
    if _OBVIOUS_NON_FOOTBALL.search(name):
        return False
    return True


class Bet365Feed:
    def __init__(self, callback: Optional[Callable] = None):
        self.callback = callback
        self.matches: Dict[str, dict] = {}
        self.running = False
        self._want_run = False
        self._last_message_at: Optional[float] = None
        self._last_frame_at: Optional[float] = None
        self._last_data_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self.alerter = None
        self._page = None
        self._browser = None
        self._playwright = None
        self._last_alert_at = 0.0
        self._announced: set = set()
        self._got_403 = False
        self._handlers_bound_for = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._reloading = False
        self._open_ws: set = set()
        self._last_recover_at = 0.0
        self._fail_streak = 0
        self._next_recover_ok_at = 0.0
        self._told_fail = False

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_callback(self, callback: Callable):
        self.callback = callback

    async def _tg(self, text: str, min_gap: float = 3.0):
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
        if self._last_data_at is None:
            return False
        return (time.time() - self._last_data_at) < 60

    def get_matches(self) -> List[dict]:
        return list(self.matches.values())

    def get_match(self, match_id: str) -> Optional[dict]:
        return self.matches.get(str(match_id))

    def _live_url(self) -> str:
        try:
            page = self._page
            if page and not page.is_closed():
                u = page.url or ""
                if "bet365" in u.lower():
                    p = urlparse(u)
                    if p.scheme and p.netloc:
                        return f"{p.scheme}://{p.netloc}/#/IP/B1"
        except Exception:
            pass
        return getattr(Config, "BET365_LIVE_URL", None) or DEFAULT_LIVE

    async def _announce(self, fi: str, name: str, ss: str):
        if not self.running or not self._want_run:
            return
        if fi in self._announced:
            return
        self._announced.add(fi)
        if self.alerter:
            try:
                await self.alerter.send(
                    f"⚽ <b>FOOTBALL</b>\n"
                    f"🆔 <code>{fi}</code>\n"
                    f"📋 {name}\n"
                    f"📊 {ss or '?'}"
                )
            except Exception:
                pass

    def _parse_frame(self, text: str):
        if not self.running or not self._want_run:
            return
        if not text or len(text) < 8:
            return

        parts = re.split(r"[|\x01\x08]", text)
        current_na = None
        current_fi = None
        current_ss = None

        for part in parts:
            part = part.strip()
            if not part:
                continue

            for m in RE_NA.finditer(part):
                na = m.group(1).strip()
                if re.search(r"\bv\b|\bvs\b|@", na, re.I) or " v " in na.lower():
                    current_na = na

            for m in RE_FI.finditer(part):
                current_fi = m.group(1)
            for m in RE_ID.finditer(part):
                if not current_fi:
                    current_fi = m.group(1)
            for m in RE_OV.finditer(part):
                if not current_fi:
                    current_fi = m.group(1)

            for m in RE_SS.finditer(part):
                current_ss = re.sub(r"\s+", "", m.group(1).replace(":", "-"))

            if current_fi and (current_na or current_ss):
                prev = self.matches.get(current_fi, {})
                name = current_na or prev.get("name") or ""
                ss = current_ss or prev.get("ss") or ""

                if name or ss:
                    changed = prev.get("ss") != ss or prev.get("name") != name

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

                    if changed and name and _is_probably_football(name):
                        asyncio.create_task(self._announce(current_fi, name, ss))
                        logger.info(f"[FOOTBALL] FI={current_fi} {name}")

                    if (
                        ss
                        and prev.get("ss")
                        and ss != prev.get("ss")
                        and self.callback
                        and name
                        and _is_probably_football(name)
                    ):
                        try:
                            h, a = map(int, ss.split("-"))
                            self.matches[current_fi]["home_score"] = h
                            self.matches[current_fi]["away_score"] = a
                        except Exception:
                            pass
                        asyncio.create_task(self._safe_cb(self.matches[current_fi]))

                current_na = None
                current_ss = None

        self._last_message_at = time.time()
        self._last_data_at = time.time()
        if self._fail_streak:
            self._fail_streak = 0
            self._told_fail = False

    async def _safe_cb(self, rec: dict):
        if not self.running or not self._want_run or not self.callback:
            return
        try:
            await self.callback(dict(rec))
        except Exception:
            pass

    def _bind_page_handlers(self, page):
        if self._handlers_bound_for is page:
            return

        def on_response(resp):
            try:
                if resp.status == 403:
                    self._got_403 = True
            except Exception:
                pass

        def on_websocket(ws):
            url = (getattr(ws, "url", "") or "").lower()
            live = "zap" in url or "sportspublisher" in url
            key = id(ws)
            if live:
                self._open_ws.add(key)

            def on_frame(ev):
                self._last_frame_at = time.time()
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

            def on_close(*_):
                self._open_ws.discard(key)

            ws.on("framereceived", on_frame)
            ws.on("close", on_close)

        page.on("response", on_response)
        page.on("websocket", on_websocket)
        self._handlers_bound_for = page

    async def _pick_existing_page(self):
        if not self._browser or not self._browser.contexts:
            return None
        ctx = self._browser.contexts[0]
        for p in ctx.pages:
            try:
                u = (p.url or "").lower()
                if "bet365" in u and not p.is_closed():
                    return p
            except Exception:
                continue
        if self._page and not self._page.is_closed():
            return self._page
        if ctx.pages:
            return ctx.pages[0]
        return None

    async def _recover_live(self) -> bool:
        """Fast: commit goto + short data poll. No hard reload. Backoff on fail."""
        now = time.time()
        if self._reloading:
            return False
        if now < self._next_recover_ok_at:
            return False

        page = self._page
        if not page or page.is_closed():
            page = await self._pick_existing_page()
            self._page = page
        if not page or page.is_closed():
            if not self._told_fail:
                await self._tg("⚠️ Bet365 — no tab (CDP 9222).")
                self._told_fail = True
            self._next_recover_ok_at = now + FAIL_BACKOFF_SECS
            return False

        live = self._live_url()
        self._reloading = True
        self._got_403 = False
        self._last_recover_at = now
        marker = self._last_data_at or 0.0

        try:
            logger.warning(f"[BET365] idle → fast goto {live}")
            if self._fail_streak == 0:
                await self._tg(f"🔄 Bet365 idle → {live}")

            self._bind_page_handlers(page)
            try:
                await page.bring_to_front()
            except Exception:
                pass

            # FAST: commit only (URL changes immediately)
            await page.goto(live, wait_until="commit", timeout=15000)
            try:
                await page.evaluate("() => { location.hash = '#/IP/B1'; }")
            except Exception:
                pass

            if self._got_403:
                await self._tg("⚠️ Bet365 403 — change VPN")
                self._fail_streak += 1
                self._next_recover_ok_at = now + LONG_PAUSE_SECS
                self._told_fail = True
                return False

            # FAST: poll data max DATA_WAIT_SECS (8s)
            deadline = time.time() + DATA_WAIT_SECS
            while time.time() < deadline:
                if self._last_data_at and self._last_data_at > marker:
                    logger.success("[BET365] data after fast goto")
                    self._fail_streak = 0
                    self._told_fail = False
                    self._next_recover_ok_at = 0.0
                    await self._tg("✅ Bet365 data back")
                    return True
                await asyncio.sleep(0.2)

            self._fail_streak += 1
            if self._fail_streak >= MAX_FAILS_THEN_LONG_PAUSE:
                self._next_recover_ok_at = time.time() + LONG_PAUSE_SECS
                if not self._told_fail:
                    await self._tg(
                        "❌ Bet365 quiet after live URL (×3)\n"
                        "Paused 10 min. Check VPN / live tab."
                    )
                    self._told_fail = True
            else:
                self._next_recover_ok_at = time.time() + FAIL_BACKOFF_SECS
                if self._fail_streak == 1:
                    await self._tg(
                        "❌ Bet365 still quiet after live URL\n"
                        f"Retry in {FAIL_BACKOFF_SECS // 60} min (no spam)"
                    )
            return False
        except Exception as e:
            logger.error(f"[BET365] recover: {e}")
            self._fail_streak += 1
            self._next_recover_ok_at = time.time() + FAIL_BACKOFF_SECS
            return False
        finally:
            self._reloading = False

    async def _keep_awake_once(self):
        page = self._page
        if not page or page.is_closed() or self._reloading:
            return
        try:
            await page.bring_to_front()
        except Exception:
            pass
        try:
            await page.mouse.wheel(0, 60)
            await asyncio.sleep(0.1)
            await page.mouse.wheel(0, -60)
        except Exception:
            pass

    async def _keepalive_loop(self):
        try:
            while self._want_run:
                await asyncio.sleep(KEEPALIVE_SECS)
                if not self._want_run:
                    break
                await self._keep_awake_once()
        except asyncio.CancelledError:
            pass

    async def _connect_cdp_once(self) -> bool:
        if self._browser:
            try:
                _ = self._browser.contexts
                return True
            except Exception:
                pass
            await self._detach_cdp()

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(CDP_URL)
            return True
        except Exception as e:
            logger.warning(f"[BET365] CDP: {e}")
            if self._want_run:
                await self._tg("⚠️ Bet365 CDP offline")
            return False

    async def _detach_cdp(self):
        self._page = None
        self._browser = None
        self._handlers_bound_for = None
        self._open_ws.clear()
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = None

    async def _run_loop(self):
        if not await self._connect_cdp_once():
            return

        page = await self._pick_existing_page()
        if not page:
            await self._tg("⚠️ No Bet365 tab — open live football on CDP Chrome")
            return
        self._page = page

        self._got_403 = False
        self._last_frame_at = None
        self._last_data_at = None
        self._fail_streak = 0
        self._told_fail = False
        self._next_recover_ok_at = 0.0

        try:
            await page.bring_to_front()
        except Exception:
            pass

        self._bind_page_handlers(page)

        try:
            u = (page.url or "").lower()
            if "bet365" not in u or "ip" not in u:
                live = self._live_url()
                await page.goto(live, wait_until="commit", timeout=15000)
                await asyncio.sleep(0.8)
                if self._got_403:
                    await self._tg("⚠️ Bet365 403 — change VPN")
                    return
        except Exception as e:
            logger.error(f"[BET365] initial nav: {e}")
            return

        self.running = True
        now = time.time()
        self._last_message_at = now
        self._last_frame_at = now
        self._last_data_at = now
        logger.success("[BET365] UP — fast recover")
        await self._tg(
            "✅ Bet365 ON\n"
            f"Live: {self._live_url()}\n"
            "Idle → fast #/IP/B1 (no spam)"
        )

        if not self._keepalive_task or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        while self._want_run:
            if self._got_403:
                await self._tg("⚠️ Bet365 403 — change VPN")
                break

            if not self._reloading and self._last_data_at:
                data_idle = time.time() - self._last_data_at
                if data_idle >= STUCK_SECS and time.time() >= self._next_recover_ok_at:
                    await self._recover_live()

            await asyncio.sleep(IDLE_CHECK_TICK)

        self.running = False

    async def _supervisor(self):
        while self._want_run:
            try:
                await self._run_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[BET365] loop: {e!r}")
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
        kt = self._keepalive_task
        self._keepalive_task = None
        if kt and not kt.done():
            kt.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(kt), timeout=2.0)
            except Exception:
                pass
        await self._detach_cdp()
