"""
balldontlie MLB API client.

Centralised HTTP client with request counting so you can monitor
how close you are to the GOAT tier's 600 req/min ceiling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger("barboards.bdl_client")


class BDLClient:
    """Async HTTP client for the balldontlie MLB API."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        # Rolling request counter: list of timestamps
        self._request_log: List[float] = []

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.bdl_base_url,
            headers={"Authorization": settings.bdl_api_key},
            timeout=httpx.Timeout(15.0),
        )
        logger.info("BDL client started")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            logger.info("BDL client closed")

    # ── Request tracking ────────────────────────

    def _log_request(self) -> None:
        now = time.time()
        self._request_log.append(now)
        # Prune entries older than 60s
        self._request_log = [t for t in self._request_log if now - t < 60]

    @property
    def requests_last_minute(self) -> int:
        now = time.time()
        return sum(1 for t in self._request_log if now - t < 60)

    @property
    def headroom(self) -> int:
        """How many requests remain before hitting 600/min GOAT ceiling."""
        return max(0, 600 - self.requests_last_minute)

    # ── Generic GET ─────────────────────────────

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self._client:
            raise RuntimeError("BDL client not started. Call .start() first.")

        if self.headroom <= 10:
            logger.warning(
                "Approaching rate limit: %d req in last 60s. Backing off 5s.",
                self.requests_last_minute,
            )
            await asyncio.sleep(5)

        self._log_request()
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Typed endpoint methods ──────────────────

    async def get_teams(self) -> List[Dict]:
        data = await self._get("/teams")
        return data.get("data", [])

    async def get_games(
        self,
        dates: Optional[List[str]] = None,
        team_ids: Optional[List[int]] = None,
        seasons: Optional[List[int]] = None,
    ) -> List[Dict]:
        params: Dict[str, Any] = {}
        if dates:
            for i, d in enumerate(dates):
                params[f"dates[{i}]"] = d
        if team_ids:
            for i, tid in enumerate(team_ids):
                params[f"team_ids[{i}]"] = tid
        if seasons:
            for i, s in enumerate(seasons):
                params[f"seasons[{i}]"] = s
        data = await self._get("/games", params=params)
        return data.get("data", [])

    async def get_game(self, game_id: int) -> Dict:
        data = await self._get(f"/games/{game_id}")
        return data.get("data", {})

    async def get_betting_odds(self, game_id: int) -> List[Dict]:
        """Fetch betting odds for a specific game. GOAT tier required."""
        params = {f"game_ids[0]": game_id}
        data = await self._get("/odds", params=params)
        return data.get("data", [])

    async def get_player_props(self, game_id: int) -> List[Dict]:
        params = {"game_id": game_id, "per_page": 100}
        data = await self._get("/odds/player_props", params=params)
        return data.get("data", [])

    async def get_stats(self, game_id: int) -> List[Dict]:
        """Fetch per-game player stats (live box score). ALL-STAR+ tier."""
        params = {f"game_ids[0]": game_id}
        data = await self._get("/stats", params=params)
        return data.get("data", [])

    async def get_lineups(self, game_id: int) -> List[Dict]:
        """Fetch lineups for a game. GOAT tier required."""
        params = {"game_ids[]": game_id}
        data = await self._get("/lineups", params=params)
        return data.get("data", [])

    async def get_roster(self, team_id: int) -> list:
        """Roster for a team."""
        data = await self._get("/players", params={"team_ids[]": team_id, "per_page": 100})
        return data.get("data", [])

    async def get_players(
        self,
        search: Optional[str] = None,
        team_ids: Optional[List[int]] = None,
    ) -> List[Dict]:
        params: Dict[str, Any] = {"per_page": 100}
        if search:
            params["search"] = search
        if team_ids:
            params["team_ids[]"] = team_ids[0]
        data = await self._get("/players", params=params)
        return data.get("data", [])
    
    async def get_plate_appearances(
        self,
        game_id: int,
        cursor: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Fetch plate appearances for a game. Returns the full response
        (not just .data) so the caller can handle pagination via meta.next_cursor.
        """
        params: Dict[str, Any] = {"game_id": game_id}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._get("/plate_appearances", params=params)


# Module-level singleton
bdl_client = BDLClient()

