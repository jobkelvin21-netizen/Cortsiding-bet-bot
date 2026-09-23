"""
Arbitrage Bot — SportyBet (Playwright) + Bet365 (CDP attach only).
Bet365 Chrome is opened by YOU with --remote-debugging-port=9222.
main.py never opens Bet365 tabs.
"""

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any

from loguru import logger
from playwright.async_api import async_playwright, BrowserContext, Page

from config import Config
from feeds.bet365_ws import Bet365Feed
from core.executor import BetExecutor
from utils.telegram import TelegramAlerter, TelegramCommandHandler


class ArbitrageBot:
    def __init__(self):
        self.alerter = TelegramAlerter()
        self.executor = BetExecutor(self.alerter, None, None)
        self.bet365 = Bet365Feed()
        self.bet365.set_alerter(self.alerter)

        self._bot_active = False
        self._playwright = None
        self.context: Optional[BrowserContext] = None
        self.login_page: Optional[Page] = None
        self.match_pages: Dict[str, Page] = {}
        self._locked: Optional[Dict[str, Any]] = None
        self._awaiting_switch_decision = False
        self._previous_ht_match = None
        self._cmd_handler: Optional[TelegramCommandHandler] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def _tg(self, msg: str):
        try:
            await self.alerter.send(msg)
        except Exception:
            pass

    # ── SportyBet browser (own profile — NOT Bet365) ──────────────

    async def _start_sporty_browser(self):
        profile = Path.home() / "chrome_profile_sporty"
        profile.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self.context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--window-size=1400,900",
            ],
            viewport={"width": 1400, "height": 900},
            ignore_default_args=["--enable-automation"],
        )
        if self.context.pages:
            self.login_page = self.context.pages[0]
        else:
            self.login_page = await self.context.new_page()
        await self.login_page.goto(
            "https://www.sportybet.com/ng/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        logger.success("[SPORTY] browser ready — log in if needed")

    async def _open_match_page(self, url: str) -> Optional[Page]:
        """Open SportyBet match on a fresh tab (about:blank first — no chrome://newtab)."""
        if not self.context:
            return None
        page = await self.context.new_page()
        try:
            await page.goto("about:blank")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(1.5)
            return page
        except Exception as e:
            logger.error(f"[OPEN] {e}")
            try:
                await page.close()
            except Exception:
                pass
            return None

    async def _close_match_page(self, mid: str):
        page = self.match_pages.pop(mid, None)
        if self.executor:
            try:
                self.executor.stop_watching(mid)
            except Exception:
                pass
        if page:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass

    # ── Telegram /slow ────────────────────────────────────────────

    async def handle_slow_game(
        self, sporty_url: str, bet365_id: str, teams: str = "Unknown"
    ):
        if not self._bot_active:
            await self._tg("⛔ Bot not active — send /startbot first")
            return

        fi = re.sub(r"\D", "", str(bet365_id))
        if not fi or not sporty_url.startswith("http"):
            await self._tg(
                "❌ Format:\n"
                "/slow\n"
                "SportyBet- https://www.sportybet.com/...\n"
                "Bet365- 151389963112\n"
                "Teams- Home vs Away"
            )
            return

        # One game only
        if self._locked and self._locked.get("bet365_id") != fi:
            old = self._locked.get("bet365_id")
            await self._close_match_page(old)
            self._locked = None

        home, away = "Home", "Away"
        if " vs " in teams.lower():
            parts = re.split(r"\s+vs\s+", teams, flags=re.I)
            if len(parts) >= 2:
                home, away = parts[0].strip(), parts[1].strip()

        page = await self._open_match_page(sporty_url)
        if not page:
            await self._tg("❌ Could not open SportyBet URL — NOT watching")
            return

        self.match_pages[fi] = page
        self._locked = {
            "bet365_id": fi,
            "sporty_url": sporty_url,
            "teams": teams,
            "home": home,
            "away": away,
            "page": page,
            "locked_at": time.time(),
        }

        # Seed score from Bet365 if known
        m = self.bet365.get_match(fi) or {}
        hs = int(m.get("home_score") or 0)
        as_ = int(m.get("away_score") or 0)

        self.executor.start_watching(
            fi, page, home, away, hs, as_, match=m or None
        )
        watch = self.executor.get_watch(fi)
        ok = await self.executor.ai_arm_from_plan(page, None, watch)

        if ok:
            await self._tg(
                f"🔒 SLOW LOCKED\n"
                f"🆔 FI=<code>{fi}</code>\n"
                f"📋 {home} vs {away}\n"
                f"📌 {watch.active_market}\n"
                f"On Confirm — waiting goal"
            )
        else:
            await self._tg("❌ Open/arm failed — NOT watching")
            await self._close_match_page(fi)
            self._locked = None

    async def handle_clear(self):
        if self._locked:
            fi = self._locked.get("bet365_id")
            await self._close_match_page(fi)
            self._locked = None
            await self._tg("🧹 Cleared active slow game")
        else:
            await self._tg("Nothing locked")

    async def handle_switch_decision(self, decision: str):
        if not self._awaiting_switch_decision:
            return
        self._awaiting_switch_decision = False
        if decision == "yes" and self._previous_ht_match:
            p = self._previous_ht_match
            await self.handle_slow_game(p["url"], p["bet365_id"], p["teams"])
            await self._tg("🔄 Switched back")
        else:
            await self._tg("✅ Staying")
        self._previous_ht_match = None

    # ── Goal from Bet365 (locked FI only) ─────────────────────────

    async def _on_bet365_update(self, rec: dict):
        if not self._bot_active or not self._locked:
            return
        fi = str(rec.get("match_id") or "")
        if fi != str(self._locked.get("bet365_id")):
            return

        watch = self.executor.get_watch(fi)
        if not watch or watch.flagged_suspended:
            return

        page = watch.page
        h = int(rec.get("home_score") or 0)
        a = int(rec.get("away_score") or 0)
        old_h, old_a = watch.home_score, watch.away_score

        # Score went DOWN → disallowed / offside → cashout
        if (h + a) < (old_h + old_a):
            watch.home_score, watch.away_score = h, a
            await self.executor.cashout_disallowed(
                page, f"Score {old_h}-{old_a} → {h}-{a} (disallowed)"
            )
            return

        # Goal
        if (h, a) != (old_h, old_a) and (h + a) > (old_h + old_a):
            watch.home_score, watch.away_score = h, a
            logger.success(f"[GOAL LOCKED] FI={fi} {old_h}-{old_a}→{h}-{a}")

            if await self.executor._is_suspended(page):
                watch.flagged_suspended = True
                watch.confirm_armed = False
                await self._tg(
                    f"⛔ MARKET SUSPENDED\nFI={fi}\nStopped — will NOT switch market"
                )
                self.executor.stop_watching(fi)
                return

            if watch.confirm_armed:
                ok = await self.executor.click_confirm_only(page)
                if ok:
                    await self._tg(
                        f"✅ BET ON GOAL\n"
                        f"{watch.home_team} vs {watch.away_team}\n"
                        f"{old_h}-{old_a} → {h}-{a}\n"
                        f"{watch.active_market}"
                    )
                    # Re-arm same market type at new line
                    watch.confirm_armed = False
                    await asyncio.sleep(0.8)
                    await self.executor.ai_arm_from_plan(page, None, watch)
                else:
                    await self._tg("❌ Confirm click failed after goal")

    # ── Start / Stop ──────────────────────────────────────────────

    async def start_bot(self):
        if self._bot_active:
            await self._tg("Already running")
            return
        self._bot_active = True

        def bet365_callback(rec: dict):
            return self._on_bet365_update(rec)

        self.bet365.set_callback(bet365_callback)
        await self.bet365.start()
        await self._tg(
            "✅ Bot ACTIVE\n"
            "Bet365 = existing Chrome :9222 (no new tabs)\n"
            "Send /slow when you have a lag"
        )
        logger.info("[START] bot active")

    async def stop_bot(self):
        self._bot_active = False
        self.bet365.set_callback(None)
        await self.bet365.stop()
        if self._locked:
            await self._close_match_page(self._locked.get("bet365_id"))
            self._locked = None
        await self._tg("⏹ Bot STOPPED — Chrome left open")
        logger.info("[STOP] bot stopped")

    async def _heartbeat(self):
        while True:
            try:
                if self._bot_active:
                    logger.info("[heartbeat] bot alive")
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(30)

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self):
        await self._start_sporty_browser()
        await self._tg(
            "🤖 Courtside ready\n"
            "1. Chrome :9222 + Proton + Bet365 live open\n"
            "2. Log in SportyBet in the bot window\n"
            "3. /startbot\n"
            "4. /slow when you have a lag"
        )
        self._cmd_handler = TelegramCommandHandler(self.alerter, self)
        await self._cmd_handler.start()
        self._heartbeat_task = asyncio.create_task(self._heartbeat())
        # Block forever
        while True:
            await asyncio.sleep(3600)

    def stop(self):
        self._bot_active = False
        try:
            asyncio.get_event_loop().create_task(self.bet365.stop())
        except Exception:
            pass
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        logger.info("Bot process stopping")


if __name__ == "__main__":
    bot = ArbitrageBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        bot.stop()
