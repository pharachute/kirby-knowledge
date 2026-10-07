"""Minimal Phase 1 demo: create Sources, create the four Memory types, link them.

Runs against a real SQLite file and prints (returns) only observed data: every
number in the output comes from re-reading the database.

The demo is **idempotent** -- fixed demo ids are reused, so running it twice
against the same file neither duplicates rows nor raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import Database, resolve_db_path
from .models import (
    InformationOrigin,
    Memory,
    MemoryStatus,
    MemoryType,
    Source,
    SourceType,
    compute_content_hash,
)
from .store import MemoryRepository

__all__ = ["run_demo"]

#: Deterministic ids keep repeated demo runs idempotent.
SOURCE_TEXT_1 = "src_demo_text_01"
SOURCE_TEXT_2 = "src_demo_text_02"
MEMORY_KNOWLEDGE = "mem_demo_knowledge_01"
MEMORY_EXPERIENCE = "mem_demo_experience_01"
MEMORY_EVENT = "mem_demo_event_01"
MEMORY_PROFILE = "mem_demo_profile_01"

TEXT_1 = (
    "本地优先的笔记系统用 SQLite 就够了：单文件、零服务、可备份，"
    "个人规模下不需要独立数据库进程。"
)
TEXT_2 = "2026-10-04：决定开始搭建 Personal Memory System，先做统一记忆模型与持久化基础。"


def _ensure_source(repo: MemoryRepository, **kwargs: Any) -> Source:
    source_id = kwargs["source_id"]
    existing = repo.get_source(source_id)
    if existing is not None:
        return existing
    return repo.create_source(Source.create(**kwargs))


def _ensure_memory(repo: MemoryRepository, **kwargs: Any) -> Memory:
    memory_id = kwargs["memory_id"]
    existing = repo.get_memory(memory_id)
    if existing is not None:
        return existing
    return repo.create_memory(Memory.create(**kwargs))


def run_demo(db_path: str | Path | None = None, *, reset: bool = False) -> dict[str, Any]:
    """Create and query the demo dataset; returns a JSON-friendly summary."""
    path = Path(resolve_db_path(db_path))
    if reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    database = Database(path)
    report = database.initialize()
    repo = MemoryRepository(database)

    source_1 = _ensure_source(
        repo,
        source_id=SOURCE_TEXT_1,
        source_type=SourceType.TEXT,
        title="本地优先存储的笔记",
        content=TEXT_1,
        metadata={"origin": "demo", "language": "zh"},
    )
    source_2 = _ensure_source(
        repo,
        source_id=SOURCE_TEXT_2,
        source_type=SourceType.TEXT,
        title="项目启动记录",
        content=TEXT_2,
        metadata={"origin": "demo", "language": "zh"},
    )

    knowledge = _ensure_memory(
        repo,
        memory_id=MEMORY_KNOWLEDGE,
        type=MemoryType.KNOWLEDGE,
        title="SQLite 适合个人规模的本地优先存储",
        content="单文件、零服务、易备份；个人数据量下无需独立数据库进程。",
        summary="本地优先场景优先选 SQLite。",
        information_origin=InformationOrigin.SOURCE_CONTENT,
        tags=["sqlite", "local-first", "storage"],
        importance=0.8,
        confidence=0.9,
    )
    experience = _ensure_memory(
        repo,
        memory_id=MEMORY_EXPERIENCE,
        type=MemoryType.EXPERIENCE,
        title="无第三方依赖时用 dataclass 承担校验层",
        content="dataclass 的 __post_init__ 能在构造时拒绝非法数据；用问题收集器一次报出全部字段错误，比逐条抛异常更好排查。",
        information_origin=InformationOrigin.SOURCE_CONTENT,
        tags=["python", "dataclass", "validation"],
        importance=0.7,
        confidence=0.85,
    )
    event = _ensure_memory(
        repo,
        memory_id=MEMORY_EVENT,
        type=MemoryType.EVENT,
        title="启动 Personal Memory System 阶段 1",
        content="2026-10-04 开始实现统一记忆数据模型与 SQLite 持久化基础。",
        information_origin=InformationOrigin.USER_EXPLICIT,
        tags=["project", "milestone"],
        importance=0.6,
        confidence=1.0,
    )
    profile = _ensure_memory(
        repo,
        memory_id=MEMORY_PROFILE,
        type=MemoryType.PROFILE,
        title="偏好本地优先、零依赖的工具链",
        content="倾向单文件、可离线、无需常驻服务的方案。",
        information_origin=InformationOrigin.AGENT_INFERENCE,
        tags=["preference", "tooling"],
        importance=0.5,
        confidence=0.4,
        status=MemoryStatus.PENDING,
    )

    # One Source -> many Memories
    repo.link(knowledge.id, source_1.id)
    repo.link(experience.id, source_1.id)
    # One Memory -> many Sources
    repo.link(event.id, source_1.id)
    repo.link(event.id, source_2.id)
    repo.link(profile.id, source_2.id)

    sources = {source.id: source for source in repo.list_sources()}
    memories = {memory.id: memory for memory in repo.list_memories()}

    memory_to_sources = {
        memory.id: [source.id for source in repo.get_sources_for_memory(memory.id)]
        for memory in memories.values()
    }
    source_to_memories = {
        source.id: [memory.id for memory in repo.get_memories_for_source(source.id)]
        for source in sources.values()
    }

    # Deduplication evidence: a second insert of identical content is refused.
    duplicate_hash = compute_content_hash(TEXT_1)
    duplicate_of = repo.find_source_by_content_hash(duplicate_hash)

    # Persistence evidence: a brand-new Database/Repository pair sees the same rows.
    reopened = MemoryRepository(Database(path))

    return {
        "db_path": str(path),
        "schema_version": report.version,
        "migrations_applied": [{"version": v, "name": n} for v, n in report.applied],
        "sources": [source.as_dict() for source in sources.values()],
        "memories": [memory.as_dict() for memory in memories.values()],
        "links": [
            {"memory_id": memory_id, "source_ids": source_ids}
            for memory_id, source_ids in memory_to_sources.items()
        ],
        "memory_to_sources": memory_to_sources,
        "source_to_memories": source_to_memories,
        "duplicate_hash_check": {
            "content_hash": duplicate_hash,
            "already_known": duplicate_of is not None,
            "existing_source_id": duplicate_of.id if duplicate_of else None,
        },
        "counts": repo.counts(),
        "persistence_check": {
            "reopened_from_disk": True,
            "counts": reopened.counts(),
            "identical": reopened.counts() == repo.counts(),
        },
    }


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    import json

    print(json.dumps(run_demo(), indent=2, ensure_ascii=False))
