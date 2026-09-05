import asyncio
import json
import threading
import time
from typing import Callable, Dict, List
from datetime import datetime
from urllib.parse import urlencode
import websocket
from loguru import logger


class Bet365Feed:
    def __init__(self, callback: Callable):
        self.callback = callback
        self.api_key = None
        self.ws_url = None
        self.running = False
        self.matches: Dict = {}
        self.last_seq = 0
        self._ws = None
        self._loop = None

    def set_api_key(self, api_key: str):
        self.api_key = api_key
        self._build_url()

    def _build_url(self):
        params = {
            'apiKey': self.api_key,
            'markets': 'ML,Spread,Totals',
            'sport': 'football',
            'status': 'live'
        }
        if self.last_seq > 0:
            params['lastSeq'] = self.last_seq
        self.ws_url = f"wss://api.odds-api.io/v3/ws?{urlencode(params)}"

    def _on_open(self, ws):
        logger.success("Odds-API WebSocket connection opened")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get('type')

            if msg_type == 'welcome':
                logger.success(f"Connected! Bookmakers: {data.get('bookmakers')} | Filters: {data.get('sport_filter')}")
                return

            if msg_type == 'resync_required':
                logger.warning(f"Resync required: {data.get('reason')} — resetting seq")
                self.last_seq = 0
                return

            if 'seq' in data:
                self.last_seq = data['seq']

            if msg_type in ('created', 'updated'):
                match = self._parse_match(data)
                if match and self.callback and self._loop:
                    asyncio.run_coroutine_threadsafe(self.callback(match), self._loop)

            elif msg_type == 'deleted':
                self.matches.pop(data.get('id'), None)

        except Exception as e:
            logger.error(f"Message processing error: {e}")

    def _parse_match(self, data: Dict):
        try:
            match_id = data.get('id')
            bookie = data.get('bookie', '')
            markets = data.get('markets', [])

            home_odds = draw_odds = away_odds = None
            for market in markets:
                if market.get('name') == 'ML' and market.get('odds'):
                    o = market['odds'][0]
                    home_odds, draw_odds, away_odds = o.get('home'), o.get('draw'), o.get('away')

            return {
                'id': match_id,
                'match_id': match_id,
                'bookie': bookie,
                'home': 'Home',
                'away': 'Away',
                'home_team': 'Home',
                'away_team': 'Away',
                'home_score': 0,
                'away_score': 0,
                'home_odds': home_odds,
                'draw_odds': draw_odds,
                'away_odds': away_odds,
                'league': 'Live',
                'timestamp': datetime.now()
            }
        except Exception as e:
            logger.error(f"Match parse error: {e}")
            return None

    def _on_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        logger.warning("WebSocket disconnected")
        if self.running:
            time.sleep(3)
            self._run_ws()

    def _run_ws(self):
        self._build_url()
        self._ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        self._ws.run_forever()

    async def start(self):
        if not self.api_key:
            logger.error("No API key set — call set_api_key() before starting")
            return
        self.running = True
        self._loop = asyncio.get_event_loop()
        threading.Thread(target=self._run_ws, daemon=True).start()

    def stop(self):
        self.running = False
        if self._ws:
            self._ws.close()

    def get_matches(self) -> List[Dict]:
        return list(self.matches.values())
