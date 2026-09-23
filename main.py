#!/usr/bin/env python3
"""Telegram control; FI-only goals; always right Over market; safe one-game."""

import asyncio
import os
import re
import sys
import time
from loguru import logger

from config import Config
from feeds.bet365_ws import Bet365Feed
from core.executor import BetExecutor
from utils.telegram import TelegramAlerter, TelegramCommandHandler
from utils.account_manager import AccountManager

logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
)


def _fi(raw: str) -> str:
    m = re.search(r"(\d{6,})", (raw or "").strip())
    return m.group(1) if m else ""


class ArbitrageBot:
    def __init__(self):
        self.account_manager = AccountManager()
        self.alerter = TelegramAlerter()
        self.bet365 = Bet365Feed()
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
        self._bot_active = False

    async def terminal_login(self):
        print("SPORTYBET LOGIN")
        phone = input("Phone: ")
        password = input("Password: ")
        await self._do_login(phone, password)
        bal = float(input("Balance: "))
        await self._finish_setup(bal)

    async def telegram_login(self, phone: str, password: str):
        await self.alerter.send("🔐 Logging in...")
        try:
            await self._do_login(phone, password)
            await self.alerter.send("✅ Login OK — send /balance AMOUNT then /startbot")
        except Exception as e:
            await self.alerter.send(f"❌ {e}")

    async def _do_login(self, phone: str, password: str):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        profile = os.path.abspath("chrome_profile_sporty")
        os.makedirs(profile, exist_ok=True)

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=profile,
            channel="chrome",
            headless=False,
            no_viewport=True,
            locale="en-NG",
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--start-maximized",
                "--disable-features=ChromeWhatsNewUI,InfiniteSessionRestore",
            ],
        )

        if self.context.pages:
            page = self.context.pages[0]
        else:
            page = await self.context.new_page()

        await page.goto(
            "https://www.sportybet.com/ng/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await asyncio.sleep(2)
        try:
            await page.click(
                'button:has-text("Log In"), a:has-text("Log In")', timeout=5000
            )
            await asyncio.sleep(1)
        except Exception:
            pass
        await page.fill('input[name="phone"], input[type="tel"]', phone)
        await page.fill('input[name="psd"], input[type="password"]', password)
        await page.click('button[name="logIn"], button:has-text("Log In")')
        await asyncio.sleep(5)

    async def _finish_setup(self, balance: float):
        self.executor = BetExecutor(self.alerter, self.account_manager, {}, None)
        self.executor.balance = balance
        self.executor.current_account = self.account_manager.get_active()
        self.executor.validation_bet_placed = True
        self.executor.validation_passed = True
        self._logged_in = True
        await self.alerter.notify_startup(balance)

    async def handle_balance(self, amount: float):
        if self._logged_in:
            await self.alerter.send("Already logged in")
            return
        await self._finish_setup(amount)

    async def start(self):
        handler = TelegramCommandHandler(self, self.alerter)
        asyncio.create_task(handler.start_polling())
        await self.terminal_login()
        while True:
            await asyncio.sleep(60)

    async def start_bot(self):
        if not self._logged_in:
            await self.alerter.send("❌ Login + balance first")
            return
        if self._bot_active:
            await self.alerter.send("Already running — /stopbot first")
            return
        self._bot_active = True

        async def bet365_callback(match):
            if not self._bot_active or not self._manual_slow_match or self._rearming:
                return
            mid = str(match.get("match_id") or "")
            locked = str(self._manual_slow_match.get("bet365_id") or "")
            if not locked or mid != locked:
                return
            try:
                h, a = map(int, (match.get("ss") or "0-0").split("-"))
            except Exception:
                h = int(match.get("home_score") or 0)
                a = int(match.get("away_score") or 0)
            new_score, total = (h, a), h + a
            if self._last_score is None:
                self._last_score, self._current_total_goals = new_score, total
                return
            if total < self._current_total_goals:
                self._last_score, self._current_total_goals = new_score, total
                if self._has_open_bet:
                    asyncio.create_task(self._handle_disallowed(h, a, total))
                return
            if total > self._current_total_goals:
                home = self._manual_slow_match.get("home", "")
                away = self._manual_slow_match.get("away", "")
                scoring = home if new_score[0] > self._last_score[0] else away
                self._last_score, self._current_total_goals = new_score, total
                asyncio.create_task(
                    self._handle_goal_and_rearm(scoring, h, a, total)
                )

        self.bet365.set_callback(bet365_callback)
        await self.bet365.start()
        logger.info("[START] bot active")
        await self.alerter.send(
            "🟢 <b>BOT STARTED</b>\n"
            "Send /slow with SportyBet URL + FI\n"
            "Always arms correct Over market only"
        )

    async def stop_bot(self):
        self._bot_active = False
        try:
            await self.bet365.stop()
        except Exception as e:
            logger.error(f"stop feed: {e}")
        if self._manual_slow_match:
            mid = self._manual_slow_match.get("match_id")
            if mid:
                await self._close_match_page(mid)
            self._manual_slow_match = None
        self._last_score = None
        self._current_total_goals = 0
        self._has_open_bet = False
        self._rearming = False
        self._awaiting_switch_decision = False
        logger.info("[STOP] bot inactive")
        await self.alerter.send("🛑 <b>BOT STOPPED</b>\n/startbot to run again")

    async def clear_slow(self):
        if self._manual_slow_match:
            mid = self._manual_slow_match.get("match_id")
            if mid:
                await self._close_match_page(mid)
            self._manual_slow_match = None
        self._last_score = None
        self._current_total_goals = 0
        self._has_open_bet = False
        self._rearming = False
        await self.alerter.send("🧹 Slow cleared")

    async def _handle_disallowed(self, home_score, away_score, total_goals):
        if not self._bot_active or not self._manual_slow_match:
            return
        mid = self._manual_slow_match.get("match_id")
        if not mid or mid not in self.match_pages or self._processing.get(mid):
            return
        self._processing[mid] = True
        self._rearming = True
        try:
            teams = self._manual_slow_match.get("teams", "Match")
            page = self.match_pages.get(mid)
            if not page or page.is_closed():
                return
            await self.alerter.send(
                f"🚫 GOAL CANCELLED {teams} {home_score}-{away_score}"
            )
            if hasattr(self.executor, "cashout_disallowed"):
                await self.executor.cashout_disallowed(
                    page, description=f"{teams} {home_score}-{away_score}"
                )
            self._has_open_bet = False
            self.executor.stop_watching(mid)
            await asyncio.sleep(1.0)
            if not self._manual_slow_match:
                return
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
            await self.executor.ai_arm_from_plan(page, None, watch)
        except Exception as e:
            await self.alerter.send(f"❌ {e}")
        finally:
            self._processing[mid] = False
            self._rearming = False

    async def _handle_goal_and_rearm(
        self, scoring_team, home_score, away_score, total_goals
    ):
        if not self._bot_active or not self._manual_slow_match:
            return
        mid = self._manual_slow_match.get("match_id")
        locked_fi = str(self._manual_slow_match.get("bet365_id") or "")
        if not mid or mid not in self.match_pages or self._processing.get(mid):
            return
        self._processing[mid] = True
        self._rearming = True
        try:
            page = self.match_pages.get(mid)
            teams = self._manual_slow_match.get("teams", "Match")
            if not page or page.is_closed():
                await self.alerter.send("❌ Page closed")
                return

            if await self.executor._is_suspended(page):
                w = self.executor.get_watch(mid)
                if w:
                    w.flagged_suspended = True
                self.executor.stop_watching(mid)
                self._manual_slow_match = None
                await self.alerter.send(
                    f"⛔ SUSPENDED on goal\n{teams}\nFI={locked_fi}\n"
                    f"Stopped — will NOT switch market"
                )
                return

            await self.alerter.notify_goal_detected(
                teams, scoring_team, 0.0, total_goals
            )
            ok = await self.executor.click_confirm_only(page)
            if ok:
                self._has_open_bet = True
                stake = self.executor.calc_stake(
                    self.executor.last_odds_used or 1.5
                )
                await self.alerter.notify_bet_placed(
                    teams,
                    f"Over {total_goals - 0.5}",
                    stake,
                    self.executor.last_odds_used,
                    f"{locked_fi}_G{total_goals}",
                    1,
                    1,
                )
                await self.alerter.send("✅ BET CREDITED / SUBMITTED")
            else:
                self.executor.stop_watching(mid)
                self._manual_slow_match = None
                await self.alerter.send(
                    f"❌ Confirm failed / locked\n{teams}\nStopped watching"
                )
                return

            self.executor.stop_watching(mid)
            await asyncio.sleep(1.0)
            if not self._manual_slow_match:
                return
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
                f"✅ Re-armed {watch.active_market if watch else ''}"
                if armed
                else "⚠️ Re-arm failed"
            )
        except Exception as e:
            await self.alerter.send(f"❌ {e}")
        finally:
            self._processing[mid] = False
            self._rearming = False

    async def handle_slow_game(self, sporty_url: str, bet365_id: str, teams: str):
        if not self._logged_in:
            await self.alerter.send("❌ Login first")
            return
        if not self._bot_active:
            await self.alerter.send("❌ Send /startbot first")
            return

        if self._manual_slow_match:
            old = self._manual_slow_match.get("match_id")
            if old:
                await self._close_match_page(old)

        self._last_score = None
        self._current_total_goals = 0
        self._rearming = False
        self._has_open_bet = False

        b365 = _fi(bet365_id)
        if not b365:
            await self.alerter.send("❌ Need numeric FI")
            return

        home, away = teams, ""
        if " vs " in teams.lower():
            p = re.split(r"\s+vs\s+", teams, flags=re.I, maxsplit=1)
            home, away = p[0].strip(), p[1].strip()
        elif " v " in teams.lower():
            p = re.split(r"\s+v\s+", teams, flags=re.I, maxsplit=1)
            home, away = p[0].strip(), p[1].strip()

        rec = self.bet365.get_match(b365)
        if rec and rec.get("ss"):
            try:
                h, a = map(int, str(rec["ss"]).split("-"))
                self._last_score = (h, a)
                self._current_total_goals = h + a
            except Exception:
                pass

        self._manual_slow_match = {
            "url": sporty_url.strip(),
            "bet365_id": b365,
            "teams": teams,
            "home": home,
            "away": away,
        }
        await self.alerter.notify_manual_slow_set(teams, b365)
        try:
            ok = await self._open_and_arm(self._manual_slow_match)
            await self.alerter.send(
                f"✅ Armed FI={b365}" if ok else "❌ Open/arm failed — NOT watching"
            )
            if not ok:
                self._manual_slow_match = None
        except Exception as e:
            self._manual_slow_match = None
            await self.alerter.send(f"❌ {e}")

    async def _open_and_arm(self, slow: dict) -> bool:
        url = slow["url"]
        home = slow.get("home", "")
        away = slow.get("away", "")
        mid = f"manual_{abs(hash(url)) % 10_000_000}"
        slow["match_id"] = mid

        page = await self.context.new_page()
        await page.goto("about:blank")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(1.5)

        if not await self.executor.wait_match_details_ready(page, home, away, 15.0):
            await page.close()
            return False

        await self.executor.open_all_tab(page)
        self.match_pages[mid] = page
        hs = self._last_score[0] if self._last_score else 0
        aws = self._last_score[1] if self._last_score else 0
        self.executor.start_watching(
            mid,
            page,
            home,
            away,
            home_score=hs,
            away_score=aws,
            match={
                "match_id": mid,
                "home_team": home,
                "away_team": away,
                "home_score": hs,
                "away_score": aws,
            },
        )
        watch = self.executor.get_watch(mid)
        return await self.executor.ai_arm_from_plan(page, None, watch)

    async def handle_switch_decision(self, decision: str):
        if not self._awaiting_switch_decision:
            return
        self._awaiting_switch_decision = False
        if decision == "yes" and self._previous_ht_match:
            p = self._previous_ht_match
            await self.handle_slow_game(p["url"], p["bet365_id"], p["teams"])
        else:
            await self.alerter.send("Staying")
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
        self._bot_active = False
        self.bet365._want_run = False
        self.bet365.running = False


if __name__ == "__main__":
    bot = ArbitrageBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        bot.stop()
