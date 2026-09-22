import aiohttp
import asyncio
import re
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
        self._min_interval = 1.0

    async def send(self, message: str, parse_mode: str = "HTML"):
        async with self._send_lock:
            now = asyncio.get_event_loop().time()
            wait = self._min_interval - (now - self._last_send_time)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send_time = asyncio.get_event_loop().time()
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "chat_id": self.chat_id,
                    "text": message[:4000],
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                }
                async with session.post(
                    f"{self.base_url}/sendMessage", json=payload
                ) as resp:
                    if resp.status == 429:
                        body = await resp.json()
                        await asyncio.sleep(
                            body.get("parameters", {}).get("retry_after", 5)
                        )
        except Exception as e:
            print(f"TG send error: {e}")

    async def send_message(self, message: str, parse_mode: str = "HTML"):
        await self.send(message, parse_mode)

    async def notify_manual_slow_set(self, teams: str, bet365_id: str):
        self.matches_flagged_slow += 1
        await self.send(
            f"🎯 <b>SLOW LOCKED</b>\n⚽ {teams}\n🆔 FI=<code>{bet365_id}</code>"
        )

    async def notify_startup(self, balance: float):
        await self.send(
            f"💰 <b>LOGGED IN</b>\nBalance: <b>₦{balance:,.2f}</b>\n\n"
            f"<code>/startbot</code> — start everything\n"
            f"<code>/stopbot</code> — stop everything\n"
            f"<code>/clear</code> — unlock slow\n\n"
            f"<code>/slow</code>\n"
            f"<code>SportyBet- URL</code>\n"
            f"<code>Bet365- FI</code>\n"
            f"<code>Teams- Home vs Away</code>"
        )

    async def notify_goal_detected(self, match_name, team, odds, goal_num):
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

    async def notify_ht_detected(self, teams: str):
        await self.send(
            f"⏸ <b>HALF TIME</b>\n⚽ {teams}\n\n"
            f"Send new /slow or wait for return."
        )

    async def notify_match_returned(self, teams: str):
        await self.send(
            f"🔄 <b>MATCH BACK FROM HT</b>\n⚽ {teams}\n"
            f"Reply <code>yes</code> or <code>no</code>"
        )


class TelegramCommandHandler:
    def __init__(self, bot, alerter: TelegramAlerter):
        self.bot = bot
        self.alerter = alerter
        self._last_update_id = 0

    async def start_polling(self):
        logger.info("[TG] polling on")
        while True:
            try:
                await self._poll_once()
                await asyncio.sleep(0.7)
            except Exception as e:
                logger.error(f"[TG] {e}")
                await asyncio.sleep(4)

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
                    self._last_update_id = max(
                        self._last_update_id, update["update_id"]
                    )
                    await self._handle_update(update)

    async def _handle_update(self, update: dict):
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = message.get("chat", {}).get("id")
        if str(chat_id) != str(Config.TELEGRAM_CHAT_ID) or not text:
            return
        low = text.lower().strip()

        if low in ("/start", "start", "/help", "help"):
            await self.alerter.send(
                "🟢 <b>Courtside</b>\n"
                "<code>/startbot</code> start all\n"
                "<code>/stopbot</code> stop all\n"
                "<code>/clear</code>\n"
                "<code>/slow</code> + SportyBet + Bet365 FI + Teams"
            )
            return

        if low in ("/stop", "stop", "/stopbot", "stopbot", "stop bot"):
            await self.bot.stop_bot()
            return

        if low in ("/startbot", "startbot", "start bot"):
            await self.bot.start_bot()
            return

        if low in ("/clear", "clear"):
            await self.bot.clear_slow()
            return

        if low.startswith("/login ") or low.startswith("login "):
            parts = text.split(maxsplit=2)
            if len(parts) >= 3:
                await self.bot.telegram_login(parts[1], parts[2])
            else:
                await self.alerter.send("❌ /login PHONE PASS")
            return

        if low.startswith("/balance ") or low.startswith("balance "):
            try:
                await self.bot.handle_balance(float(text.split()[1]))
            except Exception:
                await self.alerter.send("❌ /balance 5000")
            return

        if low.startswith("/slow") or low.startswith("slow"):
            await self._handle_slow(text)
            return

        if low in ("yes", "no") and getattr(
            self.bot, "_awaiting_switch_decision", False
        ):
            await self.bot.handle_switch_decision(low)
            return

    async def _handle_slow(self, text: str):
        sporty_url = bet365_id = teams = None
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            if low.startswith("sportybet"):
                sporty_url = line.split("-", 1)[1].strip()
            elif low.startswith("bet365"):
                bet365_id = line.split("-", 1)[1].strip()
            elif low.startswith("teams"):
                teams = line.split("-", 1)[1].strip()
        if not sporty_url or not bet365_id:
            await self.alerter.send(
                "❌\n<code>/slow</code>\n"
                "<code>SportyBet- URL</code>\n"
                "<code>Bet365- 201703339</code>\n"
                "<code>Teams- Home vs Away</code>"
            )
            return
        m = re.search(r"(\d{6,})", bet365_id)
        if not m:
            await self.alerter.send("❌ Bet365- must be numbers only")
            return
        await self.bot.handle_slow_game(sporty_url, m.group(1), teams or "Match")
