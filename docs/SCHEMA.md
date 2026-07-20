# BLM Schema Reference

## V1 Schema (SQLite — `blm.db`)

### `games` Table

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PK AUTOINCREMENT | Internal ID |
| game_id | TEXT | UNIQUE NOT NULL | External game identifier |
| league | TEXT | DEFAULT 'Cyber 2K26' | League name |
| season | TEXT | | Season identifier |
| home_team | TEXT | NOT NULL | Home team name |
| away_team | TEXT | NOT NULL | Away team name |
| status | TEXT | DEFAULT 'live' CHECK(...) | pre/live/halftime/ended |
| created_at | TEXT | DEFAULT now() | ISO 8601 timestamp |
| updated_at | TEXT | DEFAULT now() | Last update timestamp |

### `snapshots` Table

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PK AUTOINCREMENT | Internal ID |
| game_id | TEXT | FK → games(game_id) | Game reference |
| timestamp | TEXT | NOT NULL | ISO 8601 snapshot time |
| quarter | INTEGER | DEFAULT 1 | Current quarter |
| clock | TEXT | | Game clock (MM:SS) |
| home_score | INTEGER | DEFAULT 0 | Home team score |
| away_score | INTEGER | DEFAULT 0 | Away team score |
| total_line | REAL | | Current total line |
| spread | REAL | | Current spread |
| total_odds | TEXT | | Total market odds |
| spread_odds | TEXT | | Spread market odds |
| moneyline_home | TEXT | | Home moneyline odds |
| moneyline_away | TEXT | | Away moneyline odds |
| home_projection | REAL | | Projected home total |
| away_projection | REAL | | Projected away total |
| pace | REAL | | Current game pace |
| possessions | INTEGER | | Current possessions count |
| created_at | TEXT | DEFAULT now() | Insert timestamp |

### Indexes
- `idx_snapshots_game_ts` ON snapshots(game_id, timestamp DESC)
- `idx_snapshots_game_id` ON snapshots(game_id)

## V2 Schema (Time Series — InfluxDB / SQLite fallback)

### Full Snapshot (V2)

The V2 snapshot is stored as a complete document/row containing ALL fields below. Stored in InfluxDB as a measurement called `blm_snapshots` with tags and fields, or in SQLite as a JSON column.

### Tags (InfluxDB)
- `game_id` — Game identifier
- `league` — League name
- `quarter` — Quarter number (as string)

### Fields (InfluxDB)
All numeric fields from the full snapshot schema.

### SQLite Fallback (`blm_ts.db`)

```sql
CREATE TABLE snapshots_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    data TEXT NOT NULL,  -- Full snapshot as JSON
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX idx_v2_game_ts ON snapshots_v2(game_id, timestamp);
```

## Event Schema

Events are immutable records stored alongside snapshots:

```json
{
  "id": "evt_001",
  "type": "ThreePointerMade",
  "game_id": "game-001",
  "timestamp": "2026-07-20T14:30:00.123Z",
  "data": {
    "team": "home",
    "player": "PlayerName",
    "score_before": 45,
    "score_after": 48
  },
  "metadata": {
    "snapshot_id": "snap_042",
    "league": "Cyber 2K26",
    "quarter": 2
  }
}
```

### Event Types
- ThreePointerMade
- Timeout
- QuarterStart
- QuarterEnd
- RotationChange
- Injury
- TrapTriggered
- MomentumSwing
- SharpMoney
- MarketMove
- ConfidenceDrop
- ModelCorrection
