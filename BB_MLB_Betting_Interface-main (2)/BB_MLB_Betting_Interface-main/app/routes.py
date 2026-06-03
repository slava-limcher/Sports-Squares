"""
REST API routes.

These supplement the WebSocket — used for:
  - Room creation / lookup (the QR code landing page hits these)
  - Game search (find today's games to set up a room)
  - Admin: rate limit monitoring, room status
  - Player search (for the "pick your favorite player" flow)
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from app.seeding import fetch_initial_room_state, fetch_final_state
from app.state import GameState


from app.bdl_client import bdl_client
from app.room import room_manager

logger = logging.getLogger("barboards.api")

router = APIRouter(prefix="/api", tags=["api"])


# ── Games ───────────────────────────────────────

@router.get("/games/today")
async def get_todays_games() -> Dict[str, Any]:
    """
    Fetch today's MLB games from balldontlie.
    Used by the venue operator to pick which game a room tracks.
    """
    today = date.today().isoformat()
    games = await bdl_client.get_games(dates=[today])
    return {"games": games, "date": today}


@router.get("/games/{game_id}")
async def get_game(game_id: int) -> Dict[str, Any]:
    game = await bdl_client.get_game(game_id)
    return {"game": game}


# ── Players ─────────────────────────────────────
@router.get("/players/team/{team_id}")
async def get_team_players(team_id: int) -> Dict[str, Any]:
    players = await bdl_client.get_players(team_ids=[team_id])
    return {"players": players}

@router.get("/players/search")
async def search_players(
    q: str = Query(..., min_length=2, description="Search query"),
    team_id: Optional[int] = Query(None, description="Filter by team ID"),
) -> Dict[str, Any]:
    """
    Search for players by name, optionally filtered by team.
    Used in the mobile app's "pick your favorite player" screen.
    """
    team_ids = [team_id] if team_id else None
    players = await bdl_client.get_players(search=q, team_ids=team_ids)
    return {"players": players}


# ── Rooms ───────────────────────────────────────

@router.post("/rooms")
async def create_room(room_code: str, game_id: int) -> Dict[str, Any]:
    existing = room_manager.get_room(room_code)
    if existing and existing.game_id == game_id:
        return {
            "room_code": room_code,
            "game_id": existing.game_id,
            "client_count": existing.client_count,
            "created": False,
            "message": "Room already exists",
        }
    # Either no existing room, or game_id changed — create fresh
    if existing:
        room_manager.remove_room(room_code)
    room = room_manager.create_room(room_code, game_id)

    # NEW: seed the room from BDL
    ctx, game_data, odds, props = await fetch_initial_room_state(game_id)
    if ctx is None:
        room_manager.remove_room(room_code)
        raise HTTPException(status_code=502, detail="Failed to seed room from BDL")

    room.game = game_data or {}
    
    final = await fetch_final_state(game_id)
    if final:
        room.final_box_score = final
        # game_state stays None → frontend renders FINAL view
    else:
        room.game_state = GameState()
        room.lineup_context = ctx

    if odds is not None:
        room.odds = odds
    if props is not None:
        room.props = props
    return {
        "room_code": room_code,
        "game_id": game_id,
        "client_count": 0,
        "created": True,
    }


@router.get("/rooms/{room_code}")
async def get_room(room_code: str) -> Dict[str, Any]:
    room = room_manager.get_room(room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return {
        "room_code": room.room_code,
        "game_id": room.game_id,
        "client_count": room.client_count,
        "total_bets": len(room.bets),
        "users": len(room.users),
    }


@router.get("/rooms/{room_code}/leaderboard")
async def get_leaderboard(room_code: str) -> Dict[str, Any]:
    room = room_manager.get_room(room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    board = room.get_leaderboard()
    return {"leaderboard": [e.model_dump() for e in board]}


# ── Admin / monitoring ──────────────────────────

@router.get("/admin/status")
async def admin_status() -> Dict[str, Any]:
    """
    Monitor rate limit headroom and active rooms.
    Hit this to make sure you're not burning through API calls.
    """
    return {
        "bdl_requests_last_minute": bdl_client.requests_last_minute,
        "bdl_headroom": bdl_client.headroom,
        "active_rooms": len(room_manager.active_rooms),
        "active_game_ids": list(room_manager.all_active_game_ids()),
        "rooms": {
            code: {
                "game_id": room.game_id,
                "clients": room.client_count,
                "bets": len(room.bets),
            }
            for code, room in room_manager.active_rooms.items()
        },
    }
