"""Atomicity tests: Source + Memories + links must commit or roll back together.

The failure is injected inside the shared transaction, which is exactly the
dangerous window in the Phase 2 requirements ("Memory saved, Source failed").
"""

from __future__ import annotations

import unittest
from unittest import mock

from personal_memory import (
    Database,
    MemoryFormationService,
    MemoryQualityGate,
    MemoryRepository,
    RawInput,
)
from personal_memory.store import MemoryUnitOfWork

from .helpers import RepositoryTestCase
from .llm_fakes import mock_client, response_for

from .test_extraction import HIGH_VALUE_INPUT, memory_item, payload


class FormationAtomicityTest(RepositoryTestCase):
    def build_service(self, *items):
        return MemoryFormationService(self.repo, mock_client(*items))

    def raw_counts(self) -> dict[str, int]:
        with self.database.connection() as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
                for table in ("sources", "memories", "memory_sources")
            }

    def test_failure_while_linking_rolls_back_source_and_memories(self) -> None:
        service = self.build_service(
            response_for(payload(memories=[memory_item(), memory_item(title="第二条", content="第二条结论")]))
        )
        with mock.patch.object(MemoryUnitOfWork, "link", side_effect=RuntimeError("injected link failure")):
            with self.assertRaises(RuntimeError):
                service.process(RawInput(content=HIGH_VALUE_INPUT))

        self.assertEqual(self.raw_counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_failure_on_the_second_memory_rolls_back_the_first_one_too(self) -> None:
        service = self.build_service(
            response_for(payload(memories=[memory_item(), memory_item(title="第二条", content="第二条结论")]))
        )
        original = MemoryUnitOfWork.create_memory
        calls = {"n": 0}

        def flaky(self, memory):  # noqa: ANN001 - test double
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected failure on the second memory")
            return original(self, memory)

        with mock.patch.object(MemoryUnitOfWork, "create_memory", flaky):
            with self.assertRaises(RuntimeError):
                service.process(RawInput(content=HIGH_VALUE_INPUT))

        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.raw_counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_failure_on_the_source_rolls_back_everything(self) -> None:
        service = self.build_service(response_for(payload(memories=[memory_item()])))
        with mock.patch.object(MemoryUnitOfWork, "create_source", side_effect=RuntimeError("injected source failure")):
            with self.assertRaises(RuntimeError):
                service.process(RawInput(content=HIGH_VALUE_INPUT))
        self.assertEqual(self.raw_counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_failure_on_the_second_memory_with_the_quality_gate_rolls_back_too(self) -> None:
        """The Phase 4 gate must not weaken Phase 2 atomicity: nothing half-written."""
        service = MemoryFormationService(
            self.repo,
            mock_client(
                response_for(payload(memories=[memory_item(), memory_item(title="第二条", content="第二条结论")]))
            ),
            quality=MemoryQualityGate(self.repo),
        )
        original = MemoryUnitOfWork.create_memory
        calls = {"n": 0}

        def flaky(self, memory):  # noqa: ANN001 - test double
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected failure on the second memory (quality gate on)")
            return original(self, memory)

        with mock.patch.object(MemoryUnitOfWork, "create_memory", flaky):
            with self.assertRaises(RuntimeError):
                service.process(RawInput(content=HIGH_VALUE_INPUT))

        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.raw_counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        self.assertEqual(self.repo.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_successful_formation_commits_all_three_artifacts_together(self) -> None:
        service = self.build_service(
            response_for(
                payload(
                    memories=[
                        memory_item(title=f"结论 {index}", content=f"第 {index} 条结论") for index in range(3)
                    ]
                )
            )
        )
        outcome = service.process(RawInput(content=HIGH_VALUE_INPUT))
        self.assertEqual(outcome.status, "persisted")

        # a brand-new connection (separate Database/Repository) sees the committed unit
        fresh = MemoryRepository(Database(self.db_path))
        self.assertEqual(fresh.counts(), {"sources": 1, "memories": 3, "memory_sources": 3})
        self.assertEqual(len(fresh.get_memories_for_source(outcome.source.id)), 3)
        for memory in outcome.memories:
            self.assertEqual([s.id for s in fresh.get_sources_for_memory(memory.id)], [outcome.source.id])

    def test_database_stays_usable_after_a_rollback(self) -> None:
        failing_service = self.build_service(response_for(payload(memories=[memory_item()])))
        with mock.patch.object(MemoryUnitOfWork, "link", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                failing_service.process(RawInput(content=HIGH_VALUE_INPUT))

        healthy_service = self.build_service(response_for(payload(memories=[memory_item()])))
        outcome = healthy_service.process(RawInput(content=HIGH_VALUE_INPUT))
        self.assertEqual(outcome.status, "persisted")
        self.assertEqual(self.repo.counts(), {"sources": 1, "memories": 1, "memory_sources": 1})

    def test_no_value_input_never_opens_a_write_transaction(self) -> None:
        service = self.build_service(response_for(payload(False, "没有长期价值", [])))
        with mock.patch.object(MemoryUnitOfWork, "create_memory", side_effect=AssertionError("must not write")):
            with mock.patch.object(MemoryUnitOfWork, "create_source", side_effect=AssertionError("must not write")):
                outcome = service.process(RawInput(content="今天下午喝了一杯奶茶。"))
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(self.raw_counts(), {"sources": 0, "memories": 0, "memory_sources": 0})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
