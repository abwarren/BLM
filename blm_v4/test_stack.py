"""THE 🔴 TEST STACK — pre-runtime auto-bet validation environment
(directive Phase 3: "Your 🔴 TEST ENVIRONMENT becomes the integration
environment").

Isolation guarantees (all enforced HERE, in one reviewable place):

  * DATABASES   separate TEST SQLite files (analytics + betting), never
                the production paths, whatever the ambient environment
                says — the paths are OVERRIDDEN, not inherited.
  * COLLECTOR   OFF — no Playwright, no scraping, no source connection.
                The collector must NOT be required to test auto-bet.
  * PROVIDER    DryRunProvider (config dry_run FORCED true).  There is
                no code path in this module that can produce a real
                submission; the real provider remains a fail-safe stub.
  * AUTO-BET    OFF initially (fresh TEST betting store, switch default
                OFF); armed only through the TEST API by an operator.
  * NETWORK     binds 127.0.0.1 by default — the TEST stack is not
                reachable from outside the machine.
  * UI          every HTML response carries the 🔴 TEST ENVIRONMENT
                banner, injected at response time WITHOUT modifying the
                shared static assets (production serves the same files).

The deterministic alert fixture (`FixtureAlertFeed`) feeds the
BettingWorker DIRECTLY — no collector, no analytics pipeline — so a
failure is diagnosable in one step (directive: "Feed a deterministic
alert fixture directly into the betting worker").

This module must never be imported by the production server.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

TEST_ENV_NAME = "TEST"
TEST_PORT_DEFAULT = 2263

#: the injected banner (id used by GATE-1 checks and tests)
BANNER_MARK = "🔴 TEST ENVIRONMENT"
_BANNER_HTML = (
    '<div id="testEnvBanner" '
    'style="position:fixed;top:0;left:0;right:0;z-index:99999;'
    'background:#7f1d1d;color:#fff;font:700 12px/1 monospace;'
    'padding:6px 12px;text-align:center;letter-spacing:2px;">'
    f'{BANNER_MARK} — PORT @TEST_PORT@ · DRY-RUN ONLY</div>'
    '<style>body{padding-top:26px !important;}</style>'
)


def _banner(port: int) -> str:
    # str.format is unsafe here — the banner embeds CSS braces — so a
    # plain token replacement is used.
    return _BANNER_HTML.replace("@TEST_PORT@", str(port))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FixtureAlertFeed:
    """The deterministic alert fixture — the EXACT /api/v4/live game
    shape the BettingWorker consumes, with a scenario switch:

      qualifying   an ACTIVE alert on a live, eligible, fresh game
      inactive     the same game with under_alert.active = false
      stale        the same game with an aged observation (alert_stale)

    The fixture's captured_at is REGENERATED on every read so the game
    stays inside the freshness bound unless the scenario is ``stale``.
    """

    SCENARIOS = ("qualifying", "inactive", "stale")

    def __init__(self, game_id: str = "TEST-GAME-9001"):
        self.game_id = game_id
        self.enabled = True
        self.scenario = "qualifying"

    def game(self) -> dict:
        now = datetime.now(timezone.utc)
        fresh_s = 1.0 if self.scenario != "stale" else 600.0
        g = {
            "game_id": self.game_id,
            "live": True,
            "live_reason": None,
            "status": "live",
            "competition_slug": "betual-nba",
            "classification": "BETUAL_NBA",
            "under_alert": {"active": self.scenario == "qualifying",
                            "checkpoint": 75, "trigger_line": 180.5},
            "under_alert_eligibility": {"eligible": True,
                                        "reason": "market_live"},
            "market": {"total_line": 180.5, "market_status": "LIVE"},
            "projector": {"required_pts_per_min": 5.0,
                          "progress_pct": 80.0,
                          "actual_pts_per_min": 4.0,
                          "captured_at": _iso(
                              now - timedelta(seconds=fresh_s)),
                          "market_status": "LIVE"},
        }
        return g

    def games(self) -> list:
        """The live_payload_fn the worker + betting API consume."""
        if not self.enabled:
            return []
        return [self.game()]

    def set_scenario(self, scenario: str, enabled: bool = True) -> None:
        if scenario not in self.SCENARIOS:
            raise ValueError(f"scenario must be one of {self.SCENARIOS}")
        self.scenario = scenario
        self.enabled = bool(enabled)


def make_test_paths(root: Path) -> dict:
    """The TEST database paths — under <root>/test_env/, never the
    production locations."""
    test_dir = root / "test_env"
    test_dir.mkdir(exist_ok=True)
    return {
        "analytics_db": str(test_dir / "blm_pokerbet_test.db"),
        "betting_db": str(test_dir / "blm_betting_test.db"),
        "auth_db": str(test_dir / "blm_auth_test.db"),
    }


def apply_test_environment(root: Path) -> dict:
    """FORCE the TEST environment variables (overriding anything
    inherited — a TEST process must not inherit production targets).
    Returns the effective targets for the GATE-1 record."""
    paths = make_test_paths(root)
    port = int(os.environ.get("TEST_PORT", TEST_PORT_DEFAULT))
    overrides = {
        "BLM_ENV": TEST_ENV_NAME,
        "BLM_POKERBET_DB": paths["analytics_db"],
        "BETTING_DB_PATH": paths["betting_db"],
        # AUTH: the TEST stack gets a SEPARATE auth database and auth stays
        # ON, so the guard is exercised in staging exactly as in production.
        # Cookies are not Secure here because the TEST stack is served over
        # plain http on loopback.
        "BLM_AUTH_DB": paths["auth_db"],
        "BLM_AUTH_ENABLED": "1",
        "BLM_AUTH_COOKIE_SECURE": "0",
        "BLM_AUTH_TRUST_PROXY": "0",
        "BLM_LOGIN_BANNER": (f"{BANNER_MARK} — PORT {port} · "
                             "DRY-RUN ONLY · AUTH ON"),
        # DRY_RUN is FORCED true in the TEST stack — no configuration
        # may flip this process into live submission.
        "BETTING_DRY_RUN": "true",
        "BLM_PACE_REF_WORKER": "0",   # no background scan in TEST
        # TEST risk limits — SIMULATED exposure only (dry-run forced):
        # without them the executor's fail-closed gate refuses every
        # candidate (limits_not_configured) and the stack can't exercise
        # the full workflow.  They are deliberately small and local to
        # TEST; production limits are configured in production's env.
        "BETTING_MAX_STAKE_PER_BET": "10.0",
        "BETTING_MAX_BETS_PER_DAY": "50",
        "BETTING_MAX_DAILY_EXPOSURE": "100.0",
        "BETTING_STAKE_UNITS": "1.0",
    }
    for k, v in overrides.items():
        os.environ[k] = v
    return {"environment": TEST_ENV_NAME, **paths,
            "dry_run": True, "port": port}


def build_test_app(root: Path, *, with_worker: bool = True) -> tuple:
    """Compose the TEST FastAPI app.  Returns ``(app, feed, store, cfg,
    worker|None, targets)``.  The worker is created stopped — the caller
    starts it when running the real server (tests drive poll_once).

    ``root`` locates ONLY the TEST databases (``<root>/test_env/``);
    the dashboard static assets always come from this package, so the
    composition works from any temporary root (tests included)."""
    targets = apply_test_environment(root)

    # fresh TEST analytics DB with the pipeline schema (empty is fine —
    # the fixture feed drives betting; the v4 live endpoint serves [])
    from blm_v4.storage import PokerBetStore
    PokerBetStore(Path(targets["analytics_db"]))

    from blm_v4.betting.api import configure_betting, router as betting_router
    from blm_v4.betting.config import BettingConfig
    from blm_v4.betting.store import BettingStore
    from blm_v4.betting.worker import BettingWorker

    cfg = BettingConfig.from_env(root)
    assert cfg.dry_run is True, "TEST stack must run DRY_RUN"
    store = BettingStore(targets["betting_db"])

    feed = FixtureAlertFeed()
    configure_betting(store, cfg, live_payload_fn=feed.games)

    worker: Optional[BettingWorker] = None
    if with_worker:
        worker = BettingWorker(cfg, store, feed.games, poll_interval_s=5.0)

    app = FastAPI(title="BLM 🔴 TEST", version="test")

    # ── the production-shape routers, pointed at TEST stores ─────────
    from blm_v4.api import router as v4_router
    app.include_router(v4_router)
    app.include_router(betting_router)

    # ── AUTH: the SAME guard as production, on a SEPARATE test database ──
    # The staging environment exercises the real login flow rather than a
    # bypass, so a staging pass means something.  Test accounts are seeded
    # only from the environment (never a literal in source); if no seed
    # password is supplied the stack has zero users and every protected
    # route simply redirects/401s — which is the correct fail-closed state.
    from blm_v4.auth import install as install_auth
    import io as _io
    from blm_v4.auth.config import AuthConfig as _AuthConfig
    from blm_v4.auth.seed import seed as seed_auth
    from blm_v4.auth.store import AuthStore as _AuthStore

    _acfg = _AuthConfig.from_env(root)
    _accounts = []
    if os.environ.get("BLM_AUTH_SEED_ADMIN_PASSWORD"):
        _accounts.append({
            "username": os.environ.get("BLM_AUTH_SEED_ADMIN_USERNAME",
                                       "t-admin"),
            "password": os.environ["BLM_AUTH_SEED_ADMIN_PASSWORD"],
            "role": "admin", "email": None, "label": "admin"})
    if os.environ.get("BLM_AUTH_SEED_USER_PASSWORD"):
        _accounts.append({
            "username": os.environ.get("BLM_AUTH_SEED_USER_USERNAME",
                                       "t-user"),
            "password": os.environ["BLM_AUTH_SEED_USER_PASSWORD"],
            "role": "user", "email": None, "label": "user"})
    if _accounts:
        # seeded BEFORE install so the stack never logs a false
        # "no users" warning
        seed_auth(_AuthStore(_acfg.db_path), rounds=_acfg.bcrypt_rounds,
                  accounts=_accounts, out=_io.StringIO())
    auth_state = install_auth(app, root, trust_proxy=False)

    # ── TEST-only control surface ────────────────────────────────────
    test = APIRouter(prefix="/api/v4/test", tags=["blm-test-only"])

    @test.get("/env")
    def test_env() -> dict:
        """The GATE-1 record: what this process is wired to."""
        return {**targets,
                "collector": "OFF",
                "auto_betting_enabled": store.is_enabled(),
                "provider": "DryRunProvider",
                "banner": BANNER_MARK}

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        """The same public liveness probe production exposes."""
        return {"status": "ok", "service": "blm-test",
                "environment": TEST_ENV_NAME}

    @test.get("/fixtures/alert")
    def get_fixture() -> dict:
        return {"enabled": feed.enabled, "scenario": feed.scenario,
                "game": feed.game()}

    @test.post("/fixtures/alert")
    def set_fixture(payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        scenario = payload.get("scenario", feed.scenario)
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400,
                                detail="enabled must be boolean")
        try:
            feed.set_scenario(scenario, enabled)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"enabled": feed.enabled, "scenario": feed.scenario,
                "game": feed.game()}

    app.include_router(test)

    # ── the operator dashboard, WITH the 🔴 TEST banner ──────────────
    static_dir = Path(__file__).resolve().parent / "dashboard" / "static"

    @app.get("/", include_in_schema=False)
    async def operator_dashboard():
        html = (static_dir / "index.html").read_text()
        banner = _banner(targets["port"])
        if "<body>" in html:
            html = html.replace("<body>", "<body>" + banner, 1)
        else:
            html = banner + html
        return Response(content=html, media_type="text/html")

    app.mount("/static", __import__("fastapi.staticfiles", fromlist=[
        "StaticFiles"]).StaticFiles(directory=str(static_dir)),
        name="blm_test_static")

    @app.on_event("startup")
    async def _start() -> None:
        if worker is not None:
            worker.start()

    @app.on_event("shutdown")
    async def _stop() -> None:
        if worker is not None:
            worker.stop()

    return app, feed, store, cfg, worker, targets
