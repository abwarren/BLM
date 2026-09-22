#!/usr/bin/env python3
"""Query the persistent BLM context index.

Usage:
    python3 rag/query_context.py "under alert trigger condition"
    python3 rag/query_context.py --top 5 "checkpoint market"
    python3 rag/query_context.py --kind py "freeze detector"
    python3 rag/query_context.py --path scorecard "settlement"
    python3 rag/query_context.py --commits "temporal freshness"

Results are ranked by FTS5 BM25 relevance. Use --rebuild to refresh the index
from the current repository state before querying.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "blm_context.db"
REPO_ROOT = Path(__file__).resolve().parent.parent

KIND_LABELS = {
    "md": "doc",
    "txt": "doc",
    "py": "code",
    "json": "data",
    "jsonl": "data",
    "csv": "data",
    "tsv": "data",
    "html": "frontend",
    "js": "frontend",
    "css": "frontend",
    "yaml": "config",
    "yml": "config",
    "sh": "script",
    "service": "config",
    "sql": "sql",
    "rst": "doc",
    "toml": "config",
    "ini": "config",
    "cfg": "config",
    "out": "log",
}


def label(kind: str) -> str:
    return KIND_LABELS.get(kind.lower(), kind or "?")


def current_git_commit() -> dict:
    """Return metadata for the current HEAD commit (empty dict if unavailable)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", "-1",
             "--format=%H%x1f%an%x1f%ad%x1f%s",
             "--date=iso"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            parts = proc.stdout.strip().split("\x1f")
            if len(parts) == 4:
                return {
                    "commit_hash": parts[0],
                    "commit_author": parts[1],
                    "commit_date": parts[2],
                    "commit_subject": parts[3],
                }
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {}


def fts_quote(query: str) -> str:
    """Quote each token so FTS5 reserved operators are treated literally."""
    tokens = query.split()
    out = []
    for tok in tokens:
        # Strip surrounding quotes already present, then re-quote the token.
        t = tok.strip('"')
        if not t:
            continue
        out.append('"' + t.replace('"', '""') + '"')
    return " ".join(out) if out else '""'


def search_chunks(query: str, top: int, kind: str | None,
                  path_filter: str | None) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        conds = ["chunks_fts MATCH ?"]
        args: list = [fts_quote(query)]
        if kind:
            conds.append("c.kind = ?")
            args.append(kind)
        if path_filter:
            conds.append("c.rel_path LIKE ?")
            args.append(f"%{path_filter}%")
        args.append(top)

        rows = con.execute(
            f"""
            SELECT c.id, c.rel_path, c.kind, c.title,
                   c.line_start, c.line_end, c.symbol, c.content_hash,
                   snippet(chunks_fts, 3, '[', ']', '…', 14) AS snip,
                   bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            WHERE {" AND ".join(conds)}
            ORDER BY score
            LIMIT ?
            """,
            args,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def search_commits(query: str, top: int) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT hash, author, date, subject, body
            FROM commits
            WHERE commits MATCH ?
            ORDER BY date DESC
            LIMIT ?
            """,
            [query, top],
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        # Fall back to LIKE if the commits table has no FTS index.
        rows = con.execute(
            """
            SELECT hash, author, date, subject, body
            FROM commits
            WHERE subject LIKE ? OR body LIKE ?
            ORDER BY date DESC
            LIMIT ?
            """,
            [f"%{query}%", f"%{query}%", top],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def search_schemas(query: str, top: int) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT db, table_name, ddl
            FROM schemas
            WHERE table_name LIKE ? OR ddl LIKE ?
            ORDER BY db, table_name
            LIMIT ?
            """,
            [f"%{query}%", f"%{query}%", top],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", help="search terms (FTS5 query syntax)")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--kind", help="restrict to file extension, e.g. py or md")
    ap.add_argument("--path", dest="path_filter", help="restrict to path substring")
    ap.add_argument("--commits", action="store_true", help="search git history instead")
    ap.add_argument("--schemas", action="store_true", help="search DB schemas instead")
    ap.add_argument("--rebuild", action="store_true", help="rebuild index first")
    ap.add_argument("--json", action="store_true", help="emit JSON (agent-friendly)")
    ap.add_argument("--store", action="store_true",
                    help="store every retrieved chunk in the persistent knowledge store")
    args = ap.parse_args()

    if args.rebuild or not DB_PATH.exists():
        import subprocess
        print("Rebuilding index...", file=sys.stderr)
        proc = subprocess.run([sys.executable, str(Path(__file__).parent / "build_context.py")])
        if proc.returncode != 0:
            return proc.returncode

    if not args.query:
        ap.print_help()
        return 1

    if args.commits:
        rows = search_commits(args.query, args.top)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        for row in rows:
            print(f"[commit] {row['hash'][:10]} {row['date']} {row['subject']}")
            if row["body"]:
                body = row["body"].strip().splitlines()
                if body:
                    print(f"          {body[0][:200]}")
            print()
        return 0

    if args.schemas:
        rows = search_schemas(args.query, args.top)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        for row in rows:
            print(f"[schema] {row['db']} :: {row['table_name']}")
            print("  " + row["ddl"].replace("\n", "\n  ")[:1200])
            print()
        return 0

    hits = search_chunks(args.query, args.top, args.kind, args.path_filter)
    if args.store:
        import importlib.util
        kb_path = Path.home() / ".clai" / "kb.py"
        spec = importlib.util.spec_from_file_location("kb", kb_path)
        kb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(kb)
        stored = 0
        repo_root = Path(__file__).resolve().parent.parent
        commit = current_git_commit()
        for h in hits:
            key = f"{h['rel_path']}#{h['id']}"
            # Absolute filesystem path for the source file.
            if h["rel_path"].startswith("external/"):
                file_path = str(Path.home() / h["rel_path"].replace("external/", "", 1))
            else:
                file_path = str(repo_root / h["rel_path"])
            # 1-based line range for display.
            line_start = int(h.get("line_start", 0)) + 1
            line_end = int(h.get("line_end", 0)) + 1
            symbol = h.get("symbol", "")
            content_hash = h.get("content_hash", "")
            kb.upsert_chunk(
                key=key,
                title=f"{h['rel_path']}:{line_start}-{line_end} :: {h['title']}",
                content=f"{h['snip']}",
                tags=f"blm,rag,{h['kind']}",
                source=h["rel_path"],
                file_path=file_path,
                line_start=line_start,
                line_end=line_end,
                symbol=symbol,
                content_hash=content_hash,
                commit_hash=commit.get("commit_hash", ""),
                commit_author=commit.get("commit_author", ""),
                commit_date=commit.get("commit_date", ""),
                commit_subject=commit.get("commit_subject", ""),
            )
            stored += 1
        print(f"stored {stored} chunks in {kb.DB}", file=sys.stderr)
    if args.json:
        print(json.dumps(hits, indent=2))
        return 0
    if not hits:
        print("No matches.")
        return 0

    for i, h in enumerate(hits, 1):
        print(f"{i}. [{label(h['kind'])}] {h['title']}")
        print(f"   {h['rel_path']}:{h['line_start']+1}-{h['line_end']+1}")
        print(f"   {h['snip']}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
