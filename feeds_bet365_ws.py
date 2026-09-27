"""Bet365 WS — same tab only. Idle WS -> hard reload (cache-bypass) immediately. Never new tab."""

import asyncio
import re
import time
from typing import Callable, Dict, List, Optional

from loguru import logger
from playwright.async_api import async_playwright

from config import Config

CDP_URL = "http://127.0.0.1:9222"
# Was 90s — dropped hard so a dead feed is caught almost immediately.
WS_IDLE_SECS = 10
# Was 25s — each reload attempt now fails fast so retries happen sooner
# instead of burning most of a minute on one stuck attempt.
WS_RESUME_WAIT_SECS = 6
# Every 10 minutes, nudge the page with a tiny scroll so Chrome doesn't
# treat it as fully idle/backgrounded while it's not the visible tab.
KEEPALIVE_SECS = 600
# Main loop's own idle-check cadence — was 0.5s, tightened so idle
# detection isn't delayed by its own polling interval.
IDLE_CHECK_TICK = 0.2

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
        # Tracks which exact page object handlers are currently bound to,
        # so we never stack duplicate listeners on repeated reloads.
        self._handlers_bound_for = None
        # Keep-alive: periodic tiny scroll task, separate from the main loop.
        self._keepalive_task: Optional[asyncio.Task] = None

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
        """Attach WS + 403 listeners to the same page (no new tab).

        Skips entirely if already bound to this exact page object —
        Playwright page listeners survive navigation, so there's no need
        to re-bind after a reload of the SAME page (and re-binding with
        freshly created closures can never be removed later, since
        remove_listener needs the exact same function reference).
        """
        if self._handlers_bound_for is page:
            return

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

        page.on("response", on_response)
        page.on("websocket", on_websocket)
        self._handlers_bound_for = page

    async def _pick_existing_page(self):
        """Use existing Bet365 tab only — never open a new tab for reconnect."""
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
        return await ctx.new_page()

    async def _wait_for_ws_resume(self, timeout: float = WS_RESUME_WAIT_SECS) -> bool:
        """True only if real frames arrive after reload (updates _last_message_at).
        Polls at 0.2s instead of 0.4s so a quick recovery is noticed fast."""
        deadline = time.time() + timeout
        marker = self._last_message_at or 0.0
        while time.time() < deadline:
            if self._got_403:
                return False
            if self._last_message_at and self._last_message_at > marker:
                return True
            await asyncio.sleep(0.2)
        return bool(self._last_message_at and self._last_message_at > marker)

    async def _reload_same_page(self) -> bool:
        """Refresh SAME tab when WS is quiet. Success only if frames resume.

        CONFIRMED WORKING (standalone test, 2026-09-27): a plain
        page.reload() can leave the tab stuck on a loading spinner with no
        WS ever reopening. What actually fixes it is a HARD reload
        (cache bypassed) via CDP Page.stopLoading + Page.reload({
        ignoreCache: True}), combined with bring_to_front() so the tab
        isn't frozen/backgrounded. Retried up to 3 times if a given
        attempt still doesn't bring frames back, mirroring what worked
        manually (click refresh again if the first click doesn't help).

        SPEED (this version): WS_IDLE_SECS and WS_RESUME_WAIT_SECS were
        both cut down sharply — idle detection used to wait 90s before
        even trying, and each attempt could wait up to 25s for frames.
        Odds move fast right before/after a goal, so the whole recovery
        chain now aims to be seconds, not minutes.
        """
        page = self._page
        if not page or page.is_closed():
            page = await self._pick_existing_page()
            self._page = page
        if not page or page.is_closed():
            logger.error("[BET365] reload aborted — no page")
            await self._tg("⚠️ Bet365 reload failed — no tab")
            return False

        url = getattr(Config, "BET365_LIVE_URL", "https://www.bet365.com/#/IP/B1")
        self._got_403 = False
        before = self._last_message_at

        try:
            await page.bring_to_front()
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.warning(f"[BET365] bring_to_front failed: {e}")

        self._bind_page_handlers(page)

        for attempt in range(1, 4):  # up to 3 hard-reload attempts
            logger.warning(f"[BET365] WS idle → hard reload attempt {attempt}/3")
            if attempt == 1:
                await self._tg("🔄 Bet365 WS idle → hard reload...")

            try:
                cdp = await page.context.new_cdp_session(page)
                await cdp.send("Page.stopLoading")
                await asyncio.sleep(0.1)
                await cdp.send("Page.reload", {"ignoreCache": True})
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass  # even if this times out, we still check for frames below
                try:
                    await cdp.detach()
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[BET365] CDP hard reload failed: {e}")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=20000)
                except Exception as e2:
                    logger.warning(f"[BET365] reload failed, goto same tab: {e2}")
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e3:
                        logger.error(f"[BET365] goto failed: {e3}")
                        continue  # try next attempt

            if self._got_403:
                await self._tg("⚠️ Bet365 403 after reload — change VPN")
                return False

            try:
                await page.bring_to_front()
            except Exception:
                pass

            await asyncio.sleep(0.8)
            for sel in ("text=Football", "text=Soccer"):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=1500)
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    pass

            resumed = await self._wait_for_ws_resume(WS_RESUME_WAIT_SECS)
            if resumed:
                logger.success(
                    f"[BET365] WS frames resumed after hard reload (attempt {attempt})"
                )
                await self._tg("✅ Bet365 WS resumed after hard reload")
                return True

            logger.warning(f"[BET365] attempt {attempt} — still stuck/no frames, retrying")
            await asyncio.sleep(0.3)

        # All attempts failed — honest failure, do not fake activity.
        if before:
            self._last_message_at = before
        logger.error("[BET365] all hard-reload attempts failed — NO WS frames")
        await self._tg(
            "❌ Bet365 hard-reloaded 3x but still no live data\n"
            "Check VPN / open live football on that tab"
        )
        return False

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
            logger.warning(f"[BET365] CDP connect failed: {e}")
            if self._want_run:
                await self._tg("⚠️ Bet365 CDP offline")
            return False

    async def _detach_cdp(self):
        """Detach only — do NOT close user's Chrome tabs."""
        self._page = None
        self._browser = None
        self._handlers_bound_for = None
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = None

    # ── keep-alive: tiny periodic scroll so the tab doesn't go fully idle ──

    async def _keepalive_scroll(self):
        """One small scroll down, then back up. Does not touch focus,
        does not navigate, does not affect betting/watching in any way —
        it only exists to give the page a little activity so Chrome is
        less likely to freeze it while it sits in the background."""
        page = self._page
        if not page or page.is_closed():
            return
        try:
            await page.mouse.wheel(0, 120)
            await asyncio.sleep(0.2)
            await page.mouse.wheel(0, -120)
            logger.info("[BET365] keep-alive scroll done")
        except Exception as e:
            logger.warning(f"[BET365] keep-alive scroll failed: {e}")

    async def _keepalive_loop(self):
        """Runs independently of the main session loop. Fires every
        KEEPALIVE_SECS while the feed is supposed to be running, regardless
        of whether WS frames are flowing — this is purely about keeping
        the tab 'awake', not about detecting idle WS (that's WS_IDLE_SECS'
        job elsewhere)."""
        try:
            while self._want_run:
                await asyncio.sleep(KEEPALIVE_SECS)
                if not self._want_run:
                    break
                await self._keepalive_scroll()
        except asyncio.CancelledError:
            pass

    async def _run_loop(self):
        """One long session: same tab; on idle → hard reload same tab immediately."""
        if not await self._connect_cdp_once():
            return

        page = await self._pick_existing_page()
        if not page:
            logger.error("[BET365] no page available")
            return
        self._page = page

        url = getattr(Config, "BET365_LIVE_URL", "https://www.bet365.com/#/IP/B1")
        self._got_403 = False

        try:
            await page.bring_to_front()
        except Exception:
            pass

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
        await self._tg("✅ Bet365 ON — same tab; idle = fast hard reload")

        # Start the keep-alive scroller alongside this session, if not
        # already running (it's independent of reload/reconnect cycles).
        if not self._keepalive_task or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        while self._want_run:
            if self._got_403:
                await self._tg("⚠️ Bet365 403")
                break

            if self._last_message_at and (
                time.time() - self._last_message_at > WS_IDLE_SECS
            ):
                ok = await self._reload_same_page()
                if not ok:
                    await self._detach_cdp()
                    if not await self._connect_cdp_once():
                        await asyncio.sleep(1)
                        continue
                    page = await self._pick_existing_page()
                    self._page = page
                    if page:
                        await self._reload_same_page()
                continue

            # Was 0.5s — tightened so idle detection isn't itself a
            # bottleneck on top of WS_IDLE_SECS.
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
            await asyncio.sleep(1)
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
