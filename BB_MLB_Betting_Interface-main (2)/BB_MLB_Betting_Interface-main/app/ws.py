"""
WebSocket endpoint for real-time client communication.

Two types of clients connect:
  1. TV displays:  join with alias "TV-{room_code}" — receive-only
  2. Mobile users:  join with a chosen alias — send bets, receive updates

Message protocol (client → server):
  { "type": "join_room",            "data": { "room_code": "...", "game_id": 12345, "alias": "..." } }
  { "type": "set_team",             "data": { "team": "home" | "away" } }
  { "type": "set_favorite_player",  "data": { "player_id": 208, "player_name": "Ohtani" } }
  { "type": "place_bet",            "data": { "market": "moneyline", "side": "away", "amount": 10, "odds": -210, "description": "LAD ML" } }
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.models import (
    BetMarket,
    BetSide,
    TeamAffiliation,
    WSMessage,
    WSMessageType,
)
from app.room import room_manager

logger = logging.getLogger("barboards.ws")

router = APIRouter()


@router.websocket("/ws/{room_code}")
async def websocket_endpoint(ws: WebSocket, room_code: str) -> None:
    """
    Main WebSocket handler. Clients connect to /ws/{room_code}.

    The first message MUST be a join_room with alias and game_id.
    After that, the client can send bets, team picks, etc.
    """
    alias: str | None = None
    room = None

    try:
        # Wait for the join message before accepting fully
        # (accept happens inside room.connect)
        # We need to accept first to receive the join message
        await ws.accept()
        raw = await ws.receive_json()

        msg_type = raw.get("type")
        data = raw.get("data", {})

        if msg_type != "join_room":
            await ws.send_json({
                "type": "error",
                "data": {"message": "First message must be join_room"},
            })
            await ws.close()
            return

        alias = data.get("alias", f"anon-{id(ws) % 10000:04d}")
        game_id = data.get("game_id")

        if not game_id:
            await ws.send_json({
                "type": "error",
                "data": {"message": "game_id is required"},
            })
            await ws.close()
            return

        # Get or create the room
        room = room_manager.get_or_create(room_code, game_id)

        # Register the connection (we already accepted above, so just store it)
        room._connections[alias] = ws
        logger.info("Room %s: %s connected (%d total)", room_code, alias, room.client_count)

        # Send full state snapshot
        state_msg = WSMessage(
            type=WSMessageType.ROOM_STATE,
            data=room._build_full_state(alias),
        )
        await ws.send_json(state_msg.model_dump(mode="json"))

        # Broadcast updated client count
        await room.broadcast(
            WSMessage(
                type=WSMessageType.ROOM_STATE,
                data={"client_count": room.client_count},
            ),
            exclude=alias,
        )

        # ── Message loop ────────────────────────
        while True:
            raw = await ws.receive_json()
            msg_type = raw.get("type")
            data = raw.get("data", {})

            if msg_type == "set_team":
                team = data.get("team")
                if team in ("home", "away"):
                    room.set_user_team(alias, TeamAffiliation(team))
                    await ws.send_json({
                        "type": "ack",
                        "data": {"action": "set_team", "team": team},
                    })

            elif msg_type == "set_favorite_player":
                player_id = data.get("player_id")
                player_name = data.get("player_name", "")
                if player_id:
                    room.set_favorite_player(alias, player_id, player_name)
                    await ws.send_json({
                        "type": "ack",
                        "data": {
                            "action": "set_favorite_player",
                            "player_id": player_id,
                            "player_name": player_name,
                        },
                    })

            elif msg_type == "place_bet":
                try:
                    market = BetMarket(data.get("market", ""))
                    side = BetSide(data.get("side", ""))
                    amount = float(data.get("amount", 0))
                    odds = int(data.get("odds", 0))
                    description = data.get("description", "")
                    player_id = data.get("player_id")

                    if amount <= 0:
                        raise ValueError("Amount must be positive")

                    bet = room.place_bet(
                        alias=alias,
                        market=market,
                        side=side,
                        amount=amount,
                        odds=odds,
                        description=description,
                        player_id=player_id,
                    )

                    # Ack to the bettor
                    await ws.send_json({
                        "type": "ack",
                        "data": {
                            "action": "place_bet",
                            "bet_id": bet.id,
                            "market": bet.market.value,
                            "side": bet.side.value,
                            "amount": bet.amount,
                        },
                    })

                    # Broadcast updated popularity to the room
                    popularity = room.get_popularity()
                    await room.broadcast(
                        WSMessage(
                            type=WSMessageType.BETS_UPDATE,
                            data={
                                "popularity": {
                                    k: {
                                        "left_pct": v.left_pct,
                                        "right_pct": v.right_pct,
                                        "left_label": v.left_label,
                                        "right_label": v.right_label,
                                        "left_count": v.left_count,
                                        "right_count": v.right_count,
                                    }
                                    for k, v in popularity.items()
                                },
                                "total_bets": len(room.bets),
                            },
                        )
                    )

                except (ValueError, KeyError) as e:
                    await ws.send_json({
                        "type": "error",
                        "data": {"message": f"Invalid bet: {e}"},
                    })

            else:
                logger.debug("Unknown message type from %s: %s", alias, msg_type)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: %s", alias)
    except Exception:
        logger.exception("WebSocket error for %s", alias)
    finally:
        if room and alias:
            await room.disconnect(alias)
            # Broadcast updated count
            if room.client_count > 0:
                await room.broadcast(
                    WSMessage(
                        type=WSMessageType.ROOM_STATE,
                        data={"client_count": room.client_count},
                    )
                )
