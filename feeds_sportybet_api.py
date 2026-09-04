import aiohttp
import asyncio
from datetime import datetime
from typing import Callable, Dict
from loguru import logger
from config import Config

class SportyBetFeed:
    def __init__(self):
        self.matches: Dict[str, dict] = {}
        self.running = False
        self.callback = None
        
    async def start(self, callback: Callable):
        self.callback = callback
        self.running = True
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 13; SM-G998B)',
            'Accept': 'application/json',
            'Referer': 'https://www.sportybet.com/ng/'
        }
        
        async with aiohttp.ClientSession(headers=headers) as session:
            while self.running:
                try:
                    url = f"{Config.SPORTYBET_API_BASE}/liveScores"
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for m in data.get('data', []):
                                match = {
                                    'match_id': str(m['id']),
                                    'home_team': m['homeName'],
                                    'away_team': m['awayName'],
                                    'home_score': int(m['score']['home']),
                                    'away_score': int(m['score']['away']),
                                    'timestamp': datetime.now(),
                                    'league': m.get('tournament', {}).get('name', '')
                                }
                                self.matches[match['match_id']] = match
                                if self.callback:
                                    await self.callback(match)
                except Exception as e:
                    logger.debug(f"SB feed: {e}")
                await asyncio.sleep(2)
                
    def stop(self):
        self.running = False
