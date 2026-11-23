"""BLM health monitoring — probes, thresholds and evaluation.

Design rules (operator directive, 2026-11-02):

  * **Health is decided by data, not by process existence.** A collector whose
    process is alive but which has stopped ticking, or ticks without persisting,
    is not healthy — the directive is explicit about that.
  * **Read-only against production.** The hot path reads one small JSON file
    (``collector_state.json``). The database probe is a single ``select 1`` over
    a ``mode=ro`` URI with a short busy timeout. Nothing here writes to a
    production database, opens a browser, or touches collector polling.
  * **Pure evaluation.** ``evaluate()`` takes plain values and returns a plain
    dict, so every threshold can be tested without a clock, a network or a host.

Vocabulary follows the directive's table: ONLINE/OFFLINE, HEALTHY/DEGRADED/
OFFLINE, CONNECTED/DISCONNECTED, ACTIVE/STALE/STOPPED, FAILED, ERROR — with a
colour (GREEN/YELLOW/RED/GREY) for the panel.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

GREEN, YELLOW, RED, GREY = "GREEN", "YELLOW", "RED", "GREY"
SEVERITY = {GREEN: 0, YELLOW: 1, RED: 2, GREY: 3}


@dataclass
class Thresholds:
    """Every number the panel uses, in one place, so the docs cannot drift."""

    # collector tick
    tick_healthy_s: float = 120.0
    tick_degraded_s: float = 300.0
    # data collection freshness (last successful capture)
    fresh_healthy_s: float = 120.0
    fresh_stale_s: float = 600.0
    # "ticking but nothing persisted" — snapshots_total flat for this long
    flat_line_window_s: float = 180.0
    # persistence worker
    queue_degraded: int = 1000
    queue_failed: int = 5000
    persist_lag_degraded: int = 200
    # connectivity
    error_recent_s: float = 300.0
    empty_ticks_disconnected: int = 3
    browser_stale_s: float = 3600.0
    # backend / frontend http
    backend_degraded_ms: float = 2000.0
    # event-page hydration (verified event-view captures / attempts)
    hydration_healthy_pct: float = 90.0
    hydration_degraded_pct: float = 50.0
    # market observation coverage (active games with an observed market)
    market_healthy_pct: float = 90.0
    market_degraded_pct: float = 50.0
    # host
    cpu_degraded_pct: float = 90.0
    ram_degraded_pct: float = 92.0
    # Operator rule (2026-11-20): the root SSD must never exceed 80% used.
    # 80% used == 20% free -> YELLOW; 90% used == 10% free -> RED.
    disk_degraded_free_pct: float = 20.0
    disk_failed_free_pct: float = 10.0
    # alert flapping
    confirm_cycles: int = 2


# ── probes ──────────────────────────────────────────────────────────────
def read_state(path: str) -> tuple[dict | None, float | None, str | None]:
    """Read collector_state.json. Returns (state, age_seconds, error)."""
    try:
        st = os.stat(path)
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), max(0.0, time.time() - st.st_mtime), None
    except FileNotFoundError:
        return None, None, "state file missing"
    except (OSError, ValueError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def probe_unit(unit: str) -> dict:
    """systemd --user unit state. Never raises.

    Parsed as Key=Value: systemctl prints properties in its own order, so a
    positional read put the monotonic timestamp where the state belongs and
    reported a healthy collector as OFFLINE (seen live 2026-11-02).
    """
    out = {"active": False, "pid": None, "n_restarts": None, "error": None}
    try:
        raw = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "ActiveState",
             "-p", "MainPID", "-p", "NRestarts"],
            capture_output=True, text=True, timeout=5, check=False).stdout
        props = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        out["active"] = props.get("ActiveState", "").strip() == "active"
        pid = (props.get("MainPID") or "").strip()
        out["pid"] = int(pid) if pid.isdigit() else None
        nr = (props.get("NRestarts") or "").strip()
        out["n_restarts"] = int(nr) if nr.isdigit() else None
    except (OSError, subprocess.SubprocessError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def probe_pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def probe_http(url: str, timeout_s: float = 5.0, marker: str | None = None) -> dict:
    """GET a URL. Returns status/latency/marker-hit; never raises."""
    out = {"ok": False, "status": None, "latency_ms": None,
           "marker": None, "error": None}
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "blm-health/1"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
            out["ok"] = 200 <= resp.status < 400
            out["status"] = resp.status
            out["latency_ms"] = (time.monotonic() - started) * 1000.0
            if marker:
                out["marker"] = marker in body
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
        out["error"] = f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
        out["latency_ms"] = (time.monotonic() - started) * 1000.0
    return out


def probe_sqlite(path: str, timeout_s: float = 0.25) -> dict:
    """One read-only `select 1`. Deliberately tiny: the monitor must never hold
    a read transaction long enough to starve the collector's writer."""
    out = {"ok": False, "size_bytes": None, "wal": False, "error": None}
    try:
        out["size_bytes"] = os.path.getsize(path)
        out["wal"] = os.path.exists(path + "-wal")
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout_s)
        try:
            conn.execute("select 1").fetchone()
            out["ok"] = True
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def probe_host() -> dict:
    """CPU load, RAM and disk from /proc and statvfs — no third-party deps."""
    out = {"cpu_pct": None, "ram_pct": None, "disk_free_pct": None,
           "load1": None, "error": None}
    try:
        with open("/proc/loadavg") as fh:
            out["load1"] = float(fh.read().split()[0])
        n = os.cpu_count() or 1
        out["cpu_pct"] = min(100.0, 100.0 * out["load1"] / n)
        mem = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                mem[k] = float(v.strip().split()[0])
        total = mem.get("MemTotal") or 1.0
        avail = mem.get("MemAvailable", 0.0)
        out["ram_pct"] = 100.0 * (1.0 - avail / total)
        out["disk_free_pct"] = 100.0 * shutil.disk_usage("/").free / shutil.disk_usage("/").total
    except (OSError, ValueError, ZeroDivisionError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ── evaluation ──────────────────────────────────────────────────────────
@dataclass
class Inputs:
    now: float
    state: dict | None = None
    state_age_s: float | None = None
    state_error: str | None = None
    unit: dict = field(default_factory=dict)
    backend: dict = field(default_factory=dict)
    frontend: dict = field(default_factory=dict)
    db: dict = field(default_factory=dict)
    host: dict = field(default_factory=dict)
    snapshot_history: list = field(default_factory=list)   # [(ts, snapshots_total)]


def _component(key, label, state, color, detail, **metrics) -> dict:
    return {"key": key, "label": label, "state": state, "color": color,
            "detail": detail, "metrics": metrics}


def _plat(history: list, now: float, window_s: float) -> int | None:
    """How many snapshots_total samples inside `window_s` — detects a collector
    that ticks but persists nothing."""
    pts = [(t, v) for t, v in history if now - t <= window_s and v is not None]
    if len(pts) < 2:
        return None
    return int(pts[-1][1] - pts[0][1])


def _rate(history: list, now: float, window_s: float) -> tuple[float | None, float | None]:
    """Collection frequency over the available history window.

    Returns (snapshots_per_minute, window_seconds_used).  The window is the
    shorter of the requested lookback and the span actually observed, so a
    just-started monitor reports a real rate instead of pretending a full
    window has elapsed.
    """
    pts = [(t, v) for t, v in history if now - t <= window_s and v is not None]
    if len(pts) < 2:
        return None, None
    span = pts[-1][0] - pts[0][0]
    if span <= 0:
        return None, None
    delta = pts[-1][1] - pts[0][1]
    return round(max(0, delta) / span * 60.0, 2), round(span, 1)


def evaluate(inp: Inputs, th: Thresholds | None = None) -> dict:
    th = th or Thresholds()
    now, st = inp.now, inp.state or {}
    comps: list[dict] = []

    # ── backend ────────────────────────────────────────────────────────
    b = inp.backend or {}
    if b.get("ok") and (b.get("latency_ms") or 0) <= th.backend_degraded_ms:
        comps.append(_component("backend", "BLM Backend", "ONLINE", GREEN,
                                f"/healthz {b.get('status')} in {b.get('latency_ms', 0):.0f} ms",
                                latency_ms=round(b.get("latency_ms") or 0, 1)))
    elif b.get("ok"):
        comps.append(_component("backend", "BLM Backend", "ONLINE", YELLOW,
                                f"/healthz slow: {(b.get('latency_ms') or 0):.0f} ms",
                                latency_ms=round(b.get("latency_ms") or 0, 1)))
    else:
        comps.append(_component("backend", "BLM Backend", "OFFLINE", RED,
                                b.get("error") or "no response", latency_ms=b.get("latency_ms")))

    # ── api (payload validity, not just liveness) ──────────────────────
    if b.get("ok") and (b.get("marker") is not False):
        comps.append(_component("api", "API", "HEALTHY", GREEN,
                                "health payload valid"))
    elif b.get("ok"):
        comps.append(_component("api", "API", "HEALTHY", YELLOW,
                                "responds but payload did not match the expected shape"))
    else:
        comps.append(_component("api", "API", "ERROR", RED, b.get("error") or "unreachable"))

    # ── frontend ───────────────────────────────────────────────────────
    f = inp.frontend or {}
    if f.get("ok") and f.get("marker") is not False:
        comps.append(_component("frontend", "BLM Frontend", "ONLINE", GREEN,
                                f"HTTP {f.get('status')} in {f.get('latency_ms', 0):.0f} ms",
                                latency_ms=round(f.get("latency_ms") or 0, 1)))
    elif f.get("ok"):
        comps.append(_component("frontend", "BLM Frontend", "ONLINE", YELLOW,
                                "HTTP 200 but the page marker is missing"))
    else:
        comps.append(_component("frontend", "BLM Frontend", "OFFLINE", RED,
                                f.get("error") or "unreachable"))

    # ── collector process + tick ───────────────────────────────────────
    alive = bool(inp.unit.get("active")) or probe_pid_alive(inp.unit.get("pid"))
    last_tick = st.get("last_tick_at")
    tick_age = _age(now, last_tick)
    proc_bits = []
    if inp.unit.get("n_restarts") is not None:
        proc_bits.append(f"restarts={inp.unit['n_restarts']}")

    # Explicit process-status row: ONLINE only when the unit is active or the
    # PID is alive.  The tick row below keeps the health/vs-liveness split.
    comps.append(_component(
        "collector_process", "Collector Process",
        "ONLINE" if alive else "OFFLINE",
        GREEN if alive else RED,
        f"pid {inp.unit.get('pid')} alive" if alive else
            (inp.state_error or "process not running"),
        pid=inp.unit.get("pid"), n_restarts=inp.unit.get("n_restarts")))

    if alive and tick_age is not None and tick_age <= th.tick_healthy_s:
        comps.append(_component("collector", "PokerBet Collector", "HEALTHY", GREEN,
                                f"tick {tick_age:.0f}s ago", tick_age_s=round(tick_age, 1)))
    elif alive and tick_age is not None and tick_age <= th.tick_degraded_s:
        comps.append(_component("collector", "PokerBet Collector", "DEGRADED", YELLOW,
                                f"process alive but tick is {tick_age:.0f}s old"
                                + (f" ({', '.join(proc_bits)})" if proc_bits else ""),
                                tick_age_s=round(tick_age, 1)))
    elif alive:
        comps.append(_component("collector", "PokerBet Collector", "OFFLINE", RED,
                                f"process alive but no tick for {tick_age:.0f}s"
                                if tick_age is not None else
                                "process alive but the state file has no tick",
                                tick_age_s=None if tick_age is None else round(tick_age, 1)))
    else:
        comps.append(_component("collector", "PokerBet Collector", "OFFLINE", RED,
                                inp.state_error or "process not running",
                                tick_age_s=None if tick_age is None else round(tick_age, 1)))

    # ── data collection (the directive's "genuinely collecting") ───────
    fresh_age = _age(now, st.get("last_success_at") or last_tick)
    delta = _plat(inp.snapshot_history, now, th.flat_line_window_s)
    # Process running but no actual data being collected: the collector tick
    # itself is fresh, yet no snapshots are landing.  This is a distinct,
    # operator-visible state — never hidden behind the process-health row.
    running_no_data = bool(alive and (fresh_age is None or fresh_age > th.fresh_healthy_s
                                      or (delta is not None and delta == 0)))
    if fresh_age is None:
        comps.append(_component("collection", "Data Collection", "STOPPED", RED,
                                "process running but no successful collection recorded"
                                if alive else "no successful collection recorded",
                                fresh_age_s=round(fresh_age, 1), snapshots_delta=delta,
                                running_no_data=bool(alive)))
    elif fresh_age <= th.fresh_healthy_s and (delta is None or delta > 0):
        comps.append(_component("collection", "Data Collection", "ACTIVE", GREEN,
                                f"last capture {fresh_age:.0f}s ago"
                                + (f", +{delta} snapshots/{int(th.flat_line_window_s)}s" if delta else ""),
                                fresh_age_s=round(fresh_age, 1), snapshots_delta=delta,
                                running_no_data=False))
    elif fresh_age <= th.fresh_stale_s or (delta is not None and delta == 0):
        # ticking but persisting nothing is the directive's headline failure
        detail = (f"ticking but snapshots_total is flat for "
                  f"{int(th.flat_line_window_s)}s" if delta == 0 else
                  f"last capture {fresh_age:.0f}s ago")
        if alive and delta == 0:
            detail = (f"process running but no data collected — snapshots_total "
                      f"flat for {int(th.flat_line_window_s)}s")
        elif running_no_data:
            detail = f"process running but no capture for {fresh_age:.0f}s"
        comps.append(_component("collection", "Data Collection", "STALE", YELLOW, detail,
                                fresh_age_s=round(fresh_age, 1), snapshots_delta=delta,
                                running_no_data=running_no_data))
    else:
        comps.append(_component("collection", "Data Collection", "STOPPED", RED,
                                f"process running but no capture for {fresh_age:.0f}s"
                                if alive else f"no capture for {fresh_age:.0f}s",
                                fresh_age_s=round(fresh_age, 1), snapshots_delta=delta,
                                running_no_data=bool(alive)))

    # ── pokerbet connectivity ──────────────────────────────────────────
    err_age = _age(now, st.get("last_error_at"))
    empties = int(st.get("consecutive_empty_ticks") or 0)
    browser_age = st.get("browser_age_s")
    if (err_age is not None and err_age <= th.error_recent_s) or empties >= th.empty_ticks_disconnected:
        comps.append(_component("connectivity", "PokerBet Connectivity", "DISCONNECTED", RED,
                                f"last error {err_age:.0f}s ago, {empties} empty ticks"
                                if err_age is not None else f"{empties} empty ticks",
                                empty_ticks=empties))
    elif browser_age is not None and browser_age > th.browser_stale_s:
        comps.append(_component("connectivity", "PokerBet Connectivity", "CONNECTED", YELLOW,
                                f"browser session {browser_age / 60:.0f} min old",
                                browser_age_s=browser_age))
    else:
        comps.append(_component("connectivity", "PokerBet Connectivity", "CONNECTED", GREEN,
                                "no recent errors", empty_ticks=empties))

    # ── browser / playwright health ────────────────────────────────────
    slow = st.get("slow_worker") or {}
    page_ok = bool(slow.get("page_available"))
    slow_alive = bool(slow.get("alive"))
    slow_degraded = bool(slow.get("degraded"))
    slow_error = slow.get("error")
    if not slow_alive:
        comps.append(_component("browser", "Browser / Playwright", "FAILED", RED,
                                "slow worker not alive",
                                page_available=page_ok, slow_rounds=slow.get("rounds")))
    elif slow_error:
        comps.append(_component("browser", "Browser / Playwright", "DEGRADED", YELLOW,
                                f"slow worker error: {slow_error}"[:200],
                                page_available=page_ok, slow_rounds=slow.get("rounds")))
    elif not page_ok or slow_degraded:
        comps.append(_component("browser", "Browser / Playwright", "DEGRADED", YELLOW,
                                "slow page unavailable" if not page_ok else "slow worker degraded",
                                page_available=page_ok, slow_rounds=slow.get("rounds")))
    else:
        comps.append(_component("browser", "Browser / Playwright", "HEALTHY", GREEN,
                                f"slow page available, {slow.get('rounds', 0)} rounds, "
                                f"{slow.get('resolve_ok', 0)} resolved",
                                page_available=page_ok, slow_rounds=slow.get("rounds")))

    # ── event-page hydration ───────────────────────────────────────────
    # New-game identity resolution is the canonical event-page hydration
    # event: row click -> SPA navigation -> verified event-view parse.
    hyd_attempts = int(slow.get("resolve_ok") or 0) + int(slow.get("resolve_failures") or 0)
    hyd_ok = int(slow.get("resolve_ok") or 0)
    hyd_fail = int(slow.get("resolve_failures") or 0)
    hyd_rate = round(100.0 * hyd_ok / hyd_attempts, 1) if hyd_attempts else None
    if hyd_attempts == 0:
        comps.append(_component("hydration", "Event-Page Hydration", "UNKNOWN", GREY,
                                "no event-page resolutions yet",
                                attempts=hyd_attempts, success=hyd_ok, failures=hyd_fail))
    elif hyd_rate is not None and hyd_rate >= th.hydration_healthy_pct:
        comps.append(_component("hydration", "Event-Page Hydration", "HEALTHY", GREEN,
                                f"{hyd_ok}/{hyd_attempts} resolved ({hyd_rate}%)",
                                attempts=hyd_attempts, success=hyd_ok, failures=hyd_fail, rate_pct=hyd_rate))
    elif hyd_rate is not None and hyd_rate >= th.hydration_degraded_pct:
        comps.append(_component("hydration", "Event-Page Hydration", "DEGRADED", YELLOW,
                                f"{hyd_ok}/{hyd_attempts} resolved ({hyd_rate}%)",
                                attempts=hyd_attempts, success=hyd_ok, failures=hyd_fail, rate_pct=hyd_rate))
    else:
        comps.append(_component("hydration", "Event-Page Hydration", "FAILED", RED,
                                f"{hyd_ok}/{hyd_attempts} resolved ({hyd_rate}%)"
                                if hyd_rate is not None else f"{hyd_ok}/{hyd_attempts} resolved",
                                attempts=hyd_attempts, success=hyd_ok, failures=hyd_fail, rate_pct=hyd_rate))

    # ── market observation capture ─────────────────────────────────────
    mf = st.get("market_freshness") or {}
    m_tracked = int(mf.get("tracked_games") or mf.get("active_games") or 0)
    m_with = int(mf.get("with_market") or 0)
    m_never = int(mf.get("never_observed") or 0)
    m_coverage = round(100.0 * m_with / m_tracked, 1) if m_tracked else None
    mc2 = st.get("market_collection") or {}
    m_attempts = int(mc2.get("attempts") or 0)
    m_failed = int(mc2.get("failed") or 0)
    m_backoff = int(mc2.get("skipped_backoff") or 0)
    m_success = max(0, m_attempts - m_failed - m_backoff)
    m_rate = round(100.0 * m_success / m_attempts, 1) if m_attempts else None
    if m_tracked == 0:
        comps.append(_component("market", "Market Observations", "UNKNOWN", GREY,
                                "no active games tracked",
                                coverage_pct=m_coverage, attempts=m_attempts,
                                success=m_success, failures=m_failed))
    elif m_coverage is not None and m_coverage >= th.market_healthy_pct:
        comps.append(_component("market", "Market Observations", "HEALTHY", GREEN,
                                f"{m_with}/{m_tracked} games with market ({m_coverage}%)",
                                coverage_pct=m_coverage, attempts=m_attempts,
                                success=m_success, failures=m_failed))
    elif m_coverage is not None and m_coverage >= th.market_degraded_pct:
        comps.append(_component("market", "Market Observations", "DEGRADED", YELLOW,
                                f"{m_with}/{m_tracked} games with market ({m_coverage}%)",
                                coverage_pct=m_coverage, attempts=m_attempts,
                                success=m_success, failures=m_failed))
    else:
        comps.append(_component("market", "Market Observations", "FAILED", RED,
                                f"{m_with}/{m_tracked} games with market ({m_coverage}%)"
                                if m_coverage is not None else f"{m_with}/{m_tracked} with market",
                                coverage_pct=m_coverage, attempts=m_attempts,
                                success=m_success, failures=m_failed))

    # ── persistence worker ─────────────────────────────────────────────
    w = st.get("betual_line_worker") or {}
    q = int(w.get("queue_depth") or 0)
    enq, per = int(w.get("enqueued") or 0), int(w.get("persisted") or 0)
    lag = max(0, enq - per)
    failed, dropped = int(w.get("failed") or 0), int(w.get("dropped") or 0)
    if not w:
        comps.append(_component("persistence", "Persistence Worker", "FAILED", RED,
                                "no worker telemetry in the collector state"))
    elif not w.get("worker_alive", False):
        comps.append(_component("persistence", "Persistence Worker", "FAILED", RED,
                                "worker thread is not alive",
                                queue_depth=q, lag=lag, failed=failed, dropped=dropped))
    elif lag > th.persist_lag_degraded * 10 or q > th.queue_failed:
        comps.append(_component("persistence", "Persistence Worker", "FAILED", RED,
                                f"queue={q} lag={lag} — persistence is not keeping up",
                                queue_depth=q, lag=lag, failed=failed, dropped=dropped))
    elif q > th.queue_degraded or lag > th.persist_lag_degraded or failed or dropped:
        comps.append(_component("persistence", "Persistence Worker", "DEGRADED", YELLOW,
                                f"queue={q} lag={lag} failed={failed} dropped={dropped}",
                                queue_depth=q, lag=lag, failed=failed, dropped=dropped))
    else:
        comps.append(_component("persistence", "Persistence Worker", "HEALTHY", GREEN,
                                f"queue={q} lag={lag} persisted={per:,}",
                                queue_depth=q, lag=lag, failed=failed, dropped=dropped))

    # ── database ───────────────────────────────────────────────────────
    d = inp.db or {}
    write_errs = int(st.get("errors") or 0)
    if not d.get("ok"):
        comps.append(_component("database", "Database", "ERROR", RED,
                                d.get("error") or "unreadable"))
    elif write_errs and err_age is not None and err_age <= th.error_recent_s:
        comps.append(_component("database", "Database", "CONNECTED", YELLOW,
                                f"{write_errs} collector errors in the last window",
                                errors=write_errs))
    else:
        comps.append(_component("database", "Database", "CONNECTED", GREEN,
                                f"{_gb(d.get('size_bytes'))} GB, WAL {'on' if d.get('wal') else 'off'}",
                                size_bytes=d.get("size_bytes")))

    # ── host ───────────────────────────────────────────────────────────
    h = inp.host or {}
    worst, why = GREEN, []
    if (h.get("disk_free_pct") or 100) < th.disk_failed_free_pct:
        worst, _ = RED, why.append(f"disk {h['disk_free_pct']:.1f}% free")
    elif (h.get("disk_free_pct") or 100) < th.disk_degraded_free_pct:
        worst = max([worst, YELLOW], key=SEVERITY.get); why.append(f"disk {h['disk_free_pct']:.1f}% free")
    if (h.get("ram_pct") or 0) > th.ram_degraded_pct:
        worst = max([worst, YELLOW], key=SEVERITY.get); why.append(f"RAM {h['ram_pct']:.0f}%")
    if (h.get("cpu_pct") or 0) > th.cpu_degraded_pct:
        worst = max([worst, YELLOW], key=SEVERITY.get); why.append(f"CPU {h['cpu_pct']:.0f}%")
    comps.append(_component("host", "Host", "HEALTHY" if worst == GREEN else "DEGRADED", worst,
                            ", ".join(why) or f"cpu {h.get('cpu_pct') or 0:.0f}% · "
                            f"ram {h.get('ram_pct') or 0:.0f}% · disk {h.get('disk_free_pct') or 0:.0f}% free",
                            cpu_pct=None if h.get("cpu_pct") is None else round(h["cpu_pct"], 1),
                            ram_pct=None if h.get("ram_pct") is None else round(h["ram_pct"], 1),
                            disk_free_pct=None if h.get("disk_free_pct") is None else round(h["disk_free_pct"], 1)))

    overall = max((c["color"] for c in comps), key=SEVERITY.get)
    return {
        "generated_at": _iso(now),
        "overall": "HEALTHY" if overall == GREEN else
                   ("DEGRADED" if overall == YELLOW else
                    ("FAILURE" if overall == RED else "UNKNOWN")),
        "overall_color": overall,
        "components": comps,
        "signals": {
            "started_at": st.get("started_at"),
            "collector_process": "ONLINE" if alive else "OFFLINE",
            "collector_pid": inp.unit.get("pid"),
            "n_restarts": inp.unit.get("n_restarts"),
            "polling_interval_s": (st.get("fast_cycle") or {}).get("tick_s"),
            "observed_poll_interval_ms_p50": (
                (st.get("tick_timing") or {}).get("fast_cycle_interval_ms") or {}).get("p50"),
            "collection_rate_per_min": _rate(inp.snapshot_history, now, th.flat_line_window_s)[0],
            "collection_rate_window_s": _rate(inp.snapshot_history, now, th.flat_line_window_s)[1],
            "last_tick_at": last_tick,
            "last_success_at": st.get("last_success_at"),
            "seconds_since_collection": None if fresh_age is None else round(fresh_age, 1),
            "tick_age_s": None if tick_age is None else round(tick_age, 1),
            "collector_running_no_data": running_no_data,
            "snapshots_total": st.get("snapshots_total"),
            "successful_collections": st.get("snapshots_total"),
            "failed_collections": st.get("errors"),
            "last_tick_snapshots": (st.get("performance") or {}).get("snapshots_per_tick"),
            "games_tracked": st.get("games_tracked"),
            "queue_depth": q, "enqueued": enq, "persisted": per, "lag": lag,
            "failed": failed, "dropped": dropped,
            "database_write_failures": failed + int(w.get("batch_fallback_rows_failed") or 0),
            "database_write_retries": int(w.get("batch_retries") or 0),
            "database_write_fallback_events": int(w.get("batch_fallback_events") or 0),
            "database_write_fallback_rows_failed": int(w.get("batch_fallback_rows_failed") or 0),
            "persistence_worker_health": (
                "FAILED" if not w or not w.get("worker_alive", False)
                or lag > th.persist_lag_degraded * 10 or q > th.queue_failed
                else "DEGRADED" if q > th.queue_degraded or lag > th.persist_lag_degraded
                or failed or dropped else "HEALTHY"),
            "pending_resolve_depth": int((st.get("fast_cycle") or {}).get("pending_resolve") or 0),
            "market_queue_depth": int((st.get("market_collection") or {}).get("queue_len") or 0),
            "errors": write_errs,
            "ticks": st.get("tick"),
            "pokerbet_connectivity": (
                "DISCONNECTED" if (err_age is not None and err_age <= th.error_recent_s)
                or empties >= th.empty_ticks_disconnected else "CONNECTED"),
            "consecutive_empty_ticks": empties,
            "last_error_at": st.get("last_error_at"),
            "seconds_since_last_error": None if err_age is None else round(err_age, 1),
            "browser_age_s": None if browser_age is None else round(browser_age, 1),
            "browser_health": (
                "FAILED" if not slow_alive else "DEGRADED" if (slow_error or not page_ok or slow_degraded)
                else "HEALTHY"),
            "browser_page_available": page_ok,
            "browser_slow_worker_alive": slow_alive,
            "browser_slow_worker_degraded": slow_degraded,
            "browser_slow_rounds": slow.get("rounds"),
            "browser_slow_resolve_ok": slow.get("resolve_ok"),
            "browser_slow_resolve_failures": slow.get("resolve_failures"),
            "event_page_hydration_attempts": hyd_attempts,
            "event_page_hydration_success": hyd_ok,
            "event_page_hydration_failures": hyd_fail,
            "event_page_hydration_rate_pct": hyd_rate,
            "event_view_market_attempts": int((st.get("market_collection") or {}).get("attempts") or 0),
            "event_view_market_failures": int((st.get("market_collection") or {}).get("failed") or 0),
            "event_view_market_skipped_backoff": int((st.get("market_collection") or {}).get("skipped_backoff") or 0),
            "event_view_market_dropped_untracked": int((st.get("market_collection") or {}).get("dropped_untracked") or 0),
            "market_observation_games_tracked": m_tracked,
            "market_observation_games_with_market": m_with,
            "market_observation_games_never_observed": m_never,
            "market_observation_coverage_pct": m_coverage,
            "market_observation_attempts": m_attempts,
            "market_observation_successes": m_success,
            "market_observation_failures": m_failed,
            "market_observation_skipped_backoff": m_backoff,
            "market_observation_attempt_success_pct": m_rate,
            "collection_success_pct": (
                round(100.0 * (1 - write_errs / st["tick"]), 2)
                if st.get("tick") else None),
            "collection_error_pct": (
                round(100.0 * write_errs / st["tick"], 2)
                if st.get("tick") else None),
            "cpu_pct": None if h.get("cpu_pct") is None else round(h["cpu_pct"], 1),
            "ram_pct": None if h.get("ram_pct") is None else round(h["ram_pct"], 1),
            "disk_free_pct": None if h.get("disk_free_pct") is None else round(h["disk_free_pct"], 1),
            "backend_ms": None if b.get("latency_ms") is None else round(b["latency_ms"], 1),
        },
    }


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _age(now: float, iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        from datetime import datetime
        return max(0.0, now - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _gb(n) -> str:
    try:
        return f"{float(n) / 1e9:.1f}"
    except (TypeError, ValueError):
        return "?"


def collect(unit: str, state_path: str, db_path: str, backend_url: str,
            frontend_url: str, frontend_marker: str | None,
            snapshot_history: list, th: Thresholds | None = None) -> dict:
    """Run every probe once and evaluate. This is what the runner calls."""
    th = th or Thresholds()
    now = time.time()
    state, state_age, state_error = read_state(state_path)
    inp = Inputs(
        now=now, state=state, state_age_s=state_age, state_error=state_error,
        unit=probe_unit(unit) if unit else {"active": False, "pid": None},
        backend=probe_http(backend_url + "/healthz", marker='"status"'),
        frontend=probe_http(frontend_url, marker=frontend_marker),
        db=probe_sqlite(db_path),
        host=probe_host(),
        snapshot_history=list(snapshot_history),
    )
    return evaluate(inp, th)


def dumps(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=False, default=str)
