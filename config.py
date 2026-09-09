import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

class Config:
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    MAX_PROFIT_PER_BET = float(os.getenv('MAX_PROFIT_PER_BET', '14000'))
    SUBMISSION_TIMEOUT = int(os.getenv('SUBMISSION_TIMEOUT', '5'))
    MAX_CONSECUTIVE_SLOW = int(os.getenv('MAX_CONSECUTIVE_SLOW', '2'))
    ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', 'auto')
    PROXY = os.getenv('PROXY', None)

    SPORTYBET_BASE_URL = 'https://www.sportybet.com'
    SPORTYBET_API_BASE = 'https://www.sportybet.com/api/ng/factsCenter'

    POLYMARKET_WS_URL = 'wss://sports-api.polymarket.com/ws'
    SLOW_THRESHOLD_SECONDS = float(os.getenv('SLOW_THRESHOLD_SECONDS', '3'))

    VALIDATION_STAKE = float(os.getenv('VALIDATION_STAKE', '10'))

    MAX_STACK_PER_GOAL = 3
    MIN_STACK_DELAY = 0.8
    MAX_STACK_DELAY = 1.5
