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
        self.total_cashed_out = 0
        self.total_cashout_profit = 0.0
        self.matches_matched = 0
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

    async def notify_startup(self, balance: float):
        msg = (
            f"💰 <b>BOT STARTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Starting balance: <b>₦{balance:,.2f}</b>\n"
            f"Fast feed: Polymarket (soccer only)\n"
            f"You'll get a notification every time a match is confirmed\n"
            f"live on BOTH Polymarket and SportyBet.\n"
            f"First matched goal triggers a ₦{int(Config.VALIDATION_STAKE)} validation bet.\n"
            f"Started: {self.session_start.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        await self.send(msg)

    async def notify_match_matched(self, home_team: str, away_team: str):
        self.matches_matched += 1
        msg = (
            f"🔗 <b>MATCH MATCHED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {home_team} vs {away_team}\n"
            f"Now live on both Polymarket and SportyBet — monitoring for goals.\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Total matched this session: {self.matches_matched}"
        )
        await self.send(msg)

    async def notify_market_skipped(self, home_team: str, away_team: str):
        msg = (
            f"🚫 <b>BET SKIPPED — MARKET MOVED ON</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {home_team} vs {away_team}\n"
            f"SportyBet already reflects this goal — betting now would\n"
            f"hit the wrong (next) goal market. No bet placed."
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
            f"ONE-TIME real bet at minimal stake to confirm the entire\n"
            f"pipeline works end-to-end.\n\n"
            f"⚽ Match: {match_name}\n"
            f"🎯 Backing: <b>{team}</b>\n"
            f"⚡ Goal #{goal_num} just detected\n"
            f"📈 Odds: @{odds}\n"
            f"💵 Validation stake: <b>₦{stake:,.2f}</b> (real money)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Success -> full staking activates automatically.\n"
            f"Failure -> bot stops and reports the exact problem."
        )
        await self.send(msg)

    async def notify_validation_step(self, step_name: str, detail: str):
        msg = f"⚙️ <b>Step: {step_name}</b>\n{detail}"
        await self.send(msg)

    async def notify_validation_result(self, success: bool, match_name: str,
                                        team: str, stake: float, odds: float,
                                        error_detail: str = None):
        if success:
            msg = (
                f"✅ <b>VALIDATION SUCCESSFUL — FULL STAKING NOW ACTIVE</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚽ {match_name}\n"
                f"🎯 {team} @ {odds}\n"
                f"💵 ₦{stake:,.2f} bet placed and confirmed\n\n"
                f"✅ Match pairing → Goal detection → Market select →\n"
                f"Stake entry → Submission → Confirmation — all working.\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎉 Bot will now place real, full-stake bets automatically."
            )
        else:
            msg = (
                f"❌ <b>VALIDATION FAILED — BOT STOPPED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚽ {match_name}\n"
                f"🎯 {team} @ {odds}\n"
                f"💵 Attempted: ₦{stake:,.2f}\n\n"
                f"🔍 <b>Problem:</b> {error_detail}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"The bot has stopped. Fix the issue above and restart."
            )
        await self.send(msg)

    async def send_daily_report(self):
        msg = (
            f"📊 <b>SESSION REPORT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Uptime: {self._uptime()}\n"
            f"🔗 Matches matched: {self.matches_matched}\n"
            f"🎯 Total bets placed: {self.total_bets}\n"
            f"💵 Total staked: ₦{self.total_staked:,.2f}\n"
            f"🐌 Slow submissions: {self.slow_submissions}\n"
            f"💸 Cashouts executed: {self.total_cashed_out}\n"
            f"💰 Cashout profit: ₦{self.total_cashout_profit:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Bot is still running."
        )
        await self.send(msg)
