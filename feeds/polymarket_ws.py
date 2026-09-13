import asyncio
import json
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List

from loguru import logger
import websocket


class PolymarketFeed:
    WS_URL = "wss://sports-api.polymarket.com/ws"

    def __init__(self, callback: Callable):
        self.callback = callback
        self.running = False

        self.matches: Dict[str, dict] = {}

        self._loop = None
        self._ws = None
        self._thread = None

        self.last_message_at = None

        self._reconnect_delay = 3.0
        self._lock = threading.RLock()

        # NEW: count skipped non-soccer events instead of logging each one —
        # per-message debug string formatting was real overhead at high volume
        self._skipped_count = 0
        self._last_skip_log = 0.0

    def is_healthy(self) -> bool:
        if self.last_message_at is None:
            return False
        return (time.time() - self.last_message_at) < 30

    def _on_message(self, ws, message):
        if message == "ping":
            try:
                ws.send("pong")
            except Exception as e:
                logger.debug(f"[POLYMARKET] Failed to answer ping: {e}")
            return

        try:
            data = json.loads(message)
        except Exception:
            return

        self.last_message_at = time.time()

        if not isinstance(data, dict):
            return

        if "homeTeam" not in data:
            return

        event_state = data.get("eventState")
        if not isinstance(event_state, dict):
            event_state = {}

        event_type = str(event_state.get("type", "")).strip().lower()

        if event_type not in ("soccer", "football"):
            # CHANGED: no per-message logging here anymore — just count.
            # Logged in aggregate at most once every 10 seconds below.
            self._skipped_count += 1
            now = time.time()
            if now - self._last_skip_log > 10:
                logger.debug(f"[POLYMARKET] Skipped {self._skipped_count} non-soccer events in the last 10s")
                self._skipped_count = 0
                self._last_skip_log = now
            return

        game_id = data.get("gameId")
        if game_id is None:
            return

        ended = bool(data.get("ended", event_state.get("ended", False)))
        match_id = f"poly:{game_id}"

        # Purge ended matches immediately — prevents stale post-match
        # updates from ever being read as a live goal.
        if ended:
            with self._lock:
                removed = self.matches.pop(match_id, None)
            if removed:
                logger.info(f"[POLYMARKET] Match ended, removing from tracking: "
                            f"{removed.get('home_team')} vs {removed.get('away_team')}")
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
            "period": data.get("period", event_state.get("period", "")),
            "live": live,
            "ended": ended,
            "status": data.get("status", ""),
            "league": data.get("leagueAbbreviation", ""),
            "home_score": home_score,
            "away_score": away_score,
            "elapsed": data.get("elapsed", event_state.get("elapsed", "")),
            "timestamp": datetime.now(),
        }

        with self._lock:
            self.matches[match["match_id"]] = match

        # CHANGED: this debug line now only fires for real, live soccer
        # updates — a small fraction of total traffic — so it's cheap.
        logger.debug(
            f"[POLYMARKET] {match['home_team']} {home_score}-{away_score} "
            f"{match['away_team']} | league={match['league']} | live={live}"
        )

        if self.callback and self._loop:
            try:
                future = asyncio.run_coroutine_threadsafe(self.callback(match), self._loop)
                _ = future
            except Exception as e:
                logger.debug(f"[POLYMARKET] Callback scheduling error: {e}")

    @staticmethod
    def _parse_score(score):
        if score is None:
            return 0, 0
        text = str(score).strip()
        if "-" not in text:
            return 0, 0
        parts = text.split("-", 1)
        if len(parts) != 2:
            return 0, 0
        try:
            return int(parts[0].strip()), int(parts[1].strip())
        except (TypeError, ValueError):
            return 0, 0

    def _on_error(self, ws, error):
        logger.debug(f"[POLYMARKET] WebSocket error: {error}")

    def _on_close(self, ws, code, msg):
        logger.warning(f"[POLYMARKET] WebSocket disconnected (code={code}, msg={msg})")

    def _on_open(self, ws):
        self.last_message_at = time.time()
        logger.success("Polymarket sports WebSocket connected! (soccer/football)")

    def _run(self):
        while self.running:
            try:
                logger.info("[POLYMARKET] Connecting to sports WebSocket...")
                self._ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever()
            except Exception as e:
                logger.error(f"[POLYMARKET] WebSocket thread error: {e}")
            finally:
                self._ws = None

            if self.running:
                time.sleep(self._reconnect_delay)

    async def start(self):
        if self.running:
            return
        self.running = True
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run, name="polymarket-websocket", daemon=True)
        self._thread.start()
        logger.success("Polymarket feed started (soccer/football only)")

    def stop(self):
        self.running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        logger.info("Polymarket feed stopped.")

    def get_matches(self) -> List[Dict]:
        with self._lock:
            return list(self.matches.values())

    def get_match(self, match_id: str):
        with self._lock:
            return self.matches.get(match_id)
