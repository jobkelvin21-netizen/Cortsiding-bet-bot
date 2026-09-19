import aiohttp
import asyncio
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
        self.matches_flagged_slow = 0
        self.slow_submissions = 0

        # Rate limiter
        self._send_lock = asyncio.Lock()
        self._last_send_time = 0.0
        self._min_interval = 1.2

        self._setup_log_mirror()

    def _setup_log_mirror(self):
        """Mirror WARNING+ logs to Telegram."""
        def sink(message):
            record = message.record
            level = record["level"].name
            text = record["message"]
            module = record["name"]

            if level not in ("WARNING", "ERROR", "CRITICAL"):
                return

            emoji = {
                "WARNING": "⚠️",
                "ERROR": "❌",
                "CRITICAL": "🔥",
            }.get(level, "📋")

            formatted = f"{emoji} <code>[{module}]</code> {text}"

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self.send(formatted))
            except RuntimeError:
                pass

        logger.add(sink, level="WARNING", format="{message}")

    async def send(self, message: str, parse_mode: str = "HTML"):
        """Send message to Telegram with rate limiting."""
        async with self._send_lock:
            now = asyncio.get_event_loop().time()
            wait = self._min_interval - (now - self._last_send_time)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send_time = asyncio.get_event_loop().time()

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/sendMessage"
                payload = {
                    "chat_id": self.chat_id,
                    "text": message[:4000],
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                }
                async with session.post(url, json=payload) as resp:
                    if resp.status == 429:
                        body = await resp.json()
                        retry_after = body.get("parameters", {}).get("retry_after", 5)
                        print(f"Telegram rate limited, backing off {retry_after}s")
                        await asyncio.sleep(retry_after)
                    elif resp.status != 200:
                        body = await resp.text()
                        print(f"Telegram error: {body}")
        except Exception as e:
            print(f"Telegram send error: {e}")

    def _uptime(self) -> str:
        delta = datetime.now() - self.session_start
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{hours}h {minutes}m"

    # NEW METHOD: Link notification
    async def notify_link_found(self, home_team: str, away_team: str, sb_match_id: str):
        """Notify when match is linked."""
        msg = (
            f"🔗 <b>MATCH LINKED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {home_team} vs {away_team}\n"
            f"🆔 SportyBet ID: <code>{sb_match_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Now monitoring for clock lag..."
        )
        await self.send(msg)

    async def notify_startup(self, balance: float):
        msg = (
            f"💰 <b>BOT STARTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Starting balance: <b>₦{balance:,.2f}</b>\n"
            f"Data sources: Bet365 (fast) + SportyBet (slow)\n"
            f"Mode: Detect lag → Arm → Goal → Bet\n"
            f"Started: {self.session_start.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        await self.send(msg)

    async def notify_slow_match_found(self, home_team: str, away_team: str, gap_seconds: float):
        self.matches_flagged_slow += 1
        msg = (
            f"🐢 <b>SLOW MATCH DETECTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {home_team} vs {away_team}\n"
            f"⏱ Lag: {gap_seconds:.1f} seconds\n"
            f"📊 Monitoring for goals...\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Total this session: {self.matches_flagged_slow}"
        )
        await self.send(msg)

    async def notify_goal_detected(self, match_name: str, team: str, odds: float, goal_num: int):
        msg = (
            f"⚡ <b>GOAL DETECTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {match_name}\n"
            f"🎯 Goal #{goal_num} — {team}\n"
            f"⏳ Placing bet now..."
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
            f"🏆 Potential: <b>₦{potential_win:,.2f}</b>\n"
            f"📦 {stack_info}\n"
            f"🆔 <code>{bet_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Session: {self.total_bets} bets | ₦{self.total_staked:,.0f} staked"
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
            f"💸 <b>CASHOUT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 {description}\n"
            f"💵 Stake: ₦{original_stake:,.2f}\n"
            f"💰 Cashed: <b>₦{cashout_value:,.2f}</b>\n"
            f"{profit_emoji} Profit: <b>₦{profit:,.2f}</b>"
        )
        await self.send(msg)

    async def notify_submission_slow(self, match_name: str, reason: str, elapsed_seconds: float):
        self.slow_submissions += 1
        msg = (
            f"🐌 <b>SLOW SUBMISSION</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {match_name}\n"
            f"⏱ {elapsed_seconds:.1f}s (limit: {Config.SUBMISSION_TIMEOUT}s)\n"
            f"📝 {reason}"
        )
        await self.send(msg)

    async def notify_account_flagged(self, username: str):
        msg = (
            f"⚠️ <b>ACCOUNT FLAGGED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Account: <b>{username}</b>\n"
            f"Reason: Slow submissions"
        )
        await self.send(msg)

    async def notify_account_switched(self, old_username: str, new_username: str):
        msg = (
            f"🔄 <b>ACCOUNT SWITCHED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"From: {old_username}\n"
            f"To: <b>{new_username}</b>"
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
            f"🔬 <b>VALIDATION BET</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {match_name}\n"
            f"🎯 {team} @ {odds}\n"
            f"💵 ₦{stake:,.2f}"
        )
        await self.send(msg)

    async def notify_validation_step(self, step_name: str, detail: str):
        msg = f"⚙️ <b>{step_name}</b>\n{detail}"
        await self.send(msg)

    async def notify_validation_result(self, success: bool, match_name: str,
                                        team: str, stake: float, odds: float,
                                        error_detail: str = None):
        if success:
            msg = (
                f"✅ <b>VALIDATION SUCCESS</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚽ {match_name}\n"
                f"🎉 Full staking now active!"
            )
        else:
            msg = (
                f"❌ <b>VALIDATION FAILED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚽ {match_name}\n"
                f"🔍 {error_detail}"
            )
        await self.send(msg)

    async def send_daily_report(self):
        msg = (
            f"📊 <b>REPORT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Uptime: {self._uptime()}\n"
            f"⚽ Matches: {self.matches_flagged_slow}\n"
            f"🎯 Bets: {self.total_bets}\n"
            f"💵 Staked: ₦{self.total_staked:,.2f}\n"
            f"🐌 Slow: {self.slow_submissions}\n"
            f"💸 Cashouts: {self.total_cashed_out}"
        )
        await self.send(msg)
