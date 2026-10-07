"""Phase 3 laboratory: what does SQLite FTS5 actually do with English and Chinese?

Standalone experiment (no project code): builds three FTS5 tables with different
tokenizers over the same four columns (title, content, summary, tags), plus a
LIKE baseline, and measures:

  1. English / acronym queries           (RAG, SQLite, Agent)
  2. Chinese multi-character queries     (长期记忆, 本地存储)
  3. Chinese short queries               (记忆, 存储)
  4. field coverage                      (title / content / summary / tags)
  5. ranking (bm25 direction and ordering)
  6. precision                          (does "RAG" also match "storage"?)
  7. hostile query characters           (", ', *, :, (, ), AND, OR)

Output: JSON + a readable table on stdout.  Run:
    python scripts/phase3_fts5_lab.py --out docs/phase3-fts5-lab.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

MEMORIES = [
    {
        "id": "mem_1",
        "title": "RAG 的基本原理",
        "content": "RAG 通过检索外部知识为模型提供上下文。",
        "summary": "检索增强生成把检索与生成结合起来。",
        "tags": "rag retrieval augmented",
    },
    {
        "id": "mem_2",
        "title": "Agent Memory",
        "content": "长期记忆可以帮助 Agent 保留用户信息。",
        "summary": "Agent 需要长期记忆来维持上下文。",
        "tags": "agent memory",
    },
    {
        "id": "mem_3",
        "title": "SQLite 本地存储",
        "content": "SQLite 适合个人规模的本地应用。",
        "summary": "单文件、零服务的本地数据库。",
        "tags": "sqlite storage local-first",
    },
    # ranking / precision probes
    {
        "id": "mem_4",
        "title": "顺便一提",
        "content": "这里顺便提到 RAG，但没有展开。",
        "summary": None,
        "tags": "",
    },
    {
        "id": "mem_5",
        "title": "对象存储笔记",
        "content": "对象存储（object storage）与本地存储的区别。",
        "summary": None,
        "tags": "storage",
    },
    {
        "id": "mem_6",
        "title": "标签探针",
        "content": "这条记忆的关键词只写在标签里。",
        "summary": None,
        "tags": "RAG",
    },
    {
        "id": "mem_7",
        "title": "摘要探针",
        "content": "正文里没有出现目标词。",
        "summary": "摘要里提到长期记忆的维护成本。",
        "tags": "",
    },
]

TOKENIZERS = ("unicode61", "trigram", "porter")

QUERIES = [
    ("english_upper", "RAG"),
    ("english_lower", "rag"),
    ("english_word", "SQLite"),
    ("english_word2", "Agent"),
    ("english_prefix", "retriev"),
    ("cjk_4char", "长期记忆"),
    ("cjk_2char", "记忆"),
    ("cjk_multi", "本地存储"),
    ("cjk_phrase2", "存储"),
    ("mixed", "RAG 原理"),
    ("field_summary", "维护成本"),
    ("field_tags", "local-first"),
    ("hostile_quote", '"'),
    ("hostile_apostrophe", "'"),
    ("hostile_star", "*"),
    ("hostile_colon", ":"),
    ("hostile_paren", "("),
    ("hostile_and", "AND"),
    ("hostile_or", "OR"),
]


def build(db_path: Path) -> sqlite3.Connection:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, title TEXT, content TEXT, summary TEXT, tags TEXT)")
    for tokenizer in TOKENIZERS:
        conn.execute(
            f"CREATE VIRTUAL TABLE fts_{tokenizer} USING fts5("
            "memory_id UNINDEXED, title, content, summary, tags, "
            f"tokenize='{tokenizer}')"
        )
    for row in MEMORIES:
        conn.execute(
            "INSERT INTO memories (id, title, content, summary, tags) VALUES (?, ?, ?, ?, ?)",
            (row["id"], row["title"], row["content"], row["summary"], row["tags"]),
        )
        for tokenizer in TOKENIZERS:
            conn.execute(
                f"INSERT INTO fts_{tokenizer} (memory_id, title, content, summary, tags) VALUES (?, ?, ?, ?, ?)",
                (row["id"], row["title"], row["content"], row["summary"] or "", row["tags"]),
            )
    conn.commit()
    return conn


def fts_query(conn: sqlite3.Connection, tokenizer: str, match: str, limit: int = 5) -> dict:
    """Run one MATCH and report ids + raw bm25 (SQLite: more negative = better)."""
    try:
        rows = conn.execute(
            f"SELECT memory_id, bm25(fts_{tokenizer}) AS bm25, rank FROM fts_{tokenizer} "
            f"WHERE fts_{tokenizer} MATCH ? ORDER BY bm25 LIMIT ?",
            (match, limit),
        ).fetchall()
        return {
            "ok": True,
            "match_expression": match,
            "ids": [r["memory_id"] for r in rows],
            "bm25": [round(r["bm25"], 4) for r in rows],
            "rank": [round(r["rank"], 4) for r in rows],
        }
    except sqlite3.Error as exc:
        return {"ok": False, "match_expression": match, "error": f"{type(exc).__name__}: {exc}"}


def like_query(conn: sqlite3.Connection, needle: str, limit: int = 5) -> dict:
    pattern = f"%{needle}%"
    rows = conn.execute(
        "SELECT id, title, content, summary, tags FROM memories "
        "WHERE title LIKE ? OR content LIKE ? OR COALESCE(summary,'') LIKE ? OR tags LIKE ? "
        "ORDER BY id LIMIT ?",
        (pattern, pattern, pattern, pattern, limit),
    ).fetchall()
    return {"ok": True, "pattern": pattern, "ids": [r["id"] for r in rows]}


def quote_token(token: str) -> str:
    """The safe way to put arbitrary user text into an FTS5 MATCH expression."""
    return '"' + token.replace('"', '""') + '"'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/phase3-fts5-lab.json")
    parser.add_argument("--db", default="data/phase3-fts5-lab.db")
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = build(db_path)

    report: dict = {
        "sqlite_version": sqlite3.sqlite_version,
        "fts5_compiled": bool(
            conn.execute("SELECT 1 FROM pragma_compile_options WHERE compile_options LIKE '%FTS5%'").fetchone()
        ),
        "tokenizers": list(TOKENIZERS),
        "dataset": MEMORIES,
        "queries": [],
    }

    for label, query in QUERIES:
        entry = {"label": label, "query": query, "results": {}}
        for tokenizer in TOKENIZERS:
            entry["results"][tokenizer] = {
                "raw": fts_query(conn, tokenizer, query),
                "quoted": fts_query(conn, tokenizer, quote_token(query)),
            }
        entry["results"]["like"] = like_query(conn, query)
        report["queries"].append(entry)

    # multi-token query: OR of quoted tokens (the expression the project will build)
    entry = {"label": "multi_token_or", "query": "RAG 原理", "results": {}}
    expression = " OR ".join(quote_token(t) for t in "RAG 原理".split())
    for tokenizer in TOKENIZERS:
        entry["results"][tokenizer] = {"quoted_or": fts_query(conn, tokenizer, expression)}
    report["queries"].append(entry)

    # bm25 direction check on a query with a clear best match
    report["bm25_direction"] = {
        "note": "SQLite bm25(): more negative = better match; ascending order puts the best first",
        "probe": {
            tokenizer: fts_query(conn, tokenizer, quote_token("RAG")) for tokenizer in TOKENIZERS
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # readable summary
    print(f"SQLite {report['sqlite_version']}  | FTS5 compiled: {report['fts5_compiled']}")
    print(f"{'query':<18}{'tokenizer':<12}{'ids':<24}bm25")
    for entry in report["queries"]:
        for name in ("unicode61", "trigram", "porter"):
            block = entry["results"].get(name, {})
            raw = block.get("raw") or block.get("quoted_or")
            if raw is None:
                continue
            if raw["ok"]:
                print(f"{entry['query'][:16]:<18}{name:<12}{','.join(raw['ids'])[:22]:<24}{raw['bm25']}")
            else:
                print(f"{entry['query'][:16]:<18}{name:<12}ERROR: {raw['error'][:60]}")
        like = entry["results"].get("like")
        if like:
            print(f"{entry['query'][:16]:<18}{'LIKE':<12}{','.join(like['ids'])[:22]}")
        trigram_block = entry["results"].get("trigram", {})
        if "quoted_or" in trigram_block:
            print(f"{entry['query'][:16]:<18}{'OR(trigram)':<12}{trigram_block['quoted_or']['ids']}")
        print()
    print(f"evidence -> {out_path}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
