#!/usr/bin/env python3
"""Manual slow via Telegram → Groq arm → Confirm on goal; cashout only if goal cancelled."""

import asyncio
import os
import re
import time
from loguru import logger

from config import Config
from feeds.bet365_ws import Bet365Feed
from core.executor import BetExecutor
from utils.telegram import TelegramAlerter, TelegramCommandHandler
from utils.account_manager import AccountManager


def _normalize_bet365_id(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = re.search(r"OV\d+-(\d+)_\d+", raw, re.I)
    if m:
        return m.group(1)
    m = re.search(r"(\d{6,})", raw)
    if m:
        return m.group(1)
    return raw


class ArbitrageBot:
    def __init__(self):
        self.account_manager = AccountManager()
        self.alerter = TelegramAlerter()
        # Pass context later after login
        self.bet365 = Bet365Feed(existing_context=None)
        self.bet365.set_alerter(self.alerter)
        self.executor = None
        self.match_pages = {}
        self._processing = {}
        self.context = None
        self.playwright = None
        self._manual_slow_match = None
        self._previous_ht_match = None
        self._awaiting_switch_decision = False
        self._last_score = None
        self._current_total_goals = 0
        self._rearming = False
        self._logged_in = False
        self._has_open_bet = False

    async def terminal_login(self):
        print("\n" + "=" * 50)
        print("SPORTYBET LOGIN")
        print("=" * 50)
        phone = input("Phone Number: ")
        password = input("Password: ")
        await self._do_login(phone, password)
        bal = float(input("\nBalance: "))
        await self._finish_setup(bal)

    async def telegram_login(self, phone: str, password: str):
        await self.alerter.send("🔐 Logging in...")
        await self._do_login(phone, password)
        await self.alerter.send("✅ Login OK — send <code>/balance 5000</code>")

    async def _do_login(self, phone: str, password: str):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        # FIX: Use your Chrome profile with VPN extension
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=os.path.expanduser("~/.config/google-chrome"),
            channel="chrome",
            headless=False,
            no_viewport=True,
            locale="en-NG",
            # FIX: Keep VPN extension enabled
            ignore_default_args=[
                "--enable-automation",
                "--disable-extensions",
                "--disable-component-extensions-with-background-pages",
            ],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--start-maximized",
                "--disable-dev-shm-usage",
            ],
        )
        
        # FIX: Pass context to Bet365 so it uses same browser
        self.bet365._context = self.context
        self.bet365._owns_context = False
        
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        try:
            await page.click('button:has-text("Log In"), a:has-text("Log In")', timeout=5000)
            await asyncio.sleep(1)
        except Exception:
            pass
        await page.fill('input[name="phone"]', phone)
        await page.fill('input[name="psd"]', password)
        await page.click('button[name="logIn"]')
        await asyncio.sleep(5)
        logger.success("SportyBet login complete")

    async def _finish_setup(self, balance: float):
        self.executor = BetExecutor(self.alerter, self.account_manager, {}, None)
        self.executor.balance = balance
        self.executor.current_account = self.account_manager.get_active()
        self._logged_in = True
        await self.alerter.notify_startup(balance)
        await self._start_feeds()

    async def start(self):
        handler = TelegramCommandHandler(self, self.alerter)
        asyncio.create_task(handler.start_polling())
        await self.terminal_login()
        while True:
            await asyncio.sleep(60)

    async def _start_feeds(self):
        async def bet365_callback(match):
            if not self._manual_slow_match or self._rearming:
                return
            mid = str(match.get("match_id") or "")
            locked = str(self._manual_slow_match.get("bet365_id") or "")
            if not mid or mid != locked:
                return

            score = match.get("ss") or ""
            try:
                h, a = map(int, score.split("-"))
            except Exception:
                h = int(match.get("home_score") or 0)
                a = int(match.get("away_score") or 0)
            new_score = (h, a)
            total = h + a

            if self._last_score is None:
                self._last_score = new_score
                self._current_total_goals = total
                return

            # GOAL CANCELLED / DISALLOWED
            if total < self._current_total_goals:
                logger.warning(
                    f"[DISALLOWED] score {self._last_score} → {new_score} "
                    f"(total {self._current_total_goals} → {total})"
                )
                self._last_score = new_score
                self._current_total_goals = total
                if self._has_open_bet:
                    asyncio.create_task(
                        self._handle_disallowed(h, a, total)
                    )
                return

            # NORMAL GOAL
            if total > self._current_total_goals:
                home = self._manual_slow_match.get("home", "")
                away = self._manual_slow_match.get("away", "")
                scoring = home if new_score[0] > self._last_score[0] else away
                self._last_score = new_score
                self._current_total_goals = total
                logger.success(f"[GOAL] {scoring} → {h}-{a}")
                asyncio.create_task(self._handle_goal_and_rearm(scoring, h, a, total))

        self.bet365.set_callback(bet365_callback)
        await self.bet365.start()
        logger.success("Bet365 up — send /slow on @Kelves_bot")

    async def _handle_disallowed(self, home_score: int, away_score: int, total_goals: int):
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

            await self.alerter.send(
                f"🚫 <b>GOAL CANCELLED</b>\n⚽ {teams}\n"
                f"Score now {home_score}-{away_score}\n💸 Cashing out…"
            )
            ok = await self.executor.cashout_disallowed(
                page, description=f"{teams} goal cancelled → {home_score}-{away_score}"
            )
            self._has_open_bet = False

            await self.alerter.send(f"🔄 Re-arm Over {total_goals + 0.5} after cancel…")
            self.executor.stop_watching(mid)
            await asyncio.sleep(1.5)
            self.executor.start_watching(
                mid,
                page,
                self._manual_slow_match.get("home", ""),
                self._manual_slow_match.get("away", ""),
                home_score=home_score,
                away_score=away_score,
                match={
                    "match_id": mid,
                    "home_team": self._manual_slow_match.get("home", ""),
                    "away_team": self._manual_slow_match.get("away", ""),
                    "home_score": home_score,
                    "away_score": away_score,
                },
            )
            watch = self.executor.get_watch(mid)
            armed = await self.executor.ai_arm_from_plan(page, None, watch)
            await self.alerter.send(
                f"✅ Re-armed after cancel" if armed else "⚠️ Re-arm failed after cancel"
            )
            if not ok:
                await self.alerter.send("⚠️ Cashout may have failed — check My Bets")
        finally:
            self._processing[mid] = False
            self._rearming = False

    async def _handle_goal_and_rearm(self, scoring_team, home_score, away_score, total_goals):
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

            await self.alerter.notify_goal_detected(teams, scoring_team, 0.0, total_goals)
            ok = await self.executor.click_confirm_only(page)
            if ok:
                self._has_open_bet = True
                stake = self.executor.calc_stake(self.executor.last_odds_used or 1.5)
                bet_id = f"{mid}_G{total_goals}_{int(time.time())}"
                await self.alerter.notify_bet_placed(
                    teams,
                    f"Over {total_goals - 0.5}",
                    stake,
                    self.executor.last_odds_used,
                    bet_id,
                    1,
                    1,
                )
                for i in range(1, getattr(Config, "MAX_STACK_PER_GOAL", 3)):
                    await asyncio.sleep(getattr(Config, "MIN_STACK_DELAY", 0.8))
                    if await self.executor._try_rebet_once(page):
                        await self.alerter.send(f"✅ Rebet #{i + 1}")
            else:
                await self.alerter.send("❌ Confirm failed — re-arming")
                self._has_open_bet = False

            await self.alerter.send(f"🔄 Re-arm Over {total_goals + 0.5}…")
            self.executor.stop_watching(mid)
            await asyncio.sleep(1.5)
            self.executor.start_watching(
                mid,
                page,
                self._manual_slow_match.get("home", ""),
                self._manual_slow_match.get("away", ""),
                home_score=home_score,
                away_score=away_score,
                match={
                    "match_id": mid,
                    "home_team": self._manual_slow_match.get("home", ""),
                    "away_team": self._manual_slow_match.get("away", ""),
                    "home_score": home_score,
                    "away_score": away_score,
                },
            )
            watch = self.executor.get_watch(mid)
            armed = await self.executor.ai_arm_from_plan(page, None, watch)
            await self.alerter.send(
                f"✅ Armed Over {total_goals + 0.5}" if armed else "⚠️ Re-arm failed"
            )
        finally:
            self._processing[mid] = False
            self._rearming = False

    async def handle_balance(self, amount: float):
        if self._logged_in:
            await self.alerter.send("❌ Already running")
            return
        await self._finish_setup(amount)

    async def handle_slow_game(self, sporty_url: str, bet365_id: str, teams: str):
        if not self._logged_in:
            await self.alerter.send("❌ Login first")
            return

        if self._manual_slow_match:
            old = self._manual_slow_match.get("match_id")
            if old:
                await self._close_match_page(old)

        self._last_score = None
        self._current_total_goals = 0
        self._rearming = False
        self._has_open_bet = False

        b365 = _normalize_bet365_id(bet365_id)
        home, away = teams, ""
        if " vs " in teams.lower():
            parts = re.split(r"\s+vs\s+", teams, flags=re.I, maxsplit=1)
            home, away = parts[0].strip(), parts[1].strip()

        self._manual_slow_match = {
            "url": sporty_url.strip(),
            "bet365_id": b365,
            "teams": teams,
            "home": home,
            "away": away,
            "in_ht": False,
        }
        await self.alerter.notify_manual_slow_set(teams, b365)
        ok = await self._open_and_arm(self._manual_slow_match)
        await self.alerter.send(
            f"✅ Armed — waiting goal id={b365}" if ok else "❌ Open/arm failed"
        )

    async def _open_and_arm(self, slow: dict) -> bool:
        url = slow.get("url")
        home, away = slow.get("home", ""), slow.get("away", "")
        mid = f"manual_{abs(hash(url)) % 10_000_000}"
        slow["match_id"] = mid
        try:
            page = await self.context.new_page()
            page.set_default_timeout(20000)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(1.5)
            for sel in ('button:has-text("Accept")', 'button:has-text("OK")'):
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=500)
                except Exception:
                    pass

            if not await self.executor.wait_match_details_ready(page, home, away, 12.0):
                await page.close()
                return False

            await self.executor.open_all_tab(page)
            self.match_pages[mid] = page
            self.executor.start_watching(
                mid,
                page,
                home,
                away,
                match={"match_id": mid, "home_team": home, "away_team": away},
            )
            watch = self.executor.get_watch(mid)
            return await self.executor.ai_arm_from_plan(page, None, watch)
        except Exception as e:
            logger.error(f"[ARM] {e}")
            return False

    async def handle_switch_decision(self, decision: str):
        if not self._awaiting_switch_decision:
            return
        self._awaiting_switch_decision = False
        if decision == "yes" and self._previous_ht_match:
            p = self._previous_ht_match
            await self.handle_slow_game(p["url"], p["bet365_id"], p["teams"])
            await self.alerter.send("🔄 Switched back")
        else:
            await self.alerter.send("✅ Staying")
        self._previous_ht_match = None

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

    def stop(self):
        self.bet365.stop()


if __name__ == "__main__":
    bot = ArbitrageBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        bot.stop()
