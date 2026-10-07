"""Phase 4 conflict-safety audit probe: does a conflict ever UPDATE/DELETE the old Memory?

Uses **raw SQLite audit triggers** (created outside the package) so the answer does not
depend on the package's own bookkeeping: any UPDATE or DELETE on ``memories`` while the
quality gate runs is recorded, and the audit must stay empty.  This is the strongest
available evidence for the Phase 4 rule "新信息 ≠ 自动覆盖旧信息".

Run::

    python scripts/phase4_conflict_audit_probe.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from personal_memory import (  # noqa: E402
    Database,
    InformationOrigin,
    Memory,
    MemoryFormationService,
    MemoryQualityGate,
    MemoryRepository,
    MemoryType,
    RawInput,
    utcnow_iso,
)
from personal_memory.quality import LLMConflictClassifier  # noqa: E402
from tests.llm_fakes import mock_client, response_for  # noqa: E402


def run(db_path: pathlib.Path) -> dict:
    for suffix in ("", "-wal", "-shm"):
        candidate = pathlib.Path(f"{db_path}{suffix}")
        if candidate.exists():
            candidate.unlink()

    repository = MemoryRepository(Database(db_path))
    old = repository.create_memory(
        Memory.create(
            type=MemoryType.PROFILE,
            title="用户偏好的数据库方案",
            content="用户喜欢 A 方案，把它用于个人知识库。",
            information_origin=InformationOrigin.USER_EXPLICIT,
        )
    )

    # audit triggers, created with raw SQL outside the package
    with Database(db_path).transaction() as conn:
        conn.execute(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, op TEXT NOT NULL, memory_id TEXT NOT NULL, at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TRIGGER audit_memories_update AFTER UPDATE ON memories "
            "BEGIN INSERT INTO audit_log (op, memory_id, at) VALUES ('update', old.id, datetime('now')); END"
        )
        conn.execute(
            "CREATE TRIGGER audit_memories_delete AFTER DELETE ON memories "
            "BEGIN INSERT INTO audit_log (op, memory_id, at) VALUES ('delete', old.id, datetime('now')); END"
        )

    payload = {
        "worth_remembering": True,
        "reason": "probe",
        "memories": [
            {
                "type": "profile",
                "title": "用户偏好的数据库方案",
                "content": "用户更喜欢 B 方案，把它用于个人知识库。",
                "information_origin": "user_explicit",
                "requires_source": False,
            }
        ],
    }
    gate = MemoryQualityGate(
        repository,
        classifier=LLMConflictClassifier(
            mock_client(response_for({"relation": "conflict", "reason": "audit probe"}))
        ),
    )
    outcome = MemoryFormationService(
        repository, mock_client(response_for(payload)), quality=gate
    ).process(RawInput(content="用户更喜欢 B 方案，把它用于个人知识库。"))

    with Database(db_path).connection() as conn:
        audit = [dict(row) for row in conn.execute("SELECT op, memory_id FROM audit_log").fetchall()]
    stored = repository.require_memory(old.id)
    new_memory = repository.require_memory(outcome.memories[0].id)

    return {
        "old_memory_id": old.id,
        "new_memory_id": new_memory.id,
        "outcome_status": outcome.status,
        "relation": outcome.quality.decisions[0].relation,
        "new_memory_status": str(new_memory.status),
        "old_memory_unchanged": stored.as_dict() == old.as_dict(),
        "update_or_delete_statements_on_memories": audit,
        "memories_after": repository.counts()["memories"],
        "index_consistent": repository.index_consistency()["consistent"],
        "ok": audit == [] and stored.as_dict() == old.as_dict() and str(new_memory.status) == "pending",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 conflict-safety audit probe")
    parser.add_argument("--db", default="data/_conflict-audit-probe.db")
    parser.add_argument("--evidence", default="docs/phase4-conflict-audit.txt")
    args = parser.parse_args()

    db_path = pathlib.Path(args.db)
    report = run(db_path)
    for suffix in ("", "-wal", "-shm"):
        candidate = pathlib.Path(f"{db_path}{suffix}")
        if candidate.exists():
            candidate.unlink()

    lines = [
        "Phase 4 conflict-safety audit -- raw SQLite triggers, Mock LLM (no network)",
        f"generated_at : {utcnow_iso()}",
        "command      : python scripts/phase4_conflict_audit_probe.py",
        "",
        "Setup: one active profile Memory (用户喜欢 A 方案) + AFTER UPDATE / AFTER DELETE",
        "audit triggers on `memories` created with raw SQL.  A formation whose candidate is",
        "classified `conflict` then runs through the real quality gate + real repository path.",
        f"Verdict: {'PASS' if report['ok'] else 'FAIL'}",
        "",
        json.dumps(report, ensure_ascii=False, indent=2),
        "",
    ]
    evidence_path = pathlib.Path(args.evidence)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"evidence: {evidence_path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
