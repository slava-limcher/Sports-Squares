"""
Replay controller.

Drives a room's state from a pre-fetched timeline of plate appearances
from a finished game. Lets us test the UI without live games — useful
in the offseason, on off days, or just for development iteration.

How it works:
  1. ReplayController fetches all plate appearances for a historical game
  2. It also snapshots the game's final odds and props as static reference data
  3. A background asyncio task ticks through the PAs at a configurable
     speed, updating room.game_state and broadcasting messages just like
     the live poller would
  4. Webhook-equivalent events fire on scoring plays, home runs, and
     inning changes — same code path as the real webhook handler
  5. The room is marked replay_mode=True so the live poller skips it

Each room can have at most one replay running. Starting a new replay
on a room with an existing one stops the old one first.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.bdl_client import bdl_client
from app.models import WSMessage, WSMessageType
from app.poller import polling_engine
from app.room import GameRoom, room_manager

logger = logging.getLogger("barboards.replay")


# Speed presets — seconds to wait between PAs.
# "instant" means no wait; useful for scrubbing through fast.
SPEED_PRESETS = {
    "real_time": 25.0,    # ~25s per PA, full game ~2.5 hours
    "fast": 2.5,          # ~2.5s per PA, full game ~15 min
    "very_fast": 0.5,     # ~0.5s per PA, full game ~3 min
    "instant": 0.0,       # no wait, ticks as fast as asyncio allows
}


class ReplayController:
    """Drives a single room's state through a historical game's plays."""

    def __init__(
        self,
        room: GameRoom,
        game_id: int,
        speed: str = "fast",
    ) -> None:
        self.room = room
        self.source_game_id = game_id
        self.speed = speed
        self.tick_interval = SPEED_PRESETS.get(speed, SPEED_PRESETS["fast"])

        # Timeline data — populated by load()
        self.plate_appearances: List[Dict[str, Any]] = []
        self.game_data: Dict[str, Any] = {}
        self.cached_odds: List[Dict[str, Any]] = []
        self.cached_props: List[Dict[str, Any]] = []
        self.cached_lineups: List[Dict[str, Any]] = []

        # Replay state
        self.current_index: int = 0
        self.paused: bool = False
        self.running: bool = False
        self._task: Optional[asyncio.Task] = None

        # Score tracking — we rebuild as we replay
        self.away_runs: int = 0
        self.home_runs: int = 0
        self.away_hits: int = 0
        self.home_hits: int = 0
        self.current_inning: int = 1
        self.current_half: str = "top"
        self.current_outs: int = 0

    # ── Loading ──────────────────────────────────

    async def load(self) -> None:
        """Fetch all data needed for the replay from balldontlie."""
        logger.info("Replay: loading game %d", self.source_game_id)

        # Fetch the game itself for team info
        self.game_data = await bdl_client.get_game(self.source_game_id)

        # Fetch all plate appearances (paginated)
        self.plate_appearances = await self._fetch_all_plate_appearances()

        # Snapshot odds and props (these stay static during the replay
        # in this version — phase 2 would synthesize WE-based shifts)
        try:
            self.cached_odds = await bdl_client.get_betting_odds(self.source_game_id)
        except Exception:
            logger.exception("Replay: failed to fetch odds")

        try:
            self.cached_props = await bdl_client.get_player_props(self.source_game_id)
        except Exception:
            logger.exception("Replay: failed to fetch props")

        try:
            self.cached_lineups = await bdl_client.get_lineups(self.source_game_id)
        except Exception:
            logger.exception("Replay: failed to fetch lineups")

        logger.info(
            "Replay: loaded %d plate appearances, %d odds books, %d props",
            len(self.plate_appearances),
            len(self.cached_odds),
            len(self.cached_props),
        )

    async def _fetch_all_plate_appearances(self) -> List[Dict[str, Any]]:
        """
        Fetch all PAs for the game, paginated.
        Note: this requires a get_plate_appearances method on bdl_client
        which doesn't exist yet — see the integration notes.
        """
        if not hasattr(bdl_client, "get_plate_appearances"):
            logger.warning(
                "bdl_client.get_plate_appearances not implemented yet. "
                "Replay will have no PAs to walk through."
            )
            return []

        all_pas: List[Dict[str, Any]] = []
        cursor = None
        while True:
            try:
                page = await bdl_client.get_plate_appearances(
                    game_id=self.source_game_id,
                    cursor=cursor,
                    per_page=100,
                )
            except Exception:
                logger.exception("Failed to fetch PA page")
                break

            data = page.get("data", []) if isinstance(page, dict) else page
            if not data:
                break
            all_pas.extend(data)

            meta = page.get("meta", {}) if isinstance(page, dict) else {}
            cursor = meta.get("next_cursor")
            if not cursor:
                break

        return all_pas

    # ── Lifecycle ────────────────────────────────

    async def start(self) -> None:
        """Mark room as replay mode, seed initial state, and start ticking."""
        if self.running:
            logger.warning("Replay already running for room %s", self.room.room_code)
            return

        # Seed the room with the historical game's team info but reset scores
        self.room.game_state = self._build_initial_game_state()
        self.room.odds = self.cached_odds
        self.room.props = self.cached_props
        self.room.lineups = self.cached_lineups
        self.room.replay_mode = True

        # Send a fresh room_state to all connected clients
        await self.room.broadcast(WSMessage(
            type=WSMessageType.ROOM_STATE,
            data=self.room._build_full_state(),
        ))

        self.running = True
        self.paused = False
        self._task = asyncio.create_task(self._tick_loop())
        logger.info(
            "Replay started for room %s (game %d, %d PAs, speed=%s)",
            self.room.room_code,
            self.source_game_id,
            len(self.plate_appearances),
            self.speed,
        )

    async def stop(self) -> None:
        """Stop ticking and unmark replay mode."""
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.room.replay_mode = False
        logger.info("Replay stopped for room %s", self.room.room_code)

    def pause(self) -> None:
        self.paused = True
        logger.info("Replay paused for room %s", self.room.room_code)

    def resume(self) -> None:
        self.paused = False
        logger.info("Replay resumed for room %s", self.room.room_code)

    def set_speed(self, speed: str) -> None:
        if speed not in SPEED_PRESETS:
            raise ValueError(f"Unknown speed: {speed}")
        self.speed = speed
        self.tick_interval = SPEED_PRESETS[speed]
        logger.info("Replay speed set to %s for room %s", speed, self.room.room_code)

    def seek(self, play_index: int) -> None:
        """
        Jump to a specific play index. Replays scoring from the start
        up to that point so the score is consistent.
        """
        if play_index < 0 or play_index >= len(self.plate_appearances):
            raise ValueError(f"Invalid play index: {play_index}")

        # Reset and fast-forward
        self.away_runs = 0
        self.home_runs = 0
        self.away_hits = 0
        self.home_hits = 0
        self.current_inning = 1
        self.current_half = "top"
        self.current_outs = 0
        self.current_index = 0

        for i in range(play_index):
            self._apply_pa_to_state(self.plate_appearances[i], broadcast=False)
        self.current_index = play_index

        # Sync room state and broadcast
        self.room.game_state = self._build_current_game_state()
        asyncio.create_task(self.room.broadcast(WSMessage(
            type=WSMessageType.GAME_UPDATE,
            data=self.room.game_state,
        )))
        logger.info("Replay seeked to PA %d for room %s", play_index, self.room.room_code)

    # ── Tick loop ────────────────────────────────

    async def _tick_loop(self) -> None:
        """
        Main loop. Walks through plate appearances, applying each one
        to the room state and broadcasting updates.
        """
        try:
            while self.running and self.current_index < len(self.plate_appearances):
                if self.paused:
                    await asyncio.sleep(0.5)
                    continue

                pa = self.plate_appearances[self.current_index]
                await self._apply_pa(pa)
                self.current_index += 1

                if self.tick_interval > 0:
                    await asyncio.sleep(self.tick_interval)

            if self.current_index >= len(self.plate_appearances):
                logger.info(
                    "Replay finished for room %s (reached end of PAs)",
                    self.room.room_code,
                )
                # Mark game as final
                self.room.game_state["status"] = "STATUS_FINAL"
                await self.room.broadcast(WSMessage(
                    type=WSMessageType.GAME_UPDATE,
                    data=self.room.game_state,
                ))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Replay tick loop crashed")

    async def _apply_pa(self, pa: Dict[str, Any]) -> None:
        """Apply a single plate appearance to room state and broadcast."""
        self._apply_pa_to_state(pa, broadcast=True)

        # Update room state
        self.room.game_state = self._build_current_game_state()

        # Broadcast game update
        await self.room.broadcast(WSMessage(
            type=WSMessageType.GAME_UPDATE,
            data=self.room.game_state,
        ))

        # If this PA caused a scoring event, fire the equivalent webhook event
        # so the UI gets the same notification it would in live mode
        await self._maybe_fire_webhook_event(pa)

    def _apply_pa_to_state(self, pa: Dict[str, Any], broadcast: bool = True) -> None:
        """
        Update internal score/inning tracking based on a single PA.

        The exact field names from balldontlie's plate_appearances endpoint
        need to be confirmed against live data — this implementation uses
        a best guess based on common shapes (result, runs_scored, inning,
        inning_half, outs_after, etc.). We'll calibrate these against
        actual data the first time we run the replay against a real game.
        """
        # Inning tracking
        new_inning = pa.get("inning")
        new_half = pa.get("inning_half") or pa.get("half")
        if new_inning is not None:
            self.current_inning = int(new_inning)
        if new_half:
            self.current_half = new_half

        # Outs after this PA
        outs_after = pa.get("outs_after")
        if outs_after is not None:
            self.current_outs = int(outs_after)

        # Runs scored on this PA
        runs_on_play = pa.get("runs_scored", 0) or 0
        if runs_on_play > 0:
            if self.current_half == "top":
                self.away_runs += runs_on_play
            else:
                self.home_runs += runs_on_play

        # Hits
        result = (pa.get("result") or "").lower()
        if any(r in result for r in ["single", "double", "triple", "home run", "homer"]):
            if self.current_half == "top":
                self.away_hits += 1
            else:
                self.home_hits += 1

    async def _maybe_fire_webhook_event(self, pa: Dict[str, Any]) -> None:
        """
        If this PA matches a webhook event type, broadcast it as a
        webhook_event message — same shape the real webhook handler
        produces, so downstream UI code is identical.
        """
        result = (pa.get("result") or "").lower()
        runs_on_play = pa.get("runs_scored", 0) or 0
        event_type = None

        if "home run" in result or "homer" in result:
            event_type = "mlb.batter.home_run"
        elif runs_on_play > 0:
            event_type = "mlb.team.scored"
        elif "strikeout" in result or "struck out" in result:
            event_type = "mlb.batter.strikeout"
        elif any(r in result for r in ["single", "double", "triple"]):
            event_type = "mlb.batter.hit"

        if not event_type:
            return

        payload = {
            "event_type": event_type,
            "game": {"id": self.source_game_id},
            "play": {
                "text": pa.get("description") or pa.get("result") or "",
                "inning": self.current_inning,
                "inning_half": self.current_half,
                "home_score": self.home_runs,
                "away_score": self.away_runs,
            },
            "batter": {
                "id": pa.get("batter_id") or pa.get("player_id"),
                "first_name": pa.get("batter_first_name", ""),
                "last_name": pa.get("batter_last_name", ""),
            },
        }

        await self.room.broadcast(WSMessage(
            type=WSMessageType.WEBHOOK_EVENT,
            data={"event_type": event_type, "payload": payload},
        ))

    # ── State building ───────────────────────────

    def _build_initial_game_state(self) -> Dict[str, Any]:
        """Build the room's game_state at the start of the replay."""
        return {
            **self.game_data,
            "home_team_data": {"runs": 0, "hits": 0, "errors": 0, "inning_scores": []},
            "away_team_data": {"runs": 0, "hits": 0, "errors": 0, "inning_scores": []},
            "status": "STATUS_IN_PROGRESS",
            "period": 1,
            "inning_half": "top",
            "outs": 0,
            "_replay": True,
        }

    def _build_current_game_state(self) -> Dict[str, Any]:
        """Build the room's game_state from current replay tracking."""
        return {
            **self.game_data,
            "home_team_data": {
                "runs": self.home_runs,
                "hits": self.home_hits,
                "errors": 0,
                "inning_scores": [],
            },
            "away_team_data": {
                "runs": self.away_runs,
                "hits": self.away_hits,
                "errors": 0,
                "inning_scores": [],
            },
            "status": "STATUS_IN_PROGRESS",
            "period": self.current_inning,
            "inning_half": self.current_half,
            "outs": self.current_outs,
            "_replay": True,
            "_replay_progress": {
                "current_pa": self.current_index,
                "total_pas": len(self.plate_appearances),
            },
        }

    # ── Status ───────────────────────────────────

    def status(self) -> Dict[str, Any]:
        return {
            "room_code": self.room.room_code,
            "source_game_id": self.source_game_id,
            "running": self.running,
            "paused": self.paused,
            "speed": self.speed,
            "current_pa": self.current_index,
            "total_pas": len(self.plate_appearances),
            "score": {"away": self.away_runs, "home": self.home_runs},
            "inning": self.current_inning,
            "half": self.current_half,
        }


class ReplayManager:
    """Global registry of active replays, keyed by room code."""

    def __init__(self) -> None:
        self._replays: Dict[str, ReplayController] = {}

    async def start_replay(
        self,
        room_code: str,
        game_id: int,
        speed: str = "fast",
    ) -> ReplayController:
        """
        Start (or restart) a replay on a room. The room must already exist.
        If a replay is already running on this room, stops it first.
        """
        room = room_manager.get_room(room_code)
        if not room:
            raise ValueError(f"Room {room_code} does not exist")

        # Stop any existing replay
        if room_code in self._replays:
            await self._replays[room_code].stop()
            del self._replays[room_code]

        controller = ReplayController(room=room, game_id=game_id, speed=speed)
        await controller.load()
        await controller.start()
        self._replays[room_code] = controller
        return controller

    async def stop_replay(self, room_code: str) -> bool:
        if room_code not in self._replays:
            return False
        await self._replays[room_code].stop()
        del self._replays[room_code]
        return True

    def get(self, room_code: str) -> Optional[ReplayController]:
        return self._replays.get(room_code)

    @property
    def active_replays(self) -> Dict[str, ReplayController]:
        return self._replays


# Module-level singleton
replay_manager = ReplayManager()
