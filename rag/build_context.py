#!/usr/bin/env python3
"""Build a persistent, queryable context index over the BLM repository.

The index is a single SQLite database (rag/blm_context.db) using FTS5 for
full-text search. It ingests:

  * Markdown / text / config docs (chunked by section and paragraph)
  * Python source (chunked per top-level module/class/function where possible)
  * SQLite database schemas (table DDL from every *.db in the repo)
  * Git history (subject + body of every commit)

No external dependencies are required beyond the Python standard library and
a SQLite build with FTS5 enabled (present in CPython's bundled SQLite).

Rebuild from the repo root:
    python3 rag/build_context.py
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAG_DIR = ROOT / "rag"
DB_PATH = RAG_DIR / "blm_context.db"

# Directories to skip entirely.
SKIP_DIRS = {
    ".git",
    "venv",
    ".venv",
    "env",
    ".env",
    "virtualenv",
    ".virtualenvs",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".coverage",
    ".cache",
    ".ipynb_checkpoints",
    ".pyre",
    "dist",
    "build",
    "target",
    "backups",
    "rag",  # never index the index itself
}

# File extensions we index as text.
TEXT_EXTS = {
    ".md", ".txt", ".rst", ".py", ".json", ".jsonl", ".csv", ".tsv",
    ".html", ".js", ".css", ".yaml", ".yml", ".sh", ".service", ".sql",
    ".toml", ".ini", ".cfg", ".out", ".svg",
}

# Extensionless files we still index as text.
TEXT_NO_EXT = {
    "blm-cli",
    ".gitignore",
    "Dockerfile",
    "Makefile",
    "LICENSE",
}

# Binary or huge artifacts we explicitly never index.
SKIP_NAMES = {
    "blm_context.db",
}

# Compiled/generated artifact extensions that are never indexed as text.
SKIP_EXTS = {
    ".pyc", ".pyo", ".so", ".o", ".a", ".class", ".jar", ".war",
    ".whl", ".parquet", ".arrow", ".feather", ".pkl", ".pickle",
    ".pt", ".onnx", ".bin", ".iso", ".zip", ".tar", ".gz", ".bz2",
    ".xz", ".7z", ".exe", ".dll", ".dylib",
}

# File names that must never be indexed (secrets, keys, credentials, etc.).
SKIP_PATTERNS = [
    ".env",          # covers .env, .env.bak, .env.local, .env.*
    ".aws-",         # AWS credential files
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",  # SSH private keys
    ".npmrc",        # may contain auth tokens
    ".pypirc",
    "credentials",
    ".netrc",
]

# PDFs get their text extracted at ingest time.
PDF_EXTS = {".pdf"}

# Regular expressions for values that must never be stored in the index.
# Any match is replaced with a redaction marker before chunking.
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                                    # AWS access key
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),                              # Google API key
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                                 # GitHub PAT
    re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),                        # GitHub fine-grained PAT
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),                       # Slack token
    re.compile(r"sk_live_[A-Za-z0-9]{20,}"),                            # Stripe live key
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"),                      # Bearer token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.]{8,}[\"']?"),
    # Any credential-ish key assigned a value in env/ini style.
    re.compile(r"(?im)^\s*([A-Za-z_][A-Za-z0-9_]*)?(api[_-]?key|apikey|secret|password|passwd|access[_-]?token|auth[_-]?token)\s*=\s*.+$"),
]

REDACT_MARKER = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Replace secret-looking values before they reach the index."""
    for pat in SECRET_PATTERNS:
        text = pat.sub(REDACT_MARKER, text)
    return text

# Production logs/results that live OUTSIDE the repo root (e.g. in $HOME).
# These are indexed under a synthetic "external/" rel_path so they remain
# clearly separated from repository files.
EXTERNAL_FILES = [
    "blm_report.out",
    "blm_under_backtest.out",
    "blm_under_backtest.json",
    "oos_run.log",
]

MAX_FILE_BYTES = 8 * 1024 * 1024  # hard ceiling; most text files are far smaller
CHUNK_TARGET = 1400
CHUNK_OVERLAP = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id       INTEGER PRIMARY KEY,
    rel_path TEXT NOT NULL UNIQUE,
    kind     TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    sha256   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    title    TEXT NOT NULL,
    content  TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    line_start INTEGER NOT NULL DEFAULT 0,
    line_end   INTEGER NOT NULL DEFAULT 0,
    symbol     TEXT NOT NULL DEFAULT '',
    UNIQUE(file_id, seq)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    rel_path UNINDEXED,
    kind     UNINDEXED,
    title,
    content,
    tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS schemas (
    db         TEXT NOT NULL,
    table_name TEXT NOT NULL,
    ddl        TEXT NOT NULL,
    PRIMARY KEY (db, table_name)
);

CREATE TABLE IF NOT EXISTS commits (
    hash    TEXT PRIMARY KEY,
    author  TEXT,
    date    TEXT,
    subject TEXT,
    body    TEXT
);
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_into_chunks(text: str, target: int = CHUNK_TARGET,
                      overlap: int = CHUNK_OVERLAP) -> list[tuple[str, int]]:
    """Split text into roughly equal chunks.

    Returns list of (chunk_text, start_line_offset) where start_line_offset is
    the 0-based line index within *text* where the chunk begins.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) <= target:
        return [(text, 0)] if text.strip() else []

    lines = text.split("\n")
    chunks: list[tuple[list[str], int]] = []
    buf: list[str] = []
    buf_len = 0
    buf_start = 0

    for i, line in enumerate(lines):
        if buf and buf_len + len(line) + 1 > target:
            chunks.append((buf, buf_start))
            # Keep a small overlap so cross-boundary matches survive.
            overlap_text = "\n".join(buf)
            kept = overlap_text[-overlap:]
            buf = [kept] if kept else []
            buf_len = len(kept)
            buf_start = max(0, i - 1)
        if not buf:
            buf_start = i
        buf.append(line)
        buf_len += len(line) + 1

    if buf:
        chunks.append((buf, buf_start))

    # Merge tiny trailing chunks into the previous one, keeping earlier start.
    merged: list[tuple[str, int]] = []
    for buf, start in chunks:
        text_part = "\n".join(buf)
        if merged and len(text_part) < 300:
            prev_text, prev_start = merged[-1]
            merged[-1] = (prev_text + "\n" + text_part, prev_start)
        else:
            merged.append((text_part, start))
    return merged


def chunk_markdown(text: str, title: str) -> list[tuple[str, str, int, str]]:
    """Split markdown by ATX headings, then by size within each section.

    Returns (title, content, start_line, symbol) where symbol is '' for docs.
    """
    lines = text.split("\n")
    sections: list[tuple[str, list[str], int]] = []
    current_title = title
    current: list[str] = []
    current_start = 0

    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            if current or sections:
                sections.append((current_title, current, current_start))
            current_title = f"{title} > {m.group(2).strip()}"
            current = [line]
            current_start = i
        else:
            if not current:
                current_start = i
            current.append(line)
    if current:
        sections.append((current_title, current, current_start))

    out: list[tuple[str, str, int, str]] = []
    for sec_title, sec_lines, sec_start in sections:
        sec = "\n".join(sec_lines)
        for chunk, off in split_into_chunks(sec):
            out.append((sec_title, chunk, sec_start + off, ""))
    return out


def chunk_python(path: str, text: str) -> list[tuple[str, str, int, str]]:
    """Chunk Python by top-level module/class/function when parseable.

    Returns (title, content, start_line, symbol) where start_line is 0-based
    within text and symbol is the containing function/class name (or '').
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [(t, c, l, "") for t, c, l in chunk_markdown(text, path)]

    lines = text.split("\n")
    out: list[tuple[str, str, int, str]] = []

    module_doc = ast.get_docstring(tree)
    if module_doc:
        out.append((f"{path}:module", module_doc.strip(), 0, ""))

    def emit_node(node) -> None:
        start = node.lineno - 1
        end = getattr(node, "end_lineno", node.lineno)
        seg = "\n".join(lines[start:end])
        name = getattr(node, "name", f"<{type(node).__name__}>")
        kind = type(node).__name__.lower()
        title = f"{path}:{name} ({kind})"
        for chunk, off in split_into_chunks(seg):
            out.append((title, chunk, start + off, name))

        # Nested methods/functions inside classes get their own chunks.
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                emit_node(child)

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            emit_node(node)

    # If the module is mostly top-level statements and produced little, add raw.
    if not out:
        for chunk, off in split_into_chunks(text):
            out.append((f"{path}:module", chunk, off, ""))
    return out


def extract_pdf_text(path: Path) -> str:
    """Extract text from a PDF using pdftotext if available."""
    try:
        proc = subprocess.run(
            ["pdftotext", str(path), "-"],
            capture_output=True, timeout=60,
        )
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def chunk_file(path: Path, text: str) -> list[tuple[str, str, int, str]]:
    ext = path.suffix.lower()
    if ext == ".py":
        return chunk_python(path.as_posix(), text)
    if ext == ".md":
        return chunk_markdown(text, path.as_posix())
    if ext == ".pdf":
        return chunk_markdown(text, path.as_posix())
    # Generic: treat every non-empty line block as searchable text.
    return [(path.as_posix(), chunk, off, "")
            for chunk, off in split_into_chunks(text)]


def iter_text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        parts = set(rel.parts)
        if parts & SKIP_DIRS:
            continue
        if path.name in SKIP_NAMES:
            continue
        if path.suffix.lower() in SKIP_EXTS:
            continue
        if any(path.name.startswith(p) or p in path.name for p in SKIP_PATTERNS):
            continue
        if (path.suffix.lower() not in TEXT_EXTS
                and path.name not in TEXT_NO_EXT
                and path.suffix.lower() not in PDF_EXTS):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                print(f"  skip (large): {rel}", file=sys.stderr)
                continue
        except OSError:
            continue
        yield path, rel


def iter_external_files():
    """Yield (path, synthetic_rel) for production logs outside the repo root."""
    for name in EXTERNAL_FILES:
        path = ROOT.parent / name
        if not path.is_file():
            continue
        rel = Path("external") / name
        yield path, rel


def ingest_files(con: sqlite3.Connection) -> int:
    cur = con.cursor()
    total_chunks = 0
    n_files = 0

    def emit(path: Path, rel: Path):
        nonlocal total_chunks, n_files
        try:
            if path.suffix.lower() == ".pdf":
                text = extract_pdf_text(path)
                if not text.strip():
                    print(f"  skip (no pdf text): {rel}", file=sys.stderr)
                    return
                raw = text.encode("utf-8")
            else:
                raw = path.read_bytes()
                text = raw.decode("utf-8", errors="replace")
            text = redact_secrets(text)
        except OSError as exc:
            print(f"  read error {rel}: {exc}", file=sys.stderr)
            return

        stat = path.stat()
        cur.execute(
            "INSERT INTO files(rel_path, kind, size, mtime, sha256) VALUES (?,?,?,?,?)",
            (rel.as_posix(), path.suffix.lower().lstrip("."), stat.st_size,
             stat.st_mtime, sha256_bytes(raw)),
        )
        file_id = cur.lastrowid

        for seq, (title, content, line_start, symbol) in enumerate(chunk_file(path, text)):
            if not content.strip():
                continue
            line_count = content.count("\n") + 1
            line_end = line_start + line_count - 1
            content_hash = sha256_bytes(content.encode("utf-8"))
            cur.execute(
                "INSERT INTO chunks(file_id, seq, kind, rel_path, title, content, content_hash, line_start, line_end, symbol) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (file_id, seq, path.suffix.lower().lstrip("."), rel.as_posix(),
                 title, content, content_hash, line_start, line_end, symbol),
            )
            cur.execute(
                "INSERT INTO chunks_fts(rowid, rel_path, kind, title, content) "
                "VALUES (?,?,?,?,?)",
                (cur.lastrowid, rel.as_posix(), path.suffix.lower().lstrip("."),
                 title, content),
            )
            total_chunks += 1
        n_files += 1

    for path, rel in iter_text_files(ROOT):
        emit(path, rel)
    for path, rel in iter_external_files():
        emit(path, rel)
    return n_files, total_chunks


def ingest_schemas(con: sqlite3.Connection) -> int:
    cur = con.cursor()
    n = 0
    for db in sorted(ROOT.glob("*.db")):
        if db.name == "blm_context.db":
            continue
        try:
            src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            print(f"  skip db {db.name}: {exc}", file=sys.stderr)
            continue
        try:
            rows = src.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='table' AND sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            for name, ddl in rows:
                cur.execute(
                    "INSERT OR REPLACE INTO schemas(db, table_name, ddl) VALUES (?,?,?)",
                    (db.name, name, ddl),
                )
                n += 1
        finally:
            src.close()
    return n


def ingest_git(con: sqlite3.Connection) -> int:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "log",
             "--pretty=format:%x1e%H%x1f%an%x1f%ad%x1f%s%x1f%b%x1e",
             "--date=short"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  git log unavailable: {exc}", file=sys.stderr)
        return 0
    if proc.returncode != 0:
        return 0

    cur = con.cursor()
    n = 0
    for block in proc.stdout.split("\x1e"):
        block = block.strip()
        if not block:
            continue
        fields = block.split("\x1f")
        if len(fields) < 5:
            continue
        h, author, date, subject, body = fields[0], fields[1], fields[2], fields[3], fields[4]
        cur.execute(
            "INSERT OR REPLACE INTO commits(hash, author, date, subject, body) "
            "VALUES (?,?,?,?,?)",
            (h, author, date, subject, body.strip()),
        )
        n += 1
    return n


def main() -> int:
    RAG_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript("PRAGMA journal_mode=WAL;")
    con.executescript(SCHEMA)

    # Rebuild the content-bearing tables from scratch.
    con.executescript("""
        DROP TABLE IF EXISTS chunks_fts;
        DROP TABLE IF EXISTS chunks;
        DROP TABLE IF EXISTS files;
        CREATE TABLE files (
            id       INTEGER PRIMARY KEY,
            rel_path TEXT NOT NULL UNIQUE,
            kind     TEXT NOT NULL,
            size     INTEGER NOT NULL,
            mtime    REAL NOT NULL,
            sha256   TEXT NOT NULL
        );
        CREATE TABLE chunks (
            id       INTEGER PRIMARY KEY,
            file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            seq      INTEGER NOT NULL,
            kind     TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            title    TEXT NOT NULL,
            content  TEXT NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            line_start INTEGER NOT NULL DEFAULT 0,
            line_end   INTEGER NOT NULL DEFAULT 0,
            symbol     TEXT NOT NULL DEFAULT '',
            UNIQUE(file_id, seq)
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            rel_path UNINDEXED,
            kind     UNINDEXED,
            title,
            content,
            tokenize = 'porter unicode61 remove_diacritics 2'
        );
    """)

    print("Indexing text files...")
    n_files, total_chunks = ingest_files(con)
    print(f"  {n_files} files -> {total_chunks} chunks")

    print("Indexing SQLite schemas...")
    n_schemas = ingest_schemas(con)
    print(f"  {n_schemas} tables")

    print("Indexing git history...")
    n_commits = ingest_git(con)
    print(f"  {n_commits} commits")

    con.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('built_at', datetime('now'))"
    )
    con.commit()

    print(f"\nWrote {DB_PATH}")
    print(f"  files={n_files} chunks={total_chunks} schemas={n_schemas} commits={n_commits}")
    print("Query with: python3 rag/query_context.py '<query>'")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
