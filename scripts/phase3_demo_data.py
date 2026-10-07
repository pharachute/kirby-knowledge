"""Seed a small Phase 3 demo database (the spec's sample memories + their Sources).

Reproducible CLI acceptance input::

    python scripts/phase3_demo_data.py --db data/phase3-demo.db --reset
    python -m personal_memory search "RAG" --db data/phase3-demo.db

No LLM, no network.  Fixed ids keep repeated runs idempotent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from personal_memory import Database, Memory, MemoryRepository, Source  # noqa: E402

MEMORIES = (
    {
        "memory_id": "mem_demo_rag",
        "type": "knowledge",
        "title": "RAG 的基本原理",
        "content": "RAG 通过检索外部知识为模型提供上下文，从而降低幻觉。",
        "summary": "检索增强生成把检索与生成结合起来。",
        "tags": ["rag", "retrieval"],
        "information_origin": "source_content",
        "importance": 0.8,
        "confidence": 0.9,
        "sources": ("src_demo_rag_intro", "src_demo_chat"),
    },
    {
        "memory_id": "mem_demo_agent",
        "type": "knowledge",
        "title": "Agent Memory",
        "content": "长期记忆可以帮助 Agent 保留用户信息，维持跨会话的上下文。",
        "summary": None,
        "tags": ["agent", "memory"],
        "information_origin": "source_content",
        "importance": 0.7,
        "confidence": 0.85,
        "sources": ("src_demo_chat",),
    },
    {
        "memory_id": "mem_demo_sqlite",
        "type": "knowledge",
        "title": "SQLite 本地存储",
        "content": "SQLite 适合个人规模的本地应用，单文件、零服务、易备份。",
        "summary": None,
        "tags": ["sqlite", "local-first"],
        "information_origin": "user_explicit",
        "importance": 0.6,
        "confidence": 0.95,
        "sources": (),
    },
)

SOURCES = (
    {
        "source_id": "src_demo_rag_intro",
        "title": "《RAG 入门》",
        "content": "RAG 是 Retrieval-Augmented Generation 的缩写：先检索，再生成。",
    },
    {
        "source_id": "src_demo_chat",
        "title": "某次聊天记录",
        "content": "用户提到：我想让 Agent 记住我说过的话，长期记忆很重要。",
    },
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/phase3-demo.db")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    repository = MemoryRepository(Database(db_path))

    sources = {}
    for spec in SOURCES:
        existing = repository.get_source(spec["source_id"])
        sources[spec["source_id"]] = existing or repository.create_source(
            Source.create(
                source_type="text",
                title=spec["title"],
                content=spec["content"],
                source_id=spec["source_id"],
                metadata={"origin": "phase3-demo"},
            )
        )

    memories = []
    for spec in MEMORIES:
        existing = repository.get_memory(spec["memory_id"])
        memory = existing or repository.create_memory(
            Memory.create(
                type=spec["type"],
                title=spec["title"],
                content=spec["content"],
                summary=spec["summary"],
                tags=spec["tags"],
                importance=spec["importance"],
                confidence=spec["confidence"],
                information_origin=spec["information_origin"],
                memory_id=spec["memory_id"],
            )
        )
        memories.append(memory)
        for source_id in spec["sources"]:
            repository.link(memory.id, sources[source_id].id)

    print(f"db: {db_path}")
    print(f"counts: {repository.counts()}")
    print(f"index rows: word={repository.index_row_count('word')} trigram={repository.index_row_count('trigram')}")
    for memory in memories:
        linked = [source.title for source in repository.get_sources_for_memory(memory.id)]
        print(f"  {memory.id}  {memory.title}  sources={linked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
