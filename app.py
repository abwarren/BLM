"""
BLM — Betting Logic Model
Entry point for V1 (legacy) research console.

Usage:
    python3 app.py          # V1 console on :5000
    python3 server.py       # V2 platform on :8000

See README.md for details.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from blm_v1.app import main

if __name__ == "__main__":
    main()
