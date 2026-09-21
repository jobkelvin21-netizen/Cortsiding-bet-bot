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

        self._send_lock = asyncio.Lock()
        self._last_send_time = 0.0
        self._min_interval = 1.2
        self._setup_log_mirror()

    def _setup_log_mirror(self):
        def sink(message):
            record = message.record
            level = record["level"].name
            if level not in ("WARNING", "ERROR", "CRITICAL"):
                return
            emoji = {"WARNING": "⚠️", "ERROR": "❌", "CRITICAL": "🔥"}.get(level, "📋")
            formatted = f"{emoji} <code>[{record['name']}]</code> {record['message']}"
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self.send(formatted))
            except RuntimeError:
                pass

        logger.add(sink, level="WARNING", format="{message}")

    async def send(self, message: str, parse_mode: str = "HTML"):
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
                        await asyncio.sleep(retry_after)
                    elif resp.status != 200:
                        print(f"Telegram error: {await resp.text()}")
        except Exception as e:
            print(f"Telegram send error: {e}")

    async def send_message(self, message: str, parse_mode: str = "HTML"):
        await self.send(message, parse_mode)

    def _uptime(self) -> str:
        delta = datetime.now() - self.session_start
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{hours}h {minutes}m"

    async def notify_manual_slow_set(self, teams: str, bet365_id: str):
        self.matches_flagged_slow += 1
        await self.send(
            f"🐢 <b>SLOW GAME LOCKED</b>\n"
            f"⚽ {teams}\n"
            f"🆔 Bet365 ID: <code>{bet365_id}</code>\n"
            f"✅ Groq arms market + Confirm\n"
            f"✅ Click Confirm on Bet365 WS goal"
        )

    async def notify_ht_detected(self, teams: str):
        await self.send(
            f"⏸ <b>HALF TIME</b>\n⚽ {teams}\n\n"
            f"Send new slow:\n"
            f"<code>/slow</code>\n"
            f"<code>SportyBet- URL</code>\n"
            f"<code>Bet365- ID</code>\n"
            f"<code>Teams- A vs B</code>"
        )

    async def notify_match_returned(self, teams: str):
        await self.send(
            f"🔄 <b>MATCH BACK FROM HT</b>\n⚽ {teams}\n"
            f"Reply <code>yes</code> or <code>no</code>"
        )

    async def notify_startup(self, balance: float):
        await self.send(
            f"💰 <b>BOT STARTED</b>\n"
            f"Balance: <b>₦{balance:,.2f}</b>\n"
            f"Mode: Manual slow via Telegram\n"
            f"{self.session_start.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    async def notify_goal_detected(self, match_name: str, team: str, odds: float, goal_num: int):
        await self.send(
            f"⚡ <b>GOAL DETECTED</b>\n⚽ {match_name}\n"
            f"🎯 Goal #{goal_num} — {team}\n⏳ Clicking Confirm..."
        )

    async def notify_bet_placed(
        self, match_name, team, stake, odds, bet_id, stack_num, total_stack
    ):
        self.total_bets += 1
        self.total_staked += stake
        await self.send(
            f"✅ <b>BET PLACED</b>\n⚽ {match_name}\n🎯 {team}\n"
            f"💵 ₦{stake:,.2f} @ {odds}\n🆔 <code>{bet_id}</code>\n"
            f"Session: {self.total_bets} bets | ₦{self.total_staked:,.0f}"
        )

    async def notify_submission_slow(self, match_name, reason, elapsed_seconds):
        self.slow_submissions += 1
        await self.send(
            f"🐌 <b>SLOW SUBMISSION</b>\n⚽ {match_name}\n"
            f"⏱ {elapsed_seconds:.1f}s\n📝 {reason}"
        )

    async def notify_account_flagged(self, username: str):
        await self.send(f"⚠️ <b>ACCOUNT FLAGGED</b>\n{username}")

    async def notify_account_switched(self, old_username, new_username):
        await self.send(f"🔄 Switched {old_username} → <b>{new_username}</b>")

    async def notify_error(self, error_message: str):
        await self.send(f"❌ <b>ERROR</b>\n{error_message}")


class TelegramCommandHandler:
    def __init__(self, bot, alerter: TelegramAlerter):
        self.bot = bot
        self.alerter = alerter
        self._last_update_id = 0

    async def start_polling(self):
        while True:
            try:
                await self._poll_once()
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[TG POLL] {e}")
                await asyncio.sleep(5)

    async def _poll_once(self):
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {"offset": self._last_update_id + 1, "limit": 10, "timeout": 20}
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
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = message.get("chat", {}).get("id")
        if str(chat_id) != str(Config.TELEGRAM_CHAT_ID):
            return
        if not text:
            return

        low = text.lower().strip()

        if low in ("/start", "start"):
            await self.alerter.send(
                "🟢 <b>Courtside ready</b>\n\n"
                "<code>/login PHONE PASS</code>\n"
                "<code>/balance 5000</code>\n"
                "<code>/slow</code> then:\n"
                "<code>SportyBet- URL</code>\n"
                "<code>Bet365- NUMERIC_ID</code>\n"
                "<code>Teams- Home vs Away</code>\n"
                "<code>/stop</code>"
            )
            return

        if low in ("/stop", "stop"):
            await self.alerter.send("🛑 Stopping...")
            self.bot.stop()
            return

        if low.startswith("/login ") or low.startswith("login "):
            parts = text.split(maxsplit=2)
            if len(parts) >= 3:
                await self.bot.telegram_login(parts[1], parts[2])
            else:
                await self.alerter.send("❌ <code>/login 0801… password</code>")
            return

        if low.startswith("/balance ") or low.startswith("balance "):
            try:
                amount = float(text.split()[1])
                await self.bot.handle_balance(amount)
            except Exception:
                await self.alerter.send("❌ <code>/balance 5000</code>")
            return

        if low.startswith("/slow") or low.startswith("slow"):
            await self._handle_slow_command(text)
            return

        if low in ("yes", "no") and getattr(self.bot, "_awaiting_switch_decision", False):
            await self.bot.handle_switch_decision(low)
            return

    async def _handle_slow_command(self, text: str):
        sporty_url = bet365_id = teams = None
        for line in text.strip().split("\n"):
            line = line.strip()
            low = line.lower()
            if low.startswith("sportybet-"):
                sporty_url = line.split("-", 1)[1].strip()
            elif low.startswith("bet365-"):
                bet365_id = line.split("-", 1)[1].strip()
            elif low.startswith("teams-"):
                teams = line.split("-", 1)[1].strip()

        if not sporty_url or not bet365_id:
            await self.alerter.send(
                "❌ Format:\n"
                "<code>/slow</code>\n"
                "<code>SportyBet- https://www.sportybet.com/...</code>\n"
                "<code>Bet365- 34990250</code>\n"
                "<code>Teams- Juventus vs Atalanta</code>\n\n"
                "Bet365 ID = number next to [SS] in terminal, not OV…"
            )
            return

        await self.bot.handle_slow_game(
            sporty_url, bet365_id, teams or "Unknown"
        )
