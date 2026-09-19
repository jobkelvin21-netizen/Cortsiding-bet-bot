"""Bet365 InPlay WS — primary fast feed with team name scraping."""

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


class Bet365Feed:
    """Primary fast feed: scores (SS=) + clock (TM=) with team name scraping."""

    def __init__(self, callback: Optional[Callable] = None):
        self.callback = callback
        self.matches: Dict[str, dict] = {}
        self.running = False
        self.rotator = ProxyRotator()
        self._last_message_at: Optional[float] = None
        self._task = None
        self.alerter = None
        self._ws_url: Optional[str] = None

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
        """Notify callback with match data."""
        if self.callback:
            try:
                await self.callback(dict(match))
            except Exception as e:
                logger.debug(f"[BET365] callback error: {e}")

    def _parse_frame(self, raw: str):
        raw = (raw or "").replace("\x01", "").strip()
        if not raw:
            return
        
        # DEBUG: Log all frames with SS= or NA= (like standalone)
        if "SS=" in raw or "NA=" in raw:
            logger.debug(f"[BET365 RAW] {raw[:220]}...")
        
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

            mid = data.get("ID") or data.get("FI")
            if not mid:
                m = ID_RE.search(part)
                if m:
                    mid = m.group(1)
            if not mid:
                continue

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
                    "name": "",
                    "updated": 0.0,
                },
            )

            # Parse team names from NA=
            if "NA" in data:
                name = data["NA"]
                rec["name"] = name
                low = name.lower()
                if " v " in low or " vs " in low:
                    sep = " v " if " v " in low else " vs "
                    bits = re.split(sep, name, flags=re.I, maxsplit=1)
                    if len(bits) == 2:
                        rec["home_team"] = bits[0].strip()
                        rec["away_team"] = bits[1].strip()
                        # DEBUG: Log when we find team names
                        logger.info(f"[BET365 TEAMS] {rec['home_team']} vs {rec['away_team']} (ID={mid})")

            # Parse score from SS=
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
                        logger.success(f"[BET365][GOAL] {rec.get('home_team') or mid} {old}→{rec['ss']}")

            # Extract minutes and convert to seconds
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

            # Backup SS= parsing
            for m in SS_RE.finditer(part):
                h, a = int(m.group(1)), int(m.group(2))
                ss = f"{h}-{a}"
                if rec.get("ss") != ss:
                    rec["ss"] = ss
                    rec["home_score"] = h
                    rec["away_score"] = a
                    score_changed = True

            rec["updated"] = time.time()
            self.matches[mid] = rec
            self._last_message_at = time.time()

            # DEBUG: Log every update with team names
            if rec.get("home_team") and rec.get("ss"):
                logger.info(f"[BET365] {rec['home_team']} {rec['ss']} {rec['away_team']} min={rec.get('tm')}")

            # Notify callback
            if self.callback:
                asyncio.create_task(self._notify(rec))

    async def _scrape_live_matches(self, page) -> Dict[str, dict]:
        """Scrape live match IDs and team names from the page DOM."""
        matches = {}
        try:
            # Wait for content to load
            await asyncio.sleep(2)
            
            # Try multiple selectors for match rows
            selectors = [
                '[data-automation-id*="match"]',
                '[class*="EventWrapper"]',
                '[class*="Fixture"]',
                '.src-EventWrapper',
                '.src-StandardFixture',
                '[data-testid*="match"]',
            ]
            
            for selector in selectors:
                try:
                    elements = await page.locator(selector).all()
                    logger.debug(f"[BET365 SCRAPE] Found {len(elements)} elements with {selector}")
                    
                    for el in elements:
                        try:
                            # Try to get match ID from data attributes
                            mid = await el.get_attribute('data-automation-id') or ''
                            # Extract numeric ID
                            m = re.search(r'(\d+)', mid)
                            if m:
                                mid = m.group(1)
                            else:
                                # Try from inner HTML
                                html = await el.inner_html()
                                m = re.search(r'ID[=:](\d+)', html)
                                if m:
                                    mid = m.group(1)
                                else:
                                    continue
                            
                            # Get text content
                            text = await el.inner_text()
                            lines = [l.strip() for l in text.split('\n') if l.strip()]
                            
                            # Look for team names (usually separated by ' v ' or ' vs ')
                            for line in lines:
                                line_lower = line.lower()
                                if ' v ' in line_lower or ' vs ' in line_lower:
                                    sep = ' v ' if ' v ' in line_lower else ' vs '
                                    parts = line.split(sep, 1)
                                    if len(parts) == 2:
                                        home = parts[0].strip()
                                        away = parts[1].strip()
                                        # Validate: both should be reasonable team names
                                        if len(home) > 2 and len(away) > 2:
                                            matches[mid] = {
                                                'home_team': home,
                                                'away_team': away
                                            }
                                            logger.info(f"[BET365 SCRAPE] {mid}: {home} v {away}")
                                            break
                        except Exception as e:
                            logger.debug(f"[BET365 SCRAPE] element error: {e}")
                            continue
                            
                    if matches:
                        logger.success(f"[BET365 SCRAPE] Scraped {len(matches)} matches")
                        break
                        
                except Exception as e:
                    logger.debug(f"[BET365 SCRAPE] selector {selector} failed: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"[BET365 SCRAPE] error: {e}")
        
        return matches

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

            def on_response(resp):
                nonlocal got_403
                try:
                    if resp.status == 403:
                        got_403 = True
                        logger.warning(f"[BET365] 403 on {resp.url[:80]}")
                except Exception:
                    pass

            page.on("response", on_response)

            def on_websocket(ws):
                url = ws.url
                self._ws_url = url
                logger.success(f"[BET365][WS] Connected: {url[:60]}")

                def on_frame(ev):
                    try:
                        payload = getattr(ev, "payload", ev)
                        text = (
                            payload.decode("utf-8", errors="ignore")
                            if isinstance(payload, (bytes, bytearray))
                            else str(payload)
                        )
                        # DEBUG: Log every frame like standalone
                        if "SS=" in text or "NA=" in text:
                            logger.debug(f"[BET365 FRAME] {text[:150]}...")
                        
                        if "SS=" in text or "TM=" in text or "EV;" in text:
                            self._parse_frame(text)
                    except Exception as e:
                        logger.debug(f"[BET365] frame error: {e}")

                ws.on("framereceived", on_frame)

            page.on("websocket", on_websocket)

            try:
                resp = await page.goto(Config.BET365_LIVE_URL, wait_until="domcontentloaded", timeout=45000)
                if (resp and resp.status == 403) or got_403:
                    logger.warning("[BET365] 403 on page load")
                    await context.close()
                    return False

                await asyncio.sleep(3)
                if got_403:
                    await context.close()
                    return False

                # Click Football
                for sel in ("text=Football", "text=Soccer", 'a[href*="IP"]'):
                    try:
                        loc = page.locator(sel)
                        if await loc.count() > 0:
                            await loc.first.click(timeout=2000)
                            logger.info("[BET365] clicked Football")
                            await asyncio.sleep(2)
                            break
                    except Exception:
                        pass

                # SCRAPE TEAM NAMES from page
                logger.info("[BET365] Scraping team names from page...")
                scraped = await self._scrape_live_matches(page)
                
                # Merge scraped names into matches
                for mid, teams in scraped.items():
                    if mid not in self.matches:
                        self.matches[mid] = {
                            "source": "bet365",
                            "match_id": mid,
                            "home_team": teams["home_team"],
                            "away_team": teams["away_team"],
                            "home_score": 0,
                            "away_score": 0,
                            "ss": "",
                            "tm": "",
                            "minute": 0,
                            "played_seconds": 0,
                            "updated": time.time(),
                        }
                    else:
                        self.matches[mid]["home_team"] = teams["home_team"]
                        self.matches[mid]["away_team"] = teams["away_team"]
                    logger.info(f"[BET365] Scraped teams for {mid}: {teams['home_team']} v {teams['away_team']}")

                if self.alerter:
                    try:
                        await self.alerter.send(f"✅ <b>Bet365 WS connected</b>\nScraped {len(scraped)} team names")
                    except Exception:
                        pass

                logger.success("[BET365] feed LIVE - waiting for data...")
                
                # Keep connection alive
                while self.running:
                    if got_403:
                        logger.warning("[BET365] 403 detected - rotating proxy")
                        await context.close()
                        return False
                    
                    idle = time.time() - (self._last_message_at or 0)
                    if idle > 60:
                        logger.warning(f"[BET365] idle {idle:.0f}s - reconnecting")
                        break
                    
                    await asyncio.sleep(2)

                await context.close()
                return True
                
            except Exception as e:
                logger.error(f"[BET365] error: {e}")
                try:
                    await context.close()
                except Exception:
                    pass
                return False

    async def _supervisor(self):
        """Rotate proxies on failure."""
        while self.running:
            proxy = self.rotator.get_next()
            if proxy is None:
                logger.warning("[BET365] all proxies bad - resetting")
                self.rotator.reset()
                proxy = self.rotator.get_next()
            
            ok = await self._run_with_proxy(proxy)
            
            if not self.running:
                break
            
            if not ok and proxy:
                self.rotator.mark_bad(proxy)
            
            logger.info("[BET365] reconnecting in 2s...")
            await asyncio.sleep(2.0)

    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._supervisor())
        logger.success("[BET365] feed started (primary)")

    def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
        logger.info("[BET365] feed stopped")
