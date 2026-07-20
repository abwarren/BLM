"""BLM V2 — Dashboard FastAPI Sub-App

Serves the BLM V2 analytics dashboard as a self-contained FastAPI
sub-application.  Static files (HTML, JS, CSS) are served from the
``static/`` directory.  The main dashboard page is mounted at ``/``.

This sub-app is designed to be mounted on the main BLM V2 server:

    from blm_v2.dashboard.server import create_dashboard_app
    app.mount("/dashboard", create_dashboard_app())
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_HERE = Path(__file__).resolve().parent
_STATIC_DIR = _HERE / "static"


def create_dashboard_app() -> FastAPI:
    """Build and return the dashboard FastAPI sub-application.

    The dashboard is a single-page application served from the
    ``static/`` directory.  All routes serve the main ``index.html``
    for SPA-compatible navigation, and ``/static/*`` paths serve
    the static assets directly.
    """
    app = FastAPI(
        title="BLM V2 Dashboard",
        version="2.0.0",
        description="Betting Logic Model — Live Analytics Dashboard",
        docs_url=None,
        redoc_url=None,
    )

    # ── Mount static file serving ──────────────────────────────────
    static_app = StaticFiles(directory=str(_STATIC_DIR), html=False)
    app.mount("/static", static_app, name="dashboard_static")

    # Validate that the index.html exists at startup
    index_path = _STATIC_DIR / "index.html"
    if not index_path.exists():
        raise RuntimeError(
            f"Dashboard index.html not found at {index_path}. "
            "Run from the project root or ensure static/ is populated."
        )

    # ── Routes ─────────────────────────────────────────────────────
    @app.get("/")
    async def get_dashboard():
        """Serve the main dashboard SPA page."""
        return FileResponse(str(index_path))

    @app.get("/replay")
    async def get_replay():
        """Serve the replay UI page."""
        replay_path = _STATIC_DIR / "replay.html"
        if replay_path.exists():
            return FileResponse(str(replay_path))
        # Fall back to dashboard if replay.html not in static dir yet
        return FileResponse(str(index_path))

    return app
