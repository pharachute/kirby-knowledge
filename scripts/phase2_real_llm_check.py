"""Phase 2 real-LLM end-to-end check (opt-in; makes real provider calls).

Run it only when a real credential is configured::

    $env:DEEPSEEK_API_KEY = "<key>"          # or PERSONAL_MEMORY_LLM_API_KEY
    python scripts/phase2_real_llm_check.py --db data/phase2-real.db --reset

What it verifies, with the *actually configured* model:

    raw input -> real LLM -> value judgment -> structured Memory
              -> Memory Schema validation -> SQLite -> re-read from a new connection

Two cases are sent: a professional-knowledge input (expected: worth remembering)
and a one-off chat line (expected: not worth remembering, nothing persisted).
The API key is never printed and never written to the evidence file.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from personal_memory import (  # noqa: E402
    Database,
    LLMClient,
    MemoryFormationService,
    MemoryRepository,
    RawInput,
    load_config,
    utcnow_iso,
)
from personal_memory.prompts import PROMPT_VERSION  # noqa: E402

CASES = (
    {
        "name": "high_value_knowledge",
        "expectation": "worth_remembering = true, memories >= 1, Memory row readable",
        "title": "RAG 基础",
        "content": (
            "Retrieval-Augmented Generation（RAG）把检索与生成结合起来：先用检索器从外部语料库中取回"
            "相关片段，再让生成模型只基于这些片段作答。这样做可以降低幻觉，并让答案能够追溯到具体资料。"
        ),
    },
    {
        "name": "low_value_chat",
        "expectation": "worth_remembering = false, memories = 0, source not persisted",
        "title": None,
        "content": "今天下午喝了一杯奶茶。",
    },
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 real-LLM end-to-end check")
    parser.add_argument("--db", default="data/phase2-real.db", help="SQLite file for the check")
    parser.add_argument("--reset", action="store_true", help="delete the check database first")
    parser.add_argument("--evidence", default="docs/phase2-real-llm.json", help="where to write evidence")
    args = parser.parse_args()

    db_path = Path(args.db)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    config = load_config()  # key comes from the environment; never printed
    print(f"provider={config.provider} model={config.model} base_url={config.base_url} key=<set>")
    print(f"prompt_version={PROMPT_VERSION} db={db_path}")

    repository = MemoryRepository(Database(db_path))
    service = MemoryFormationService(repository, LLMClient(config))

    evidence = {
        "generated_at": utcnow_iso(),
        "phase": "phase-2 (memory formation)",
        "prompt_version": PROMPT_VERSION,
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "db_path": str(db_path),
        "llm_config": config.as_public_dict(),
        "cases": [],
    }

    for case in CASES:
        raw_input = RawInput(title=case["title"], content=case["content"])
        before = repository.counts()
        started = time.time()
        outcome = service.process(raw_input)
        elapsed = time.time() - started
        after = repository.counts()

        # independent re-read through a brand-new Database/Repository (new connection)
        fresh = MemoryRepository(Database(db_path))
        reread = []
        for memory in outcome.memories:
            stored = fresh.require_memory(memory.id)
            reread.append(
                {
                    "id": stored.id,
                    "type": str(stored.type),
                    "information_origin": str(stored.information_origin),
                    "status": str(stored.status),
                    "importance": stored.importance,
                    "confidence": stored.confidence,
                    "schema_version": stored.schema_version,
                    "matched_in_memory_object": stored.as_dict() == memory.as_dict(),
                }
            )

        record = {
            "name": case["name"],
            "expectation": case["expectation"],
            "input_chars": len(case["content"]),
            "elapsed_seconds": round(elapsed, 2),
            "status": outcome.status,
            "worth_remembering": outcome.worth_remembering,
            "reason": outcome.reason,
            "attempts": outcome.attempts,
            "model": outcome.model,
            "llm": dict(outcome.llm),
            "memories": [memory.as_dict() for memory in outcome.memories],
            "dropped": list(outcome.dropped),
            "source_persisted": outcome.source is not None,
            "source_reused": outcome.source_reused,
            "source_id": outcome.source.id if outcome.source else None,
            "evidence": [dict(item) for item in outcome.evidence],
            "counts_before": before,
            "counts_after": after,
            "reread_from_new_connection": reread,
        }
        evidence["cases"].append(record)

        print(
            f"\n[{case['name']}] status={outcome.status} worth={outcome.worth_remembering} "
            f"memories={len(outcome.memories)} source={'yes' if outcome.source else 'no'} "
            f"attempts={outcome.attempts} {elapsed:.1f}s"
        )
        print(f"  reason: {outcome.reason}")
        for memory in outcome.memories:
            print(
                f"  - [{memory.type}/{memory.information_origin}] {memory.title} "
                f"(importance={memory.importance:.2f} confidence={memory.confidence:.2f} "
                f"status={memory.status})"
            )
        print(f"  counts: {before} -> {after}")
        print(f"  llm: {outcome.llm}")

    evidence["counts_after_all_cases"] = repository.counts()

    # never write the credential into the evidence file
    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if config.api_key and config.api_key in payload:
        raise SystemExit("refusing to write evidence: the API key leaked into the payload")

    evidence_path = Path(args.evidence)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(payload + "\n", encoding="utf-8")

    print(f"\nfinal counts: {evidence['counts_after_all_cases']}")
    print(f"evidence written to {evidence_path} ({len(payload)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
