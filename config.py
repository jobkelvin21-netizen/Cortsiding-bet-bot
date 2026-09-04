import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

class Config:
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    
    TEST_MODE = os.getenv('TEST_MODE', 'true').lower() == 'true'
    TEST_DURATION_HOURS = int(os.getenv('TEST_DURATION_HOURS', '2'))
    
    MAX_PROFIT_PER_BET = float(os.getenv('MAX_PROFIT_PER_BET', '15000'))
    SUBMISSION_TIMEOUT = int(os.getenv('SUBMISSION_TIMEOUT', '5'))
    MAX_CONSECUTIVE_SLOW = int(os.getenv('MAX_CONSECUTIVE_SLOW', '2'))
    
    ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', 'default_key_change_me')
    
    SPORTYBET_BASE_URL = 'https://www.sportybet.com'
    SPORTYBET_API_BASE = 'https://www.sportybet.com/api'
    BET365_BASE_URL = 'https://www.bet365.com'
    
    PROXY = os.getenv('PROXY', None)
    
    MAX_STACK_PER_GOAL = 3
    MIN_STACK_DELAY = 0.8
    MAX_STACK_DELAY = 1.5
    
    LEARNING_ENABLED = True
    SECURITY_ENABLED = True
    
    @classmethod
    def check_test_mode_expired(cls):
        if cls.TEST_MODE and cls.TEST_END_TIME:
            return datetime.now() >= cls.TEST_END_TIME
        return False
        
    @classmethod
    def enable_real_mode(cls):
        cls.TEST_MODE = False
        os.environ['TEST_MODE'] = 'false'
