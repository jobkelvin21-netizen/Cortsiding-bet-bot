import asyncio
import re
from datetime import datetime
from typing import Callable, Dict
from loguru import logger
from curl_cffi.requests import AsyncSession  # CHANGED: from aiohttp to curl_cffi
from config import Config


class SportyBetFeed:
    def __init__(self):
        self.matches = {}
        self.running = False
        self.callback = None
        self.cookies = {}  # ADDED: Store cookies here

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

    # ADDED: Method to update cookies from main bot
    def set_cookies(self, cookies: Dict[str, str]):
        self.cookies = cookies
        logger.info(f"Updated SportyBet cookies: {len(cookies)} cookies")

    async def start(self, callback):
        self.callback = callback
        self.running = True

        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://www.sportybet.com/ng/sport/football/',
            'Origin': 'https://www.sportybet.com',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }

        url = f"{Config.SPORTYBET_API_BASE}/liveOrPrematchEvents?sportId=sr:sport:1"
        logger.info(f"SportyBet feed polling: {url}")

        # CHANGED: Use AsyncSession with impersonate
        async with AsyncSession(headers=headers, impersonate="chrome110") as session:
            while self.running:
                try:
                    # ADDED: Set cookies if we have them
                    if self.cookies:
                        session.cookies.update(self.cookies)
                    
                    # CHANGED: Use curl_cffi request
                    resp = await session.get(url, timeout=30)
                    
                    if resp.status_code == 403:
                        logger.error("SportyBet feed: 403 Forbidden - Need fresh cookies from browser")
                        await asyncio.sleep(5)
                        continue
                    
                    if resp.status_code != 200:
                        logger.warning(f"SportyBet feed: HTTP status {resp.status_code}")
                        await asyncio.sleep(2)
                        continue

                    try:
                        events = resp.json()
                    except Exception:
                        body_preview = resp.text[:200]
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

                        logger.info(f"[SportyBet] {match['home_team']} vs {match['away_team']} -> status={match['match_status']} played_seconds={match['played_seconds']} score={match['home_score']}:{match['away_score']}")

                        if self.callback:
                            await self.callback(match)

                except Exception as e:
                    logger.error(f"SportyBet feed loop error: {e}")

                await asyncio.sleep(2)

    def stop(self):
        self.running = False
