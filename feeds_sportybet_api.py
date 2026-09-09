import aiohttp
import asyncio
import re
from datetime import datetime
from typing import Callable, Dict
from loguru import logger
from config import Config


class SportyBetFeed:
    def __init__(self):
        self.matches = {}
        self.running = False
        self.callback = None

    def _parse_played_seconds(self, played_seconds_str):
        try:
            parts = [int(p) for p in played_seconds_str.split(':')]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
        except Exception:
            pass
        return 0.0

    def _extract_sportradar_numeric_id(self, event_id):
        match = re.search(r'(\d+)$', event_id or '')
        return match.group(1) if match else ''

    async def start(self, callback):
        self.callback = callback
        self.running = True

        headers = {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 13; SM-G998B)',
            'Accept': 'application/json',
            'Referer': 'https://www.sportybet.com/ng/',
        }

        url = f"{Config.SPORTYBET_API_BASE}/liveOrPrematchEvents?sportId=sr:sport:1"
        logger.info(f"SportyBet feed polling: {url}")

        async with aiohttp.ClientSession(headers=headers) as session:
            while self.running:
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.warning(f"SportyBet feed: HTTP status {resp.status}")
                            await asyncio.sleep(2)
                            continue

                        try:
                            events = await resp.json()
                        except Exception:
                            body_preview = (await resp.text())[:200]
                            logger.error(f"SportyBet feed: invalid JSON. Preview: {body_preview}")
                            await asyncio.sleep(2)
                            continue

                        if not isinstance(events, list):
                            events = events.get('data', []) if isinstance(events, dict) else []

                        if not events:
                            logger.warning("SportyBet feed: 200 OK but 0 events returned")

                        for detail in events:
                            event_id = detail.get('eventId')
                            if not event_id:
                                continue

                            match = {
                                'match_id': event_id,
                                'sportradar_id': self._extract_sportradar_numeric_id(event_id),
                                'home_team': detail.get('homeTeamName', ''),
                                'away_team': detail.get('awayTeamName', ''),
                                'home_score': 0,
                                'away_score': 0,
                                'period': detail.get('period', ''),
                                'match_status': detail.get('matchStatus', ''),
                                'played_seconds': self._parse_played_seconds(
                                    detail.get('playedSeconds', '0:0')
                                ),
                                'timestamp': datetime.now(),
                            }

                            game_score = detail.get('gameScore', [])
                            if game_score:
                                try:
                                    last = game_score[-1]
                                    h, a = last.split(':')
                                    match['home_score'] = int(h)
                                    match['away_score'] = int(a)
                                except Exception:
                                    pass

                            self.matches[event_id] = match
                            if self.callback:
                                await self.callback(match)

                except Exception as e:
                    logger.error(f"SportyBet feed loop error: {e}")

                await asyncio.sleep(2)

    def stop(self):
        self.running = False
