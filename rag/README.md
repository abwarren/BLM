# BLM Persistent Context Index (RAG)

A dependency-free, persistent search index over the whole BLM repository.
It lets an agent (or human) retrieve relevant code, docs, DB schemas, and
git history without reading every file each session.

## Files

| File | Purpose |
|------|---------|
| `build_context.py` | Rebuild `blm_context.db` from the repo |
| `query_context.py` | Query the index (code/docs/schemas/commits) |
| `blm_context.db` | SQLite FTS5 index (generated, do not edit) |

## Quick start

```bash
cd /home/ubuntu/BLM

# Rebuild from current repo state
python3 rag/build_context.py

# Search code + docs (BM25 ranked)
python3 rag/query_context.py "under alert trigger condition"

# Search only Python code
python3 rag/query_context.py --kind py "freeze detector"

# Search only docs
python3 rag/query_context.py --kind md "stability gate"

# Restrict to a path substring
python3 rag/query_context.py --path scorecard "settlement"

# Search SQLite schemas
python3 rag/query_context.py --schemas "checkpoint"

# Search git history
python3 rag/query_context.py --commits "temporal freshness"

# Control result count
python3 rag/query_context.py --top 20 "pace projection"
```

## What is indexed

- **Text files** — `.md`, `.txt`, `.py`, `.json`, `.jsonl`, `.csv`, `.html`,
  `.js`, `.css`, `.yaml`, `.yml`, `.sh`, `.sql`, etc.
- **Python** — chunked by top-level module/class/function where the file parses
  as valid Python, so a match points to the containing symbol.
- **Markdown** — chunked by `#`…`####` headings, then by size.
- **SQLite schemas** — table DDL from every `*.db` in the repo root.
- **Git history** — author, date, subject, and body of every commit.

The following are deliberately excluded: `.git`, `venv`, `__pycache__`,
`.pytest_cache`, `node_modules`, `backups`, `rag/` itself, and files over 8 MB.

## How it works

- `blm_context.db` uses SQLite FTS5 with the `porter unicode61` tokenizer and
  BM25 ranking (`bm25(chunks_fts)`), plus `snippet()` for highlighted excerpts.
- Rebuilding is idempotent: the `files`/`chunks`/`chunks_fts` tables are
  dropped and repopulated from the current repository.
- No vector embeddings or external models are required; retrieval is lexical
  and fully reproducible on any Python 3 build with FTS5.

## Rebuild policy

The index is a snapshot. Rebuild it when the repository changes materially:

```bash
python3 rag/build_context.py
```

Or rebuild-and-query in one step:

```bash
python3 rag/query_context.py --rebuild "some query"
```
