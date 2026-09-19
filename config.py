import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    MAX_PROFIT_PER_BET = float(os.getenv("MAX_PROFIT_PER_BET", "8000"))
    SUBMISSION_TIMEOUT = int(os.getenv("SUBMISSION_TIMEOUT", "5"))
    MAX_CONSECUTIVE_SLOW = int(os.getenv("MAX_CONSECUTIVE_SLOW", "2"))
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "auto")
    PROXY = os.getenv("PROXY", None)

    SPORTYBET_BASE_URL = "https://www.sportybet.com"
    SPORTYBET_API_BASE = "https://www.sportybet.com/api/ng/factsCenter"

    POLYMARKET_WS_URL = "wss://sports-api.polymarket.com/ws"
    BET365_LIVE_URL = "https://www.bet365.com/#/IP/B1"

    # Clock lag (seconds Bet365 clock must stay ahead of SportyBet)
    SLOW_THRESHOLD_SECONDS = float(os.getenv("SLOW_THRESHOLD_SECONDS", "5"))
    # Only treat as slow after this match minute
    SLOW_MIN_MINUTE = int(os.getenv("SLOW_MIN_MINUTE", "8"))

    VALIDATION_STAKE = float(os.getenv("VALIDATION_STAKE", "10"))
    MAX_STACK_PER_GOAL = int(os.getenv("MAX_STACK_PER_GOAL", "3"))
    MIN_STACK_DELAY = float(os.getenv("MIN_STACK_DELAY", "0.8"))
    MAX_STACK_DELAY = float(os.getenv("MAX_STACK_DELAY", "1.5"))

    CONFIRMATION_TIMEOUT_MS = int(os.getenv("CONFIRMATION_TIMEOUT_MS", "5000"))
    ODDS_READ_TIMEOUT_MS = int(os.getenv("ODDS_READ_TIMEOUT_MS", "1500"))
    ODDS_TRACK_INTERVAL = float(os.getenv("ODDS_TRACK_INTERVAL", "1.0"))

    LINK_GRACE_PERIOD_SECONDS = float(os.getenv("LINK_GRACE_PERIOD_SECONDS", "12"))
    MAX_PREOPENED_MATCH_PAGES = int(os.getenv("MAX_PREOPENED_MATCH_PAGES", "8"))
    SPORTYBET_POLL_INTERVAL = float(os.getenv("SPORTYBET_POLL_INTERVAL", "1.5"))

    # Primary fast feed: bet365 | polymarket
    PRIMARY_FAST_FEED = os.getenv("PRIMARY_FAST_FEED", "bet365")
