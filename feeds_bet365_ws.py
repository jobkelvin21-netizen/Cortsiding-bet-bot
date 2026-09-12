import asyncio
import json
import threading
import time
from typing import Callable, Dict, List
from datetime import datetime
from loguru import logger
import websocket


class Bet365Feed:
    """
    Fast-feed source: Polymarket sports WebSocket. No auth needed, no key,
    genuinely free. Kept the class/file name for compatibility with
    main.py — internally this is 100% Polymarket, not Bet365.
    """

    def __init__(self, callback: Callable):
        self.callback = callback
        self.running = False
        self.matches: Dict[str, dict] = {}
        self._loop = None
        self._ws = None

    def _on_message(self, ws, message):
        if message == "ping":
            ws.send("pong")
            return
        try:
            data = json.loads(message)
            if not isinstance(data, dict) or 'homeTeam' not in data:
                return

            event_type = data.get('eventState', {}).get('type', '')
            if event_type != 'soccer':
                return

            elapsed_seconds = self._parse_elapsed(data.get('elapsed'))

            match = {
                'source': 'polymarket',
                'match_id': f"poly:{data.get('gameId')}",
                'home_team': data.get('homeTeam', ''),
                'away_team': data.get('awayTeam', ''),
                'period': data.get('period', ''),
                'played_seconds': elapsed_seconds,
                'live': data.get('live', False),
                'ended': data.get('ended', False),
                'league': data.get('leagueAbbreviation', ''),
                'timestamp': datetime.now(),
                'home_score': 0,
                'away_score': 0,
            }
            score = data.get('score', '')
            if '-' in str(score):
                try:
                    h, a = str(score).split('-')
                    match['home_score'] = int(h)
                    match['away_score'] = int(a)
                except Exception:
                    pass

            self.matches[match['match_id']] = match
            if self.callback and self._loop:
                asyncio.run_coroutine_threadsafe(self.callback(match), self._loop)

        except Exception as e:
            logger.debug(f"Polymarket message error: {e}")

    def _parse_elapsed(self, elapsed_raw):
        if elapsed_raw is None:
            return None
        try:
            elapsed_str = str(elapsed_raw).strip()
            if ':' in elapsed_str:
                parts = [int(p) for p in elapsed_str.split(':')]
                if len(parts) == 2:
                    return float(parts[0] * 60 + parts[1])
            else:
                return float(elapsed_str) * 60
        except Exception:
            return None
        return None

    def _on_error(self, ws, error):
        logger.debug(f"Polymarket WS error: {error}")

    def _on_close(self, ws, code, msg):
        logger.warning("Polymarket WebSocket disconnected — reconnecting in 3s")
        if self.running:
            time.sleep(3)
            self._run()

    def _on_open(self, ws):
        logger.success("Polymarket sports WebSocket connected! (soccer-only filter active)")

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
        logger.success("Fast feed started: Polymarket (soccer only)")

    def stop(self):
        self.running = False
        if self._ws:
            self._ws.close()

    def get_matches(self) -> List[Dict]:
        return list(self.matches.values())
