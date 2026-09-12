import asyncio
import json
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List

from loguru import logger
import websocket


class PolymarketFeed:
    """
    Polymarket live sports WebSocket.

    Responsibilities:
    - Receive live sports events.
    - Keep the latest event for every Polymarket game.
    - Pass soccer/football score updates to the supplied callback.
    - Maintain WebSocket reconnect behavior.

    Goal detection itself is intentionally NOT performed here.
    MatchLinker handles score-change detection.
    """

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

    # =========================================================================
    # HEALTH
    # =========================================================================

    def is_healthy(self) -> bool:
        if self.last_message_at is None:
            return False

        return (time.time() - self.last_message_at) < 30

    # =========================================================================
    # MESSAGE HANDLER
    # =========================================================================

    def _on_message(self, ws, message):
        # Polymarket heartbeat.
        if message == "ping":
            try:
                ws.send("pong")
            except Exception as e:
                logger.debug(f"[POLYMARKET] Failed to answer ping: {e}")
            return

        try:
            data = json.loads(message)
        except Exception as e:
            logger.debug(f"[POLYMARKET] JSON decode error: {e}")
            return

        self.last_message_at = time.time()

        if not isinstance(data, dict):
            return

        if "homeTeam" not in data:
            return

        # ---------------------------------------------------------------------
        # Soccer filter
        #
        # Your terminal test confirmed that Polymarket explicitly provides:
        #
        # eventState.type = "soccer"
        #
        # We also retain "football" for compatibility with your previous feed.
        # College football is deliberately NOT accepted.
        # ---------------------------------------------------------------------

        event_state = data.get("eventState")

        if not isinstance(event_state, dict):
            event_state = {}

        event_type = str(
            event_state.get("type", "")
        ).strip().lower()

        if event_type not in ("soccer", "football"):
            logger.debug(
                "[POLYMARKET] Skipping non-soccer event: "
                f"type='{event_type}' "
                f"home='{data.get('homeTeam')}'"
            )
            return

        game_id = data.get("gameId")

        if game_id is None:
            logger.debug("[POLYMARKET] Soccer event has no gameId.")
            return

        # ---------------------------------------------------------------------
        # Score
        # ---------------------------------------------------------------------

        home_score, away_score = self._parse_score(
            data.get("score")
        )

        # ---------------------------------------------------------------------
        # Build standard match structure.
        # ---------------------------------------------------------------------

        match = {
            "source": "polymarket",
            "match_id": f"poly:{game_id}",
            "game_id": game_id,
            "sportradar_game_id": data.get("sportradarGameId"),

            "home_team": str(
                data.get("homeTeam", "")
            ).strip(),

            "away_team": str(
                data.get("awayTeam", "")
            ).strip(),

            "period": data.get(
                "period",
                event_state.get("period", "")
            ),

            "live": bool(
                data.get(
                    "live",
                    event_state.get("live", False)
                )
            ),

            "ended": bool(
                data.get(
                    "ended",
                    event_state.get("ended", False)
                )
            ),

            "status": data.get("status", ""),

            "league": data.get(
                "leagueAbbreviation",
                ""
            ),

            "home_score": home_score,
            "away_score": away_score,

            "elapsed": data.get(
                "elapsed",
                event_state.get("elapsed", "")
            ),

            "timestamp": datetime.now(),
        }

        # ---------------------------------------------------------------------
        # Store latest version.
        # ---------------------------------------------------------------------

        with self._lock:
            self.matches[match["match_id"]] = match

        logger.debug(
            f"[POLYMARKET] "
            f"{match['home_team']} "
            f"{home_score}-{away_score} "
            f"{match['away_team']} "
            f"| league={match['league']} "
            f"| live={match['live']} "
            f"| ended={match['ended']}"
        )

        # ---------------------------------------------------------------------
        # Pass to MatchLinker.
        #
        # run_coroutine_threadsafe is necessary because websocket-client
        # executes this callback on its own thread.
        # ---------------------------------------------------------------------

        if self.callback and self._loop:

            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.callback(match),
                    self._loop
                )

                # We deliberately do not call future.result().
                # Blocking the WebSocket thread would be dangerous.
                _ = future

            except Exception as e:
                logger.debug(
                    f"[POLYMARKET] Callback scheduling error: {e}"
                )

    # =========================================================================
    # SCORE PARSER
    # =========================================================================

    @staticmethod
    def _parse_score(score):
        """
        Parse normal soccer scores such as:

            0-0
            1-0
            2-1

        Returns (home, away).
        """

        if score is None:
            return 0, 0

        text = str(score).strip()

        if "-" not in text:
            return 0, 0

        parts = text.split("-", 1)

        if len(parts) != 2:
            return 0, 0

        try:
            return (
                int(parts[0].strip()),
                int(parts[1].strip()),
            )
        except (TypeError, ValueError):
            return 0, 0

    # =========================================================================
    # ERROR / CLOSE / OPEN
    # =========================================================================

    def _on_error(self, ws, error):
        logger.debug(
            f"[POLYMARKET] WebSocket error: {error}"
        )

    def _on_close(self, ws, code, msg):
        logger.warning(
            f"[POLYMARKET] WebSocket disconnected "
            f"(code={code}, msg={msg})"
        )

    def _on_open(self, ws):
        self.last_message_at = time.time()

        logger.success(
            "Polymarket sports WebSocket connected! "
            "(soccer/football)"
        )

    # =========================================================================
    # WEBSOCKET THREAD
    # =========================================================================

    def _run(self):
        while self.running:

            try:
                logger.info(
                    "[POLYMARKET] Connecting to sports WebSocket..."
                )

                self._ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )

                self._ws.run_forever()

            except Exception as e:
                logger.error(
                    f"[POLYMARKET] WebSocket thread error: {e}"
                )

            finally:
                self._ws = None

            if self.running:
                time.sleep(self._reconnect_delay)

    # =========================================================================
    # START / STOP
    # =========================================================================

    async def start(self):
        if self.running:
            return

        self.running = True
        self._loop = asyncio.get_running_loop()

        self._thread = threading.Thread(
            target=self._run,
            name="polymarket-websocket",
            daemon=True,
        )

        self._thread.start()

        logger.success(
            "Polymarket feed started "
            "(soccer/football only)"
        )

    def stop(self):
        self.running = False

        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

        logger.info("Polymarket feed stopped.")

    # =========================================================================
    # ACCESS
    # =========================================================================

    def get_matches(self) -> List[Dict]:
        with self._lock:
            return list(self.matches.values())

    def get_match(self, match_id: str):
        with self._lock:
            return self.matches.get(match_id)
