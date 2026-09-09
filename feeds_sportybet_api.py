import asyncio
import re
from datetime import datetime
from typing import Callable, Dict
from loguru import logger
from curl_cffi.requests import AsyncSession
from config import Config


class SportyBetFeed:
    def __init__(self):
        self.matches = {}
        self.running = False
        self.callback = None
        self.cookies = {}
        self.connected = False

    def _parse_played_seconds(self, played_seconds_str):
        try:
            parts = [int(p) for p in str(played_seconds_str).split(':')]
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

    def set_cookies(self, cookies: Dict[str, str]):
        self.cookies = cookies
        logger.info(f"Updated SportyBet cookies: {len(cookies)} cookies")

    async def start(self, callback: Callable):
        self.callback = callback
        self.running = True
        self.connected = False

        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.sportybet.com/ng/sport/football/',
            'Origin': 'https://www.sportybet.com',
        }

        url = f"{Config.SPORTYBET_API_BASE}/liveOrPrematchEvents?sportId=sr:sport:1"
        logger.info(f"SportyBet feed polling: {url}")

        async with AsyncSession(headers=headers, impersonate="chrome120") as session:
            # Get initial cookies
            try:
                await session.get("https://www.sportybet.com/ng/", timeout=20)
                logger.info("Visited SportyBet homepage for cookies")
            except Exception as e:
                logger.warning(f"Homepage visit failed: {e}")

            while self.running:
                try:
                    if self.cookies:
                        session.cookies.update(self.cookies)

                    resp = await session.get(url, timeout=25)

                    if resp.status_code != 200:
                        logger.warning(f"SportyBet feed: HTTP {resp.status_code}")
                        await asyncio.sleep(3)
                        continue

                    data = resp.json()

                    if isinstance(data, dict):
                        events = data.get('data', [])
                    else:
                        events = data

                    if not events:
                        logger.warning("SportyBet feed: 0 events returned")
                        await asyncio.sleep(2)
                        continue

                    # Only show success after we actually received valid data
                    if not self.connected:
                        logger.success("SportyBet feed connected successfully!")
                        self.connected = True

                    for detail in events:
                        event_list = detail.get('events', [detail]) if isinstance(detail, dict) else [detail]

                        for event in event_list:
                            event_id = event.get('eventId')
                            if not event_id:
                                continue

                            match = {
                                'match_id': event_id,
                                'sportradar_id': self._extract_sportradar_numeric_id(event_id),
                                'home_team': event.get('homeTeamName', ''),
                                'away_team': event.get('awayTeamName', ''),
                                'home_score': 0,
                                'away_score': 0,
                                'period': event.get('period', ''),
                                'match_status': event.get('matchStatus', ''),
                                'played_seconds': self._parse_played_seconds(
                                    event.get('playedSeconds', '0:0')
                                ),
                                'timestamp': datetime.now(),
                            }

                            game_score = event.get('gameScore', [])
                            if game_score:
                                try:
                                    last = game_score[-1]
                                    h, a = last.split(':')
                                    match['home_score'] = int(h)
                                    match['away_score'] = int(a)
                                except Exception:
                                    pass

                            self.matches[event_id] = match

                            logger.info(
                                f"[SportyBet] {match['home_team']} vs {match['away_team']} | "
                                f"{match['home_score']}-{match['away_score']} | "
                                f"{match['match_status']} | {match['played_seconds']}s"
                            )

                            if self.callback:
                                await self.callback(match)

                except Exception as e:
                    logger.error(f"SportyBet feed error: {e}")

                await asyncio.sleep(2)

    def stop(self):
        self.running = False
