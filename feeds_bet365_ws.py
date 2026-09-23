"""Bet365 WS — REAL FOOTBALL ONLY FI+name to Telegram; goals only for locked FI."""

import asyncio
import re
import time
from typing import Callable, Dict, List, Optional, Set

from loguru import logger
from playwright.async_api import async_playwright

from config import Config

CDP_URL = "http://127.0.0.1:9222"
WS_IDLE_SECS = 90
CATCHUP_INTERVAL_SECS = 15.0

SS_RE = re.compile(r"(?:^|[|;,\s])SS=(\d{1,2})\s*[-:]\s*(\d{1,2})", re.I)
FI_RE = re.compile(r"(?:^|[|;,\s])FI=(\d{6,})", re.I)
ID_RE = re.compile(r"(?:^|[|;,\s])ID=(\d{6,})", re.I)
NA_RE = re.compile(r"(?:^|[|;,\s])NA=([^|;]+)", re.I)
TM_RE = re.compile(r"(?:^|[|;,\s])TM=(\d+)", re.I)
OV_RE = re.compile(r"OV(\d{6,})C\d+", re.I)

# Fast reject: obvious esports/virtual football keywords in the match name.
_ESPORTS_HINTS = re.compile(
    r"esoccer|efootball|e-football|e[\s\-]?sports?|cyber\s*football|"
    r"virtual\s*football|gt\s*league|h2h\s*gg|egames|e-games|liga\s*pro|"
    r"volta\b|battle\b|fifa\s*\d|pes\s*\d",
    re.I,
)
# Real football teams almost never carry a parenthetical gamer tag.
_GAMER_TAG_RE = re.compile(r"\([^)]{1,25}\)")

# Individual-athlete sports (tennis, table tennis, darts, snooker, badminton)
# almost always name players as "Surname I." on each side — football teams
# never look like this.
_INDIVIDUAL_SPORT_NAME_RE = re.compile(
    r"^[A-Z][a-zA-Z'\-]+\s+[A-Z]\.?\s*(?:vs\.?|v\.?)\s*[A-Z][a-zA-Z'\-]+\s+[A-Z]\.?$",
    re.I,
)
# Explicit non-football sport keywords that occasionally show up in the name.
_NON_FOOTBALL_KEYWORDS = re.compile(
    r"\btennis\b|\bvolleyball\b|\bbasketball\b|\btable\s*tennis\b|\bhandball\b|"
    r"\bdarts\b|\bsnooker\b|\bbadminton\b|\bice\s*hockey\b|\brugby\b|\bcricket\b|"
    r"\bfutsal\b|\bbaseball\b|\bboxing\b|\bmma\b|\bbeach\s*volleyball\b",
    re.I,
)


def _split_teams(na: str):
    for sep in (r"\s+vs\.?\s+", r"\s+v\.?\s+", r"\s+@\s+"):
        parts = re.split(sep, (na or "").strip(), maxsplit=1, flags=re.I)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    return (na or "").strip(), ""


class Bet365Feed:
    def __init__(self, callback: Optional[Callable] = None):
        self.callback = callback
        self.matches: Dict[str, dict] = {}
        self.running = False
        self._want_run = False
        self._last_message_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._catchup_task: Optional[asyncio.Task] = None
        self.alerter = None
        self._page = None
        self._browser = None
        self._playwright = None
        self._last_alert_at = 0.0
        self._announced: Set[str] = set()
        self._football_cache: Dict[str, bool] = {}

    def set_alerter(self, alerter):
        self.alerter = alerter

    def set_callback(self, callback: Callable):
        self.callback = callback

    async def _tg(self, text: str, min_gap: float = 12.0):
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

    async def _ask_groq_is_football(self, name: str) -> bool:
        """Only called for ambiguous names that pass the fast regex filters.
        Fails CLOSED (returns False) if Groq is unavailable or errors, so a
        broken AI call never leaks non-football matches through again."""
        try:
            from core.groq_ai import _client_or_none

            client = _client_or_none()
            if not client:
                logger.warning("[BET365][GROQ] no client — treating as non-football")
                return False
            resp = client.chat.completions.create(
                model=getattr(Config, "GROQ_TEXT_MODEL", "llama-3.3-70b-versatile"),
                temperature=0,
                max_tokens=10,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Is this a REAL human football (soccer) match between "
                            "real clubs or national teams — NOT tennis, table tennis, "
                            "volleyball, basketball, darts, snooker, badminton, "
                            "esports, eFootball, virtual football, FIFA/PES video "
                            "game leagues, or any other non-football sport?\n"
                            f"Match: {name}\n"
                            "Answer with exactly one word: YES or NO."
                        ),
                    }
                ],
            )
            answer = (resp.choices[0].message.content or "").strip().upper()
            return answer.startswith("Y")
        except Exception as e:
            logger.warning(f"[BET365][GROQ] classify failed, treating as non-football: {e}")
            return False

    async def _is_real_football(self, fi: str, name: str) -> bool:
        cached = self._football_cache.get(fi)
        if cached is not None:
            return cached

        if _ESPORTS_HINTS.search(name) or _GAMER_TAG_RE.search(name):
            self._football_cache[fi] = False
            logger.debug(f"[BET365] filtered esports FI={fi} {name}")
            return False

        if _NON_FOOTBALL_KEYWORDS.search(name):
            self._football_cache[fi] = False
            logger.debug(f"[BET365] filtered non-football keyword FI={fi} {name}")
            return False

        if _INDIVIDUAL_SPORT_NAME_RE.match(name.strip()):
            self._football_cache[fi] = False
            logger.debug(f"[BET365] filtered individual-sport name pattern FI={fi} {name}")
            return False

        result = await self._ask_groq_is_football(name)
        self._football_cache[fi] = result
        if not result:
            logger.debug(f"[BET365] filtered non-football (groq) FI={fi} {name}")
        return result

    async def _announce(self, fi: str, name: str, ss: str):
        if not self.running or not self._want_run:
            return
        if fi in self._announced:
            return
        if not name or not re.search(r"\s+v(?:s)?\.?\s+|\s+@\s+", name, re.I):
            return

        is_football = await self._is_real_football(fi, name)
        self._announced.add(fi)
        if not is_football:
            return

        if self.alerter:
            try:
                await self.alerter.send(
                    f"⚽ <b>LIVE FOOTBALL</b>\n"
                    f"🆔 FI=<code>{fi}</code>\n"
                    f"📋 {name}\n"
                    f"SS={ss or '?'}"
                )
            except Exception:
                pass

    async def _catchup_loop(self):
        """Periodically re-scan everything currently tracked and send any
        football match that hasn't been announced yet — covers matches
        that were already live before the bot connected, or any that
        didn't get announced the first time a frame for them arrived."""
        while self._want_run:
            try:
                await asyncio.sleep(CATCHUP_INTERVAL_SECS)
                if not self.running or not self._want_run:
                    continue
                for fi, rec in list(self.matches.items()):
                    if fi in self._announced:
                        continue
                    name = rec.get("name") or ""
                    if not name:
                        continue
                    await self._announce(fi, name, rec.get("ss") or "")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[BET365][CATCHUP] {e}")

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
                if re.search(r"\s+v(?:s)?\.?\s+|\s+@\s+", na, re.I):
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
                    self._announce(mid, cur_na, cur_ss or rec.get("ss") or "")
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
                await self._tg("⚠️ Bet365 CDP offline")
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
            self.running = True
            await self._tg("✅ Bet365 ON — real football FI → Telegram")
            while self._want_run:
                if got_403:
                    await self._tg("⚠️ Bet365 403")
                    return False
                if self._last_message_at and (
                    time.time() - self._last_message_at > WS_IDLE_SECS
                ):
                    return False
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

    async def start(self):
        if self._want_run and self._task and not self._task.done():
            return
        self._want_run = True
        self.running = False
        self._task = asyncio.create_task(self._supervisor())
        if not self._catchup_task or self._catchup_task.done():
            self._catchup_task = asyncio.create_task(self._catchup_loop())

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
        ct = self._catchup_task
        self._catchup_task = None
        if ct and not ct.done():
            ct.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(ct), timeout=2.0)
            except Exception:
                pass
        await self._close_browser()
