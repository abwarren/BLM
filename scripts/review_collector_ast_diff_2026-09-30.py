#!/usr/bin/env python3
"""REVIEW TOOL (read-only) — AST-level function diff of collector.py.

Proves which functions in blm_v4/collector.py changed between the
production base (d50b230) and the PR head (f174ade).  For every function
present in BOTH revisions, compares the hash of its exact source segment
(ast-sourced, whitespace-exact).  Lists NEW functions separately.

Used by the PR #3 final review to certify that alert-adjacent collector
code (final-state detection, end-game handling entry points, reconciliation)
is source-identical, and that the only changed/new functions are the
documented capture-efficiency additions.
"""
from __future__ import annotations

import ast
import hashlib
import subprocess
import sys

BASE = "d50b230"
HEAD = "f174ade"
PATH = "blm_v4/collector.py"

# Alert-adjacent collector functions that MUST be identical.
MUST_BE_IDENTICAL = (
    "_capture_event_state", "_is_final_state", "_reconcile",
    "_detect_event_reset", "_detect_instance_reset", "_end_game_body_ok",
    "_verified_event_view", "_infer_status",
)


def _src(rev: str) -> str:
    return subprocess.run(
        ["git", "show", f"{rev}:{PATH}"], capture_output=True, text=True,
        check=True).stdout


def _functions(src: str) -> dict[str, str]:
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    seg = "".join(lines[sub.lineno - 1:sub.end_lineno])
                    out[f"{node.name}.{sub.name}"] = seg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seg = "".join(lines[node.lineno - 1:node.end_lineno])
            out[node.name] = seg
    return out


def h(seg: str) -> str:
    return hashlib.sha256(seg.encode()).hexdigest()[:16]


def main() -> int:
    base_f = _functions(_src(BASE))
    head_f = _functions(_src(HEAD))
    changed, same, added = [], [], []
    for name, seg in head_f.items():
        if name in base_f:
            (same if h(seg) == h(base_f[name]) else changed).append(name)
        else:
            added.append(name)
    removed = [n for n in base_f if n not in head_f]

    print(f"functions: base={len(base_f)} head={len(head_f)}")
    print(f"IDENTICAL: {len(same)}")
    print(f"CHANGED ({len(changed)}):")
    for n in sorted(changed):
        print(f"  ~ {n}")
    print(f"NEW ({len(added)}):")
    for n in sorted(added):
        print(f"  + {n}")
    print(f"REMOVED ({len(removed)}):")
    for n in sorted(removed):
        print(f"  - {n}")

    # The directive's must-be-identical names may live at class level
    # (Class.method) — check both spellings.
    problems = []
    for want in MUST_BE_IDENTICAL:
        cands = [n for n in changed if n == want or n.endswith(f".{want}")]
        if cands:
            problems.append((want, cands))
    print()
    if problems:
        print("MUST-BE-IDENTICAL VIOLATIONS:")
        for want, cands in problems:
            print(f"  !! {want} changed: {cands}")
        return 1
    print("must-be-identical alert-adjacent functions: ALL IDENTICAL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
