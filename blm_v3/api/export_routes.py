"""
BLM V3 — Export Routes.

CSV, JSON, and ML dataset export endpoints for the historical database.
Mounted under ``/api/v2/historical/export/``.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Optional
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from blm_v3.historical.database import HistoricalDatabase
from blm_v3.historical.config import (
    ML_DEFAULT_FEATURES,
    ML_DEFAULT_LABEL,
)


# ── Fields to exclude from CSV/JSON export ───────────────────────────

EXCLUDE_FIELDS = {"raw_json", "ingested_at", "id"}


def create_export_router(
    db: Optional[HistoricalDatabase] = None,
) -> APIRouter:
    """Create an ``APIRouter`` with historical export endpoints.

    Args:
        db: Optional ``HistoricalDatabase`` instance.

    Returns:
        Configured ``APIRouter``.
    """
    router = APIRouter()
    historical_db = db or HistoricalDatabase()

    @router.on_event("startup")
    async def init_db():
        await historical_db.init()

    # ── CSV Export ───────────────────────────────────────────────

    @router.get("/csv")
    async def export_csv(
        game_ids: str = Query(..., description="Comma-separated game IDs"),
        metrics: str = Query(
            "all",
            description="Comma-separated metric names, or 'all'",
        ),
        limit: int = Query(50000, ge=1, le=500000),
    ):
        """Export historical snapshots as CSV."""
        ids = [g.strip() for g in game_ids.split(",") if g.strip()]
        if not ids:
            raise HTTPException(400, "At least one game_id required")

        all_snapshots: list[dict[str, Any]] = []
        for gid in ids:
            snaps = await historical_db.query_snapshots(
                game_id=gid, limit=limit // max(len(ids), 1),
            )
            all_snapshots.extend(snaps)

        if not all_snapshots:
            raise HTTPException(404, "No snapshots found")

        # Determine columns
        sample = all_snapshots[0]
        if metrics == "all":
            columns = [k for k in sample.keys() if k not in EXCLUDE_FIELDS]
        else:
            wanted = [m.strip() for m in metrics.split(",") if m.strip()]
            columns = [c for c in wanted if c in sample]

        # Stream CSV
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for snap in all_snapshots:
            writer.writerow(snap)
        csv_content = output.getvalue()

        return StreamingResponse(
            iter([csv_content]),
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=blm-historical-export.csv",
                "Content-Length": str(len(csv_content.encode("utf-8"))),
            },
        )

    # ── JSON Export ──────────────────────────────────────────────

    @router.get("/json")
    async def export_json(
        game_ids: str = Query(..., description="Comma-separated game IDs"),
        limit: int = Query(50000, ge=1, le=500000),
    ):
        """Export historical snapshots as JSON."""
        ids = [g.strip() for g in game_ids.split(",") if g.strip()]
        if not ids:
            raise HTTPException(400, "At least one game_id required")

        all_snapshots: list[dict[str, Any]] = []
        for gid in ids:
            snaps = await historical_db.query_snapshots(
                game_id=gid, limit=limit // max(len(ids), 1),
            )
            all_snapshots.extend(snaps)

        if not all_snapshots:
            raise HTTPException(404, "No snapshots found")

        # Strip internal fields from each snapshot
        clean = []
        for snap in all_snapshots:
            clean.append({k: v for k, v in snap.items()
                          if k not in EXCLUDE_FIELDS})

        json_content = json.dumps(clean, indent=2, default=str)

        return StreamingResponse(
            iter([json_content]),
            media_type="application/json",
            headers={
                "Content-Disposition": "attachment; filename=blm-historical-export.json",
                "Content-Length": str(len(json_content.encode("utf-8"))),
            },
        )

    # ── ML Dataset Export ────────────────────────────────────────

    @router.get("/ml")
    async def export_ml_dataset(
        game_ids: str = Query(..., description="Comma-separated game IDs"),
        features: str = Query(
            ",".join(ML_DEFAULT_FEATURES),
            description="Comma-separated feature column names",
        ),
        label: str = Query(
            ML_DEFAULT_LABEL,
            description="Target label column: final_total, final_result, over_under, clv, or trap_success",
        ),
        limit: int = Query(100000, ge=1, le=1000000),
    ):
        """Export ML training dataset as CSV.

        Every historical snapshot becomes one training row.
        Features and label are configurable.

        Label options:
          - ``final_total`` (default): raw final total score
          - ``final_result``: 1 if over the opening line, 0 if under
          - ``over_under``: 'over' or 'under' string label
          - ``clv``: Closing Line Value (line movement from open to close)
          - ``trap_success``: 1 if trap conditions were profitable, 0 if not

        Uses the ML pipeline to compute game-level labels from snapshot data.
        """
        ids = [g.strip() for g in game_ids.split(",") if g.strip()]
        if not ids:
            raise HTTPException(400, "At least one game_id required")

        feature_list = [f.strip() for f in features.split(",") if f.strip()]
        columns = list(dict.fromkeys(feature_list + [label]))

        # Use MlPipeline for advanced labels
        from blm_v3.engine.ml_pipeline import MlPipeline
        pipeline = MlPipeline(historical_db)

        if label in ('final_result', 'over_under', 'clv', 'trap_success'):
            rows: list[dict] = []
            rows_per_game = max(1, limit // max(len(ids), 1))
            for gid in ids:
                game_labels = await pipeline.compute_labels(gid)
                snapshots = await historical_db.query_snapshots(
                    game_id=gid, limit=rows_per_game,
                )
                if not snapshots:
                    continue
                for snap in snapshots:
                    row = {f: snap.get(f) for f in feature_list if snap.get(f) is not None}
                    row[label] = game_labels.get(label)
                    row['game_id'] = gid
                    row['timestamp'] = snap.get('timestamp', '')
                    if len(row) > 2:
                        rows.append(row)
        else:
            rows = []
            for gid in ids:
                snaps = await historical_db.query_snapshots(
                    game_id=gid, limit=limit // max(len(ids), 1),
                )
                rows.extend(snaps)
            if not rows:
                raise HTTPException(404, "No snapshots found")
            rows = [
                {f: s.get(f) for f in columns if s.get(f) is not None}
                for s in rows
            ]

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        csv_content = output.getvalue()
        return StreamingResponse(
            iter([csv_content]),
            media_type="text/csv",
            headers={
                "Content-Disposition":
                    "attachment; filename=blm-ml-dataset.csv",
                "Content-Length": str(len(csv_content.encode("utf-8"))),
            },
        )

    return router
