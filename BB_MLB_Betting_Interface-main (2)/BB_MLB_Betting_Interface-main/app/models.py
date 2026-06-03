"""
Data models for the BarBoards backend.

These mirror the balldontlie API response shapes where relevant,
and define the internal structures for rooms, bets, and leaderboards.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# balldontlie API response models (subset)
# ──────────────────────────────────────────────

class BDLTeam(BaseModel):
    id: int
    slug: str
    abbreviation: str
    display_name: str
    short_display_name: str
    name: str
    location: str
    league: str
    division: str


class BDLTeamData(BaseModel):
    hits: int = 0
    runs: int = 0
    errors: int = 0
    inning_scores: List[Optional[int]] = []


class BDLScoringPlay(BaseModel):
    play: str
    inning: str          # "top" | "bottom"
    period: str          # "1st", "2nd", etc.
    away_score: int
    home_score: int


class BDLGame(BaseModel):
    id: int
    home_team: BDLTeam
    away_team: BDLTeam
    home_team_data: Optional[BDLTeamData] = None
    away_team_data: Optional[BDLTeamData] = None
    season: int
    date: str
    venue: Optional[str] = None
    status: str          # STATUS_SCHEDULED, STATUS_IN_PROGRESS, STATUS_FINAL
    period: Optional[int] = None
    display_clock: Optional[str] = None
    scoring_summary: List[BDLScoringPlay] = []

    # We'll track inning half ourselves from play-by-play / webhooks
    inning_half: Optional[str] = None  # "top" | "bottom"
    outs: Optional[int] = None


class BDLOddsBook(BaseModel):
    """A single sportsbook's odds for a game."""
    book_name: Optional[str] = None
    home_ml: Optional[int] = None
    away_ml: Optional[int] = None
    home_spread: Optional[float] = None
    away_spread: Optional[float] = None
    home_spread_odds: Optional[int] = None
    away_spread_odds: Optional[int] = None
    over_under: Optional[float] = None
    over_odds: Optional[int] = None
    under_odds: Optional[int] = None


class BDLPlayerProp(BaseModel):
    player_id: int
    player_name: str
    team_abbreviation: Optional[str] = None
    market: str          # "hits", "home_runs", "strikeouts", "rbi", etc.
    line: float
    over_odds: Optional[int] = None
    under_odds: Optional[int] = None


# ──────────────────────────────────────────────
# Internal app models
# ──────────────────────────────────────────────

class TeamAffiliation(str, Enum):
    HOME = "home"
    AWAY = "away"


class BetSide(str, Enum):
    HOME = "home"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"


class BetMarket(str, Enum):
    SPREAD = "spread"
    MONEYLINE = "moneyline"
    OVER_UNDER = "over_under"
    PLAYER_PROP = "player_prop"


class PlacedBet(BaseModel):
    id: str
    user_alias: str
    market: BetMarket
    side: BetSide
    amount: float
    odds: int
    description: str = ""         # e.g. "Ohtani O 1.5 Hits"
    player_id: Optional[int] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    settled: bool = False
    won: Optional[bool] = None


class UserSession(BaseModel):
    alias: str
    team: TeamAffiliation
    favorite_player_id: Optional[int] = None
    favorite_player_name: Optional[str] = None


class LeaderboardEntry(BaseModel):
    alias: str
    units: float = 0.0
    wins: int = 0
    losses: int = 0
    streak: int = 0
    badge: Optional[str] = None    # "fire", "crown", etc.


class PopularityMetric(BaseModel):
    """The 60/40 weighted composite for a single market."""
    market: str
    left_label: str
    right_label: str
    left_money: float = 0
    right_money: float = 0
    left_count: int = 0
    right_count: int = 0

    @property
    def left_pct(self) -> int:
        money_total = self.left_money + self.right_money
        count_total = self.left_count + self.right_count
        if money_total == 0 and count_total == 0:
            return 50
        money_pct = (self.left_money / money_total * 100) if money_total > 0 else 50
        count_pct = (self.left_count / count_total * 100) if count_total > 0 else 50
        return round(money_pct * 0.6 + count_pct * 0.4)

    @property
    def right_pct(self) -> int:
        return 100 - self.left_pct


class KalshiMarket(BaseModel):
    ticker: str
    title: str
    subtitle: str
    yes_price: float       # 0.0–1.0
    volume: float
    category: Optional[str] = None


# ──────────────────────────────────────────────
# WebSocket message envelope
# ──────────────────────────────────────────────

class WSMessageType(str, Enum):
    # Server → Client
    GAME_TICK = "game_tick"
    ROOM_STATE = "room_state"           # Full state snapshot
    GAME_UPDATE = "game_update"         # Score / inning change
    ODDS_UPDATE = "odds_update"         # New odds from poll or webhook-trigger
    PROPS_UPDATE = "props_update"       # Player props refresh
    BETS_UPDATE = "bets_update"         # Popularity metrics changed
    LEADERBOARD_UPDATE = "leaderboard_update"
    KALSHI_UPDATE = "kalshi_update"
    WEBHOOK_EVENT = "webhook_event"     # Raw play notification (HR, hit, etc.)
    PLAYER_PROP_ALERT = "player_prop_alert"  # Favorite player prop changed
    LINEUP_UPDATE = "lineup_update"
    WE_UPDATE = "we_update"

    # Client → Server
    JOIN_ROOM = "join_room"
    PLACE_BET = "place_bet"
    SET_TEAM = "set_team"
    SET_FAVORITE_PLAYER = "set_favorite_player"


class WSMessage(BaseModel):
    type: WSMessageType
    data: Dict[str, Any] = {}
    timestamp: datetime = Field(default_factory=datetime.utcnow)
