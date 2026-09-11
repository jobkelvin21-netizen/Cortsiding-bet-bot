# feeds/polymarket_ws.py
import asyncio
import json
import threading
import time
from typing import Callable, Dict, List
from datetime import datetime
from loguru import logger
import websocket


class PolymarketFeed:
    """
    Backup fast-feed source: Polymarket sports WebSocket.
    Used only when bet365 is unhealthy/unreachable. Filters strictly
    to soccer/football matches (confirmed field: eventState.type).
    """

    def __init__(self, callback: Callable):
        self.callback = callback
        self.running = False
        self.matches: Dict[str, dict] = {}
        self._loop = None
        self._ws = None
        self.last_message_at = None

    def is_healthy(self) -> bool:
        if self.last_message_at is None:
            return False
        return (time.time() - self.last_message_at) < 30

    def _on_message(self, ws, message):
        if message == "ping":
            ws.send("pong")
            return
        try:
            data = json.loads(message)
            self.last_message_at = time.time()

            if not isinstance(data, dict) or 'homeTeam' not in data:
                return

            event_type = data.get('eventState', {}).get('type', '')
            if event_type != 'soccer':
                return

            match = {
                'source': 'polymarket',
                'match_id': f"poly:{data.get('gameId')}",
                'home_team': data.get('homeTeam', ''),
                'away_team': data.get('awayTeam', ''),
                'period': data.get('period', ''),
                'live': data.get('live', False),
                'ended': data.get('ended', False),
                'league': data.get('leagueAbbreviation', ''),
                'timestamp': datetime.now(),
            }
            score = data.get('score', '')
            if '-' in str(score):
                try:
                    h, a = str(score).split('-')
                    match['home_score'] = int(h)
                    match['away_score'] = int(a)
                except Exception:
                    match['home_score'] = 0
                    match['away_score'] = 0

            self.matches[match['match_id']] = match
            if self.callback and self._loop:
                asyncio.run_coroutine_threadsafe(self.callback(match), self._loop)

        except Exception as e:
            logger.debug(f"Polymarket message error: {e}")

    def _on_error(self, ws, error):
        logger.debug(f"Polymarket WS error: {error}")

    def _on_close(self, ws, code, msg):
        logger.warning("Polymarket WebSocket disconnected")
        if self.running:
            time.sleep(3)
            self._run()

    def _on_open(self, ws):
        logger.success("Polymarket sports WebSocket connected! (soccer-only filter active)")
        self.last_message_at = time.time()

    def _run(self):
        self._ws = websocket.WebSocketApp(
            "wss://sports-api.polymarket.com/ws",
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever()

    async def start(self):
        self.running = True
        self._loop = asyncio.get_event_loop()
        threading.Thread(target=self._run, daemon=True).start()
        logger.success("Backup feed started: Polymarket (soccer only)")

    def stop(self):
        self.running = False
        if self._ws:
            self._ws.close()

    def get_matches(self) -> List[Dict]:
        return list(self.matches.values())
