"""Bet365 live feed: goals for watched FI + Telegram FI/name list (once per FI per day)."""
import asyncio
import os
import re
import shutil
import subprocess
import time
import urllib.request
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger
from config import Config

CHROME = os.getenv("BET365_CHROME", "/usr/bin/google-chrome")
RUN_AS = os.getenv("BET365_RUN_AS", "botuser")
DISPLAY = os.getenv("BET365_DISPLAY", ":77")
TZ = os.getenv("BET365_TZ", "Europe/Amsterdam")
CDP_PORT = int(os.getenv("BET365_CDP_PORT", "9333"))
STALL_SECS = float(os.getenv("BET365_STALL_SECS", "90"))
FIRST_DATA_SECS = float(os.getenv("BET365_FIRST_DATA_SECS", "120"))
DISALLOW_CONFIRM = float(os.getenv("DISALLOW_CONFIRM_SECS", "0.6"))
LIST_FLUSH_SECS = float(os.getenv("BET365_LIST_FLUSH_SECS", "12"))

RE_FI = re.compile(r"(?:^|[|;,\s])FI=(\d{6,})", re.I)
RE_ID = re.compile(r"(?:^|[|;,\s])ID=(\d{6,})", re.I)
RE_NA = re.compile(r"(?:^|[|;,\s])NA=([^|;]+)", re.I)
RE_SS = re.compile(r"(?:^|[|;,\s])SS=(\d{1,2}\s*[-:]\s*\d{1,2})", re.I)
RE_OV = re.compile(r"OV(\d{6,})C\d+", re.I)
SPLIT = re.compile(r"[|\x01\x08]")
HAS_VS = re.compile(r"\bv\b|\bvs\b|@", re.I)


class ScoreTracker:
    def __init__(self):
        self.state: Dict[str, dict] = {}

    def feed(self, text: str) -> List[Tuple[str, int, int, int, int, str]]:
        changes = []
        cur_na = cur_fi = cur_ss = None
        for part in SPLIT.split(text):
            part = part.strip()
            if not part:
                continue
            for m in RE_NA.finditer(part):
                na = m.group(1).strip()
                if HAS_VS.search(na) or " v " in na.lower():
                    cur_na = na
            for m in RE_FI.finditer(part):
                cur_fi = m.group(1)
            for m in RE_ID.finditer(part):
                if not cur_fi:
                    cur_fi = m.group(1)
            for m in RE_OV.finditer(part):
                if not cur_fi:
                    cur_fi = m.group(1)
            for m in RE_SS.finditer(part):
                cur_ss = re.sub(r"\s+", "", m.group(1).replace(":", "-"))
            if cur_fi and (cur_na or cur_ss):
                prev = self.state.get(cur_fi) or {}
                name = cur_na or prev.get("name", "")
                ss = cur_ss or prev.get("ss", "")
                h, a = prev.get("h"), prev.get("a")
                if cur_ss:
                    try:
                        h, a = map(int, cur_ss.split("-"))
                    except ValueError:
                        cur_ss = None
                        ss = prev.get("ss", "")
                if cur_ss and prev.get("ss") and prev["ss"] != cur_ss:
                    changes.append((cur_fi, prev["h"], prev["a"], h, a, name))
                self.state[cur_fi] = {
                    "name": name, "ss": ss, "h": h, "a": a, "updated": time.time()
                }
                cur_na = cur_ss = None
        return changes


class Bet365Feed:
    def __init__(self, callback: Optional[Callable] = None):
        self.callback = callback
        self.alerter = None
        self.tracker = ScoreTracker()
        self.running = False
        self._want_run = False
        self._task = self._list_task = None
        self._watch: Optional[str] = None
        self._goals_seen: set = set()
        self._last_data_at: Optional[float] = None
        self._last_alert_at = 0.0
        self._pw = self._browser = self._page = self._proc = None
        self._profile: Optional[str] = None
        self._cooldown = 0.0
        self._announced_day: Optional[str] = None
        self._announced: set = set()
        self._pending_tg: List[str] = []
        self._initial_dump_done = False

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_callback(self, callback: Callable):
        self.callback = callback

    def watch(self, fi: str) -> Optional[dict]:
        self._watch = str(fi)
        self._goals_seen = set()
        return self.get_match(self._watch)

    def unwatch(self):
        self._watch = None
        self._goals_seen = set()

    def get_match(self, fi: str) -> Optional[dict]:
        s = self.tracker.state.get(str(fi))
        if not s:
            return None
        return {
            "match_id": str(fi),
            "name": s["name"],
            "ss": s["ss"],
            "home_score": s["h"] or 0,
            "away_score": s["a"] or 0,
        }

    def is_healthy(self) -> bool:
        return bool(
            self.running and self._last_data_at and time.time() - self._last_data_at < 120
        )

    def _roll_day(self):
        today = date.today().isoformat()
        if self._announced_day != today:
            self._announced_day = today
            self._announced.clear()
            self._initial_dump_done = False

    def _queue_match_line(self, fi: str, name: str, ss: str):
        self._roll_day()
        if not fi or not name or fi in self._announced:
            return
        if not HAS_VS.search(name) and " v " not in name.lower():
            return
        self._announced.add(fi)
        self._pending_tg.append(f"<code>{fi}</code>  {ss or '?'}  {name}")

    def _queue_all_current(self):
        self._roll_day()
        for fi, s in sorted(
            self.tracker.state.items(), key=lambda x: (x[1].get("name") or "")
        ):
            self._queue_match_line(fi, s.get("name") or "", s.get("ss") or "")

    async def _list_flusher(self):
        while self._want_run:
            await asyncio.sleep(LIST_FLUSH_SECS)
            if not self._initial_dump_done and self.running and self.tracker.state:
                self._queue_all_current()
                self._initial_dump_done = True
            if not self._pending_tg or not self.alerter:
                continue
            chunk = self._pending_tg[:40]
            del self._pending_tg[:40]
            try:
                await self.alerter.send(
                    "📋 <b>Bet365 live (FI + name)</b>\n" + "\n".join(chunk)
                )
            except Exception:
                pass

    async def send_full_match_list(self):
        if not self.alerter:
            return
        rows = []
        for fi, s in sorted(
            self.tracker.state.items(), key=lambda x: (x[1].get("name") or "")
        ):
            name = s.get("name") or ""
            if not name:
                continue
            if not HAS_VS.search(name) and " v " not in name.lower():
                continue
            rows.append(f"<code>{fi}</code>  {s.get('ss') or '?'}  {name}")
        if not rows:
            await self.alerter.send("No named matches yet — wait for Bet365 feed UP")
            return
        for i in range(0, len(rows), 40):
            await self.alerter.send(
                "📋 <b>Bet365 live (FI + name)</b>\n" + "\n".join(rows[i : i + 40])
            )

    async def start(self):
        if self._want_run and self._task and not self._task.done():
            return
        self._want_run = True
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._want_run = False
        self.running = False
        for t in (self._list_task, self._task):
            if t and not t.done():
                t.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(t), timeout=3.0)
                except Exception:
                    pass
        self._list_task = self._task = None
        await self._teardown()

    def _on_frame(self, ev):
        try:
            payload = getattr(ev, "payload", ev)
            text = (
                payload.decode("utf-8", "ignore")
                if isinstance(payload, (bytes, bytearray))
                else str(payload)
            )
        except Exception:
            return
        has_ss = "SS=" in text
        if has_ss or "\x15OV" in text or "\x14OV" in text:
            self._last_data_at = time.time()
        if has_ss:
            changes = self.tracker.feed(text)
            for fi, s in list(self.tracker.state.items()):
                name = s.get("name") or ""
                if name:
                    self._queue_match_line(fi, name, s.get("ss") or "")
            self._on_changes(changes)

    def _on_changes(self, changes):
        for fi, ph, pa, h, a, name in changes:
            if fi != self._watch:
                continue
            pt, t = ph + pa, h + a
            base = {
                "match_id": fi,
                "name": name,
                "prev_ss": f"{ph}-{pa}",
                "ss": f"{h}-{a}",
                "home_score": h,
                "away_score": a,
                "ts": time.time(),
            }
            if t > pt:
                if t in self._goals_seen:
                    continue
                dh, da = h - ph, a - pa
                scorer = (
                    "home"
                    if dh > 0 and da <= 0
                    else "away"
                    if da > 0 and dh <= 0
                    else "home"
                    if dh > da
                    else "away"
                    if da > dh
                    else ""
                )
                if not scorer:
                    logger.warning(
                        f"[BET365] goal scorer unclear {ph}-{pa} -> {h}-{a}"
                    )
                    continue
                self._goals_seen.add(t)
                self._emit(
                    {
                        **base,
                        "event": "goal",
                        "scorer": scorer,
                        "goal_no": pt + 1,
                        "goals_added": t - pt,
                    }
                )
            elif t < pt:
                asyncio.get_running_loop().call_later(
                    DISALLOW_CONFIRM, self._confirm_disallowed, fi, pt, dict(base)
                )

    def _confirm_disallowed(self, fi: str, prev_total: int, base: dict):
        s = self.tracker.state.get(fi) or {}
        if (s.get("h") or 0) + (s.get("a") or 0) >= prev_total or fi != self._watch:
            return
        self._goals_seen = {x for x in self._goals_seen if x < prev_total}
        base = {
            **base,
            "ss": s.get("ss", base["ss"]),
            "home_score": s.get("h"),
            "away_score": s.get("a"),
        }
        self._emit({**base, "event": "disallowed"})

    def _emit(self, evt: dict):
        logger.info(
            f"[BET365] {evt['event']} {evt.get('scorer', '')} "
            f"{evt['prev_ss']} -> {evt['ss']}"
        )
        # Locked FI → Telegram immediately
        if self.alerter and evt.get("event") in ("goal", "disallowed"):
            try:
                loop = asyncio.get_running_loop()
                if evt["event"] == "goal":
                    msg = (
                        f"⚡ <b>GOAL</b> FI=<code>{evt.get('match_id')}</code>\n"
                        f"⚽ {evt.get('name') or '?'}\n"
                        f"🎯 {evt.get('scorer')}  {evt.get('prev_ss')} → {evt.get('ss')}\n"
                        f"#{evt.get('goal_no')}"
                    )
                else:
                    msg = (
                        f"🚫 <b>DISALLOWED</b> FI=<code>{evt.get('match_id')}</code>\n"
                        f"⚽ {evt.get('name') or '?'}\n"
                        f"{evt.get('prev_ss')} → {evt.get('ss')}"
                    )
                loop.create_task(self.alerter.send(msg))
            except Exception as e:
                logger.error(f"[BET365] tg goal: {e}")

        if not self.callback:
            return
        try:
            res = self.callback(evt)
            if asyncio.iscoroutine(res):
                asyncio.ensure_future(res)
        except Exception as e:
            logger.error(f"[BET365] callback: {e}")

    def _bind(self, page):
        def on_ws(ws):
            url = (getattr(ws, "url", "") or "").lower()
            if "zap" in url or "sportspublisher" in url:
                ws.on("framereceived", self._on_frame)

        page.on("websocket", on_ws)

    async def _tg(self, text: str, min_gap: float = 300.0):
        now = time.time()
        if now - self._last_alert_at < min_gap or not self.alerter:
            return
        self._last_alert_at = now
        try:
            await self.alerter.send(text)
        except Exception:
            pass

    def _chrome_cmd(self, use_xvfb: bool = False) -> List[str]:
        flags = [
            CHROME,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={self._profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--use-gl=angle",
            "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader",
            "--lang=en-US",
            "--window-size=1600,900",
            "about:blank",
        ]
        env = ["env", f"TZ={TZ}", "LANG=en_US.UTF-8"]
        if use_xvfb:
            cmd = env + ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24"] + flags
        else:
            cmd = env + [f"DISPLAY={DISPLAY}"] + flags
        if hasattr(os, "geteuid") and os.geteuid() == 0 and RUN_AS and RUN_AS != "root":
            cmd = ["sudo", "-u", RUN_AS] + cmd
        return cmd

    def _kill_chrome(self):
        if self._profile:
            try:
                subprocess.run(
                    ["pkill", "-f", "--", f"--user-data-dir={self._profile}"],
                    timeout=5,
                )
            except Exception:
                pass
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._proc = None

    async def _spawn_chrome(self, use_xvfb: bool) -> bool:
        self._kill_chrome()
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)
        self._profile = f"/tmp/b365-chrome-{int(time.time())}"
        self._proc = subprocess.Popen(
            self._chrome_cmd(use_xvfb),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(40):
            await asyncio.sleep(0.5)
            if self._proc.poll() is not None:
                return False
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1
                ).read()
                return True
            except Exception:
                pass
        return False

    async def _start_browser(self) -> bool:
        from playwright.async_api import async_playwright

        if not await self._spawn_chrome(False) and not await self._spawn_chrome(True):
            return False
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{CDP_PORT}"
            )
            ctx = self._browser.contexts[0]
            self._page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            self._bind(self._page)
            await self._page.goto(
                getattr(Config, "BET365_LIVE_URL", None)
                or "https://www.bet365.com/#/IP/B1",
                wait_until="commit",
                timeout=45000,
            )
            asyncio.create_task(self._accept_cookies(self._page))
            return True
        except Exception as e:
            logger.error(f"[BET365] browser: {e}")
            return False

    async def _accept_cookies(self, page):
        for _ in range(12):
            await asyncio.sleep(2.5)
            try:
                body = (await page.inner_text("body", timeout=1500) or "").lower()
                if "uses cookies" in body:
                    for t in ("Accept all", "Accept All", "Alles accepteren", "Akkoord"):
                        try:
                            await page.click(f"text={t}", timeout=1500)
                            return
                        except Exception:
                            pass
            except Exception:
                return

    async def _teardown(self):
        self.running = False
        for closer in (
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer:
                try:
                    await asyncio.wait_for(closer(), timeout=5)
                except Exception:
                    pass
        self._pw = self._browser = self._page = None
        self._kill_chrome()
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)

    async def _watch_loop(self) -> str:
        t_start, last_block_check = time.time(), 0.0
        self._last_data_at = None
        while self._want_run:
            await asyncio.sleep(0.25)
            now = time.time()
            if self._proc and self._proc.poll() is not None:
                return "browser exited"
            if self._last_data_at is None:
                if now - t_start > 40 and now - last_block_check > 20:
                    last_block_check = now
                    try:
                        if "attention required" in (await self._page.title()).lower():
                            await self._tg(
                                "Bet365 blocked this IP - change VPN server"
                            )
                            self._cooldown = 120.0
                            return "IP blocked"
                    except Exception:
                        pass
                if now - t_start > FIRST_DATA_SECS:
                    return "no data after load"
            else:
                if not self.running:
                    self.running = True
                    logger.success("[BET365] UP")
                    await self._tg("Bet365 feed ON", min_gap=30.0)
                if now - self._last_data_at > STALL_SECS:
                    return f"no data for {int(now - self._last_data_at)}s"
        return "stopped"

    async def _run(self):
        backoff = 5.0
        while self._want_run:
            try:
                ok = await self._start_browser()
                if not ok:
                    await self._tg("Bet365 browser failed to start - retrying")
                    await self._teardown()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 120.0)
                    continue
                backoff = 5.0
                if self._list_task and not self._list_task.done():
                    self._list_task.cancel()
                self._list_task = asyncio.create_task(self._list_flusher())
                reason = await self._watch_loop()
                if not self._want_run:
                    break
                logger.warning(f"[BET365] restart: {reason}")
                await self._tg(f"Bet365 restarting: {reason}")
                await self._teardown()
                await asyncio.sleep(3.0 + self._cooldown)
                self._cooldown = 0.0
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[BET365] loop: {e!r}")
                await self._teardown()
                await asyncio.sleep(5.0)
        await self._teardown()
