"""Phase 4 concurrency probe: two PROCESSES form the same content at the same time.

Verifies the documented claim that the in-transaction duplicate scan (with
``BEGIN IMMEDIATE`` holding the write lock) really prevents a second identical row
across processes -- not just inside one process.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from personal_memory import utcnow_iso  # noqa: E402

WORKER = r'''
import sys, json, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from personal_memory import Database, MemoryFormationService, MemoryQualityGate, MemoryRepository, RawInput
from tests.llm_fakes import mock_client, response_for

db_path = Path(sys.argv[2])
delay = float(sys.argv[3])
payload = {
    "worth_remembering": True,
    "reason": "concurrency probe",
    "memories": [{
        "type": "knowledge",
        "title": "并发一致性探针",
        "content": "两个进程同时形成同一内容 concurrency-probe-body。",
        "information_origin": "user_explicit",
        "requires_source": False,
    }],
}
repository = MemoryRepository(Database(db_path))
service = MemoryFormationService(
    repository, mock_client(response_for(payload)), quality=MemoryQualityGate(repository)
)
time.sleep(delay)
outcome = service.process(RawInput(content="两个进程同时形成同一内容 concurrency-probe-body。"))
print(json.dumps({"status": outcome.status, "memories": len(outcome.memories),
                  "reused": [m.id for m in outcome.reused]}))
'''

CLIENT = r'''
import sys, json
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from personal_memory import Database, MemoryRepository
repository = MemoryRepository(Database(Path(sys.argv[2])))
memories = repository.list_memories(limit=100)
print(json.dumps({"memories": len(memories), "ids": [m.id for m in memories],
                  "consistency": repository.index_consistency()}))
'''


def run_worker(db_path: pathlib.Path, delay: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", WORKER, str(PROJECT_ROOT), str(db_path), str(delay)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/_concurrency-probe.db")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--evidence", default="docs/phase4-concurrency.txt")
    args = parser.parse_args()

    results = []
    for round_index in range(args.rounds):
        db_path = pathlib.Path(f"{args.db}.{round_index}")
        for suffix in ("", "-wal", "-shm"):
            candidate = pathlib.Path(f"{db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()
        # two processes racing on the same content; the second is nudged to start first
        first = subprocess.Popen(
            [sys.executable, "-c", WORKER, str(PROJECT_ROOT), str(db_path), "0.0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        second = subprocess.Popen(
            [sys.executable, "-c", WORKER, str(PROJECT_ROOT), str(db_path), "0.05"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        out_first, err_first = first.communicate(timeout=60)
        out_second, err_second = second.communicate(timeout=60)

        check = subprocess.run(
            [sys.executable, "-c", CLIENT, str(PROJECT_ROOT), str(db_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        final = json.loads(check.stdout.strip().splitlines()[-1])
        record = {
            "round": round_index,
            "first": {"exit": first.returncode, "stdout": out_first.strip(), "stderr": err_first.strip()[-300:]},
            "second": {"exit": second.returncode, "stdout": out_second.strip(), "stderr": err_second.strip()[-300:]},
            "final": final,
            "ok": final["memories"] == 1 and final["consistency"]["consistent"],
        }
        results.append(record)
        print(json.dumps(record, ensure_ascii=True))

    verdict = all(r["ok"] for r in results)
    lines = [
        "Phase 4 concurrency probe -- two independent processes, identical content",
        f"generated_at : {utcnow_iso()}",
        f"command      : python scripts/phase4_concurrency_probe.py --rounds {args.rounds}",
        "each line is one round: both worker outcomes + the final database state",
        f"verdict      : {'PASS' if verdict else 'FAIL'} ({len(results)} round(s))",
        "=" * 100,
    ]
    lines.extend(json.dumps(record, ensure_ascii=False) for record in results)
    evidence_path = pathlib.Path(args.evidence)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for record in results:  # do not leave probe databases behind
        candidate = pathlib.Path(f"{args.db}.{record['round']}")
        for suffix in ("", "-wal", "-shm"):
            leftover = pathlib.Path(f"{candidate}{suffix}")
            if leftover.exists():
                leftover.unlink()
    print("all rounds ok:", verdict)
    print(f"evidence: {evidence_path}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
