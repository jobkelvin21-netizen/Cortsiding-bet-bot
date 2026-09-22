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
        self.matches_flagged_slow = 0
        self.slow_submissions = 0
        self._send_lock = asyncio.Lock()
        self._last_send_time = 0.0
        self._min_interval = 1.05
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
                        await asyncio.sleep(body.get("parameters", {}).get("retry_after", 5))
                    elif resp.status != 200:
                        print(f"Telegram error: {await resp.text()}")
        except Exception as e:
            print(f"Telegram send error: {e}")

    async def send_message(self, message: str, parse_mode: str = "HTML"):
        await self.send(message, parse_mode)

    async def notify_manual_slow_set(self, teams: str, bet365_id: str):
        self.matches_flagged_slow += 1
        await self.send(
            f"🎯 <b>SLOW LOCKED</b>\n"
            f"⚽ {teams}\n"
            f"🆔 FI=<code>{bet365_id}</code>"
        )

    async def notify_startup(self, balance: float):
        await self.send(
            f"💰 <b>BOT STARTED</b>\n"
            f"Balance: <b>₦{balance:,.2f}</b>\n\n"
            f"Football FI list arrives as matches appear.\n"
            f"Copy FI → send /slow\n\n"
            f"<code>/startbot</code> · <code>/stopbot</code> · <code>/clear</code>\n\n"
            f"<code>/slow</code>\n"
            f"<code>SportyBet- URL</code>\n"
            f"<code>Bet365- 201703339</code>\n"
            f"<code>Teams- Home vs Away</code>"
        )

    async def notify_goal_detected(self, match_name: str, team: str, odds: float, goal_num: int):
        await self.send(
            f"⚡ <b>GOAL</b>\n⚽ {match_name}\n🎯 #{goal_num} {team}\n⏳ Confirm..."
        )

    async def notify_bet_placed(
        self, match_name, team, stake, odds, bet_id, stack_num, total_stack
    ):
        self.total_bets += 1
        self.total_staked += stake
        await self.send(
            f"✅ <b>BET PLACED</b>\n⚽ {match_name}\n🎯 {team}\n"
            f"💵 ₦{stake:,.2f} @ {odds}\n🆔 <code>{bet_id}</code>"
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
        await self.send(f"🔄 {old_username} → <b>{new_username}</b>")

    async def notify_error(self, error_message: str):
        await self.send(f"❌ <b>ERROR</b>\n{error_message}")


class TelegramCommandHandler:
    def __init__(self, bot, alerter: TelegramAlerter):
        self.bot = bot
        self.alerter = alerter
        self._last_update_id = 0

    async def start_polling(self):
        logger.success("[TG] polling started")
        while True:
            try:
                await self._poll_once()
                await asyncio.sleep(0.8)
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

        if low in ("/start", "start", "/help", "help"):
            await self.alerter.send(
                "🟢 <b>Courtside</b>\n\n"
                "1. CDP Chrome + Proton + Bet365 Football\n"
                "2. <code>/login</code> · <code>/balance</code>\n"
                "3. Wait for ⚽ FI messages\n"
                "4. Copy FI → /slow\n\n"
                "<code>/slow</code>\n"
                "<code>SportyBet- https://...</code>\n"
                "<code>Bet365- 201703339</code>\n"
                "<code>Teams- Home vs Away</code>\n\n"
                "<code>/startbot</code> · <code>/stopbot</code> · <code>/clear</code>"
            )
            return

        if low in ("/startbot", "startbot", "start bot"):
            if hasattr(self.bot, "start_bot"):
                await self.bot.start_bot()
            return

        if low in ("/stopbot", "stopbot", "stop bot", "/stop", "stop"):
            if hasattr(self.bot, "stop_bot"):
                await self.bot.stop_bot()
            else:
                self.bot.stop()
                await self.alerter.send("🛑 Stopped")
            return

        if low in ("/clear", "clear"):
            if hasattr(self.bot, "clear_slow"):
                await self.bot.clear_slow()
            return

        if low.startswith("/login ") or low.startswith("login "):
            parts = text.split(maxsplit=2)
            if len(parts) >= 3:
                await self.bot.telegram_login(parts[1], parts[2])
            else:
                await self.alerter.send("❌ <code>/login PHONE PASS</code>")
            return

        if low.startswith("/balance ") or low.startswith("balance "):
            try:
                await self.bot.handle_balance(float(text.split()[1]))
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
            if not line:
                continue
            low = line.lower()
            if low.startswith("sportybet-") or low.startswith("sportybet -"):
                sporty_url = line.split("-", 1)[1].strip()
            elif low.startswith("bet365-") or low.startswith("bet365 -"):
                bet365_id = line.split("-", 1)[1].strip()
            elif low.startswith("teams-") or low.startswith("teams -"):
                teams = line.split("-", 1)[1].strip()

        if not sporty_url or not bet365_id:
            await self.alerter.send(
                "❌ Format:\n"
                "<code>/slow</code>\n"
                "<code>SportyBet- https://www.sportybet.com/...</code>\n"
                "<code>Bet365- 201703339</code>\n"
                "<code>Teams- Home vs Away</code>\n\n"
                "Copy FI from the ⚽ messages the bot sends."
            )
            return

        if "sportybet.com" not in sporty_url.lower():
            await self.alerter.send("❌ SportyBet must be a sportybet.com URL")
            return

        # numeric FI only
        m = re_search_id(bet365_id)
        if not m:
            await self.alerter.send("❌ Bet365- must be numeric FI e.g. 201703339")
            return

        await self.bot.handle_slow_game(sporty_url, m, teams or "Match")


def re_search_id(raw: str) -> str:
    import re
    raw = (raw or "").strip()
    m = re.search(r"(\d{6,})", raw)
    return m.group(1) if m else ""
