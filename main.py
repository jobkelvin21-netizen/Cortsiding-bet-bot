#!/usr/bin/env python3
"""Telegram control; Bet365 goal → SportyBet API place+rebet (scoring team); cashout on drop."""

import asyncio
import getpass
import re
import sys
from typing import Optional

from loguru import logger

from config import Config
from feeds.bet365_ws import Bet365Feed
from feeds.sportybet_client import SportyBetClient
from core.executor import BetExecutor, BetResult
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


def _event_id_from_url(url: str) -> str:
    m = re.search(r"(sr:match:\d+)", url or "")
    return m.group(1) if m else ""


class ArbitrageBot:
    def __init__(self):
        self.account_manager = AccountManager()
        self.alerter = TelegramAlerter()
        self.bet365 = Bet365Feed()
        self.bet365.set_alerter(self.alerter)
        self.sb = SportyBetClient()
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
        self._arm_task: Optional[asyncio.Task] = None
        self._arms = {}
        self.balance = 0.0

    async def terminal_login(self):
        print("SPORTYBET LOGIN (not saved to any file)")
        print(f"Hard cap profit: ₦{Config.MAX_PROFIT_PER_BET}")
        phone = input("Phone: ").strip()
        password = getpass.getpass("Password: ").strip()
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.sb.login(phone, password)
        )
        password = ""
        if not ok:
            raise RuntimeError("SportyBet API login failed")
        bal = float(input("Balance (NGN): ").strip() or "0")
        await self._finish_setup(bal)

    async def telegram_login(self, phone: str, password: str):
        await self.alerter.send("Logging in (API, memory only)...")
        try:
            ok = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.sb.login(phone, password)
            )
            if not ok:
                await self.alerter.send("Login failed")
                return
            await self.alerter.send("Login OK — send /balance AMOUNT then /startbot")
        except Exception as e:
            await self.alerter.send(f"{e}")

    async def _finish_setup(self, balance: float):
        self.balance = float(balance)
        self.executor = BetExecutor(self.alerter)
        self.executor.balance = balance
        self.executor.current_account = self.account_manager.get_active()
        self._logged_in = True
        await self.alerter.notify_startup(balance)

    async def handle_balance(self, amount: float):
        if self._logged_in:
            self.balance = float(amount)
            if self.executor:
                self.executor.balance = self.balance
            await self.alerter.send(f"Balance set ₦{amount:,.2f}")
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
            await self.alerter.send("Login + balance first")
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
                if new_score[0] > self._last_score[0]:
                    scoring, side = home, "home"
                else:
                    scoring, side = away, "away"
                self._last_score, self._current_total_goals = new_score, total
                asyncio.create_task(
                    self._handle_goal_and_rearm(scoring, side, h, a, total)
                )

        self.bet365.set_callback(bet365_callback)
        await self.bet365.start()
        logger.info("[START] bot active (API place on goal)")
        await self.alerter.send(
            "BOT STARTED\n"
            "Send /slow with SportyBet URL + FI\n"
            f"API place+rebet — hard cap ₦{Config.MAX_PROFIT_PER_BET}"
        )

    async def stop_bot(self):
        self._bot_active = False
        try:
            await self.bet365.stop()
        except Exception as e:
            logger.error(f"stop feed: {e}")
        if self._arm_task and not self._arm_task.done():
            self._arm_task.cancel()
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
        self._arms = {}
        logger.info("[STOP] bot inactive")
        await self.alerter.send("BOT STOPPED\n/startbot to run again")

    async def clear_slow(self):
        if self._arm_task and not self._arm_task.done():
            self._arm_task.cancel()
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
        self._arms = {}
        await self.alerter.send("Slow cleared — state wiped")

    async def _handle_disallowed(self, home_score, away_score, total_goals):
        if not self._bot_active or not self._manual_slow_match:
            return
        mid = self._manual_slow_match.get("match_id") or "api"
        if self._processing.get(mid):
            return
        self._processing[mid] = True
        self._rearming = True
        try:
            teams = self._manual_slow_match.get("teams", "Match")
            await self.alerter.send(
                f"GOAL CANCELLED {teams} {home_score}-{away_score}\nCashout API…"
            )
            out = await asyncio.get_event_loop().run_in_executor(
                None, self.sb.cashout_latest_open
            )
            ok = out.get("bizCode") == 10000
            await self.alerter.send(
                "CASHOUT SUCCESS" if ok else f"CASHOUT FAIL\n{out}"
            )
            self._has_open_bet = False
        except Exception as e:
            await self.alerter.send(f"{e}")
        finally:
            self._processing[mid] = False
            self._rearming = False

    async def _handle_goal_and_rearm(
        self, scoring_team, side, home_score, away_score, total_goals
    ):
        if not self._bot_active or not self._manual_slow_match:
            return
        mid = self._manual_slow_match.get("match_id") or "api"
        locked_fi = str(self._manual_slow_match.get("bet365_id") or "")
        if self._processing.get(mid):
            return
        self._processing[mid] = True
        self._rearming = True
        try:
            teams = self._manual_slow_match.get("teams", "Match")
            arm = self._arms.get(side)
            if not arm:
                event_id = self._manual_slow_match.get("event_id") or ""
                if event_id:
                    arms = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: self.sb.arm_next_goal_both(event_id)
                    )
                    self._arms.update(arms)
                    arm = self._arms.get(side)
            if not arm:
                await self.alerter.send(
                    f"GOAL {home_score}-{away_score} — {scoring_team}\n"
                    f"No next-goal market armed for {side}"
                )
                return

            stake = self.sb.stake_for_odds(arm.odds, self.balance)
            await self.alerter.send(
                f"GOAL {teams} — {scoring_team} ({side}) | {home_score}-{away_score}\n"
                f"{arm.market_name} / {arm.outcome_name} @ {arm.odds}\n"
                f"Stake ₦{stake} (cap ₦{Config.MAX_PROFIT_PER_BET})"
            )

            results = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.sb.place_and_rebet(arm, stake)
            )
            ok_n = sum(1 for r in results if r.get("bizCode") == 10000)
            if ok_n:
                self._has_open_bet = True
                await self.alerter.notify_bet_placed(
                    teams, scoring_team, stake, arm.odds, locked_fi, ok_n, len(results)
                )
            else:
                await self.alerter.send(
                    f"BET FAILED 0/{len(results)}\n{results[-1] if results else ''}"
                )
        except Exception as e:
            await self.alerter.send(f"{e}")
        finally:
            self._processing[mid] = False
            self._rearming = False

    async def _arm_loop(self, event_id: str):
        while self._bot_active and self._manual_slow_match:
            try:
                arms = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.sb.arm_next_goal_both(event_id)
                )
                if arms:
                    self._arms = arms
                    h = arms.get("home")
                    a = arms.get("away")
                    logger.info(
                        f"[ARM] H@{getattr(h, 'odds', '—')} A@{getattr(a, 'odds', '—')}"
                    )
            except Exception as e:
                logger.debug(f"[ARM] {e}")
            await asyncio.sleep(2.0)

    async def handle_slow_game(self, sporty_url: str, bet365_id: str, teams: str):
        if not self._logged_in:
            await self.alerter.send("Login first")
            return
        if not self._bot_active:
            await self.alerter.send("Send /startbot first")
            return

        if self._awaiting_switch_decision:
            self._awaiting_switch_decision = False
            self._previous_ht_match = None
            await self._close_parked()

        if self._arm_task and not self._arm_task.done():
            self._arm_task.cancel()

        if self._manual_slow_match:
            old_mid = self._manual_slow_match.get("match_id")
            if old_mid:
                try:
                    if self.executor:
                        await self.executor.clear_match(old_mid)
                except Exception:
                    await self._close_match_page(old_mid)

        self._last_score = None
        self._current_total_goals = 0
        self._rearming = False
        self._has_open_bet = False
        self._arms = {}

        b365 = _fi(bet365_id)
        if not b365:
            await self.alerter.send("Need numeric FI")
            return

        event_id = _event_id_from_url(sporty_url)
        if not event_id:
            await self.alerter.send("SportyBet URL must contain sr:match:ID")
            return

        home, away = teams, ""
        if " vs " in teams.lower():
            p = re.split(r"\s+vs\s+", teams, flags=re.I, maxsplit=1)
            home, away = p[0].strip(), p[1].strip()
        elif " v " in teams.lower():
            p = re.split(r"\s+v\s+", teams, flags=re.I, maxsplit=1)
            home, away = p[0].strip(), p[1].strip()

        rec = self.bet365.get_match(b365) if hasattr(self.bet365, "get_match") else None
        if rec and rec.get("ss"):
            try:
                h, a = map(int, str(rec["ss"]).split("-"))
                self._last_score = (h, a)
                self._current_total_goals = h + a
            except Exception:
                pass

        mid = f"api_{b365}"
        self._manual_slow_match = {
            "url": sporty_url.strip(),
            "bet365_id": b365,
            "teams": teams,
            "home": home,
            "away": away,
            "match_id": mid,
            "event_id": event_id,
        }
        await self.alerter.notify_manual_slow_set(teams, b365)

        self._arm_task = asyncio.create_task(self._arm_loop(event_id))
        await asyncio.sleep(2.5)
        h = self._arms.get("home")
        a = self._arms.get("away")
        await self.alerter.send(
            f"LOCKED FI={b365}\n{event_id}\n{home} vs {away}\n"
            f"Next goal Home@{getattr(h, 'odds', '—')} Away@{getattr(a, 'odds', '—')}\n"
            f"Cap ₦{Config.MAX_PROFIT_PER_BET}"
        )

    async def _park(self, info: dict):
        await self.alerter.send(
            f"{info.get('teams', 'Match')} HT — send new /slow or wait"
        )

    async def _poll_parked(self):
        return

    async def _close_parked(self):
        self._parked = None
        if self._ht_task and not self._ht_task.done():
            self._ht_task.cancel()
        self._ht_task = None

    async def handle_switch_decision(self, decision: str):
        self._awaiting_switch_decision = False
        await self.alerter.send("Staying / switch ignored in API mode")

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
        try:
            self.bet365._want_run = False
            self.bet365.running = False
        except Exception:
            pass


if __name__ == "__main__":
    bot = ArbitrageBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        bot.stop()
