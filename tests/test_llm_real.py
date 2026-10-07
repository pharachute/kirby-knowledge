"""Real-LLM smoke test (opt-in).

Skipped unless a real credential is configured, so the default suite stays
offline and free.  Run it explicitly with::

    $env:PERSONAL_MEMORY_LLM_API_KEY = "<key>"       # or DEEPSEEK_API_KEY
    python -m unittest tests.test_llm_real -v

It makes exactly ONE provider call and asserts the full chain: raw input ->
real LLM -> value judgment -> structured Memory -> Phase 1 validation -> SQLite.
"""

from __future__ import annotations

import os
import unittest

from personal_memory import (
    Database,
    InformationOrigin,
    LLMClient,
    Memory,
    MemoryFormationService,
    MemoryRepository,
    RawInput,
    load_config,
)

from .helpers import RepositoryTestCase

REAL_CREDENTIAL_PRESENT = bool(
    os.environ.get("PERSONAL_MEMORY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
)

KNOWLEDGE_INPUT = (
    "Retrieval-Augmented Generation（RAG）把检索与生成结合：先用检索器从外部语料库取回相关片段，"
    "再让生成模型只基于这些片段作答。这样做可以降低幻觉，并让答案可以追溯到具体资料。"
)


@unittest.skipUnless(
    REAL_CREDENTIAL_PRESENT,
    "real LLM credentials not configured (set PERSONAL_MEMORY_LLM_API_KEY or DEEPSEEK_API_KEY)",
)
class RealLLMEndToEndTest(RepositoryTestCase):
    def test_real_model_forms_a_valid_memory_end_to_end(self) -> None:
        config = load_config()
        service = MemoryFormationService(self.repo, LLMClient(config))

        outcome = service.process(RawInput(title="RAG 基础", content=KNOWLEDGE_INPUT))

        self.assertTrue(outcome.worth_remembering, f"model judged it worthless: {outcome.reason}")
        self.assertEqual(outcome.status, "persisted")
        self.assertGreaterEqual(len(outcome.memories), 1)

        memory = outcome.memories[0]
        self.assertIn(memory.type.value, {"knowledge", "experience", "event", "profile"})
        self.assertIn(
            memory.information_origin.value,
            {"user_explicit", "source_content", "agent_inference"},
        )
        self.assertGreaterEqual(memory.importance, 0.0)
        self.assertLessEqual(memory.importance, 1.0)
        self.assertGreaterEqual(memory.confidence, 0.0)
        self.assertLessEqual(memory.confidence, 1.0)

        # persisted and readable through a brand-new connection
        fresh = MemoryRepository(Database(self.db_path))
        reloaded = fresh.require_memory(memory.id)
        self.assertIsInstance(reloaded, Memory)
        self.assertEqual(reloaded.content, memory.content)
        self.assertGreaterEqual(fresh.counts()["memories"], 1)

        # no key material may leak into the outcome payload
        self.assertNotIn(config.api_key, str(outcome.as_dict()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
