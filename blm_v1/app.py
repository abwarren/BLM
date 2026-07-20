"""
BLM V1 — Flask API Server

Serves the research console and provides REST endpoints for live data.
Launches the Playwright collector in a background thread.
"""

import json
import logging
import threading
import os
import sys

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blm_v1.database import get_live_game, get_snapshots_chrono, get_recent_games
from blm_v1.collector import SnapshotCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder=None)
CORS(app)

collector: SnapshotCollector = None
collector_thread: threading.Thread = None


# ── V1 API Endpoints ────────────────────────────────────────────

@app.route("/api/live")
def api_live():
    """Return the current live game state."""
    if collector and collector.latest_state:
        state = dict(collector.latest_state)
        state["snapshot_count"] = collector.snapshot_count
        return jsonify(state)

    game = get_live_game()
    if game:
        snapshots = get_snapshots_chrono(game["game_id"], limit=1)
        return jsonify({
            "game": game,
            "latest_snapshot": snapshots[-1] if snapshots else None,
            "snapshot_count": len(snapshots),
        })

    return jsonify({"status": "no_game", "message": "No live game detected"})


@app.route("/api/history")
def api_history():
    """Return historical snapshots for a game."""
    game_id = request.args.get("game_id", "")
    limit = request.args.get("limit", 500, type=int)
    if not game_id:
        game = get_live_game()
        if game:
            game_id = game["game_id"]
    if game_id:
        snapshots = get_snapshots_chrono(game_id, limit=limit)
        return jsonify({"game_id": game_id, "snapshots": snapshots, "count": len(snapshots)})
    return jsonify({"status": "no_game", "message": "No game_id provided and no live game"})


@app.route("/api/games")
def api_games():
    """Return recent games."""
    limit = request.args.get("limit", 20, type=int)
    games = get_recent_games(limit=limit)
    return jsonify({"games": games, "count": len(games)})


# ── Static Files (Research Console) ─────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(STATIC_DIR, path)


# ── Server Start ────────────────────────────────────────────────

def start_collector():
    global collector
    collector = SnapshotCollector(headless=True)
    collector.start()


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def main():
    global collector_thread

    logger.info("Starting BLM V1 server...")

    # Start collector in background thread
    collector_thread = threading.Thread(target=start_collector, daemon=True)
    collector_thread.start()
    logger.info("Snapshot collector started in background thread")

    # Run Flask
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
