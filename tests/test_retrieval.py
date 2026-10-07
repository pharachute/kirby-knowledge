"""Phase 3 retrieval behaviour tests (the 26 behaviours required by the spec).

Numbering in the test docstrings maps to the requirement list so the coverage is
auditable.  Nothing here calls a model or touches the network.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from personal_memory import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    STATUS_ALL,
    Database,
    Memory,
    MemoryRepository,
    MemoryRetriever,
    MemoryStatus,
    MemoryType,
    RetrievalError,
    ValidationError,
    classify_token,
    tokenize,
)
from personal_memory.cli import main
from personal_memory.retrieval import build_match_expression, is_searchable_token

from .helpers import RepositoryTestCase


class RetrievalTestCase(RepositoryTestCase):
    """A small, realistic Chinese/English dataset shared by the behaviour tests."""

    def setUp(self) -> None:
        super().setUp()
        self.retriever = MemoryRetriever(self.repo)

        self.source_rag = self.make_source(title="RAG 入门", content="RAG 把检索与生成结合起来。")
        self.source_chat = self.make_source(title="某次聊天记录", content="我们聊到了 RAG 的做法。")

        # 1: title + content + tags
        self.memory_rag = self.make_memory(
            type=MemoryType.KNOWLEDGE,
            title="RAG 的基本原理",
            content="RAG 通过检索外部知识为模型提供上下文。",
            summary="检索增强生成把检索与生成结合。",
            tags=["rag", "retrieval"],
            information_origin="source_content",
        )
        self.repo.link(self.memory_rag.id, self.source_rag.id)
        self.repo.link(self.memory_rag.id, self.source_chat.id)  # 1 memory -> 2 sources

        # 2: Chinese content
        self.memory_agent = self.make_memory(
            type=MemoryType.KNOWLEDGE,
            title="Agent Memory",
            content="长期记忆可以帮助 Agent 保留用户信息。",
            information_origin="source_content",
        )

        # 3: SQLite
        self.memory_sqlite = self.make_memory(
            type=MemoryType.KNOWLEDGE,
            title="SQLite 本地存储",
            content="SQLite 适合个人规模的本地应用。",
            information_origin="source_content",
        )
        # the source above is shared by two memories: 1 source -> 2 memories
        self.repo.link(self.memory_sqlite.id, self.source_chat.id)

        # 4: summary-only match
        self.memory_summary = self.make_memory(
            type=MemoryType.EVENT,
            title="维护成本记录",
            content="正文里没有出现目标关键词。",
            summary="摘要里提到长期记忆的维护成本。",
            information_origin="user_explicit",
        )

        # 5: tags-only match
        self.memory_tags = self.make_memory(
            type=MemoryType.PROFILE,
            title="技术偏好",
            content="偏好本地优先的方案。",
            tags=["sqlite", "local-first"],
            information_origin="user_explicit",
        )

    def titles(self, query: str, **kwargs) -> list[str]:
        return [hit.memory.title for hit in self.retriever.search(query, **kwargs).hits]

    def make_pending(self, memory):
        """Rebuild an active fixture as a *created* pending Memory.

        Phase 4's lifecycle refuses ``active -> pending`` (now also at the repository
        level), so a fixture that needs a pending Memory creates one instead of
        updating an active Memory into that state.
        """
        self.repo.delete_memory(memory.id)
        return self.make_memory(
            type=memory.type,
            title=memory.title,
            content=memory.content,
            summary=memory.summary,
            tags=list(memory.tags),
            importance=memory.importance,
            confidence=memory.confidence,
            information_origin=memory.information_origin,
            status=MemoryStatus.PENDING,
            created_at=memory.created_at,
        )


class BasicSearchTest(RetrievalTestCase):
    def test_2_english_query(self) -> None:
        """(2) English / acronym query."""
        result = self.retriever.search("RAG")
        self.assertEqual(result.total, 1)
        self.assertEqual(result.hits[0].memory.id, self.memory_rag.id)
        self.assertGreater(result.hits[0].score, -1.0)
        self.assertEqual(result.hits[0].score_kind, "bm25")

    def test_2b_english_query_is_case_insensitive(self) -> None:
        self.assertEqual(self.titles("rag"), self.titles("RAG"))
        self.assertEqual(self.titles("agent"), ["Agent Memory"])

    def test_3_chinese_query(self) -> None:
        """(3) Chinese multi-character query."""
        self.assertEqual(set(self.titles("长期记忆")), {"Agent Memory", "维护成本记录"})
        self.assertEqual(self.titles("本地存储"), ["SQLite 本地存储"])

    def test_4_chinese_short_query_uses_the_like_fallback(self) -> None:
        """(4) Two-character Chinese cannot be served by FTS5 -> LIKE fallback."""
        result = self.retriever.search("记忆")
        self.assertEqual(result.mode, "like")
        self.assertEqual(result.hits[0].score_kind, "like")
        # both memories whose text contains 记忆, and nothing else
        self.assertEqual(set(self.titles("记忆")), {"Agent Memory", "维护成本记录"})

    def test_4b_latin_tokens_use_word_prefix_not_substring(self) -> None:
        """Latin never uses substring LIKE: ``OR`` must not match ``Memory``."""
        probe = self.make_memory(title="单字母探针", content="变量 x 的单独说明。")
        self.assertEqual([hit.memory.id for hit in self.retriever.search("x").hits], [probe.id])
        self.assertEqual(self.retriever.search("OR").total, 0)
        self.assertEqual(self.retriever.search("or").total, 0)

    def test_mixed_chinese_and_english_query(self) -> None:
        self.assertEqual(set(self.titles("RAG 长期记忆")), {"RAG 的基本原理", "Agent Memory", "维护成本记录"})

    def test_5_search_title(self) -> None:
        """(5) title is searchable."""
        self.assertEqual(self.titles("基本原理"), ["RAG 的基本原理"])

    def test_6_search_content(self) -> None:
        """(6) content is searchable."""
        self.assertEqual(self.titles("检索外部知识"), ["RAG 的基本原理"])

    def test_7_search_summary(self) -> None:
        """(7) summary is searchable even when the body is not."""
        self.assertEqual(self.titles("维护成本"), ["维护成本记录"])

    def test_8_search_tags(self) -> None:
        """(8) tags are searchable (joined, without JSON syntax)."""
        self.assertEqual(self.titles("local-first"), ["技术偏好"])

    def test_13_no_match_returns_empty_result(self) -> None:
        result = self.retriever.search("完全不存在的词xyz")
        self.assertEqual(result.total, 0)
        self.assertEqual(result.hits, ())

    def test_result_exposes_score_semantics_and_is_json_serialisable(self) -> None:
        result = self.retriever.search("RAG")
        payload = json.loads(json.dumps(result.as_dict()))
        self.assertIn("score_direction", payload)
        self.assertTrue(payload["score_direction"].startswith("higher is more relevant"))
        self.assertEqual(payload["hits"][0]["score_kind"], "bm25")
        self.assertIn("bm25", payload["hits"][0])
        # raw bm25 and the index that produced the hit are reported (not null)
        self.assertIsNotNone(payload["hits"][0]["bm25"])
        self.assertIn(payload["hits"][0]["index"], ("word", "trigram"))


    def test_matched_fields_are_reported_per_field(self) -> None:
        self.assertIn("title", self.retriever.search("基本原理").hits[0].matched_fields)
        summary_hit = next(
            hit for hit in self.retriever.search("长期记忆", status="all").hits
            if hit.memory.id == self.memory_summary.id
        )
        self.assertEqual(summary_hit.matched_fields, ("summary",))
        self.assertEqual(self.retriever.search("local-first").hits[0].matched_fields, ("tags",))
        self.assertIn("content", self.retriever.search("检索外部知识").hits[0].matched_fields)


    def test_raw_bm25_and_index_are_reported_per_hit(self) -> None:
        """Regression: the raw bm25 value and index name must reach the hit."""
        hit = self.retriever.search("RAG").hits[0]
        self.assertEqual(hit.score_kind, "bm25")
        self.assertIsNotNone(hit.bm25)
        self.assertEqual(hit.index, "word")
        self.assertAlmostEqual(hit.score, -hit.bm25)
        self.assertIn("bm25", hit.as_dict())
        self.assertEqual(hit.as_dict()["index"], "word")

        like_hit = self.retriever.search("记忆").hits[0]
        self.assertEqual(like_hit.score_kind, "like")
        self.assertIsNone(like_hit.bm25)
        self.assertIsNone(like_hit.index)

    def test_like_fallback_does_not_match_json_punctuation_in_tags(self) -> None:
        """Regression: tags are matched through json_each, not the raw tags_json text."""
        self.make_memory(
            type="knowledge",
            title="标签记忆探针",
            content="正文与标签都用于检索验证。",
            tags=["记忆"],
        )
        self.assertEqual(self.retriever.search("记忆").total >= 1, True)
        self.assertEqual(self.retriever.search('忆"').total, 0)  # JSON punctuation is not content

    def test_control_characters_in_query_are_rejected(self) -> None:
        """Regression: a NUL byte used to surface as a raw FTS error."""
        for query in ("\x00abc", "RAG\x07"):
            with self.subTest(query=query), self.assertRaises(ValidationError) as ctx:
                self.retriever.search(query)
            self.assertEqual(ctx.exception.fields, ("query",))


class FilterTest(RetrievalTestCase):
    def test_9_default_status_is_active_only(self) -> None:
        """(9) archived memories never show up in a default query."""
        self.repo.update_memory(self.memory_sqlite.id, status=MemoryStatus.ARCHIVED)
        self.assertNotIn("SQLite 本地存储", self.titles("SQLite"))
        self.memory_summary = self.make_pending(self.memory_summary)
        self.assertNotIn("维护成本记录", self.titles("维护成本"))

    def test_10_explicit_status_filters(self) -> None:
        """(10) status=active|pending|archived|all."""
        self.repo.update_memory(self.memory_sqlite.id, status="archived")
        self.memory_summary = self.make_pending(self.memory_summary)

        self.assertEqual(self.titles("SQLite", status="archived"), ["SQLite 本地存储"])
        self.assertEqual(self.titles("SQLite", status="pending"), [])
        self.assertEqual(self.titles("维护成本", status="pending"), ["维护成本记录"])
        self.assertEqual(self.titles("维护成本", status="active"), [])
        self.assertEqual(
            set(self.titles("SQLite", status="all")), {"SQLite 本地存储", "技术偏好"}
        )
        self.assertEqual(
            set(self.titles("维护成本", status="all")), {"维护成本记录"}
        )
        self.assertEqual(self.titles("SQLite", status=[MemoryStatus.ARCHIVED]), ["SQLite 本地存储"])
        self.assertEqual(
            set(self.titles("SQLite", status=[MemoryStatus.ARCHIVED, MemoryStatus.ACTIVE])),
            {"SQLite 本地存储", "技术偏好"},  # tags contain "sqlite" too
        )

    def test_11_type_filter(self) -> None:
        """(11) type filter uses the Phase 1 enum only."""
        self.assertEqual(self.titles("本地优先", type=MemoryType.PROFILE), ["技术偏好"])
        self.assertEqual(self.titles("本地优先", type="profile"), ["技术偏好"])
        self.assertEqual(self.titles("本地优先", type=MemoryType.KNOWLEDGE), [])
        self.assertEqual(self.titles("记忆", type="event"), ["维护成本记录"])

    def test_12_limit_and_offset(self) -> None:
        """(12) limit/offset paginate over a stable ordering."""
        all_titles = self.titles("记忆", status="all")
        self.assertGreaterEqual(len(all_titles), 2)
        first = self.titles("记忆", status="all", limit=1)
        self.assertEqual(first, all_titles[:1])
        second = self.titles("记忆", status="all", limit=1, offset=1)
        self.assertEqual(second, all_titles[1:2])
        self.assertEqual(self.retriever.search("RAG").limit, DEFAULT_LIMIT)
        result = self.retriever.search("记忆", status="all", limit=1, offset=1)
        self.assertEqual(result.offset, 1)
        self.assertEqual(result.total, len(all_titles))

    def test_type_and_status_combine(self) -> None:
        self.memory_summary = self.make_pending(self.memory_summary)
        self.assertEqual(self.titles("维护成本", type="event", status="pending"), ["维护成本记录"])
        self.assertEqual(self.titles("维护成本", type="knowledge", status="pending"), [])


class RankingTest(RepositoryTestCase):
    """Ordering tests on an isolated dataset.

    With the term present in every document, SQLite's bm25 is 0 for all rows, so
    the documented field-priority tie-break is what orders the result -- exactly
    the case the spec asks to be reasonable and stable.
    """

    def setUp(self) -> None:
        super().setUp()
        self.retriever = MemoryRetriever(self.repo)
        self.title_hit = self.make_memory(
            title="RAG 的基本原理", content="检索增强生成把检索与生成结合。"
        )
        self.content_hit = self.make_memory(
            title="一次技术闲聊", content="这里顺便提到 RAG，但没有展开。"
        )
        self.tag_hit = self.make_memory(title="标签记录", content="关键词只写在标签里。", tags=["RAG"])

    def test_13_ordering_is_deterministic_and_field_aware(self) -> None:
        """(13) same query -> same order; title beats tags beats content on ties."""
        runs = [[hit.memory.id for hit in self.retriever.search("RAG").hits] for _ in range(3)]
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])
        self.assertEqual(runs[0], [self.title_hit.id, self.tag_hit.id, self.content_hit.id])
        self.assertEqual(self.retriever.search("RAG").total, 3)

    def test_ordering_is_stable_across_repeated_calls_with_status_filter(self) -> None:
        first = [hit.memory.id for hit in self.retriever.search("RAG", status="all").hits]
        second = [hit.memory.id for hit in self.retriever.search("RAG", status="all").hits]
        self.assertEqual(first, second)

    def test_score_ties_are_broken_by_field_priority_not_by_chance(self) -> None:
        hits = self.retriever.search("RAG").hits
        for hit in hits:  # bm25 gives no signal when the term is in every document
            self.assertAlmostEqual(hit.score, 0.0, places=5)
        self.assertEqual(
            [hit.matched_fields for hit in hits], [("title",), ("tags",), ("content",)]
        )

    def test_updated_field_changes_its_rank(self) -> None:
        # move the term from the content to the title of the content-hit memory
        self.repo.update_memory(self.content_hit.id, title="标题命中 RAG", content="正文不再包含关键词。")
        ordered = [hit.memory.id for hit in self.retriever.search("RAG").hits]
        self.assertEqual(ordered[0], self.content_hit.id)  # now a title match, newest among title matches


class SourceRelationTest(RetrievalTestCase):
    def test_14_sources_are_returned_with_the_hit(self) -> None:
        """(14) every hit can reach its Sources."""
        hit = self.retriever.search("RAG").hits[0]
        self.assertEqual({source.id for source in hit.sources}, {self.source_rag.id, self.source_chat.id})
        self.assertEqual(hit.sources[0].source_type, "text")

    def test_15_one_memory_with_two_sources(self) -> None:
        """(15) 1 Memory -> 2 Sources."""
        hit = self.retriever.search("基本原理").hits[0]
        self.assertEqual(len(hit.sources), 2)
        self.assertEqual(
            {source.title for source in hit.sources}, {"RAG 入门", "某次聊天记录"}
        )

    def test_16_one_source_with_multiple_memories(self) -> None:
        """(16) 1 Source -> N Memories, each hit reaching that Source."""
        for query in ("基本原理", "SQLite"):
            hit = self.retriever.search(query).hits[0]
            self.assertIn(self.source_chat.id, {source.id for source in hit.sources})

    def test_source_refs_do_not_carry_the_full_content(self) -> None:
        hit = self.retriever.search("RAG").hits[0]
        payload = hit.sources[0].as_dict()
        self.assertNotIn("content", payload)
        self.assertIn("content_hint", payload)
        self.assertIn("source(source_id)", payload["content_hint"])

    def test_resolve_source_is_the_explicit_further_reading_path(self) -> None:
        source = self.retriever.resolve_source(self.source_rag.id)
        self.assertEqual(source.content, "RAG 把检索与生成结合起来。")

    def test_memory_without_sources_returns_empty_tuple(self) -> None:
        hit = self.retriever.search("基本原理").hits[0]
        self.assertEqual(hit.memory.id, self.memory_rag.id)
        bare = self.retriever.search("Agent Memory")
        self.assertEqual(bare.hits[0].sources, ())


class IndexConsistencyTest(RetrievalTestCase):
    def test_17_created_memory_is_immediately_searchable(self) -> None:
        memory = self.make_memory(title="刚刚创建的记忆", content="创建后应当立刻可被检索。")
        self.assertEqual([hit.memory.id for hit in self.retriever.search("立刻可被检索").hits], [memory.id])

    def test_18_updated_memory_index_is_correct(self) -> None:
        memory = self.make_memory(title="更新探针", content="旧内容包含关键词 oldword。")
        self.assertEqual(len(self.retriever.search("oldword").hits), 1)

        self.repo.update_memory(memory.id, content="新内容包含关键词 newword。")
        self.assertEqual(len(self.retriever.search("oldword").hits), 0)
        self.assertEqual([hit.memory.id for hit in self.retriever.search("newword").hits], [memory.id])

    def test_19_deleted_memory_is_not_returned(self) -> None:
        memory = self.make_memory(title="删除探针", content="删除关键词 goneword。")
        self.assertEqual(len(self.retriever.search("goneword").hits), 1)
        self.repo.delete_memory(memory.id)
        self.assertEqual(len(self.retriever.search("goneword").hits), 0)

    def test_20_archived_memory_is_hidden_then_restored(self) -> None:
        memory = self.make_memory(title="归档探针", content="归档关键词 archiveword。")
        self.assertEqual(len(self.retriever.search("archiveword").hits), 1)

        self.repo.update_memory(memory.id, status="archived")
        self.assertEqual(len(self.retriever.search("archiveword").hits), 0)
        self.assertEqual(
            [hit.memory.id for hit in self.retriever.search("archiveword", status="archived").hits],
            [memory.id],
        )

        self.repo.update_memory(memory.id, status="active")  # restore
        self.assertEqual([hit.memory.id for hit in self.retriever.search("archiveword").hits], [memory.id])


class QueryValidationTest(RetrievalTestCase):
    def test_23_empty_query_is_rejected(self) -> None:
        """(23) no full-table scan for empty input."""
        for query in ("", "   ", "\t\n"):
            with self.subTest(query=query), self.assertRaises(ValidationError) as ctx:
                self.retriever.search(query)
            self.assertEqual(ctx.exception.fields, ("query",))

    def test_23b_punctuation_only_query_is_rejected(self) -> None:
        for query in ('"', "'", "*", ":", "(", ")"):
            with self.subTest(query=query), self.assertRaises(ValidationError):
                self.retriever.search(query)

    def test_24_invalid_filters_are_rejected(self) -> None:
        """(24) limit/offset/type/status are validated with Phase 1 semantics."""
        for bad_limit in (0, -1, MAX_LIMIT + 1, True, "5", 1.5, None):
            with self.subTest(limit=bad_limit), self.assertRaises(ValidationError) as ctx:
                self.retriever.search("RAG", limit=bad_limit)
            self.assertEqual(ctx.exception.fields, ("limit",))
        for bad_offset in (-1, True, "0", None):
            with self.subTest(offset=bad_offset), self.assertRaises(ValidationError) as ctx:
                self.retriever.search("RAG", offset=bad_offset)
            self.assertEqual(ctx.exception.fields, ("offset",))
        for bad_type in ("idea", "", "KNOWLEDGE_TYPE", 7):
            with self.subTest(type=bad_type), self.assertRaises(ValidationError) as ctx:
                self.retriever.search("RAG", type=bad_type)
            self.assertEqual(ctx.exception.fields, ("type",))
        for bad_status in ("deleted", "", "Active2", 5):
            with self.subTest(status=bad_status), self.assertRaises(ValidationError) as ctx:
                self.retriever.search("RAG", status=bad_status)
            self.assertEqual(ctx.exception.fields, ("status",))
        with self.assertRaises(ValidationError):
            self.retriever.search("RAG", status=[])
        with self.assertRaises(ValidationError):
            self.retriever.search(123)  # not a string

    def test_25_hostile_query_characters_do_not_break_search(self) -> None:
        """(25) FTS5 operators and punctuation must not raise or match wildly."""
        for query in ('"', "'", "*", ":", "(", ")", "AND", "OR", "RAG AND", "RAG OR", "NOT", "^", "~", "-", '"RAG"'):
            with self.subTest(query=query):
                try:
                    result = self.retriever.search(query)
                except ValidationError:
                    continue  # explicit rejection is acceptable for punctuation-only input
                self.assertIsInstance(result.total, int)
        # operators must be treated as literal text, not as FTS syntax
        self.assertEqual(self.retriever.search("AND").total, 0)
        self.assertEqual(self.retriever.search("OR").total, 0)
        self.assertEqual([hit.memory.id for hit in self.retriever.search('"RAG"').hits], [self.memory_rag.id])

    def test_token_helpers_are_consistent(self) -> None:
        self.assertEqual(tokenize("  RAG   长期记忆 "), ["RAG", "长期记忆"])
        self.assertEqual(build_match_expression(["RAG", 'a"b']), '"RAG" OR "a""b"')
        self.assertEqual(build_match_expression([]), "")
        self.assertEqual(classify_token("RAG"), "word")
        self.assertEqual(classify_token("a"), "word")  # Latin always uses the word prefix
        self.assertEqual(classify_token("长期记忆"), "trigram")
        self.assertEqual(classify_token("记忆"), "short")
        self.assertFalse(is_searchable_token('"'))
        self.assertTrue(is_searchable_token("RAG"))


class CliSearchTest(RetrievalTestCase):
    prefix = "pms-search-"

    def run_cli(self, *argv: str):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--db", str(self.db_path), *argv])
        return code, buffer.getvalue()

    def test_search_cli_human_output(self) -> None:
        """(18-requirement CLI) Memory / Title / Type / Score / Summary / Sources."""
        code, output = self.run_cli("search", "RAG")
        self.assertEqual(code, 0)
        self.assertIn("RAG 的基本原理", output)
        self.assertIn("knowledge", output)
        self.assertIn("score=", output)
        self.assertIn("summary   : 检索增强生成", output)
        self.assertIn("sources (2):", output)
        self.assertIn("RAG 入门", output)
        self.assertIn("python -m personal_memory source", output)

    def test_search_cli_json_output(self) -> None:
        code, output = self.run_cli("search", "长期记忆", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["query"], "长期记忆")
        self.assertEqual(payload["statuses"], ["active"])
        self.assertGreaterEqual(payload["total"], 1)
        self.assertIn("hits", payload)

    def test_search_cli_options(self) -> None:
        code, output = self.run_cli("search", "记忆", "--status", "all", "--limit", "1", "--type", "event", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(payload["memory_type"], "event")
        self.assertLessEqual(len(payload["hits"]), 1)

    def test_search_cli_reports_validation_errors_with_exit_code_3(self) -> None:
        code, output = self.run_cli("search", "   ")
        self.assertEqual(code, 3)
        payload = json.loads(output)
        self.assertEqual(payload["error_type"], "ValidationError")

    def test_search_cli_no_match_message(self) -> None:
        code, output = self.run_cli("search", "zzz_no_such_term")
        self.assertEqual(code, 0)
        self.assertIn("no memory matched", output)

    def test_source_cli_returns_full_content(self) -> None:
        code, output = self.run_cli("source", self.source_rag.id)
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["id"], self.source_rag.id)
        self.assertEqual(payload["content"], "RAG 把检索与生成结合起来。")

    def test_source_cli_unknown_id(self) -> None:
        code, output = self.run_cli("source", "src_missing")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["error_type"], "NotFoundError")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
