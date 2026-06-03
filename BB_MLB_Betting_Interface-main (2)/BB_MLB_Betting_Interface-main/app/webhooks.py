"""
Webhook handler — primary game state driver.

Per the v2 design:
  - Every payload is logged to webhook_log.jsonl before any processing
  - Two-tier filter: signature → dedup → ordering (received_at) → state.apply_event
  - apply_event returns a TransitionResult; if state changed, broadcast
  - Pinch-runner detection events logged to pinch_events.jsonl
  - Dropped (out-of-order or stale-by-inning) events logged to dropped_events.jsonl

This file is HTTP routing + side effects (logging, broadcasting, odds fetch).
All game-state logic lives in state.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request

from app.bdl_client import bdl_client
from app.config import settings
from app.models import WSMessage, WSMessageType
from app.room import room_manager
from app.seeding import anchor_score
from app.state import (
    ALL_BATTER_EVENTS,
    SYNC_EVENTS,
    apply_event,
)
from app.win_expectancy import get_we_from_game_state

logger = logging.getLogger("barboards.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

FETCH_TIMEOUT = 6


# ── File logging (audit trail for all webhook traffic) ──────────────

_WEBHOOK_LOG_PATH = Path(getattr(settings, "webhook_log_path", "webhook_log.jsonl"))
_PINCH_LOG_PATH = Path(getattr(settings, "pinch_log_path", "pinch_events.jsonl"))
_DROPPED_LOG_PATH = Path(getattr(settings, "dropped_log_path", "dropped_events.jsonl"))

_log_lock = asyncio.Lock()


def _append_jsonl_sync(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")


async def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    try:
        async with _log_lock:
            await asyncio.to_thread(_append_jsonl_sync, path, record)
    except Exception:
        logger.exception("Failed to write JSONL log to %s", path)


# ── Dedup (by webhook ID) ──────────────────────────────────────────

_DEDUP_MAX_SIZE = 2000
_seen_event_ids: "OrderedDict[str, bool]" = OrderedDict()


def _is_duplicate(event_id: str) -> bool:
    if not event_id:
        return False
    if event_id in _seen_event_ids:
        _seen_event_ids.move_to_end(event_id)
        return True
    _seen_event_ids[event_id] = True
    if len(_seen_event_ids) > _DEDUP_MAX_SIZE:
        _seen_event_ids.popitem(last=False)
    return False


# ── HMAC signature verification ─────────────────────────────────────

def verify_signature(payload: bytes, timestamp: str, signature: str, secret: str) -> bool:
    if not secret:
        return True
    try:
        message = f"{timestamp}.{payload.decode()}"
    except UnicodeDecodeError:
        return False
    expected = "v1=" + hmac.new(
        secret.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


# ── Odds/props refresh ──────────────────────────────────────────────

async def _safe_fetch(coro, label: str, game_id: int):
    try:
        return await asyncio.wait_for(coro, timeout=FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Fetch timeout: %s for game %d", label, game_id)
        return None
    except Exception:
        logger.exception("Fetch failed: %s for game %d", label, game_id)
        return None


# ── Main route ──────────────────────────────────────────────────────

@router.post("/bdl")
@router.post("/bdl/ingest")
async def handle_bdl_webhook(
    request: Request,
    x_webhook_id: str = Header(default="", alias="x-webhook-id"),
    x_webhook_timestamp: str = Header(default="", alias="x-webhook-timestamp"),
    x_webhook_signature: str = Header(default="", alias="x-webhook-signature"),
) -> Dict[str, str]:
    received_at = datetime.utcnow()
    body = await request.body()

    # 1. Signature
    secret = getattr(settings, "bdl_webhook_secret", None)
    if secret and not verify_signature(body, x_webhook_timestamp, x_webhook_signature, secret):
        logger.warning("Invalid signature for event %s", x_webhook_id)
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 2. Parse JSON
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        logger.exception("Failed to parse webhook JSON")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("event_type", "") or ""

    print(f"\n========== WEBHOOK {event_type} ==========")
    print(json.dumps(payload, indent=2))
    print("=" * 50)

    # 3. Log every payload (audit trail), even if subsequently dropped
    await _append_jsonl(_WEBHOOK_LOG_PATH, {
        "received_at": received_at.isoformat() + "Z",
        "x_webhook_id": x_webhook_id,
        "x_webhook_timestamp": x_webhook_timestamp,
        "event_type": event_type,
        "payload": payload,
    })

    # 4. Dedup
    if _is_duplicate(x_webhook_id):
        return {"status": "duplicate"}

    game_data = payload.get("game") or {}
    game_id = game_data.get("id") if isinstance(game_data, dict) else None
    if not game_id:
        return {"status": "no_game_id"}

    rooms = room_manager.get_rooms_for_game(game_id)
    if not rooms:
        return {"status": "no_rooms"}

    # All rooms for the same game share the same GameState + LineupContext
    # (one game = one source of truth). Pick the first room as canonical.
    sample_room = rooms[0]
    state = sample_room.game_state
    ctx = sample_room.lineup_context
    if state is None or ctx is None:
        logger.warning("Room for game %d not seeded yet, skipping", game_id)
        return {"status": "not_seeded"}

    # 5. Ordering rule (the key fix from v1)
    if state.last_applied_time and received_at < state.last_applied_time:
        await _append_jsonl(_DROPPED_LOG_PATH, {
            "received_at": received_at.isoformat() + "Z",
            "x_webhook_id": x_webhook_id,
            "event_type": event_type,
            "reason": "out_of_order",
            "last_applied_time": state.last_applied_time.isoformat() + "Z",
        })
        return {"status": "out_of_order"}

    # 6. Apply event to state (pure function, mutates state and ctx in place)
    pre_synced = state.synced
    result = apply_event(state, event_type, payload, ctx)

    # Always advance the watermark on accepted events (even no-ops),
    # so future events older than this one are dropped.
    state.last_applied_time = received_at

    # Compute WE on any accepted state-changing event.
    # Only meaningful post-sync — pre-sync state.inning may be wrong.
    if state.synced and result.state_changed:
        try:
            we = get_we_from_game_state(
                inning=state.inning,
                half=state.half,
                home_score=state.home_score,
                away_score=state.away_score,
                outs=state.outs,
                runner_first=state.bases.first is not None,
                runner_second=state.bases.second is not None,
                runner_third=state.bases.third is not None,
            )
            entry = {
                "timestamp": received_at.isoformat() + "Z",
                "home_we": we,
                "inning": state.inning,
                "half": state.half,
                "outs": state.outs,
                "away_score": state.away_score,
                "home_score": state.home_score,
            }
            for room in rooms:
                room.we_history.append(entry)
                # Cap memory — 500 entries is way more than any game produces
                if len(room.we_history) > 500:
                    room.we_history = room.we_history[-300:]
        except Exception:
            logger.exception("Failed to compute WE for game %d", game_id)

    # Log stale-by-inning drops
    if "stale-by-inning" in result.note:
        await _append_jsonl(_DROPPED_LOG_PATH, {
            "received_at": received_at.isoformat() + "Z",
            "x_webhook_id": x_webhook_id,
            "event_type": event_type,
            "reason": "stale_by_inning",
            "note": result.note,
        })

    # Log pinch events
    for pe in result.pinch_events:
        await _append_jsonl(_PINCH_LOG_PATH, {
            "received_at": received_at.isoformat() + "Z",
            "x_webhook_id": x_webhook_id,
            "event_type": event_type,
            "kind": pe.kind,
            "new_id": pe.new_id,
            "replaced_id": pe.replaced_id,
            "slot": pe.slot,
            "team_side": pe.team_side,
            "reason": pe.reason,
            "prev_ids": list(pe.prev_ids),
            "new_ids": list(pe.new_ids),
        })

    # 7. First-sync score anchor: just transitioned from un-synced to synced
    if not pre_synced and state.synced:
        try:
            await anchor_score(state, game_id)
        except Exception:
            logger.exception("Score anchor failed for game %d", game_id)

    # 8. Odds/props refresh on every event we accepted (skip stale-by-inning)
    odds_data, props_data = None, None
    if result.state_changed or event_type == "mlb.team.scored":
        odds_data, props_data = await asyncio.gather(
            _safe_fetch(bdl_client.get_betting_odds(game_id), "odds", game_id),
            _safe_fetch(bdl_client.get_player_props(game_id), "props", game_id),
        )
        for room in rooms:
            if odds_data is not None:
                room.odds = odds_data
            if props_data is not None:
                room.props = props_data

    # 9. Broadcast to all rooms watching this game
    if result.state_changed or score_or_pitcher_change(result):
        msg = WSMessage(
            type=WSMessageType.WEBHOOK_EVENT,
            data={
                "event_type": event_type,
                "play": payload.get("play") or {},
                "batter": payload.get("batter") or {},
                "pitcher": payload.get("pitcher") or {},
                "state": state.to_dict(),
                "lineup": {
                    "home": ctx.home_lineup,
                    "away": ctx.away_lineup,
                },
                "score_changed": result.score_changed,
                "pitcher_changed": result.pitcher_changed,
                "displayed_pitcher_id": state.displayed_pitcher_id,
                "computing": state.computing,
                "odds": odds_data,
                "props": props_data,
                "we_history": sample_room.we_history[-100:],
            },
        )
        for room in rooms:
            await room.broadcast(msg)

    return {"status": "ok"}


def score_or_pitcher_change(result) -> bool:
    return result.score_changed or result.pitcher_changed
