#!/usr/bin/env python3
"""Main entry - Bet365 primary, one slow game focus (AI tickers ≥3s)."""

import asyncio
import os
import re
import sys
import time
import gc
from loguru import logger

from config import Config
from auth_sportybet_login import SportyBetAuth
from feeds.bet365_ws import Bet365Feed
from feeds.polymarket_ws import PolymarketFeed
from feeds.sportybet_api import SportyBetFeed
from core.match_linker import MatchLinker
from core.executor import BetExecutor, BetResult
from core.cashout import CashOutManager
from core.groq_ai import SlowGameAI, plan_over_market
from utils.telegram import TelegramAlerter
from utils.account_manager import AccountManager


class ArbitrageBot:
    def __init__(self):
        self.account_manager = AccountManager()
        self.alerter = TelegramAlerter()
        self.auth = SportyBetAuth()

        self._telegram_test_done = False

        self.sportybet = SportyBetFeed()
        self.sportybet.set_alerter(self.alerter)

        self.bet365 = Bet365Feed()
        self.bet365.set_alerter(self.alerter)

        self.polymarket = PolymarketFeed(self._noop_callback)

        self.linker = MatchLinker(self.bet365, self.sportybet.matches, self.alerter)
        self.linker.set_goal_callback(self.on_goal)
        self.linker.set_link_callback(self.on_new_link)
        self.linker.set_unlink_callback(self.on_unlink)
        self.linker.set_slow_callback(self.on_new_link)

        # AI ticker slow lock (frontend clocks only, ≥3s)
        self.slow_ai = SlowGameAI()
        self._armed_mids = set()

        self.executor = None
        self.cashout = CashOutManager(self.alerter)

        self.match_pages = {}
        self._page_contexts = {}
        self._opening_pages = set()

        self.running = False
        self._processing = {}
        self.browser = None
        self.context = None
        self._cleanup_task = None
        self.playwright = None

    async def _test_telegram(self):
        try:
            await self.alerter.send("🤖 <b>Bot starting...</b>\nTelegram alerts active!")
            logger.success("Telegram test sent")
        except Exception as e:
            logger.error(f"Telegram test failed: {e}")

    async def _noop_callback(self, match):
        pass

    async def setup(self):
        if not self.account_manager.accounts:
            print("No accounts! Add one:")
            u = input("Username: ")
            p = input("Password: ")
            ph = input("Phone: ")
            self.account_manager.add_account(u, p, ph)
        if not self.account_manager.get_active():
            logger.error("No active account")
            await self.alerter.notify_error("No active account — bot stopped")
            sys.exit(1)

    async def start(self):
        await self.setup()
        logger.info("=" * 70)
        logger.info("BOT STARTING - AI ticker slow (≥3s) + Bet365 WS goals")
        logger.info("=" * 70)

        print("\nSPORTYBET LOGIN")
        phone = input("Phone Number: ")
        password = input("Password: ")

        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        profile_dir = os.path.abspath("chrome_profile")
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            channel="chrome",
            headless=False,
            no_viewport=True,
            locale="en-NG",
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--start-maximized",
                "--disable-dev-shm-usage",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
            ],
        )
        self.browser = self.context.browser

        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        try:
            await page.click('button:has-text("Log In"), a:has-text("Log In")', timeout=5000)
            await asyncio.sleep(1)
        except Exception:
            pass

        await page.wait_for_selector('input[name="phone"]', state="visible", timeout=15000)
        await page.fill('input[name="phone"]', phone)
        await page.wait_for_selector('input[name="psd"]', state="visible", timeout=10000)
        await page.fill('input[name="psd"]', password)
        await page.click('button[name="logIn"]')
        await asyncio.sleep(5)
        logger.success("SportyBet login complete")

        self.auth.browser = self.browser
        self.auth.page = page
        self.executor = BetExecutor(self.alerter, self.account_manager, self.sportybet.matches, None)
        self.executor.current_account = self.account_manager.get_active()

        bal = float(input("Balance: "))
        self.executor.balance = bal
        await self.alerter.notify_startup(bal)

        self.running = True

        if not self._telegram_test_done:
            asyncio.create_task(self._test_telegram())
            self._telegram_test_done = True

        # Start SportyBet (REST + socket) — link generation unchanged
        await self.sportybet.start()
        await self.sportybet.attach_context(self.context, open_live_list=True)
        logger.success("SportyBet started")

        # Start Bet365 (WS goals)
        logger.info("Starting Bet365...")

        async def bet365_callback(match):
            home = match.get("home_team", "?") or "?"
            away = match.get("away_team", "?") or "?"
            minute = match.get("minute", 0)
            score = match.get("ss", "?-?") or "?-?"

            if home == "?" or away == "?":
                logger.debug(f"[BET365_RAW] mid={match.get('match_id')}")

            logger.info(f"[BET365] {home} {score} {away} min={minute}")

            if self.linker and self.linker.running:
                await self.linker.on_fast_feed(match)

        self.bet365.set_callback(bet365_callback)
        await self.bet365.start()
        logger.success("Bet365 started")

        # Polymarket backup
        self.polymarket.callback = self._on_poly_backup
        await self.polymarket.start()

        # Start linker
        await self.linker.start()
        logger.success("MatchLinker started")

        # AI ticker loop (screenshots of both frontends)
        asyncio.create_task(self._ai_slow_loop())

        # Health + cleanup
        self._cleanup_task = asyncio.create_task(self._page_cleanup_loop())
        asyncio.create_task(self._monitor_feeds())

        logger.success("=" * 70)
        logger.success("ALL SYSTEMS RUNNING")
        logger.success("=" * 70)

        await self.alerter.send(
            "🟢 BOT RUNNING\nAI tickers ≥3s → lock slow\nWS goal → Confirm"
        )

        while self.running:
            if self.executor and self.executor.stopped:
                logger.error("Executor stopped")
                self.running = False
                break
            await asyncio.sleep(1)

    async def _ai_slow_loop(self):
        """
        Groq vision: compare Bet365 + SportyBet match tickers only.
        Lock one slow game; unlock only not-slow / HT / FT.
        """
        interval = float(getattr(Config, "AI_SLOW_INTERVAL", 5))
        while self.running:
            try:
                b365_page = getattr(self.bet365, "_page", None)
                sb_page = getattr(self.sportybet, "_page", None)
                if not b365_page or not sb_page:
                    logger.debug("[AI] waiting for Bet365 + SportyBet pages")
                    await asyncio.sleep(interval)
                    continue

                b365_png = await b365_page.screenshot(type="png")
                sb_png = await sb_page.screenshot(type="png")
                lock = await self.slow_ai.update(b365_png, sb_png)
                await self.linker.apply_ai_lock(lock)
            except Exception as e:
                logger.error(f"[AI SLOW LOOP] {e}")
            await asyncio.sleep(interval)

    async def _monitor_feeds(self):
        while self.running:
            await asyncio.sleep(30)
            b365_ok = self.bet365.is_healthy() if self.bet365 else False
            sb_ok = self.sportybet.is_healthy() if self.sportybet else False

            slow_status = "NONE"
            if self.linker and self.linker.slow_match:
                sm = self.linker.slow_match
                slow_status = f"{sm.get('home_team', '?')} vs {sm.get('away_team', '?')}"

            logger.info(
                f"[HEALTH] Bet365={'✓' if b365_ok else '✗'} "
                f"SportyBet={'✓' if sb_ok else '✗'} Slow=[{slow_status}]"
            )

    async def _on_poly_backup(self, match):
        if self.bet365.is_healthy():
            return
        await self.linker.on_fast_feed(match)

    async def on_new_link(self, sb_match: dict):
        """Open SportyBet match (your URL logic) then AI-arm Over → Confirm ready."""
        mid = sb_match.get("match_id")
        if not mid or mid in self.match_pages or mid in self._opening_pages:
            return
        if len(self.match_pages) >= getattr(Config, "MAX_PREOPENED_MATCH_PAGES", 8):
            await self.alerter.send("⚠️ Max pages reached")
            return
        self._opening_pages.add(mid)
        try:
            ok = await self._open_match_page(sb_match)
            if not ok:
                return

            page = self.match_pages.get(mid)
            if not page or mid in self._armed_mids:
                return

            # AI: find market + stake (hard cap); Playwright arms, no final Confirm
            try:
                text = await page.inner_text("body")
            except Exception:
                text = ""
            plan = plan_over_market(
                text,
                sb_match.get("home_team", ""),
                sb_match.get("away_team", ""),
                int(sb_match.get("home_score") or 0),
                int(sb_match.get("away_score") or 0),
                str(sb_match.get("period") or sb_match.get("match_status") or ""),
                float(Config.MAX_PROFIT_PER_BET),
                float(getattr(self.executor, "balance", 0) or 0),
            )
            if hasattr(self.executor, "ai_arm_from_plan"):
                armed = await self.executor.ai_arm_from_plan(page, plan)
            else:
                # fallback: existing watch loop will arm Over
                armed = bool(plan.get("market_found"))
                logger.warning("[AI ARM] executor.ai_arm_from_plan missing — watch loop arms")

            if armed:
                self._armed_mids.add(mid)
                logger.success(f"[AI ARM] ready on Confirm for {mid}")
        finally:
            self._opening_pages.discard(mid)

    async def on_unlink(self, sb_match: dict):
        mid = sb_match.get("match_id")
        if mid:
            self._armed_mids.discard(mid)
            await self._close_match_page(mid, reason="unlinked")

    async def _close_match_page(self, mid: str, reason: str = ""):
        page = self.match_pages.pop(mid, None)
        self._page_contexts.pop(mid, None)
        self._armed_mids.discard(mid)
        if self.executor:
            try:
                self.executor.stop_watching(mid)
            except Exception:
                pass
        self._processing.pop(mid, None)
        if page is not None:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass
            logger.info(f"[CLEANUP] closed {mid} ({reason})")

    def _build_match_urls(self, sb_match: dict) -> list:
        """UNCHANGED — SportyBet link generation."""
        live = (sb_match.get("live_url") or "").strip()
        urls = []
        if live:
            urls.append(live)
        mid = sb_match.get("match_id") or ""
        home = sb_match.get("home_team") or "Home"
        away = sb_match.get("away_team") or "Away"
        country = sb_match.get("country") or "World"
        league = sb_match.get("league") or sb_match.get("tournament_name") or "League"
        eid = str(sb_match.get("event_id") or mid)
        if not eid.startswith("sr:match:"):
            digits = re.search(r"(\d+)", str(eid))
            eid = f"sr:match:{digits.group(1) if digits else mid}"
        path = f"{self._slug(country)}/{self._slug(league)}/{self._slug(home)}_vs_{self._slug(away)}/{eid}"
        desktop = f"https://www.sportybet.com/ng/sport/football/live/{path}"
        if desktop not in urls:
            urls.append(desktop)
        return urls

    @staticmethod
    def _slug(text: str) -> str:
        text = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "").strip())
        return text.strip("_") or "Unknown"

    async def _open_match_page(self, sb_match: dict) -> bool:
        """UNCHANGED open flow — your URL list + All tab + start_watching."""
        mid = sb_match.get("match_id")
        feed = self.sportybet.matches.get(mid) or {}
        merged = dict(feed)
        merged.update({k: v for k, v in (sb_match or {}).items() if v})
        home = (merged.get("home_team") or "").strip()
        away = (merged.get("away_team") or "").strip()
        urls = self._build_match_urls(merged)

        for attempt in range(1, 4):
            new_page = None
            try:
                new_page = await self.context.new_page()
                new_page.set_default_timeout(20000)
                try:
                    await new_page.bring_to_front()
                except Exception:
                    pass

                opened = False
                for url in urls:
                    logger.info(f"[PAGE] {attempt}/3 {home} vs {away}")
                    await new_page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(1.5)
                    for sel in (
                        'button:has-text("Accept")',
                        'button:has-text("OK")',
                        'button:has-text("Got it")',
                    ):
                        try:
                            loc = new_page.locator(sel)
                            if await loc.count() > 0:
                                await loc.first.click(timeout=500)
                        except Exception:
                            pass
                    if await self.executor.wait_match_details_ready(
                        new_page, home=home, away=away, timeout_s=12.0
                    ):
                        opened = True
                        break

                if not opened:
                    await new_page.close()
                    continue

                await self.executor.open_all_tab(new_page)
                self.match_pages[mid] = new_page
                self._page_contexts[mid] = self.context
                self.executor.start_watching(mid, new_page, home, away, match=merged)
                logger.success(f"[PAGE_READY] {home} vs {away}")
                await self.alerter.send(
                    f"📄 <b>Match page open</b>\n{home} vs {away}\n"
                    f"SportyBet id: <code>{mid}</code>"
                )
                return True
            except Exception as e:
                logger.warning(f"[PAGE] {e}")
                if new_page:
                    try:
                        await new_page.close()
                    except Exception:
                        pass
        await self.alerter.send(f"❌ <b>Could not open match page</b>\n{home} vs {away}")
        return False

    async def _page_cleanup_loop(self):
        while self.running:
            try:
                await asyncio.sleep(20)
                if self.linker.slow_match is None:
                    for mid in list(self.match_pages.keys()):
                        await self._close_match_page(mid, reason="no slow match")
                gc.collect()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[CLEANUP] {e}")

    async def on_goal(self, goal_data: dict):
        """WS goal on locked slow match → Prefer Confirm-only if AI-armed."""
        try:
            mid = goal_data["match_id"]
            home = goal_data.get("home_team", "?")
            away = goal_data.get("away_team", "?")
            scoring = goal_data.get("scoring_team", "?")
            goal_num = goal_data.get("goal_num", 1)

            await self.alerter.notify_goal_detected(
                f"{home} vs {away}", scoring, 0.0, goal_num
            )

            if mid not in self.match_pages:
                if not await self._open_match_page(
                    {
                        "match_id": mid,
                        "home_team": home,
                        "away_team": away,
                    }
                ):
                    await self.alerter.send("⚠️ Goal but page open failed")
                    return

            if self._processing.get(mid):
                return
            self._processing[mid] = True
            try:
                page = self.match_pages.get(mid)
                if not page or page.is_closed():
                    await self._close_match_page(mid, reason="dead")
                    return

                # Fast path: already on Confirm from AI arm
                if mid in self._armed_mids and hasattr(
                    self.executor, "click_confirm_only"
                ):
                    ok = await self.executor.click_confirm_only(page)
                    if ok:
                        bet_id = f"{mid}_G{goal_num}_{int(time.time())}"
                        stake = (
                            Config.VALIDATION_STAKE
                            if not self.executor.validation_passed
                            else self.executor.calc_stake(
                                self.executor.last_odds_used or 1.5
                            )
                        )
                        self.cashout.register(
                            mid, bet_id, stake, f"Over goal {goal_num}"
                        )
                        asyncio.create_task(
                            self.cashout.monitor(bet_id, goal_data, page)
                        )
                        self._armed_mids.discard(mid)
                        return

                # Fallback: full existing execute path
                match = {
                    "match_id": mid,
                    "home_team": home,
                    "away_team": away,
                }
                result = await self.executor.execute(
                    page,
                    match,
                    scoring,
                    goal_num,
                    expected_home_score=goal_data.get("home_score"),
                    expected_away_score=goal_data.get("away_score"),
                    detected_at=goal_data.get("detected_at"),
                )
                if result == BetResult.SUCCESS:
                    bet_id = f"{mid}_G{goal_num}_{int(time.time())}"
                    stake = (
                        Config.VALIDATION_STAKE
                        if not self.executor.validation_passed
                        else self.executor.calc_stake(self.executor.last_odds_used)
                    )
                    self.cashout.register(
                        mid, bet_id, stake, f"Over goal {goal_num}"
                    )
                    asyncio.create_task(
                        self.cashout.monitor(bet_id, goal_data, page)
                    )
                    self._armed_mids.discard(mid)
            finally:
                self._processing[mid] = False
        except Exception as e:
            logger.error(f"Goal handling error: {e}")
            await self.alerter.notify_error(f"Goal handling: {e}")

    def stop(self):
        self.running = False
        self.bet365.stop()
        self.linker.stop()
        self.sportybet.stop()


if __name__ == "__main__":
    bot = ArbitrageBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        bot.stop()
