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

    async def send_message(self, message: str, parse_mode: str = "HTML"):
        """Alias for send()."""
        await self.send(message, parse_mode)

    def _uptime(self) -> str:
        delta = datetime.now() - self.session_start
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{hours}h {minutes}m"

    # NEW METHODS FOR MANUAL SLOW GAME
    async def notify_manual_slow_set(self, teams: str, bet365_id: str):
        """Notify when manual slow game is set."""
        self.matches_flagged_slow += 1
        msg = (
            f"🐢 <b>SLOW GAME LOCKED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {teams}\n"
            f"🆔 Bet365 ID: <code>{bet365_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Groq will arm market + stake\n"
            f"✅ Playwright waits on Confirm\n"
            f"✅ Click Confirm on Bet365 goal"
        )
        await self.send(msg)

    async def notify_ht_detected(self, teams: str):
        """Notify when match goes HT."""
        msg = (
            f"⏸ <b>HALF TIME</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {teams}\n\n"
            f"Send new slow game:\n"
            f"<code>/slow</code>\n"
            f"<code>SportyBet- URL</code>\n"
            f"<code>Bet365- ID</code>\n"
            f"<code>Teams- Team A vs Team B</code>"
        )
        await self.send(msg)

    async def notify_match_returned(self, teams: str):
        """Notify when match returns from HT."""
        msg = (
            f"🔄 <b>MATCH BACK FROM HT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {teams}\n\n"
            f"Reply <code>yes</code> to switch back\n"
            f"Reply <code>no</code> to stay on current"
        )
        await self.send(msg)

    async def notify_startup(self, balance: float):
        msg = (
            f"💰 <b>BOT STARTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Starting balance: <b>₦{balance:,.2f}</b>\n"
            f"Mode: Manual slow game\n"
            f"Started: {self.session_start.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        await self.send(msg)

    async def notify_goal_detected(self, match_name: str, team: str, odds: float, goal_num: int):
        msg = (
            f"⚡ <b>GOAL DETECTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚽ {match_name}\n"
            f"🎯 Goal #{goal_num} — {team}\n"
            f"⏳ Clicking Confirm..."
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


# ==================== TELEGRAM COMMAND HANDLER ====================

class TelegramCommandHandler:
    def __init__(self, bot, alerter: TelegramAlerter):
        self.bot = bot
        self.alerter = alerter
        self._last_update_id = 0
        
    async def start_polling(self):
        """Poll Telegram for commands."""
        while True:
            try:
                await self._poll_once()
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[TG POLL] {e}")
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
        
        # Start command (/start or start)
        if text_lower in ("/start", "start"):
            await self.alerter.send(
                "🟢 <b>Bot Started</b>\n\n"
                "Login methods:\n"
                "1️⃣ <code>/login PHONE PASSWORD</code>\n"
                "2️⃣ Run <code>python main.py</code> in terminal\n\n"
                "Commands:\n"
                "/login PHONE PASS - Login\n"
                "/balance AMOUNT - Set balance\n"
                "/slow - Set slow game\n"
                "/stop - Stop bot\n"
                "yes/no - Switch back after HT"
            )
            return
            
        # Stop (/stop or stop)
        if text_lower in ("/stop", "stop"):
            await self.alerter.send("🛑 Stopping bot...")
            self.bot.stop()
            return
            
        # Login (/login PHONE PASSWORD or login PHONE PASSWORD)
        if text_lower.startswith("/login ") or text_lower.startswith("login "):
            parts = text.split(maxsplit=2)
            if len(parts) >= 3:
                # Remove / if present
                phone = parts[1].replace("/", "")
                password = parts[2]
                await self.bot.telegram_login(phone, password)
            else:
                await self.alerter.send("❌ Format: <code>/login 08012345678 mypassword</code>")
            return
            
        # Balance (/balance 5000 or balance 5000)
        if text_lower.startswith("/balance ") or text_lower.startswith("balance "):
            try:
                amount = float(text.split()[1].replace("/", ""))
                await self.bot.handle_balance(amount)
            except (IndexError, ValueError):
                await self.alerter.send("❌ Format: <code>/balance 5000</code>")
            return
            
        # Slow game (/slow or slow)
        if text_lower.startswith("/slow") or text_lower.startswith("slow"):
            await self._handle_slow_command(text)
            return
            
        # Yes/No for switch-back
        if text_lower in ("yes", "no") and self.bot._awaiting_switch_decision:
            await self.bot.handle_switch_decision(text_lower)
            return
            
    async def _handle_slow_command(self, text: str):
        """Parse multi-line slow command."""
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
                "❌ Format:\n<code>/slow</code>\n<code>SportyBet- URL</code>\n<code>Bet365- ID</code>\n<code>Teams- Team A vs Team B</code>"
            )
            return
            
        await self.bot.handle_slow_game(sporty_url, bet365_id, teams or "Unknown")
