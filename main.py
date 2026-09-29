#!/usr/bin/env python3
"""Telegram control; Bet365 goal fires Confirm on SportyBet; one game at a time."""

import asyncio
import os
import re
import sys
from typing import Optional

from loguru import logger

from config import Config
from feeds.bet365_ws import Bet365Feed
from core.executor import BetExecutor, BetResult
from utils.telegram import TelegramAlerter, TelegramCommandHandler
from utils.account_manager import AccountManager

logger.remove()
logger.add(
    sys.stderr, level="INFO",
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
        self.executor: Optional[BetExecutor] = None
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
        self._parked = None
        self._ht_task: Optional[asyncio.Task] = None

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
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)
        try:
            await page.click('button:has-text("Log In"), a:has-text("Log In")', timeout=5000)
            await asyncio.sleep(1)
        except Exception:
            pass
        await page.fill('input[name="phone"], input[type="tel"]', phone)
        await page.fill('input[name="psd"], input[type="password"]', password)
        await page.click('button[name="logIn"], button:has-text("Log In")')
        await asyncio.sleep(5)

    async def _finish_setup(self, balance: float):
        self.executor = BetExecutor(self.alerter)
        self.executor.balance = balance
        self.executor.current_account = self.account_manager.get_active()
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
                asyncio.create_task(self._handle_goal_and_rearm(scoring, h, a, total))

        self.bet365.set_callback(bet365_callback)
        await self.bet365.start()
        logger.info("[START] bot active")
        await self.alerter.send(
            "🟢 <b>BOT STARTED</b>\n"
            "Send /slow with SportyBet URL + FI\n"
            "Pure-code Over arm — Confirm only on Bet365 goal"
        )

    async def stop_bot(self):
        self._bot_active = False
        try:
            await self.bet365.stop()
        except Exception as e:
            logger.error(f"stop feed: {e}")
        if self._manual_slow_match:
            mid = self._manual_slow_match.get("match_id")
            if mid and self.executor:
                try:
                    await self.executor.clear_match(mid)
                except Exception:
                    await self._close_match_page(mid)
            self._manual_slow_match = None
        await self._close_parked()
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
            if mid and self.executor:
                try:
                    await self.executor.clear_match(mid)
                except Exception:
                    await self._close_match_page(mid)
            elif mid:
                await self._close_match_page(mid)
            self._manual_slow_match = None
        elif self.executor:
            try:
                await self.executor.clear_match()
            except Exception:
                pass
        await self._close_parked()
        self._last_score = None
        self._current_total_goals = 0
        self._has_open_bet = False
        self._rearming = False
        await self.alerter.send("🧹 Slow cleared — tab closed, state wiped")

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
            await self.alerter.send(f"🚫 GOAL CANCELLED {teams} {home_score}-{away_score}")
            await self.executor.cashout_disallowed(
                page, description=f"{teams} {home_score}-{away_score}")
            self._has_open_bet = False
            self.executor.stop_watching(mid)
            await asyncio.sleep(1.0)
            if not self._manual_slow_match:
                return
            self.executor.start_watching(
                mid, page,
                self._manual_slow_match.get("home", ""),
                self._manual_slow_match.get("away", ""),
                home_score=home_score, away_score=away_score,
            )
        except Exception as e:
            await self.alerter.send(f"❌ {e}")
        finally:
            self._processing[mid] = False
            self._rearming = False

    async def _handle_goal_and_rearm(self, scoring_team, home_score, away_score, total_goals):
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
                self.executor.stop_watching(mid)
                self._manual_slow_match = None
                await self.alerter.send(
                    f"⛔ SUSPENDED on goal\n{teams}\nFI={locked_fi}\nStopped — will NOT switch market")
                return

            await self.alerter.send(f"⚽ GOAL {teams} — {scoring_team} | {home_score}-{away_score}")

            result = await self.executor.confirm_goal_click(mid)

            if result == BetResult.SUCCESS:
                self._has_open_bet = True
            elif result == BetResult.ABORTED_SAFETY:
                return
            else:
                ok = await self.executor._click_confirm(page)
                if ok:
                    self._has_open_bet = True
                    await self.alerter.send("✅ BET CONFIRMED (retry)")
                else:
                    self.executor.stop_watching(mid)
                    self._manual_slow_match = None
                    await self.alerter.send(f"❌ Confirm failed / locked\n{teams}\nStopped watching")
        except Exception as e:
            await self.alerter.send(f"❌ {e}")
        finally:
            self._processing[mid] = False
            self._rearming = False

    async def handle_slow_game(self, sporty_url: str, bet365_id: str, teams: str):
        """Runs as a background task from TelegramCommandHandler, so
        /stopbot and /clear stay responsive while this (up to ~90s) runs."""
        if not self._logged_in:
            await self.alerter.send("❌ Login first")
            return
        if not self._bot_active:
            await self.alerter.send("❌ Send /startbot first")
            return

        if self._awaiting_switch_decision:
            self._awaiting_switch_decision = False
            self._previous_ht_match = None
            await self._close_parked()

        if self._manual_slow_match:
            old_mid = self._manual_slow_match.get("match_id")
            old_watch = self.executor.get_watch(old_mid) if old_mid else None
            if old_watch and old_watch.period == "HT":
                await self._park(self._manual_slow_match)
            elif old_mid:
                try:
                    await self.executor.clear_match(old_mid)
                except Exception:
                    await self._close_match_page(old_mid)

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
            "url": sporty_url.strip(), "bet365_id": b365, "teams": teams,
            "home": home, "away": away,
        }
        await self.alerter.notify_manual_slow_set(teams, b365)
        try:
            ok = await self._open_and_arm(self._manual_slow_match)
            await self.alerter.send(f"✅ Armed FI={b365}" if ok else "❌ Open/arm failed — NOT watching")
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
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2.0)

        for loc in (
            page.locator("div.m-nav-item").get_by_text("All", exact=True),
            page.locator("[role='tab']").get_by_text("All", exact=True),
        ):
            try:
                if await loc.count() > 0:
                    await loc.first.click(timeout=1500)
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                continue

        for _ in range(15):
            try:
                body = (await page.inner_text("body", timeout=2000) or "").lower()
                if home.lower()[:6] in body or away.lower()[:6] in body or "over" in body:
                    break
            except Exception:
                pass
            await asyncio.sleep(1.0)

        self.match_pages[mid] = page
        hs = self._last_score[0] if self._last_score else 0
        aws = self._last_score[1] if self._last_score else 0

        watch = self.executor.start_watching(mid, page, home, away, home_score=hs, away_score=aws)
        for _ in range(90):
            if mid not in self.executor.watches:
                return False
            if watch.confirm_armed:
                return True
            await asyncio.sleep(1.0)
        return watch.confirm_armed

    async def _park(self, info: dict):
        mid = info.get("match_id")
        page = await self.executor.park_match(mid)
        self.match_pages.pop(mid, None)
        if not page or page.is_closed():
            return
        await self._close_parked()
        self._parked = dict(info)
        self._parked["page"] = page
        self._ht_task = asyncio.create_task(self._poll_parked())
        await self.alerter.send(f"⏸ {info.get('teams', 'Match')} parked at half-time.\n"
                                f"I'll ask if you want it back when the 2nd half starts.")

    async def _poll_parked(self):
        try:
            while self._parked:
                p = self._parked
                page = p["page"]
                if page.is_closed():
                    self._parked = None
                    return
                if await self.executor.detect_period(page) == "2H":
                    self._previous_ht_match = dict(p)
                    self._awaiting_switch_decision = True
                    await self.alerter.send(
                        f"🔄 {p.get('teams', 'Old match')} is back for the 2nd half.\n"
                        f"Reply yes or no")
                    return
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            pass

    async def _close_parked(self):
        p, self._parked = self._parked, None
        if self._ht_task and not self._ht_task.done():
            self._ht_task.cancel()
        self._ht_task = None
        if p:
            try:
                if not p["page"].is_closed():
                    await p["page"].close()
            except Exception:
                pass

    async def handle_switch_decision(self, decision: str):
        if not self._awaiting_switch_decision:
            return
        self._awaiting_switch_decision = False
        old, self._previous_ht_match = self._previous_ht_match, None

        if decision == "yes" and old and not old["page"].is_closed():
            if self._manual_slow_match:
                cur_mid = self._manual_slow_match.get("match_id")
                if cur_mid:
                    try:
                        await self.executor.clear_match(cur_mid)
                    except Exception:
                        await self._close_match_page(cur_mid)

            self._parked = None
            if self._ht_task and not self._ht_task.done():
                self._ht_task.cancel()
            self._ht_task = None

            page, mid = old["page"], old["match_id"]
            self.match_pages[mid] = page
            self._manual_slow_match = {k: v for k, v in old.items() if k != "page"}
            self._last_score = None
            self._current_total_goals = 0
            self._has_open_bet = False

            watch = self.executor.start_watching(
                mid, page, old.get("home", ""), old.get("away", ""))
            ok = False
            for _ in range(90):
                if mid not in self.executor.watches:
                    break
                if watch.confirm_armed:
                    ok = True
                    break
                await asyncio.sleep(1.0)
            await self.alerter.send("✅ Switched back and armed" if ok else "⚠️ Switched back but arming failed")
        else:
            await self.alerter.send("Staying on the current match")
            await self._close_parked()

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
