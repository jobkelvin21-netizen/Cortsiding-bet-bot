"""Bet365 WS — same tab only. Idle WS → page.reload() immediately. Never new tab."""

import asyncio
import re
import time
from typing import Callable, Dict, List, Optional

from loguru import logger
from playwright.async_api import async_playwright

from config import Config

CDP_URL = "http://127.0.0.1:9222"
WS_IDLE_SECS = 90

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
        self._task: Optional[asyncio.Task] = None
        self.alerter = None
        self._page = None
        self._browser = None
        self._playwright = None
        self._last_alert_at = 0.0
        self._announced: set = set()
        self._got_403 = False

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

    async def _safe_cb(self, rec: dict):
        if not self.running or not self._want_run or not self.callback:
            return
        try:
            await self.callback(dict(rec))
        except Exception:
            pass

    def _bind_page_handlers(self, page):
        """Attach WS + 403 listeners to the same page (no new tab)."""

        def on_response(resp):
            try:
                if resp.status == 403:
                    self._got_403 = True
            except Exception:
                pass

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

        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            page.remove_listener("websocket", on_websocket)
        except Exception:
            pass
        page.on("response", on_response)
        page.on("websocket", on_websocket)

    async def _pick_existing_page(self):
        """Use existing Bet365 tab only — never open a new tab for reconnect."""
        if not self._browser or not self._browser.contexts:
            return None
        ctx = self._browser.contexts[0]
        # Prefer tab already on bet365
        for p in ctx.pages:
            try:
                u = (p.url or "").lower()
                if "bet365" in u and not p.is_closed():
                    return p
            except Exception:
                continue
        # Reuse our held page if still open
        if self._page and not self._page.is_closed():
            return self._page
        # First open only: use first page if any, else one new page once
        if ctx.pages:
            return ctx.pages[0]
        return await ctx.new_page()

    async def _reload_same_page(self) -> bool:
        """Immediately refresh the same page when WS goes quiet."""
        page = self._page
        if not page or page.is_closed():
            page = await self._pick_existing_page()
            self._page = page
        if not page or page.is_closed():
            return False

        url = getattr(Config, "BET365_LIVE_URL", "https://www.bet365.com/#/IP/B1")
        self._got_403 = False
        self._bind_page_handlers(page)

        try:
            # Fast path: reload current tab (what the video shows)
            logger.info("[BET365] WS idle → reload same tab")
            await page.reload(wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            logger.warning(f"[BET365] reload failed, goto same tab: {e}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e2:
                logger.error(f"[BET365] goto failed: {e2}")
                return False

        if self._got_403:
            await self._tg("⚠️ Bet365 403")
            return False

        await asyncio.sleep(1.5)
        for sel in ("text=Football", "text=Soccer"):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click(timeout=2000)
                    await asyncio.sleep(1.0)
                    break
            except Exception:
                pass

        self._last_message_at = time.time()  # reset idle clock after refresh
        logger.success("[BET365] same tab refreshed — listening again")
        return True

    async def _connect_cdp_once(self) -> bool:
        if self._browser:
            try:
                # still alive?
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
            logger.warning(f"[BET365] CDP connect failed: {e}")
            if self._want_run:
                await self._tg("⚠️ Bet365 CDP offline")
            return False

    async def _detach_cdp(self):
        """Detach only — do NOT close user's Chrome tabs."""
        self._page = None
        self._browser = None
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = None

    async def _run_loop(self):
        """One long session: same tab; on idle → reload same tab immediately."""
        if not await self._connect_cdp_once():
            return

        page = await self._pick_existing_page()
        if not page:
            logger.error("[BET365] no page available")
            return
        self._page = page

        url = getattr(Config, "BET365_LIVE_URL", "https://www.bet365.com/#/IP/B1")
        self._got_403 = False
        self._bind_page_handlers(page)

        try:
            u = (page.url or "").lower()
            if "bet365" not in u or "/ip/" not in u:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                if (resp and resp.status == 403) or self._got_403:
                    await self._tg("⚠️ Bet365 403")
                    return
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
        except Exception as e:
            logger.error(f"[BET365] initial nav: {e}")
            return

        self.running = True
        self._last_message_at = time.time()
        logger.success("[BET365] session UP — same tab only")
        await self._tg("✅ Bet365 ON — same tab; idle = refresh only")

        while self._want_run:
            if self._got_403:
                await self._tg("⚠️ Bet365 403")
                break

            # WS quiet → immediate reload of SAME tab (no new tab)
            if self._last_message_at and (
                time.time() - self._last_message_at > WS_IDLE_SECS
            ):
                ok = await self._reload_same_page()
                if not ok:
                    # CDP may have died — reattach, still same-tab policy
                    await self._detach_cdp()
                    if not await self._connect_cdp_once():
                        await asyncio.sleep(2)
                        continue
                    page = await self._pick_existing_page()
                    self._page = page
                    if page:
                        await self._reload_same_page()
                continue

            await asyncio.sleep(0.5)

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
            await asyncio.sleep(2)
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
        await self._detach_cdp()
