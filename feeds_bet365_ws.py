"""Bet365 InPlay WS only — score/clock by match_id. No page scrape."""

import asyncio
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

from loguru import logger
from playwright.async_api import async_playwright

from config import Config

PROXY_LIST: List[dict] = [
    {"server": "http://31.59.20.176:6754", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://45.38.107.97:6014", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://198.105.121.200:6462", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://64.137.96.74:6641", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://198.23.243.226:6361", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://38.154.185.97:6370", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://84.247.60.125:6095", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://142.111.67.146:5611", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://191.96.254.138:6185", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
    {"server": "http://31.58.9.4:6077", "username": "pzbdjger", "password": "4vfkqiesj5kw"},
]


class ProxyRotator:
    def __init__(self, proxies: Optional[List[dict]] = None):
        self.proxies = list(proxies or PROXY_LIST)
        self.bad_indices = set()
        self.current_index = 0

    def get_next(self):
        if not self.proxies:
            return None
        attempts = 0
        while attempts < len(self.proxies):
            idx = self.current_index % len(self.proxies)
            self.current_index += 1
            attempts += 1
            if idx not in self.bad_indices:
                return self.proxies[idx]
        return None

    def mark_bad(self, proxy):
        if isinstance(proxy, dict):
            for i, p in enumerate(self.proxies):
                if p.get("server") == proxy.get("server"):
                    self.bad_indices.add(i)
                    logger.warning(f"[BET365][PROXY] bad {proxy.get('server')}")
                    break

    def reset(self):
        self.bad_indices.clear()


SS_RE = re.compile(r"(?:^|[|;,\s])SS=(\d{1,2})\s*[-:]\s*(\d{1,2})", re.I)
ID_RE = re.compile(r"(?:^|[|;,\s])(?:ID|FI)=(\d+)", re.I)
TM_RE = re.compile(r"(?:^|[|;,\s])TM=(\d+)", re.I)
OV_ID_RE = re.compile(r"OV\d+-(\d+)_\d+_\d+U?", re.I)


class Bet365Feed:
    def __init__(self, callback: Optional[Callable] = None):
        self.callback = callback
        self.matches: Dict[str, dict] = {}
        self.running = False
        self.rotator = ProxyRotator()
        self._last_message_at: Optional[float] = None
        self._task = None
        self.alerter = None
        self._ws_url: Optional[str] = None
        self._page = None

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

    async def _run_with_proxy(self, proxy: Any) -> bool:
        key = proxy.get("server") if isinstance(proxy, dict) else str(proxy)
        logger.info(f"[BET365] trying proxy={key}")
        got_403 = False
        profile = os.path.abspath(f"chrome_profile_bet365_{abs(hash(key)) % 10000}")

        async with async_playwright() as p:
            kwargs = dict(
                user_data_dir=profile,
                channel="chrome",
                headless=False,
                no_viewport=True,
                locale="en-GB",
                ignore_default_args=["--enable-automation"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--start-maximized",
                    "--disable-dev-shm-usage",
                ],
            )
            if proxy:
                kwargs["proxy"] = proxy

            try:
                context = await p.chromium.launch_persistent_context(**kwargs)
            except Exception as e:
                logger.error(f"[BET365] launch fail: {e}")
                return False

            page = context.pages[0] if context.pages else await context.new_page()
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
                        if "SS=" in text or "TM=" in text or "ID=" in text or "FI=" in text:
                            self._parse_frame(text)
                    except Exception as e:
                        logger.debug(f"[BET365] frame: {e}")

                ws.on("framereceived", on_frame)

            page.on("websocket", on_websocket)

            try:
                resp = await page.goto(
                    Config.BET365_LIVE_URL, wait_until="domcontentloaded", timeout=45000
                )
                if (resp and resp.status == 403) or got_403:
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

                logger.success("[BET365] WS live (no scrape)")

                while self.running:
                    if got_403:
                        await context.close()
                        return False
                    idle = time.time() - (self._last_message_at or 0)
                    if idle > 60:
                        logger.warning(f"[BET365] idle {idle:.0f}s")
                        break
                    await asyncio.sleep(2)

                await context.close()
                return True
            except Exception as e:
                logger.error(f"[BET365] {e}")
                try:
                    await context.close()
                except Exception:
                    pass
                return False

    async def _supervisor(self):
        while self.running:
            proxy = self.rotator.get_next()
            if proxy is None:
                self.rotator.reset()
                proxy = self.rotator.get_next()
            ok = await self._run_with_proxy(proxy)
            if not self.running:
                break
            if not ok and proxy:
                self.rotator.mark_bad(proxy)
            await asyncio.sleep(2.0)

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
