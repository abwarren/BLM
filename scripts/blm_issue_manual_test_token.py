#!/usr/bin/env python3
"""Issue ONE manual REAL_MONEY_TEST (R2.00) authorization token — CLI ONLY.

This module does NOT place a bet and does NOT enable Auto-Bet.  It writes a
single-use, short-TTL bearer token to ``blm_betting.db`` (only sha256(token)
is persisted).  The authenticated operator then submits exactly ONE manual
TOTAL/UNDER request bound to the SAME ``game_id``, presenting the token as
``payload["authorization"]`` to ``POST /api/v4/betting/manual``.

The token is printed ONCE and never persisted in plaintext.  Issuance is
CLI-only by design: no HTTP route issues or exposes a token.

Usage:
  python3 scripts/blm_issue_manual_test_token.py \
      --game-id <GID> --authorized-by <operator> [--ttl 300] \
      [--market TOTAL] [--direction UNDER] [--stake 2.00] [--currency ZAR] \
      [--db /home/ubuntu/BLM/blm_betting.db]

Safety: refuses unless the exact test invariants hold (REAL_MONEY_TEST,
exactly R2.00 ZAR, TOTAL/UNDER, a concrete game_id, a positive TTL, a named
operator).  Placing the bet still requires a live CDP browser + authenticated
PokerBet session and the server restarted onto this code — all operator-gated.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.store import BettingStore


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Issue a single-use manual REAL_MONEY_TEST (R2.00) "
                    "authorization token (CLI only; no bet is placed).")
    p.add_argument("--game-id", required=True,
                   help="the ONE game_id this token is bound to")
    p.add_argument("--market", default="TOTAL")
    p.add_argument("--direction", default="UNDER")
    p.add_argument("--stake", type=float, default=2.00)
    p.add_argument("--currency", default="ZAR")
    p.add_argument("--ttl", type=float, default=300.0, dest="ttl_s",
                   help="token lifetime in seconds (default 300)")
    p.add_argument("--authorized-by", required=True,
                   help="operator identity recorded in the audit event")
    p.add_argument("--db", default=None,
                   help="override blm_betting.db path (default: BETTING_DB_PATH)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    cfg = BettingConfig.from_env(REPO)
    db_path = args.db or cfg.db_path
    store = BettingStore(db_path)

    try:
        token = store.issue_manual_authorization(
            mode="REAL_MONEY_TEST", stake=args.stake, currency=args.currency,
            market=args.market, direction=args.direction,
            game_id=args.game_id, ttl_s=args.ttl_s,
            authorized_by=args.authorized_by)
    except ValueError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2

    sha12 = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    store.audit("manual_authorization_issued", reason="cli",
                details={"mode": "REAL_MONEY_TEST", "stake": args.stake,
                         "currency": str(args.currency).upper(),
                         "market": "TOTAL", "direction": "UNDER",
                         "game_id": str(args.game_id), "ttl_s": args.ttl_s,
                         "authorized_by": args.authorized_by,
                         "token_sha256_prefix": sha12})

    print("MANUAL R2.00 ONE-SHOT AUTHORIZATION ISSUED")
    print(f"  db          : {db_path}")
    print(f"  binding     : game_id={args.game_id} market=TOTAL "
          f"direction=UNDER stake=2.00 ZAR")
    print(f"  ttl         : {args.ttl_s:g}s   authorized_by={args.authorized_by}")
    print(f"  token_sha12 : {sha12}  (audit reference)")
    print("  TOKEN (single use, show once; pass as payload['authorization']):")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
