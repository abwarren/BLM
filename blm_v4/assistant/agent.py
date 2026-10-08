"""The BLM assistant: a read-only question-answering agent over the platform.

Talks to the Anthropic Messages API (which on this box is pointed at an
Anthropic-compatible endpoint) and drives the read-only tools in
``blm_v4.assistant.tools``.  It holds no credentials of its own: they are
resolved at call time from the process environment, then ``~/.hermes/.env``,
then ``~/.env`` — Hermes loads its own .env internally and does NOT export it
into child processes, so a service that only reads ``os.environ`` finds nothing.

The agent is read-only end to end.  If the model is prompted to change
something it can only say so.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from blm_v4.assistant.tools import run_tool, tool_schemas

API_VERSION = "2023-06-01"
MAX_TOKENS = 2048
MAX_ROUNDS = 8
REQUEST_TIMEOUT_S = 120.0
TOOL_RESULT_CAP = 20000

SYSTEM_PROMPT = """You are the BLM assistant — a read-only analyst for the BLM \
live-basketball betting analytics platform, which follows PokerBet's live feed.

THE SYSTEM
- blm-collector (systemd, from ~/BLM) scrapes PokerBet's live pages into \
blm_pokerbet.db (tables: games, snapshots).
- blm-server (systemd, /home/ubuntu/blm-fix-price-mapping) runs the analytics and \
a FastAPI dashboard on 127.0.0.1:2262. It writes blm_metrics_clean.db \
(clean_projections, clean_market_observations, clean_games) and blm_betting.db \
(bet_executions, bet_audit, betting_config).
- A betting worker polls the live payload every 5 seconds and puts each game \
through one execution gate. blm_betting.db is the ledger; every attempt carries a \
status and, when it fails, an error_code.
- Bets are submitted by a SEPARATE Opera GX browser (user-data-dir \
/home/ubuntu/blm-exec-gx) over CDP at 127.0.0.1:9222. If that port is down \
nothing can be submitted, and it must be launched with \
--remote-debugging-port=9222 to have it.

THE SIGNAL vs THE EXECUTION
- The ALERT (the signal, deliberately unchanged): progress_pct >= 75 AND \
required_pace > league average pace * 1.04.
- Execution uses the relaxed bar: the same test against 0.95, served to the \
worker as under_alert_exec_armed.
- Trading window: 75-92% progress, at least 70 points already scored.
- Price floors by band: 75-80% -> 1.48, 80-85% -> 1.39, >=85% -> 1.28.
- Limits: max 1000 per bet and max 200 bets per day. The daily EXPOSURE ceiling \
was removed on 2026-10-08 by operator directive.

REASON CODES (error_code and the worker's no_bet_top tally)
- alert_not_active: outside the band, or not armed (the great majority — normal).
- alert_not_eligible:<sub>: armed but excluded; sub is one of game_finished, \
market_stale, stale_state, no_live_observation, market_missing, not-live.
- duplicate_execution / one_bet_per_game, after_execution_window, \
price_below_break_even, score_below_minimum, alert_stale, game_not_live.
- daily_bet_limit_reached, daily_exposure_reached, limits_not_configured, \
GLOBAL_AUTO_BET_OFF.
- Transport: EVENT_NOT_FOUND, POSITION_NOT_FOUND, SUBMIT_CONTROL_UNAVAILABLE, \
BETSLIP_NOT_UPDATED, STAKE_MISMATCH.
- PROVIDER_AMBIGUOUS: the book may have taken the money — it is NEVER retried.

HOW TO ANSWER
- Look before you speak. Call health, config or query and read the real value; \
never give a number you have not read, and say plainly when something cannot be \
verified rather than estimating.
- The worker emits a `betting_pass` log line about once a minute (throttled) \
with games, candidates, executed, no_bet and no_bet_top. That is the fastest \
answer to "why is it not betting".
- Quote concrete evidence: table, filter, and the numbers.
- You are READ-ONLY. You cannot change the unit price, toggle auto-betting, \
restart a service or place a bet. When asked to, say so and tell the operator \
what to do instead (the dashboard's top-bar switch, the SUDO-free \
systemctl --user restart blm-server, etc.).
- Be brief. Operators read this on a phone."""


# ── credentials ───────────────────────────────────────────────────────────
def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        return {}
    return out


def _resolve(name: str, *fallbacks: str) -> str:
    for n in (name, *fallbacks):
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    for p in (Path.home() / ".hermes" / ".env", Path.home() / ".env"):
        if p.exists():
            env = _parse_env_file(p)
            for n in (name, *fallbacks):
                v = env.get(n)
                if v and v.strip():
                    return v.strip()
    return ""


def credentials() -> dict[str, str]:
    """Resolve key/base/model without ever logging them."""
    key = _resolve("ANTHROPIC_API_KEY")
    base = _resolve("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL") or "https://api.anthropic.com"
    model = (_resolve("BLM_ASSISTANT_MODEL")
             or _resolve("ANTHROPIC_DEFAULT_SONNET_MODEL")
             or "claude-sonnet-4-5")
    return {"key": key, "base": base.rstrip("/"), "model": model}


def available() -> tuple[bool, str]:
    c = credentials()
    if not c["key"]:
        return False, "no ANTHROPIC_API_KEY found in the environment, ~/.hermes/.env or ~/.env"
    return True, ""


# ── the model call ────────────────────────────────────────────────────────
def _post(messages: list[dict], *, model: str) -> dict:
    import httpx

    c = credentials()
    if not c["key"]:
        raise RuntimeError("assistant is not configured: " + available()[1])
    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": messages,
        "tools": tool_schemas(),
    }
    headers = {
        "content-type": "application/json",
        "x-api-key": c["key"],
        "anthropic-version": API_VERSION,
    }
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as cli:
        r = cli.post(f"{c['base']}/v1/messages", json=body, headers=headers)
        if r.status_code >= 400:
            detail = r.text[:300]
            raise RuntimeError(f"model endpoint returned {r.status_code}: {detail}")
        return r.json()


# ── the loop ──────────────────────────────────────────────────────────────
def chat(history: list[dict], *, model: str = "") -> dict:
    """Run one user turn to completion.

    ``history`` is the running message list (role/content dicts, oldest first,
    ending with the new user message).  Returns a dict with the reply, the
    tools that ran, and token usage.
    """
    model = model or credentials()["model"]
    msgs: list[dict] = [dict(m) for m in history]
    ran: list[dict] = []
    usage: dict[str, Any] = {}

    for _round in range(MAX_ROUNDS):
        resp = _post(msgs, model=model)
        usage = resp.get("usage") or usage
        blocks = resp.get("content") or []

        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        tool_uses = [b for b in blocks if b.get("type") == "tool_use"]

        if not tool_uses:
            return {"reply": text.strip(), "tool_calls": ran,
                    "model": model, "usage": usage, "rounds": _round + 1}

        # Rebuild the assistant turn from TEXT + TOOL_USE only: thinking blocks
        # must never be replayed (they are unsigned here and buy nothing).
        assistant_content: list[dict] = []
        if text:
            assistant_content.append({"type": "text", "text": text})
        assistant_content += [
            {"type": "tool_use", "id": tu.get("id"), "name": tu.get("name"),
             "input": tu.get("input") or {}}
            for tu in tool_uses
        ]
        msgs.append({"role": "assistant", "content": assistant_content})

        results: list[dict] = []
        for tu in tool_uses:
            name = str(tu.get("name"))
            out = run_tool(name, tu.get("input") or {})
            ran.append({"tool": name, "input": tu.get("input") or {},
                        "preview": out[:300]})
            results.append({"type": "tool_result", "tool_use_id": tu.get("id"),
                            "content": out[:TOOL_RESULT_CAP]})
        msgs.append({"role": "user", "content": results})

    return {"reply": "Stopped: the tool loop hit its round cap without settling.",
            "tool_calls": ran, "model": model, "usage": usage, "rounds": MAX_ROUNDS}


def answer(question: str, *, history: Optional[list[dict]] = None,
           model: str = "") -> dict:
    """Convenience for one-shot use (the CLI and the API both funnel here)."""
    msgs = list(history or [])
    msgs.append({"role": "user", "content": question})
    out = chat(msgs, model=model)
    out["messages"] = msgs + [{"role": "assistant", "content": out["reply"]}]
    return out
