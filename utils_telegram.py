import aiohttp
from datetime import datetime
from loguru import logger
from config import Config


class TelegramAlerter:
    def __init__(self):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}"

        self.session_start = datetime.now()
        self.total_bets = 0
        self.total_staked = 0.0
        self.total_wins = 0
        self.total_losses = 0
        self.total_cashed_out = 0
        self.total_cashout_profit = 0.0
        self.matches_flagged_slow = 0
        self.slow_submissions = 0

    async def send(self, message: str, parse_mode: str = "HTML"):
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/sendMessage"
                payload = {
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                }
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"Telegram error: {body}")
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

    def _uptime(self) -> str:
        delta = datetime.now() - self.session_start
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{hours}h {minutes}m"

    async def notify_startup(self, balance: float, mode: str):
        mode_emoji = "🧪" if mode == "TEST" else "💰"
        msg = (
            f"{mode_emoji} <b>BOT STARTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Mode: <b>{mode}</b>\n"
            f"Starting balance: <b>₦{balance:,.2f}</b>\n"
            f"Data sources: Polymarket (fast) + SportyBet (own feed)\n"
            f"Slow-detection threshold: <b>{Config.SLOW_THRESHOLD_SECONDS}s</b>\n"
            f"Validation mode: <b>{'ON — first slow match will trigger a ₦' + str(int(Config.VALIDATION_STAKE)) + ' test bet' if Config.VALIDATION_MODE else 'OFF — full staking active'}</b>\n"
            f"Started: {self.session_start.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Watching all live football matches for Next Goal opportunities."
        )
        await self.send(msg)

    async def notify_slow_match_found(self, home_team: str, away_team: str, gap_seconds: float):
        self.matches_flagged_slow += 1
        msg = (
            f"🐢 <b>SLOW MATCH DETECTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {home_team} vs {away_team}\n"
            f"⏱ SportyBet lagging by <b>{gap_seconds:.1f}s</b>\n"
            f"📊 Now monitoring for next goal...\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Total flagged this session: {self.matches_flagged_slow}"
        )
        await self.send(msg)

    async def notify_goal_detected(self, match_name: str, team: str, odds: float, goal_num: int):
        msg = (
            f"⚡ <b>GOAL DETECTED — ACTING NOW</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {match_name}\n"
            f"🎯 Goal #{goal_num} — scored by <b>{team}</b>\n"
            f"📈 Next Goal odds still showing: <b>@{odds}</b>\n"
            f"⏳ Submitting bet before SportyBet updates..."
        )
        await self.send(msg)

    async def notify_bet_placed(self, match_name: str, team: str, stake: float,
                                odds: float, bet_id: str, stack_num: int, total_stack: int):
        self.total_bets += 1
        self.total_staked += stake
        potential_win = stake * odds
        stack_info = f"Bet {stack_num}/{total_stack}" if total_stack > 1 else "Single bet"

        msg = (
            f"✅ <b>BET PLACED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {match_name}\n"
            f"🎯 Backing: <b>{team}</b>\n"
            f"💵 Stake: <b>₦{stake:,.2f}</b> @ <b>{odds}</b>\n"
            f"🏆 Potential win: <b>₦{potential_win:,.2f}</b>\n"
            f"📦 {stack_info}\n"
            f"🆔 <code>{bet_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Session: {self.total_bets} bets | ₦{self.total_staked:,.0f} staked | Uptime {self._uptime()}"
        )
        await self.send(msg)

    async def notify_stack_complete(self, match_name: str, total_bets: int, total_staked: float):
        msg = (
            f"📦 <b>STACK COMPLETE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {match_name}\n"
            f"✅ {total_bets} bets placed\n"
            f"💵 Total staked: <b>₦{total_staked:,.2f}</b>"
        )
        await self.send(msg)

    async def notify_cashout(self, description: str, cashout_value: float, original_stake: float):
        self.total_cashed_out += 1
        profit = cashout_value - original_stake
        self.total_cashout_profit += profit
        profit_emoji = "🟢" if profit >= 0 else "🔴"

        msg = (
            f"💸 <b>CASHOUT EXECUTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 {description}\n"
            f"💵 Original stake: ₦{original_stake:,.2f}\n"
            f"💰 Cashed out for: <b>₦{cashout_value:,.2f}</b>\n"
            f"{profit_emoji} Profit: <b>₦{profit:,.2f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Session cashout profit: ₦{self.total_cashout_profit:,.2f} ({self.total_cashed_out} cashouts)"
        )
        await self.send(msg)

    async def notify_submission_slow(self, match_name: str, reason: str, elapsed_seconds: float):
        """Fires immediately, every time a submission takes too long —
        separate from the account-flagging escalation logic."""
        self.slow_submissions += 1
        msg = (
            f"🐌 <b>SLOW SUBMISSION WARNING</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {match_name}\n"
            f"⏱ Took <b>{elapsed_seconds:.1f}s</b> (limit: {Config.SUBMISSION_TIMEOUT}s)\n"
            f"📝 Reason: {reason}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Slow submissions this session: {self.slow_submissions}"
        )
        await self.send(msg)

    async def notify_account_flagged(self, username: str):
        msg = (
            f"⚠️ <b>ACCOUNT FLAGGED — SLOW SUBMISSION</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Account: <b>{username}</b>\n"
            f"Reason: bet submission exceeded {Config.SUBMISSION_TIMEOUT}s repeatedly\n"
            f"Action: switching accounts if available..."
        )
        await self.send(msg)

    async def notify_account_switched(self, old_username: str, new_username: str):
        msg = (
            f"🔄 <b>ACCOUNT SWITCHED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"From: {old_username}\n"
            f"To: <b>{new_username}</b>\n"
            f"Resuming operations on new account."
        )
        await self.send(msg)

    async def notify_mode_switch(self):
        msg = (
            f"🚨 <b>SWITCHING TO REAL MONEY MODE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Test period ({Config.TEST_DURATION_HOURS}h) has ended.\n"
            f"The bot will now place <b>real bets with real money</b>.\n"
            f"Test session summary:\n"
            f"• {self.total_bets} test bets simulated\n"
            f"• ₦{self.total_staked:,.2f} would have been staked\n"
            f"• {self.matches_flagged_slow} slow matches flagged"
        )
        await self.send(msg)

    async def notify_error(self, error_message: str):
        msg = (
            f"❌ <b>ERROR</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{error_message}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(msg)

    async def notify_validation_start(self, match_name: str, team: str, odds: float,
                                       goal_num: int, stake: float):
        msg = (
            f"🔬 <b>VALIDATION BET STARTING</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"This is a ONE-TIME real bet at minimal stake to confirm\n"
            f"the entire pipeline works end-to-end.\n\n"
            f"⚽ Match: {match_name}\n"
            f"🎯 Backing: <b>{team}</b>\n"
            f"⚡ Goal #{goal_num} just detected\n"
            f"📈 Odds: @{odds}\n"
            f"💵 Validation stake: <b>₦{stake:,.2f}</b> (fixed, real money)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"If this succeeds, full staking activates automatically\n"
            f"for the rest of this session."
        )
        await self.send(msg)

    async def notify_validation_step(self, step_name: str, detail: str):
        msg = f"⚙️ <b>Step: {step_name}</b>\n{detail}"
        await self.send(msg)

    async def notify_validation_result(self, success: bool, match_name: str,
                                        team: str, stake: float, odds: float):
        if success:
            msg = (
                f"✅ <b>VALIDATION SUCCESSFUL — FULL STAKING NOW ACTIVE</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚽ {match_name}\n"
                f"🎯 {team} @ {odds}\n"
                f"💵 ₦{stake:,.2f} bet placed and confirmed on SportyBet\n\n"
                f"✅ Slow-match detection: working\n"
                f"✅ Match navigation: working\n"
                f"✅ Next Goal market selection: working\n"
                f"✅ Team selection: working\n"
                f"✅ Stake entry: working\n"
                f"✅ Bet submission: working\n"
                f"✅ Confirmation detection: working\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎉 Full pipeline validated. The bot will now place\n"
                f"real, full-stake bets on every future slow match detected."
            )
        else:
            msg = (
                f"❌ <b>VALIDATION FAILED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚽ {match_name}\n"
                f"🎯 {team} @ {odds}\n"
                f"💵 Attempted stake: ₦{stake:,.2f}\n\n"
                f"Something in the click-through (market select, stake\n"
                f"entry, submit, or confirmation) failed.\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Check the terminal logs for the exact error. No further\n"
                f"bets will be placed until this is fixed."
            )
        await self.send(msg)

    async def send_daily_report(self):
        msg = (
            f"📊 <b>SESSION REPORT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Uptime: {self._uptime()}\n"
            f"⚽ Slow matches flagged: {self.matches_flagged_slow}\n"
            f"🎯 Total bets placed: {self.total_bets}\n"
            f"💵 Total staked: ₦{self.total_staked:,.2f}\n"
            f"🐌 Slow submissions: {self.slow_submissions}\n"
            f"💸 Cashouts executed: {self.total_cashed_out}\n"
            f"💰 Cashout profit: ₦{self.total_cashout_profit:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Bot is still running."
        )
        await self.send(msg)
