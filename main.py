#!/usr/bin/env python3
"""Main entry - Manual slow game, both login methods."""

import asyncio
import os
import sys
import time
from loguru import logger

from config import Config
from feeds.bet365_ws import Bet365Feed
from core.executor import BetExecutor
from core.cashout import CashOutManager
from utils.telegram import TelegramAlerter
from utils.account_manager import AccountManager


class ArbitrageBot:
    def __init__(self):
        self.account_manager = AccountManager()
        self.alerter = TelegramAlerter()

        self.bet365 = Bet365Feed()
        self.bet365.set_alerter(self.alerter)

        self.executor = None
        self.cashout = CashOutManager(self.alerter)

        self.match_pages = {}
        self._processing = {}
        self.browser = None
        self.context = None
        self.playwright = None
        
        # State
        self._manual_slow_match = None
        self._previous_ht_match = None
        self._awaiting_switch_decision = False
        self._last_score = None
        self._current_total_goals = 0
        self._rearming = False
        self._logged_in = False

    # ==================== LOGIN METHODS ====================

    async def terminal_login(self):
        """Method 1: Terminal asks for login when you run python main.py"""
        print("\n" + "="*50)
        print("SPORTYBET LOGIN")
        print("="*50)
        phone = input("Phone Number: ")
        password = input("Password: ")

        await self._do_login(phone, password)
        
        bal = float(input("\nBalance: "))
        await self._finish_setup(bal)

    async def telegram_login(self, phone: str, password: str):
        """Method 2: Login via Telegram command"""
        await self.alerter.send("🔐 Logging in...")
        await self._do_login(phone, password)
        await self.alerter.send("✅ Login successful!\nSend: <code>balance 5000</code>")

    async def _do_login(self, phone: str, password: str):
        """Common login logic."""
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
        except:
            pass

        await page.fill('input[name="phone"]', phone)
        await page.fill('input[name="psd"]', password)
        await page.click('button[name="logIn"]')
        await asyncio.sleep(5)
        
        logger.success("SportyBet login complete")

    async def _finish_setup(self, balance: float):
        """Setup executor and start feeds."""
        self.executor = BetExecutor(self.alerter, self.account_manager, {}, None)
        self.executor.balance = balance
        self.executor.current_account = self.account_manager.get_active()
        self._logged_in = True
        
        await self.alerter.notify_startup(balance)
        await self._start_feeds()

    # ==================== MAIN LOGIC ====================

    async def start(self):
        """Entry point - uses terminal login by default."""
        await self.terminal_login()
        
        # Keep running
        while True:
            await asyncio.sleep(60)

    async def _start_feeds(self):
        """Start Bet365 feed with goal detection."""
        async def bet365_callback(match):
            mid = match.get("match_id", "")
            home = match.get("home_team", "?")
            away = match.get("away_team", "?")
            score = match.get("ss", "?-?")
            status = str(match.get("match_status") or "").lower()

            if not self._manual_slow_match or self._rearming:
                return

            if str(mid) != str(self._manual_slow_match.get("bet365_id")):
                return

            try:
                h, a = map(int, score.split("-"))
                new_score = (h, a)
                total = h + a
            except:
                return

            # HT detection
            if any(x in status for x in ["ht", "half time"]):
                if not self._manual_slow_match.get("in_ht"):
                    self._manual_slow_match["in_ht"] = True
                    self._previous_ht_match = dict(self._manual_slow_match)
                    await self.alerter.notify_ht_detected(self._manual_slow_match.get("teams", "Match"))
            
            # Return from HT
            elif self._manual_slow_match.get("in_ht") and any(x in status for x in ["2nd", "live"]):
                self._manual_slow_match["in_ht"] = False
                if self._previous_ht_match:
                    self._awaiting_switch_decision = True
                    await self.alerter.notify_match_returned(self._previous_ht_match.get("teams", "Match"))

            # First time
            if self._last_score is None:
                self._last_score = new_score
                self._current_total_goals = total
                return

            # Goal detected
            if total > self._current_total_goals:
                scoring = home if new_score[0] > self._last_score[0] else away
                self._last_score = new_score
                self._current_total_goals = total
                
                logger.success(f"[GOAL] {scoring} scored! Now {h}-{a}")
                asyncio.create_task(self._handle_goal_and_rearm(scoring, h, a, total))

        self.bet365.set_callback(bet365_callback)
        await self.bet365.start()
        logger.success("Bet365 started - Send slow game via Telegram")

        # Start Telegram handler
        handler = TelegramCommandHandler(self, self.alerter)
        asyncio.create_task(handler.start_polling())

        print("\n" + "="*50)
        print("BOT RUNNING - Send slow game via Telegram")
        print("="*50)

    async def _handle_goal_and_rearm(self, scoring_team: str, home_score: int, away_score: int, total_goals: int):
        """Bet, then re-scan and re-arm for next Over line."""
        if not self._manual_slow_match:
            return
            
        mid = self._manual_slow_match.get("match_id")
        if not mid or mid not in self.match_pages:
            return

        if self._processing.get(mid):
            return
            
        self._processing[mid] = True
        self._rearming = True
        
        try:
            teams = self._manual_slow_match.get("teams", "Match")
            page = self.match_pages.get(mid)
            
            if not page or page.is_closed():
                return

            # Notify and bet
            await self.alerter.notify_goal_detected(teams, scoring_team, 0.0, total_goals)
            
            ok = await self.executor.click_confirm_only(page)
            
            if ok:
                stake = self.executor.calc_stake(self.executor.last_odds_used or 1.5)
                bet_id = f"{mid}_G{total_goals}_{int(time.time())}"
                
                await self.alerter.notify_bet_placed(
                    teams, f"Over {total_goals - 1}.5", stake,
                    self.executor.last_odds_used, bet_id, 1, 1
                )
                
                # Rebets
                for i in range(1, getattr(Config, "MAX_STACK_PER_GOAL", 3)):
                    await asyncio.sleep(getattr(Config, "MIN_STACK_DELAY", 0.8))
                    if await self.executor._try_rebet_once(page):
                        await self.alerter.send(f"✅ Rebet #{i+1}")
            else:
                await self.alerter.send("❌ Bet failed, re-arming...")

            # RE-ARM for next goal
            await self.alerter.send(f"🔄 Re-arming for Over {total_goals + 0.5}...")
            
            self.executor.stop_watching(mid)
            await asyncio.sleep(2)
            
            watch = self.executor.start_watching(
                mid, page, 
                self._manual_slow_match.get("home", ""),
                self._manual_slow_match.get("away", ""),
                match={
                    "match_id": mid,
                    "home_team": self._manual_slow_match.get("home", ""),
                    "away_team": self._manual_slow_match.get("away", ""),
                    "home_score": home_score,
                    "away_score": away_score,
                }
            )
            
            armed = await self.executor.ai_arm_from_plan(page, None, watch)
            
            if armed:
                await self.alerter.send(f"✅ Armed on Over {total_goals + 0.5}")
            else:
                await self.alerter.send("⚠️ Re-arm failed")
                
        finally:
            self._processing[mid] = False
            self._rearming = False

    # ==================== TELEGRAM COMMANDS ====================

    async def handle_balance(self, amount: float):
        """Set balance via Telegram."""
        if self._logged_in:
            await self.alerter.send("❌ Already running!")
            return
        await self._finish_setup(amount)

    async def handle_slow_game(self, sporty_url: str, bet365_id: str, teams: str):
        """Process slow game from Telegram."""
        if not self._logged_in:
            await self.alerter.send("❌ Not logged in! Send <code>start</code> first")
            return

        # Close previous
        if self._manual_slow_match:
            old_mid = self._manual_slow_match.get("match_id")
            if old_mid:
                await self._close_match_page(old_mid)

        self._last_score = None
        self._current_total_goals = 0
        self._rearming = False

        self._manual_slow_match = {
            "url": sporty_url,
            "bet365_id": bet365_id,
            "teams": teams,
            "home": teams.split(" vs ")[0] if " vs " in teams else teams,
            "away": teams.split(" vs ")[1] if " vs " in teams else "",
            "in_ht": False
        }

        await self.alerter.notify_manual_slow_set(teams, bet365_id)
        
        ok = await self._open_and_arm(self._manual_slow_match)
        
        if ok:
            await self.alerter.send(f"✅ Armed! Waiting for goal on ID {bet365_id}")
        else:
            await self.alerter.send("❌ Failed to load")

    async def _open_and_arm(self, slow_match: dict) -> bool:
        """Open URL and arm with Groq."""
        url = slow_match.get("url")
        home = slow_match.get("home", "")
        away = slow_match.get("away", "")
        
        mid = f"manual_{hash(url) % 10000000}"
        slow_match["match_id"] = mid

        try:
            new_page = await self.context.new_page()
            new_page.set_default_timeout(20000)
            
            await new_page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(1.5)
            
            for sel in ('button:has-text("Accept")', 'button:has-text("OK")'):
                try:
                    loc = new_page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=500)
                except:
                    pass

            if not await self.executor.wait_match_details_ready(new_page, home, away, timeout_s=12.0):
                await new_page.close()
                return False

            await self.executor.open_all_tab(new_page)
            self.match_pages[mid] = new_page
            
            watch = self.executor.start_watching(
                mid, new_page, home, away,
                match={"match_id": mid, "home_team": home, "away_team": away}
            )
            
            armed = await self.executor.ai_arm_from_plan(new_page, None, watch)
            return armed
            
        except Exception as e:
            logger.error(f"[ARM] {e}")
            return False

    async def handle_switch_decision(self, decision: str):
        """Handle switch back."""
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
            await self.alerter.send("✅ Staying")
        
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
        self.bet365.stop()
        if self.playwright:
            try:
                asyncio.create_task(self.playwright.stop())
            except:
                pass


# ==================== TELEGRAM HANDLER ====================

class TelegramCommandHandler:
    def __init__(self, bot: ArbitrageBot, alerter: TelegramAlerter):
        self.bot = bot
        self.alerter = alerter
        self._last_update_id = 0
        
    async def start_polling(self):
        """Poll Telegram for commands."""
        import aiohttp
        
        while True:
            try:
                await self._poll_once()
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[TG] {e}")
                await asyncio.sleep(5)
                
    async def _poll_once(self):
        import aiohttp
        
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {"offset": self._last_update_id + 1, "limit": 10}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                if not data.get("ok"):
                    return
                    
                for update in data.get("result", []):
                    self._last_update_id = max(self._last_update_id, update["update_id"])
                    await self._handle_update(update)

    async def _handle_update(self, update: dict):
        message = update.get("message", {})
        text = message.get("text", "").strip()
        chat_id = message.get("chat", {}).get("id")
        
        if str(chat_id) != str(Config.TELEGRAM_CHAT_ID):
            return
            
        if not text:
            return
            
        text_lower = text.lower()
        
        # Start command - Telegram login mode
        if text_lower == "start":
            await self.alerter.send(
                "🟢 <b>Bot Starting</b>\n\n"
                "Choose:\n"
                "<code>login PHONE PASSWORD</code> - Login via Telegram\n"
                "Or run <code>python main.py</code> for terminal login"
            )
            return
            
        # Stop
        if text_lower == "stop":
            await self.alerter.send("🛑 Stopping...")
            self.bot.stop()
            return
            
        # Login via Telegram
        if text_lower.startswith("login "):
            parts = text.split(maxsplit=2)
            if len(parts) >= 3:
                await self.bot.telegram_login(parts[1], parts[2])
            else:
                await self.alerter.send("❌ Format: <code>login PHONE PASSWORD</code>")
            return
            
        # Balance
        if text_lower.startswith("balance "):
            try:
                amount = float(text.split()[1])
                await self.bot.handle_balance(amount)
            except:
                await self.alerter.send("❌ Format: <code>balance 5000</code>")
            return
            
        # Slow game
        if text_lower.startswith("slow"):
            await self._handle_slow_command(text)
            return
            
        # Yes/No
        if text_lower in ("yes", "no") and self.bot._awaiting_switch_decision:
            await self.bot.handle_switch_decision(text_lower)
            return
            
    async def _handle_slow_command(self, text: str):
        """Parse slow command."""
        lines = text.strip().split("\n")
        
        sporty_url = None
        bet365_id = None
        teams = None
        
        for line in lines:
            line = line.strip()
            if line.lower().startswith("sportybet-"):
                sporty_url = line.split("-", 1)[1].strip()
            elif line.lower().startswith("bet365-"):
                bet365_id = line.split("-", 1)[1].strip()
            elif line.lower().startswith("teams-"):
                teams = line.split("-", 1)[1].strip()
                
        if not sporty_url or not bet365_id:
            await self.alerter.send(
                "❌ Format:\n<code>slow</code>\n<code>SportyBet- URL</code>\n<code>Bet365- ID</code>\n<code>Teams- Team A vs Team B</code>"
            )
            return
            
        await self.bot.handle_slow_game(sporty_url, bet365_id, teams or "Unknown")


if __name__ == "__main__":
    bot = ArbitrageBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        bot.stop()
