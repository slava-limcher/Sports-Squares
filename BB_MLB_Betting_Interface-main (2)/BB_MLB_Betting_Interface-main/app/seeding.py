"""
Room seeding — runs at room open and at first sync.

Phase 1 (room open): fetch_initial_room_state
  - Lineups (both teams)
  - Rosters (both teams, for pinch hitter/runner name lookup)
  - Starting odds and props
  - Returns a LineupContext + cached odds/props for the Room to hold

Phase 2 (first boundary event): anchor_score
  - Fetch current game state from BDL
  - If reported inning/half matches the boundary we just applied, trust the score
  - Otherwise leave score at 0-0; the next webhook event with score fields heals
  - Sets state.score_anchored so the rule table knows whether delta-math is safe

This module is the only place we call BDL's REST endpoints during normal
operation. Webhooks never trigger REST fetches except for odds/props
(which are not authoritative for game state, only display).
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from app.bdl_client import bdl_client
from app.state import GameState, LineupContext

logger = logging.getLogger("barboards.seeding")


async def fetch_initial_room_state(
    game_id: int,
) -> Tuple[Optional[LineupContext], Optional[dict], Optional[dict], Optional[dict]]:
    """Fetch lineups, rosters, starting odds and props for a new room.

    Returns (LineupContext, game_data, odds, props). Any may be None on fetch failure.
    Caller (routes.py) is responsible for storing these in the Room.
    """
    try:
        game_data = await bdl_client.get_game(game_id)
    except Exception:
        logger.exception("Failed to fetch game data for %d", game_id)
        return None, None, None, None

    home_team_id = (game_data.get("home_team") or {}).get("id")
    away_team_id = (game_data.get("away_team") or {}).get("id")
    if not home_team_id or not away_team_id:
        logger.warning("Game %d missing team IDs", game_id)
        return None, game_data, None, None

    # Lineups (active 9 per team)
    try:
        lineups_data = await bdl_client.get_lineups(game_id)
    except Exception:
        logger.exception("Failed to fetch lineups for %d", game_id)
        lineups_data = []

    # Rosters (full bench/bullpen — for pinch hitter/runner name lookup)
    home_roster, away_roster = {}, {}
    try:
        home_roster = await bdl_client.get_roster(home_team_id)
    except Exception:
        logger.exception("Failed to fetch home roster for team %d", home_team_id)
    try:
        away_roster = await bdl_client.get_roster(away_team_id)
    except Exception:
        logger.exception("Failed to fetch away roster for team %d", away_team_id)

    ctx = _build_lineup_context(
        home_team_id, away_team_id, lineups_data, home_roster, away_roster,
    )

    # Odds and props — non-blocking failures, room can still open
    odds, props = None, None
    try:
        odds = await bdl_client.get_betting_odds(game_id)
    except Exception:
        logger.exception("Failed to fetch starting odds for %d", game_id)
    try:
        props = await bdl_client.get_player_props(game_id)
    except Exception:
        logger.exception("Failed to fetch starting props for %d", game_id)

    return ctx, game_data, odds, props


def _build_lineup_context(
    home_team_id: int,
    away_team_id: int,
    lineups_data,
    home_roster,
    away_roster,
) -> LineupContext:
    """Construct a LineupContext from BDL fetches.

    `lineups_data` is the response from get_lineups — a list of lineup
    entries with team and batting_order fields. We split by team and
    sort by batting_order.

    `home_roster` and `away_roster` come from get_roster (responses vary by
    BDL client wrapping — adapt below if your client returns a different shape).
    """
    home_lineup = sorted(
        [
            {
                "id": (e.get("player") or {}).get("id"),
                "last_name": (e.get("player") or {}).get("last_name", ""),
                "first_name": (e.get("player") or {}).get("first_name", ""),
                "team_id": home_team_id,
                "batting_order": e.get("batting_order"),
            }
            for e in (lineups_data or [])
            if e.get("batting_order") is not None
            and (e.get("team") or {}).get("id") == home_team_id
        ],
        key=lambda x: x["batting_order"],
    )
    away_lineup = sorted(
        [
            {
                "id": (e.get("player") or {}).get("id"),
                "last_name": (e.get("player") or {}).get("last_name", ""),
                "first_name": (e.get("player") or {}).get("first_name", ""),
                "team_id": away_team_id,
                "batting_order": e.get("batting_order"),
            }
            for e in (lineups_data or [])
            if e.get("batting_order") is not None
            and (e.get("team") or {}).get("id") == away_team_id
        ],
        key=lambda x: x["batting_order"],
    )

    home_id_to_slot = {p["id"]: i for i, p in enumerate(home_lineup) if p["id"]}
    away_id_to_slot = {p["id"]: i for i, p in enumerate(away_lineup) if p["id"]}

    # Roster: union of both teams' bench/bullpen, plus the lineup as a backstop
    roster: dict = {}
    for r in (home_roster, away_roster):
        if isinstance(r, dict) and "data" in r:
            r = r.get("data") or []
        for entry in r or []:
            pid = entry.get("id")
            last_name = entry.get("last_name")
            if pid and last_name:
                roster[pid] = last_name
    for p in home_lineup + away_lineup:
        if p["id"] and p["id"] not in roster:
            roster[p["id"]] = p["last_name"]

    return LineupContext(
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        home_lineup=home_lineup,
        away_lineup=away_lineup,
        home_id_to_slot=home_id_to_slot,
        away_id_to_slot=away_id_to_slot,
        roster=roster,
    )


# ── Score anchor (Phase 2) ──────────────────────────────────────

async def anchor_score(state: GameState, game_id: int) -> bool:
    """Fetch current game state and prime score IF the reported inning/half
    matches the just-applied boundary state.

    Returns True if anchor succeeded (score primed, score_anchored=True),
    False otherwise (cached score stays 0-0; first batter event may flag COMPUTING).

    Failure mode: any exception or mismatch is treated as "behind" — fall
    back to 0-0. The next webhook event with score fields will heal naturally.
    """
    try:
        game = await bdl_client.get_game(game_id)
    except Exception:
        logger.exception("Score anchor fetch failed for game %d", game_id)
        return False

    if not game:
        return False

    # Compare reported inning/half to our state
    reported_inning = game.get("period")  # BDL's `period` is the inning
    reported_half = None
    # BDL games endpoint includes status/inning_half in some shape — adapt to your client.
    # Common fields: "inning_half", "top_of_inning" (boolean), "half".
    if "inning_half" in game:
        reported_half = game["inning_half"]
    elif "top_of_inning" in game:
        reported_half = "top" if game["top_of_inning"] else "bottom"

    if reported_inning is None or reported_half is None:
        logger.info("Score anchor: game data missing inning fields — falling back")
        return False

    if reported_inning != state.inning or reported_half != state.half:
        logger.info(
            "Score anchor: API behind (api=%s%s, state=%s%s) — falling back",
            reported_half[0].upper(), reported_inning,
            state.half[0].upper(), state.inning,
        )
        return False

    # Match — trust it
    home_data = game.get("home_team_data") or {}
    away_data = game.get("away_team_data") or {}
    home_score = home_data.get("runs", 0) or 0
    away_score = away_data.get("runs", 0) or 0

    state.home_score = home_score
    state.away_score = away_score
    state.score_anchored = True
    logger.info(
        "Score anchored from API: %s%d  away=%d home=%d",
        state.half[0].upper(), state.inning, away_score, home_score,
    )
    return True


# ── Final state (game already over at room open) ────────────────

async def fetch_final_state(game_id: int) -> Optional[dict]:
    """For rooms opened on a completed game — return final box score data."""
    try:
        game = await bdl_client.get_game(game_id)
    except Exception:
        logger.exception("Failed to fetch final state for game %d", game_id)
        return None
    if not game:
        return None
    if (game.get("status") or "").lower() not in ("final", "completed", "ended"):
        # Not actually over — caller should fall through to normal sync flow
        return None
    return game
