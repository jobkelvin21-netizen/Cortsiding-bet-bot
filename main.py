#!/usr/bin/env python3
"""Telegram control; Bet365 goal (scorer) → SportyBet API place+rebet; cashout on disallowed."""

import asyncio
import getpass
import re
import sys
from typing import Optional

from loguru import logger

from config import Config
from feeds.bet365_ws import Bet365Feed
from feeds.sportybet_client import SportyBetClient
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
        self._manual_slow_match = None
        self._logged_in = False
        self._bot_active = False
        self._has_open_bet = False
        self._processing = False
        self.balance = 0.0
        # After a successful bet on goal N, next target is N+1
        self._next_goal_no: int = 1

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
        self._logged_in = True
        await self.alerter.notify_startup(balance)

    async def handle_balance(self, amount: float):
        if self._logged_in:
            self.balance = float(amount)
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

        async def on_bet365(evt: dict):
            if not self._bot_active or not self._manual_slow_match:
                return
            kind = evt.get("event")
            if kind == "goal":
                asyncio.create_task(self._on_goal(evt))
            elif kind == "disallowed":
                asyncio.create_task(self._on_disallowed(evt))

        self.bet365.set_callback(on_bet365)
        await self.bet365.start()
        logger.info("[START] bot active")
        await self.alerter.send(
            "BOT STARTED\n"
            "Send /slow with SportyBet URL + FI + Teams\n"
            f"Cap ₦{Config.MAX_PROFIT_PER_BET}"
        )

    async def stop_bot(self):
        self._bot_active = False
        try:
            self.bet365.unwatch()
            await self.bet365.stop()
        except Exception as e:
            logger.error(f"stop feed: {e}")
        self._manual_slow_match = None
        self._has_open_bet = False
        self._processing = False
        await self.alerter.send("BOT STOPPED")

    async def clear_slow(self):
        try:
            self.bet365.unwatch()
        except Exception:
            pass
        self._manual_slow_match = None
        self._has_open_bet = False
        self._processing = False
        self._next_goal_no = 1
        await self.alerter.send("Slow cleared")

    async def _on_goal(self, evt: dict):
        if self._processing or not self._manual_slow_match:
            return
        locked = str(self._manual_slow_match.get("bet365_id") or "")
        mid = str(evt.get("match_id") or "")
        if locked and mid and mid != locked:
            return

        scorer = (evt.get("scorer") or "").lower()
        if scorer not in ("home", "away"):
            await self.alerter.send(f"GOAL but scorer unclear: {evt}")
            return

        goal_no = int(evt.get("goal_no") or self._next_goal_no or 1)
        event_id = self._manual_slow_match.get("event_id") or ""
        teams = self._manual_slow_match.get("teams", "Match")
        scoring = (
            self._manual_slow_match.get("home")
            if scorer == "home"
            else self._manual_slow_match.get("away")
        ) or scorer

        self._processing = True
        try:
            # Fresh market for THIS goal number — 1H preferred inside arm_for_side
            arm = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.sb.arm_for_side(event_id, scorer, goal_no=goal_no),
            )
            if not arm:
                # fallback: any next-goal for side
                arm = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.sb.arm_for_side(event_id, scorer, goal_no=None),
                )
            if not arm:
                await self.alerter.send(
                    f"GOAL {evt.get('ss')} {scoring} — no next-goal market for {scorer}"
                )
                return

            stake = self.sb.stake_for_odds(arm.odds, self.balance)
            await self.alerter.notify_goal_detected(
                teams, scoring, arm.odds, goal_no
            )
            await self.alerter.send(
                f"{arm.market_name} / {arm.outcome_name} @ {arm.odds}\n"
                f"Stake ₦{stake} (cap ₦{Config.MAX_PROFIT_PER_BET})"
            )

            results = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.sb.place_and_rebet(arm, stake)
            )
            ok_n = sum(1 for r in results if r.get("bizCode") == 10000)
            if ok_n:
                self._has_open_bet = True
                self.balance = max(0.0, self.balance - stake * ok_n)
                # Goal allowed path: move target to NEXT goal market
                self._next_goal_no = goal_no + 1
                await self.alerter.notify_bet_placed(
                    teams, scoring, stake, arm.odds, locked, ok_n, len(results)
                )
                await self.alerter.send(
                    f"Next target: goal #{self._next_goal_no} (1H then FT)"
                )
            else:
                await self.alerter.send(
                    f"BET FAILED 0/{len(results)}\n{results[-1] if results else ''}"
                )
        except Exception as e:
            await self.alerter.send(f"GOAL handler: {e}")
        finally:
            self._processing = False

    async def _on_disallowed(self, evt: dict):
        if not self._manual_slow_match:
            return
        teams = self._manual_slow_match.get("teams", "Match")
        await self.alerter.send(
            f"GOAL CANCELLED {teams} {evt.get('prev_ss')}→{evt.get('ss')}\nCashout…"
        )
        if not self._has_open_bet:
            return
        try:
            out = await asyncio.get_event_loop().run_in_executor(
                None, self.sb.cashout_latest_open
            )
            ok = out.get("bizCode") == 10000
            await self.alerter.send(
                "CASHOUT SUCCESS" if ok else f"CASHOUT FAIL\n{out}"
            )
            self._has_open_bet = False
        except Exception as e:
            await self.alerter.send(f"Cashout: {e}")

    async def handle_slow_game(self, sporty_url: str, bet365_id: str, teams: str):
        if not self._logged_in:
            await self.alerter.send("Login first")
            return
        if not self._bot_active:
            await self.alerter.send("Send /startbot first")
            return

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

        self.bet365.watch(b365)
        rec = self.bet365.get_match(b365)
        total = 0
        if rec and rec.get("ss"):
            try:
                h, a = map(int, str(rec["ss"]).split("-"))
                total = h + a
            except Exception:
                pass
        self._next_goal_no = total + 1
        self._has_open_bet = False
        self._processing = False

        self._manual_slow_match = {
            "url": sporty_url.strip(),
            "bet365_id": b365,
            "teams": teams,
            "home": home,
            "away": away,
            "event_id": event_id,
        }

        # Preload both sides for next goal number (IDs ready; odds refreshed on goal)
        arms = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.sb.arm_next_goal_both(event_id, goal_no=self._next_goal_no),
        )
        h = arms.get("home")
        a = arms.get("away")
        await self.alerter.notify_manual_slow_set(teams, b365)
        await self.alerter.send(
            f"LOCKED FI={b365}\n{event_id}\n"
            f"Next goal #{self._next_goal_no}\n"
            f"Home@{getattr(h, 'odds', '—')} ({getattr(h, 'market_name', '')})\n"
            f"Away@{getattr(a, 'odds', '—')} ({getattr(a, 'market_name', '')})\n"
            f"Cap ₦{Config.MAX_PROFIT_PER_BET}"
        )

    async def handle_switch_decision(self, decision: str):
        await self.alerter.send("Switch N/A in this mode")

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
