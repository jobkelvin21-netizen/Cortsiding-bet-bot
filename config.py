import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    MAX_PROFIT_PER_BET = float(os.getenv("MAX_PROFIT_PER_BET", "9500"))
    MAX_REBET = int(os.getenv("MAX_REBET", "3"))

    SPORTYBET_BASE_URL = "https://www.sportybet.com"
    SPORTYBET_API_BASE = "https://www.sportybet.com/api/ng/factsCenter"
    BET365_LIVE_URL = os.getenv("BET365_LIVE_URL", "https://www.bet365.com/#/IP/B1")
