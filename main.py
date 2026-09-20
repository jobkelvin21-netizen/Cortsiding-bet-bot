#!/usr/bin/env python3
"""Main entry - Manual slow game, no MatchLinker."""

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
from feeds.sportybet_api import SportyBetFeed
from core.executor import BetExecutor, BetResult
from core.cashout import CashOutManager
from utils.telegram import TelegramAlerter, TelegramCommandHandler
from utils.account_manager import AccountManager


class ArbitrageBot:
    def __init__(self):
        self.account_manager = AccountManager()
        self.alerter = TelegramAlerter()
        self.auth = SportyBetAuth()

        self.sportybet = SportyBetFeed()
        self.sportybet.set_alerter(self.alerter)

        self.bet365 = Bet365Feed()
        self.bet365.set_alerter(self.alerter)

        self.executor = None
        self.cashout = CashOutManager(self.alerter)

        self.match_pages = {}
        self._processing = {}
        self.browser = None
        self.context = None
        self.playwright = None
        
        # Manual slow game state
        self._manual_slow_match = None
        self._previous_ht_match = None
        self._awaiting_switch_decision = False
        
        # For goal detection
        self._last_score = None

    async def start_bot(self):
        """Called when user sends 'start' in Telegram."""
        if hasattr(self, 'running') and self.running:
            await self.alerter.send("⚠️ Bot already running!")
            return
            
        await self.alerter.send(
            "🟢 <b>Bot Starting</b>\n\n"
            "Send: <code>login PHONE_NUMBER PASSWORD</code>"
        )

    async def handle_login(self, phone: str, password: str):
        """Process login from Telegram."""
        from playwright.async_api import async_playwright

        await self.alerter.send(f"🔐 Logging in...")

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
            ],
        )
        self.browser = self.context.browser

        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        try:
            await page.click('button:has-text("Log In")', timeout=5000)
        except:
            pass

        await page.fill('input[name="phone"]', phone)
        await page.fill('input[name="psd"]', password)
        await page.click('button[name="logIn"]')
        await asyncio.sleep(5)
        
        await self.alerter.send("✅ <b>Login successful!</b>\nSend: <code>balance 5000</code>")

        self.executor = BetExecutor(self.alerter, self.account_manager, {}, None)
        self.executor.current_account = self.account_manager.get_active()

    async def handle_balance(self, amount: float):
        """Set balance from Telegram."""
        if not self.executor:
            await self.alerter.send("❌ Not logged in!")
            return
            
        self.executor.balance = amount
        await self.alerter.notify_startup(amount)
        await self._start_feeds()

    async def _start_feeds(self):
        """Start Bet365 feed after login."""
        self.running = True

        # Bet365 callback - direct goal detection
        async def bet365_callback(match):
            mid = match.get("match_id", "")
            home = match.get("home_team", "?")
            away = match.get("away_team", "?")
            score = match.get("ss", "?-?")
            status = str(match.get("match_status") or "").lower()

            # Check if this is our tracked match
            if self._manual_slow_match and str(mid) == str(self._manual_slow_match.get("bet365_id")):
                logger.info(f"[GOAL CHECK] {home} {score} {away}")
                
                # Parse score
                try:
                    h, a = map(int, score.split("-"))
                    new_score = (h, a)
                except:
                    return
                
                # Check HT
                if any(x in status for x in ["ht", "half time"]):
                    if not self._manual_slow_match.get("in_ht"):
                        self._manual_slow_match["in_ht"] = True
                        self._previous_ht_match = dict(self._manual_slow_match)
                        await self.alerter.notify_ht_detected(self._manual_slow_match.get("teams", "Match"))
                
                # Check return from HT
                elif self._manual_slow_match.get("in_ht") and any(x in status for x in ["2nd", "live"]):
                    self._manual_slow_match["in_ht"] = False
                    if self._previous_ht_match:
                        self._awaiting_switch_decision = True
                        await self.alerter.notify_match_returned(self._previous_ht_match.get("teams", "Match"))
                
                # Check goal
                if self._last_score and new_score != self._last_score and sum(new_score) > sum(self._last_score):
                    scoring = home if new_score[0] > self._last_score[0] else away
                    await self._on_goal(scoring, new_score[0], new_score[1])
                
                self._last_score = new_score

        self.bet365.set_callback(bet365_callback)
        await self.bet365.start()
        
        await self.alerter.send(
            "✅ <b>Ready!</b> Send slow game:\n"
            "<code>slow</code>\n"
            "<code>SportyBet- URL</code>\n"
            "<code>Bet365- ID</code>\n"
            "<code>Teams- Team A vs Team B</code>"
        )

    async def handle_slow_game(self, sporty_url: str, bet365_id: str, teams: str):
        """Process manual slow game from Telegram."""
        if not self.executor:
            await self.alerter.send("❌ Send <code>start</code> first")
            return

        # Close previous
        if self._manual_slow_match and self._manual_slow_match.get("match_id"):
            await self._close_match_page(self._manual_slow_match["match_id"])

        self._manual_slow_match = {
            "url": sporty_url,
            "bet365_id": bet365_id,
            "teams": teams,
            "home": teams.split(" vs ")[0] if " vs " in teams else teams,
            "away": teams.split(" vs ")[1] if " vs " in teams else "",
            "in_ht": False
        }
        self._last_score = None

        await self.alerter.notify_manual_slow_set(teams, bet365_id)

        # Open the pasted URL
        ok = await self._open_pasted_url(self._manual_slow_match)
        
        if ok:
            await self.alerter.send(f"✅ Armed! Waiting for Bet365 goal on ID {bet365_id}")
        else:
            await self.alerter.send("❌ Failed to load page")

    async def _open_pasted_url(self, slow_match: dict) -> bool:
        """Open the manually pasted SportyBet URL and arm with Groq."""
        url = slow_match.get("url")
        home = slow_match.get("home", "")
        away = slow_match.get("away", "")
        teams = slow_match.get("teams", "")
        
        mid = f"manual_{hash(url) % 10000000}"
        slow_match["match_id"] = mid

        new_page = None
        try:
            new_page = await self.context.new_page()
            new_page.set_default_timeout(20000)
            
            await new_page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(1.5)
            
            # Handle popups
            for sel in ('button:has-text("Accept")', 'button:has-text("OK")'):
                try:
                    loc = new_page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=500)
                except:
                    pass

            # Wait for match details
            if not await self.executor.wait_match_details_ready(
                new_page, home=home, away=away, timeout_s=12.0
            ):
                await new_page.close()
                return False

            # Open All tab
            await self.executor.open_all_tab(new_page)
            
            self.match_pages[mid] = new_page
            
            # Start watching - Groq will arm the bet
            self.executor.start_watching(mid, new_page, home, away, match={
                "match_id": mid, "home_team": home, "away_team": away,
            })
            
            logger.success(f"[PAGE_READY] {teams}")
            return True
            
        except Exception as e:
            logger.error(f"[PAGE] {e}")
            if new_page:
                try:
                    await new_page.close()
                except:
                    pass
            return False

    async def _on_goal(self, scoring_team: str, home_score: int, away_score: int):
        """Bet365 goal detected - click Confirm."""
        if not self._manual_slow_match:
            return
            
        mid = self._manual_slow_match.get("match_id")
        if not mid or mid not in self.match_pages:
            return

        if self._processing.get(mid):
            return
        self._processing[mid] = True
        
        try:
            teams = self._manual_slow_match.get("teams", "Match")
            await self.alerter.notify_goal_detected(teams, scoring_team, 0.0, home_score + away_score)

            page = self.match_pages.get(mid)
            if not page or page.is_closed():
                return

            # Click Confirm only (Groq already armed)
            ok = await self.executor.click_confirm_only(page)
            
            if ok:
                bet_id = f"{mid}_G{home_score + away_score}_{int(time.time())}"
                stake = self.executor.calc_stake(self.executor.last_odds_used or 1.5)
                
                await self.alerter.notify_bet_placed(
                    teams, f"Over {home_score + away_score}.5", stake,
                    self.executor.last_odds_used, bet_id, 1, 1
                )
                
                # Rebet logic
                await self._rebet_loop(page, teams, home_score + away_score)
                
        finally:
            self._processing[mid] = False

    async def _rebet_loop(self, page, match_name: str, goal_num: int):
        """Handle rebets."""
        for i in range(1, getattr(Config, "MAX_STACK_PER_GOAL", 3)):
            await asyncio.sleep(getattr(Config, "MIN_STACK_DELAY", 0.8))
            if await self.executor._try_rebet_once(page):
                await self.alerter.send(f"✅ Rebet #{i+1} placed")

    async def handle_switch_decision(self, decision: str):
        """Handle yes/no for switch-back."""
        if not self._awaiting_switch_decision:
            return
            
        self._awaiting_switch_decision = False
        
        if decision.lower() == "yes" and self._previous_ht_match:
            await self.handle_slow_game(
                self._previous_ht_match["url"],
                self._previous_ht_match["bet365_id"],
                self._previous_ht_match["teams"]
            )
            await self.alerter.send("🔄 Switched back")
        else:
            await self.alerter.send("✅ Staying on current")
        
        self._previous_ht_match = None

    async def _close_match_page(self, mid: str):
        page = self.match_pages.pop(mid, None)
        if self.executor:
            try:
                self.executor.stop_watching(mid)
            except:
                pass
        if page:
            try:
                if not page.is_closed():
                    await page.close()
            except:
                pass

    def stop(self):
        self.running = False
        self.bet365.stop()
        if self.playwright:
            try:
                asyncio.create_task(self.playwright.stop())
            except:
                pass


if __name__ == "__main__":
    bot = ArbitrageBot()
    handler = TelegramCommandHandler(bot, bot.alerter)
    
    try:
        asyncio.run(handler.start_polling())
    except KeyboardInterrupt:
        bot.stop()
