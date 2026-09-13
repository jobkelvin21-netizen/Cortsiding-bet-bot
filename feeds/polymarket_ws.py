import asyncio
import json
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from loguru import logger
import websocket


class PolymarketFeed:
    WS_URL = "wss://sports-api.polymarket.com/ws"

    def __init__(self, callback: Callable):
        self.callback = callback
        self.running = False

        self.matches: Dict[str, dict] = {}
        self._lock = threading.RLock()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        self._thread = None

        self.last_message_at: Optional[float] = None
        self._reconnect_delay = 1.5          # faster reconnect
        self._skipped_count = 0
        self._last_skip_log = 0.0

    def is_healthy(self) -> bool:
        if self.last_message_at is None:
            return False
        return (time.time() - self.last_message_at) < 25

    def _on_message(self, ws, message):
        # Ultra-fast path for ping
        if message == "ping":
            try:
                ws.send("pong")
            except Exception:
                pass
            return

        try:
            data = json.loads(message)
        except Exception:
            return

        self.last_message_at = time.time()

        if not isinstance(data, dict) or "homeTeam" not in data:
            return

        event_state = data.get("eventState") or {}
        event_type = str(event_state.get("type", "")).lower()

        # Only soccer / football
        if event_type not in ("soccer", "football"):
            self._skipped_count += 1
            now = time.time()
            if now - self._last_skip_log > 15:
                if self._skipped_count > 0:
                    logger.debug(f"[POLYMARKET] Skipped {self._skipped_count} non-soccer events")
                self._skipped_count = 0
                self._last_skip_log = now
            return

        game_id = data.get("gameId")
        if game_id is None:
            return

        ended = bool(data.get("ended", event_state.get("ended", False)))
        match_id = f"poly:{game_id}"

        # Remove ended matches immediately
        if ended:
            with self._lock:
                removed = self.matches.pop(match_id, None)
            if removed:
                logger.info(
                    f"[POLYMARKET] Ended → removed: "
                    f"{removed.get('home_team')} vs {removed.get('away_team')}"
                )
            return

        live = bool(data.get("live", event_state.get("live", False)))
        if not live:
            return

        home_score, away_score = self._parse_score(data.get("score"))

        match = {
            "source": "polymarket",
            "match_id": match_id,
            "game_id": game_id,
            "sportradar_game_id": data.get("sportradarGameId"),
            "home_team": str(data.get("homeTeam", "")).strip(),
            "away_team": str(data.get("awayTeam", "")).strip(),
            "period": data.get("period") or event_state.get("period", ""),
            "live": True,
            "ended": False,
            "status": data.get("status", ""),
            "league": data.get("leagueAbbreviation", ""),
            "home_score": home_score,
            "away_score": away_score,
            "elapsed": data.get("elapsed") or event_state.get("elapsed", ""),
            "timestamp": datetime.now(),
        }

        with self._lock:
            self.matches[match_id] = match

        # Fire callback as fast as possible
        if self.callback and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(self.callback(match), self._loop)
            except Exception:
                pass

    @staticmethod
    def _parse_score(score) -> tuple:
        if not score:
            return 0, 0
        try:
            text = str(score).strip()
            if "-" not in text:
                return 0, 0
            left, right = text.split("-", 1)
            return int(left.strip()), int(right.strip())
        except Exception:
            return 0, 0

    def _on_error(self, ws, error):
        # Keep this very quiet to avoid log spam
        logger.debug(f"[POLYMARKET] WS error: {error}")

    def _on_close(self, ws, code, msg):
        logger.warning(f"[POLYMARKET] Disconnected (code={code})")

    def _on_open(self, ws):
        self.last_message_at = time.time()
        logger.success("Polymarket WebSocket connected (ultra-low latency mode)")

    def _run(self):
        while self.running:
            try:
                self._ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                # Important: disable the internal ping so we handle it ourselves
                self._ws.run_forever(ping_interval=0, ping_timeout=None)
            except Exception as e:
                logger.error(f"[POLYMARKET] Thread error: {e}")
            finally:
                self._ws = None

            if self.running:
                time.sleep(self._reconnect_delay)

    async def start(self):
        if self.running:
            return
        self.running = True
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._run,
            name="polymarket-ws",
            daemon=True
        )
        self._thread.start()
        logger.success("Polymarket feed started (sub-second / ultra-low latency)")

    def stop(self):
        self.running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        logger.info("Polymarket feed stopped")

    def get_matches(self) -> List[Dict]:
        with self._lock:
            return list(self.matches.values())

    def get_match(self, match_id: str):
        with self._lock:
            return self.matches.get(match_id)
