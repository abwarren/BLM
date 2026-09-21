# BLM RAG/MEMORY SYSTEM — FROZEN FOUNDATION

This manifest freezes the foundation of the BLM RAG/memory system. It is
the authoritative baseline for detecting drift in the memory tooling itself.

**Frozen at:** 2026-09-20 19:45 UTC
**Repository HEAD:** 76dba4abc0d486fbf58645da0026e026e6cdb583
**Branch:** handoff-2026-09-07

**Status:** The existing persistent RAG infrastructure is accepted as the
baseline. Do not redesign it unless a demonstrated defect requires it
(§6g.1).

## System components

| Component | Path | Purpose |
|-----------|------|---------|
| RAG index builder | `/home/ubuntu/BLM/rag/build_context.py` | Rebuild `blm_context.db` |
| RAG query tool | `/home/ubuntu/BLM/rag/query_context.py` | Query + `--store` retrieval |
| RAG index DB | `/home/ubuntu/BLM/rag/blm_context.db` | SQLite FTS5 index |
| RAG README | `/home/ubuntu/BLM/rag/README.md` | Usage docs |
| Knowledge store CLI | `/home/ubuntu/.clai/kb.py` | Persistent KB CRUD/search |
| Knowledge store DB | `/home/ubuntu/.clai/knowledge.db` | SQLite + FTS5 entries |
| Decisions/memory | `/home/ubuntu/BLM/DECISIONS.md` | Durable project memory |

## Baseline counts

| Store | Metric | Value |
|-------|--------|-------|
| RAG | files | 358 |
| RAG | chunks | 8,728 |
| RAG | schemas | 36 |
| RAG | commits | 106 |
| RAG | built_at | 2026-09-20 19:45:14 |
| KB | entries | 15 |
| KB | journal_mode | wal |

## Frozen schemas

**RAG `chunks`:**
`id, file_id, seq, kind, rel_path, title, content, content_hash, line_start, line_end, symbol`

**KB `entries`:**
`id, title, content, tags, source, created_at, updated_at, key, file_path, line_start, line_end, symbol, commit_hash, commit_author, commit_date, commit_subject, content_hash`

## Frozen file hashes (SHA-256)

```
17a6defd2f03cef69dab10812b6126ca4a8c368be81bc11ae22db41fee70d10c  rag/build_context.py
0e1b74ed04e868216142558a10a4ae094538ed7099d3b2b293b651439b02e012  rag/query_context.py
a5d44da9c7df4ada93e2f4ed9c69eeef011cc8ad72bd99aa553e06e5be65ed17  rag/blm_context.db
1861dae654395cc056bbc0a0b232ef8e5bcfa73c072b2e96ceb03fa77355fa6c  rag/README.md
1f9dc1fdf63263c5a340816ed9ae87eaaf2b71494c0b6661ac8559b6d530d4bc  /home/ubuntu/.clai/kb.py
93be7105dcc00b318b26608a94d6f2c33f5e291c10d1ce41d22409d4f1833101  /home/ubuntu/.clai/knowledge.db
2b65424e7d02d74c03f1dea8703f7ea58371b905c125b5074a08efd41d2bf9ae  DECISIONS.md
```

Note: `rag/blm_context.db` is a generated index. Its hash changes on every
rebuild (a `built_at` timestamp is stored inside), so its hash is a
**freshness indicator**, not a stable identity. The other hashes above are
stable unless the tooling or memory is intentionally changed.

## Operating rules (durable, in DECISIONS.md)

- §6a Search BOTH stores before changes.
- §6b Memory is a guide, not ground truth; repo code is authoritative.
  If memory conflicts with code, REPORT the conflict and trust verified code.
- §6c Propose durable facts for memory.
- §6d Update memory when code/architecture/behavior changes.
- §6e NEVER store secrets in memory.
- §6f Session startup protocol.

## Freeze semantics

- This manifest is the **baseline** for the memory tooling itself.
- The RAG index and KB are **rebuildable/appendable**; `DECISIONS.md` is
  **human-authored memory**. All three are subject to the operating rules.
- If a component hash or schema changes, that is expected only when the
  tooling is intentionally updated — record the new baseline here.
- Repository code remains authoritative over everything in this system.
