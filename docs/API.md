# BLM API Reference

## V1 API (Flask — Legacy)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/live` | GET | Current live game state + latest snapshot |
| `/api/history?game_id=X&limit=N` | GET | Historical snapshots for a game |
| `/api/games?limit=N` | GET | Recent games with snapshot counts |
| `/` | GET | Research console (HTML) |

### Responses

All V1 responses are JSON. Example `/api/live`:
```json
{
  "game_id": "CyberDogs-vs-RoboHawks-2026-07-20",
  "home_team": "CyberDogs",
  "away_team": "RoboHawks",
  "home_score": 48,
  "away_score": 42,
  "quarter": 2,
  "clock": "05:30",
  "total_line": 187.5,
  "spread": -5.5,
  "snapshot_count": 142,
  "timestamp": "2026-07-20T14:30:00.123Z"
}
```

## V2 API (FastAPI — Platform)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v2/health` | GET | Health check |
| `/api/v2/live` | GET | Current live game with full BLM enrichment |
| `/api/v2/game/{game_id}` | GET | Single game details |
| `/api/v2/history/{game_id}` | GET | Historical snapshots (query: from, to, limit, offset) |
| `/api/v2/replay/{game_id}` | GET | All snapshots for replay |
| `/api/v2/chart/{game_id}` | GET | Chart-optimized data |
| `/api/v2/events/{game_id}` | GET | Events for a game |
| `/api/v2/alerts` | GET | Active alerts (query: game_id) |
| `/api/v2/traps/{game_id}` | GET | Trap detection data |
| `/api/v2/model` | GET | BLM model state and config |
| `/api/v2/games` | GET | List all games |
| `/ws` | WS | WebSocket for live push |

### V2 Response Schema

Full snapshot payload:
```json
{
  "metadata": {
    "game_id": "game-001",
    "league": "Cyber 2K26",
    "season": "2026",
    "quarter": 2,
    "clock": "05:30",
    "timestamp": "2026-07-20T14:30:00.123Z"
  },
  "game_state": {
    "home_team": "CyberDogs",
    "away_team": "RoboHawks",
    "possession": "home",
    "home_score": 48,
    "away_score": 42,
    "margin": 6,
    "total": 90
  },
  "blm": {
    "expected_winner": "CyberDogs",
    "win_probability": 0.68,
    "confidence": 0.72,
    "expected_margin": 8.5,
    "expected_total": 186.5
  },
  "pace": {
    "real_pace": 112.5,
    "expected_pace": 108.0,
    "possessions": 72,
    "remaining_possessions": 48
  },
  "betting_market": {
    "spread": -5.5,
    "live_spread": -6.0,
    "total": 187.5,
    "live_total": 186.5,
    "moneyline": null,
    "steam_movement": 0.3,
    "reverse_line_movement": -0.1
  },
  "trap_detection": {
    "trap_meter": 35,
    "bull_trap": false,
    "bear_trap": false,
    "reverse_bull_trap": false,
    "dead_market": false,
    "false_momentum": false,
    "late_trap": false,
    "sharp_trap": false
  },
  "momentum": {
    "score": 55,
    "direction": "up",
    "velocity": 2.5,
    "acceleration": 0.3,
    "strength": "moderate"
  },
  "team_totals": {
    "home_projection": 95.5,
    "away_projection": 91.0,
    "expected_team_totals": {
      "home": 94.0,
      "away": 92.5
    }
  },
  "confidence_inputs": {
    "PACE": 0.85,
    "LINE": 0.70,
    "INJURY": 1.0,
    "BLOWOUT": 0.90,
    "TEAM_TOTAL": 0.75,
    "composite_confidence": 0.72
  },
  "player_state": {
    "lineups": [],
    "injuries": [],
    "fouls": {},
    "fatigue": {},
    "rotation_changes": []
  }
}
```

### WebSocket Protocol

**Connect**: `ws://localhost:8000/ws`

**Client → Server**:
```json
{"subscribe": "game_id_here"}
{"unsubscribe": "game_id_here"}
{"ping": true}
```

**Server → Client**:
```json
{"type": "snapshot", "data": { ... full snapshot ... }}
{"type": "alert", "data": { ... alert object ... }}
{"type": "event", "data": { ... event object ... }}
{"type": "pong", "data": {}}
{"type": "subscribed", "data": {"game_id": "..."}}
{"type": "error", "data": {"message": "..."}}
```
