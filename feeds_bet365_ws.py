# feeds/bet365_ws.py
import asyncio
import json
import re
import time
import base64
import os
from datetime import datetime
from typing import Callable, Optional
from loguru import logger
from playwright.async_api import async_playwright

from proxies import ProxyRotator

SCHEMA_CACHE_FILE = "bet365_schema_cache.json"
RAW_FRAMES_LOG = "bet365_all_frames.log"


def try_base64_decode(payload: str):
    try:
        padded = payload + "=" * (-len(payload) % 4)
        decoded_bytes = base64.b64decode(padded, validate=True)
        text = decoded_bytes.decode("utf-8")
        return json.loads(text)
    except Exception:
        return None


class SchemaLearner:
    def __init__(self):
        self.learned = self._load_cache()
        self.sample_messages = []
        self.MIN_SAMPLES = 5

    def _load_cache(self):
        if os.path.exists(SCHEMA_CACHE_FILE):
            try:
                with open(SCHEMA_CACHE_FILE) as f:
                    data = json.load(f)
                    logger.success(f"Loaded cached bet365 schema: {data}")
                    return data
            except Exception:
                pass
        return None

    def _save_cache(self, schema):
        try:
            with open(SCHEMA_CACHE_FILE, "w") as f:
                json.dump(schema, f, indent=2)
            logger.success(f"Saved learned bet365 schema: {schema}")
        except Exception as e:
            logger.error(f"Failed to save schema cache: {e}")

    def is_ready(self):
        return self.learned is not None

    def feed_sample(self, data: dict):
        if self.is_ready():
            return
        self.sample_messages.append(data)
        logger.debug(f"[SCHEMA LEARNING] sample #{len(self.sample_messages)}: "
                     f"{json.dumps(data)[:300]}")
        if len(self.sample_messages) >= self.MIN_SAMPLES:
            self._attempt_learn()

    def _attempt_learn(self):
        candidate_fields = {}
        for msg in self.sample_messages:
            for key, val in self._flatten(msg).items():
                candidate_fields.setdefault(key, []).append(val)

        home_field = away_field = score_field = period_field = status_field = None

        for key, values in candidate_fields.items():
            str_vals = [v for v in values if isinstance(v, str)]
            if not str_vals:
                continue

            if all(re.match(r'^\d+[-:]\d+$', v) for v in str_vals if v):
                score_field = score_field or key
                continue

            if all(len(v) <= 5 and v.isupper() for v in str_vals if v):
                period_field = period_field or key
                continue

            if all(v.isalpha() and len(v) > 5 for v in str_vals if v):
                status_field = status_field or key
                continue

            if all(3 <= len(v) <= 40 for v in str_vals if v):
                if home_field is None:
                    home_field = key
                elif away_field is None and key != home_field:
                    away_field = key

        if home_field and away_field and score_field:
            schema = {
                "home_field": home_field,
                "away_field": away_field,
                "score_field": score_field,
                "period_field": period_field,
                "status_field": status_field,
            }
            self.learned = schema
            self._save_cache(schema)
        else:
            logger.warning("Schema learning inconclusive so far, waiting for more samples...")
            if len(self.sample_messages) > 30:
                logger.error("Could not learn bet365 schema after 30 samples. "
                             "Check bet365_raw_dump.log / bet365_all_frames.log for manual inspection.")

    def _flatten(self, d, prefix=""):
        out = {}
        if isinstance(d, dict):
            for k, v in d.items():
                full_key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    out.update(self._flatten(v, full_key))
                elif isinstance(v, (str, int, float)):
                    out[full_key] = v
        return out

    def extract(self, data: dict) -> Optional[dict]:
        if not self.is_ready():
            return None

        flat = self._flatten(data)
        schema = self.learned

        home = flat.get(schema["home_field"])
        away = flat.get(schema["away_field"])
        score_raw = flat.get(schema["score_field"], "")
        period = flat.get(schema.get("period_field") or "", "")

        if not home or not away:
            return None

        home_score, away_score = 0, 0
        m = re.match(r'^(\d+)[-:](\d+)$', str(score_raw))
        if m:
            home_score, away_score = int(m.group(1)), int(m.group(2))

        return {
            "home_team": home,
            "away_team": away,
            "home_score": home_score,
            "away_score": away_score,
            "period": period,
        }


class Bet365Feed:
    def __init__(self, callback: Callable):
        self.callback = callback
        self.running = False
        self.matches = {}
        self.last_message_at = None
        self.learner = SchemaLearner()
        self.proxy_rotator = ProxyRotator()
        self._loop = None
        self.playwright = None
        self.browser = None
        self.page = None

    def is_healthy(self) -> bool:
        if self.last_message_at is None:
            return False
        return (time.time() - self.last_message_at) < 30

    async def _launch_with_proxy(self):
        proxy, idx = self.proxy_rotator.get_next()

        if proxy is None:
            logger.warning("All proxies exhausted this cycle. Waiting 60s before retrying pool...")
            await asyncio.sleep(60)
            self.proxy_rotator.reset()
            proxy, idx = self.proxy_rotator.get_next()
            if proxy is None:
                return None, None

        logger.info(f"Trying proxy #{idx}: {proxy['server']}")

        browser = None
        try:
            if self.playwright is None:
                self.playwright = await async_playwright().start()

            browser = await self.playwright.chromium.launch(
                headless=False,
                proxy={
                    "server": proxy["server"],
                    "username": proxy["username"],
                    "password": proxy["password"],
                },
                args=['--disable-blink-features=AutomationControlled'],
            )
            page = await browser.new_page(viewport={'width': 412, 'height': 915})
            await page.goto("https://www.bet365.com/", wait_until="domcontentloaded", timeout=20000)

            title = await page.title()
            body_text = await page.evaluate("document.body.innerText.slice(0, 300)")
            if "blocked" in body_text.lower() or "blocked" in title.lower():
                logger.warning(f"Proxy #{idx} reached bet365 but got BLOCKED (Cloudflare/anti-bot page)")
                self.proxy_rotator.mark_bad(idx)
                await browser.close()
                return None, None

            logger.success(f"Proxy #{idx} reached bet365 successfully (title: '{title}')")
            return browser, page

        except Exception as e:
            logger.warning(f"Proxy #{idx} failed: {e}")
            self.proxy_rotator.mark_bad(idx)
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            return None, None

    def _on_ws_frame(self, payload):
        self.last_message_at = time.time()

        data = None

        decoded = try_base64_decode(payload)
        if decoded is not None:
            data = decoded
        else:
            try:
                data = json.loads(payload)
            except Exception:
                with open("bet365_raw_dump.log", "a") as f:
                    f.write(f"{datetime.now()} | RAW UNPARSEABLE: {payload[:500]}\n")
                return

        if not self.learner.is_ready():
            self.learner.feed_sample(data)
            return

        match = self.learner.extract(data)
        if not match:
            return

        match['source'] = 'bet365'
        match['match_id'] = f"bet365:{match['home_team']}_{match['away_team']}"
        match['timestamp'] = datetime.now()

        self.matches[match['match_id']] = match

        logger.info(f"[BET365 WS] {match['home_team']} {match['home_score']}-{match['away_score']} "
                    f"{match['away_team']} | period={match['period']}")

        if self.callback and self._loop:
            asyncio.run_coroutine_threadsafe(self.callback(match), self._loop)

    async def _run_once(self):
        browser, page = await self._launch_with_proxy()
        if not page:
            return

        self.browser = browser
        self.page = page

        def on_ws(ws):
            logger.debug(f"[BET365] WebSocket opened: {ws.url}")

            def on_frame(payload):
                # TEMP DIAGNOSTIC: dump every raw frame, no filtering, so we can
                # see the real shape of bet365's data (binary/protobuf/JSON/etc.)
                try:
                    with open(RAW_FRAMES_LOG, "a") as f:
                        f.write(f"{datetime.now()} | LEN={len(payload)} | {payload[:200]}\n")
                except Exception:
                    pass

                self._on_ws_frame(payload)

            ws.on("framereceived", on_frame)

        page.on("websocket", on_ws)

        try:
            # NEW: click "In-Play" using a much broader set of selectors,
            # since bet365's nav items are icon/div-based, not plain <a>/<button>
            in_play_link = await page.query_selector(
                'a:has-text("In-Play"), div:has-text("In-Play"), span:has-text("In-Play"), '
                '[class*="in-play"], [class*="inplay"], [href*="in-play"], [href*="inplay"], '
                'a:has-text("Live"), div:has-text("Live")'
            )
            if in_play_link:
                await in_play_link.click()
                await asyncio.sleep(3)
                logger.success("[BET365] Clicked In-Play/Live tab")
            else:
                logger.warning("[BET365] Could not find In-Play/Live nav item — trying direct URL fallback")
                try:
                    await page.goto("https://www.bet365.com/#/IP/", wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(3)
                except Exception as e:
                    logger.warning(f"[BET365] Direct In-Play URL fallback failed: {e}")

            football_link = await page.query_selector(
                'a:has-text("Football"), div:has-text("Football"), span:has-text("Football"), '
                '[class*="football"]'
            )
            if football_link:
                await football_link.click()
                await asyncio.sleep(3)
                logger.success("[BET365] Clicked Football section")
            else:
                logger.warning("[BET365] Could not find Football section link on the In-Play page")

            logger.success("bet365 connected via proxy, watching live football...")

            last_no_data_warning = 0
            while self.running:
                await asyncio.sleep(5)
                if page.is_closed():
                    logger.warning("bet365 page closed unexpectedly")
                    break

                if self.last_message_at is None:
                    if time.time() - last_no_data_warning > 15:
                        logger.warning("[BET365] Connected but no WebSocket data received yet...")
                        last_no_data_warning = time.time()
                elif (time.time() - self.last_message_at) > 45:
                    logger.warning("bet365 feed stalled (no messages in 45s) — reconnecting")
                    break

        except Exception as e:
            logger.error(f"bet365 session error: {e}")
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    async def start(self):
        self.running = True
        self._loop = asyncio.get_event_loop()
        asyncio.create_task(self._run_loop())
        logger.info("bet365 feed starting (proxy rotation active)...")

    async def _run_loop(self):
        while self.running:
            await self._run_once()
            if self.running:
                await asyncio.sleep(5)

    def stop(self):
        self.running = False

    def get_matches(self):
        return list(self.matches.values())
