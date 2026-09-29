#!/usr/bin/env python3
"""BLM 🔴 TEST — launch the isolated TEST stack on 127.0.0.1:2263.

No collector.  Separate TEST databases.  DRY_RUN forced.  🔴 banner on
the dashboard.  Never touches the production services on port 2262.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # the BLM project root
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TEST_PORT", "2263")

from blm_v4.test_stack import build_test_app, TEST_PORT_DEFAULT


def main() -> None:
    import uvicorn
    app, _feed, _store, _cfg, _worker, targets = build_test_app(ROOT)
    port = int(targets.get("port") or TEST_PORT_DEFAULT)
    print("=" * 62)
    print("🔴 TEST ENVIRONMENT")
    print(f"  analytics db : {targets['analytics_db']}")
    print(f"  betting db   : {targets['betting_db']}")
    print(f"  dry run      : {targets['dry_run']}  (forced)")
    print(f"  collector    : OFF")
    print(f"  url          : http://127.0.0.1:{port}/")
    print("=" * 62)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info",
                access_log=False)


if __name__ == "__main__":
    main()
