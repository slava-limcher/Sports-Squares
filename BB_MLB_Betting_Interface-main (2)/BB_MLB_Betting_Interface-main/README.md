# BarBoards Live Odds Backend

Real-time betting odds, social betting, and engagement platform for sports bars. Built for the BarBoards ad-break experience — displays live MLB odds on venue TVs while patrons interact from their phones.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        FastAPI Backend                          │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │  BDL Client   │  │ Kalshi Client │  │    Polling Engine     │  │
│  │  (httpx)      │  │ (httpx)       │  │                       │  │
│  │  - games      │  │ - trending    │  │  Game state:  30s     │  │
│  │  - odds       │  │   markets     │  │  Odds:        90s     │  │
│  │  - props      │  │               │  │    + webhook trigger  │  │
│  │  - stats      │  └──────────────┘  │  Props:       180s    │  │
│  │  - lineups    │                    │  Stats:        60s     │  │
│  │  - players    │                    │  Kalshi:      120s     │  │
│  └──────┬───────┘                    │  Lineups:     once     │  │
│         │ request                    └───────────┬───────────┘  │
│         │ counter                                │              │
│         │ (600/min                               │              │
│         │  ceiling)                              │              │
│  ┌──────┴───────────────────────────────────────┴────────────┐  │
│  │                      Room Manager                         │  │
│  │                                                           │  │
│  │  Room "DENVBAR01"         Room "BOULDER42"                │  │
│  │  ├─ game_id: 58590       ├─ game_id: 58591               │  │
│  │  ├─ connections: 55       ├─ connections: 23               │  │
│  │  ├─ bets: [...]           ├─ bets: [...]                   │  │
│  │  ├─ cached: game/odds/..  ├─ cached: game/odds/..         │  │
│  │  └─ popularity metrics    └─ popularity metrics            │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐  │
│  │  REST API    │  │  WebSocket   │  │  Webhook Handler       │  │
│  │  /api/*      │  │  /ws/{code}  │  │  POST /webhooks/bdl    │  │
│  │              │  │              │  │                        │  │
│  │ - today's    │  │ - join room  │  │ - verify HMAC sig      │  │
│  │   games      │  │ - place bet  │  │ - trigger odds refresh │  │
│  │ - search     │  │ - set team   │  │ - broadcast plays      │  │
│  │   players    │  │ - pick fav   │  │ - player prop alerts   │  │
│  │ - rooms      │  │   player     │  │                        │  │
│  │ - admin      │  │              │  │ Events:                │  │
│  │   status     │  │ Broadcasts:  │  │ - mlb.team.scored      │  │
│  │              │  │ - game state │  │ - mlb.batter.home_run  │  │
│  │              │  │ - odds       │  │ - mlb.batter.hit       │  │
│  │              │  │ - props      │  │ - mlb.game.inning_*    │  │
│  │              │  │ - popularity │  │                        │  │
│  │              │  │ - leaderboard│  │                        │  │
│  │              │  │ - kalshi     │  │                        │  │
│  └─────────────┘  └──────────────┘  └────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
         │                    │                     ▲
         │ HTTP               │ WebSocket           │ HTTP POST
         ▼                    ▼                     │
  ┌─────────────┐    ┌──────────────┐     ┌────────────────┐
  │ Mobile App  │    │  TV Display  │     │  balldontlie   │
  │ (React)     │    │  (React)     │     │  webhook       │
  │             │    │              │     │  delivery      │
  │ - QR scan   │    │ - scoreboard │     └────────────────┘
  │ - pick team │    │ - live odds  │
  │ - place bet │    │ - momentum   │
  │ - prop      │    │ - props      │
  │   alerts    │    │ - popularity │
  │             │    │ - leaderboard│
  │             │    │ - kalshi     │
  │             │    │ - QR code    │
  └─────────────┘    └──────────────┘
```

## Popularity Metric

The "room consensus" for each market uses a **60/40 weighted composite**:

```
popularity = (money_pct × 0.6) + (count_pct × 0.4)
```

- `money_pct`: % of total dollars wagered on one side
- `count_pct`: % of total bets placed on one side

This weights conviction (money) higher than headcount while preventing one whale from dominating the display. Both raw signals (money split and bet count split) are also available for the UI to show explicitly.

## Rate Limit Budget

GOAT tier: **600 requests/minute**

Per active game at the configured polling intervals:

| Endpoint     | Interval | Req/min | Notes                         |
|-------------|----------|---------|-------------------------------|
| Game state   | 30s      | 2       | Score, inning, outs           |
| Betting odds | 90s      | ~0.7    | + extra on webhook triggers   |
| Player props | 180s     | ~0.3    |                               |
| Box score    | 60s      | 1       |                               |
| Lineups      | once     | 0       | Fetched once at game start    |

**~4 req/min per game** → comfortably supports **~100+ simultaneous games** within the ceiling. In practice, webhook-triggered odds refreshes add maybe 5-10 extra per game per hour.

The `GET /api/admin/status` endpoint shows real-time request counts and headroom.

## Setup

```bash
# 1. Clone and install
cd barboards-backend
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your balldontlie API key and webhook secret

# 3. Run
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 4. Set up the balldontlie webhook
# In your balldontlie dashboard, create an endpoint:
#   URL: https://your-server.com/webhooks/bdl
#   Events: mlb.team.scored, mlb.batter.home_run, mlb.batter.hit,
#           mlb.batter.strikeout, mlb.game.inning_half_ended,
#           mlb.game.inning_ended, mlb.game.started, mlb.game.ended
```

## API Endpoints

### REST

| Method | Path                          | Description                          |
|--------|-------------------------------|--------------------------------------|
| GET    | `/health`                     | Health check + BDL headroom          |
| GET    | `/api/games/today`            | Today's MLB games                    |
| GET    | `/api/games/{id}`             | Single game details                  |
| GET    | `/api/players/search?q=...`   | Player search (for fav player flow)  |
| POST   | `/api/rooms?room_code=...&game_id=...` | Create a room           |
| GET    | `/api/rooms/{code}`           | Room status                          |
| GET    | `/api/rooms/{code}/leaderboard` | Room leaderboard                   |
| GET    | `/api/admin/status`           | Rate limits + all rooms overview     |

### WebSocket

Connect to `ws://host:8000/ws/{room_code}`

**Client → Server messages:**

```json
{ "type": "join_room", "data": { "alias": "SharpShooter", "game_id": 58590 } }
{ "type": "set_team", "data": { "team": "away" } }
{ "type": "set_favorite_player", "data": { "player_id": 208, "player_name": "Ohtani" } }
{ "type": "place_bet", "data": { "market": "moneyline", "side": "away", "amount": 10, "odds": -210, "description": "LAD ML" } }
```

**Server → Client message types:**

- `room_state` — Full snapshot (on connect + count changes)
- `game_update` — Score/inning changes (every 30s)
- `odds_update` — New odds (every 90s + on scoring events)
- `props_update` — Player props refresh (every 180s)
- `bets_update` — Popularity metrics changed (on every new bet)
- `leaderboard_update` — Leaderboard changed
- `kalshi_update` — Kalshi sidebar markets (every 120s)
- `webhook_event` — Raw play event (HR, hit, scoring play)
- `player_prop_alert` — Targeted alert for tracked player

## File Structure

```
app/
├── main.py          # FastAPI app + lifecycle
├── config.py        # pydantic-settings config
├── models.py        # All data models
├── bdl_client.py    # balldontlie API client + request tracking
├── kalshi_client.py # Kalshi public market data client
├── room.py          # Room manager + betting + leaderboard + popularity
├── poller.py        # Adaptive polling engine (background tasks)
├── webhooks.py      # balldontlie webhook handler
├── ws.py            # WebSocket endpoint
└── routes.py        # REST API routes
```

## Next Steps

- [ ] React TV display — swap mock data for WebSocket messages
- [ ] React mobile app — QR landing → team picker → player picker → bet screen
- [ ] Momentum meter algorithm — compute from play-by-play data
- [ ] Bet settlement — auto-settle bets on game end using final stats
- [ ] Persist rooms/bets to a database (SQLite for demo, Postgres for prod)
- [ ] ngrok or similar for webhook testing with balldontlie
