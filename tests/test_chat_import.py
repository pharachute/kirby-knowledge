"""Knowledge Base 1.0 -- Phase 3 (Chat Importer) tests.

The 30 required behaviours (12 parsing, 5 metadata, 13 integration) plus the boundaries
the spec adds: explicit failure instead of truncation for long conversations, privacy
(no conversation body in default views or errors) and the adapter seam that lets a new
platform be added without touching the core.

Everything runs offline with a Mock LLM.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import unittest

import personal_memory.importers.chat
import personal_memory.importers.chat_adapters
from personal_memory import (
    CaptureService,
    ChatConversation,
    ChatImportError,
    ChatImporter,
    ChatParseError,
    ChatRole,
    Database,
    EmptyConversationError,
    LLMRequestError,
    MemoryFormationService,
    MemoryQualityGate,
    MemoryRepository,
    MemoryRetriever,
    SourceType,
    UnsupportedChatRoleError,
    ValidationError,
    compute_content_hash,
    import_chat_file,
    parse_chat_json,
    parse_role_text,
)
from personal_memory.errors import MemorySystemError
from personal_memory.importers import ChatMessage
from personal_memory.importers.chat import (
    ROLE_MARKERS,
    UNTITLED_TITLE,
    ParsedChat,
    resolve_title,
    short_title_from_first_user_message,
)
from personal_memory.importers.chat_adapters import (
    ADAPTERS,
    GenericJsonAdapter,
    RoleTextAdapter,
    select_adapter,
)
from personal_memory.importers.files import FileMissingError, FileTooLargeError, UnsupportedFileTypeError

from .helpers import RepositoryTestCase
from .llm_fakes import mock_client, response_for
from .test_quality import draft_payload, formation_payload

ROLE_TEXT = (
    "[User]\n"
    "我最近开始系统学习 Agent，希望理解底层原理，而不是只会调用现成框架。\n"
    "\n"
    "[Assistant]\n"
    "Agent 可以理解为一个能感知环境、做出决策并采取行动的智能体。\n"
    "\n"
    "[User]\n"
    "我希望学习路线里包含底层实现细节。\n"
)

LOW_VALUE_TEXT = "[User]\n哈哈哈哈\n\n[Assistant]\n哈哈哈哈\n"

JSON_PAYLOAD = {
    "conversation_id": "conv-001",
    "title": "Agent 学习",
    "provider": "generic-exporter",
    "messages": [
        {"role": "user", "content": "我最近开始学习 Agent。", "timestamp": "2026-10-04T10:00:00Z"},
        {"role": "assistant", "content": "Agent 可以理解为……", "timestamp": "2026-10-04T10:00:05Z"},
        {"role": "user", "content": "我希望理解底层原理。", "timestamp": "2026-10-04T10:01:00Z"},
    ],
}

SECRET_SENTENCE = "紫罗兰色的鲸鱼在第七码头等待一艘纸船。"


class ChatTestCase(RepositoryTestCase):
    prefix = "pms-chat-"

    def write(self, name: str, text: str) -> pathlib.Path:
        path = self.tmpdir / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, name: str, payload: object) -> pathlib.Path:
        return self.write(name, json.dumps(payload, ensure_ascii=False))

    def capture_service(self, *items, quality: bool = False):
        client = mock_client(*items)
        formation = MemoryFormationService(
            self.repo, client, quality=MemoryQualityGate(self.repo) if quality else None
        )
        return CaptureService(formation, captured_from="chat"), client

    def counts(self) -> dict[str, int]:
        return self.repo.counts()


# --------------------------------------------------------------------------
# Parsing (1-12)
# --------------------------------------------------------------------------


class ChatParsingTest(ChatTestCase):
    def test_1_role_text_parsing(self) -> None:
        parsed = parse_role_text(ROLE_TEXT)

        self.assertEqual([str(message.role) for message in parsed.messages], ["user", "assistant", "user"])
        self.assertIn("我最近开始系统学习 Agent", parsed.messages[0].content)
        self.assertEqual(parsed.messages[0].content.count("我最近开始系统学习 Agent"), 1)
        self.assertIsNone(parsed.title)
        self.assertIsNone(parsed.conversation_id)

    def test_2_markdown_style_role_text(self) -> None:
        text = (
            "# 会话标题\n\n"
            "## User\n第一句是用户说的。\n\n"
            "### Assistant:\n助手回答，并解释 `## 原理` 只是正文里的普通标题。\n"
        )

        parsed = parse_role_text(text)

        self.assertEqual(parsed.title, "会话标题")
        self.assertEqual([str(message.role) for message in parsed.messages], ["user", "assistant"])
        self.assertIn("## 原理", parsed.messages[1].content)  # a non-role heading stays content

        # a role header is a whole line: content starts on the next line
        bracketed = parse_role_text("[USER]:\n你好。\n\n[ASSISTANT]\n你好，有什么可以帮你？\n")
        self.assertEqual([str(message.role) for message in bracketed.messages], ["user", "assistant"])
        self.assertEqual(bracketed.messages[0].content, "你好。")

    def test_3_json_conversation(self) -> None:
        parsed = parse_chat_json(JSON_PAYLOAD)

        self.assertEqual(parsed.conversation_id, "conv-001")
        self.assertEqual(parsed.title, "Agent 学习")
        self.assertEqual(parsed.provider, "generic-exporter")
        self.assertEqual(len(parsed.messages), 3)
        self.assertEqual(parsed.messages[0].timestamp, "2026-10-04T10:00:00Z")

        conversation = ChatImporter().load(self.write_json("conv.json", JSON_PAYLOAD))
        self.assertEqual(conversation.conversation_id, "conv-001")
        self.assertEqual(conversation.message_count, 3)
        self.assertEqual(str(conversation.roles()[1]), "assistant")

    def test_4_user_and_assistant_are_distinguished(self) -> None:
        conversation = ChatImporter().load(self.write("chat.txt", ROLE_TEXT))
        rendered = conversation.render_role_text()

        self.assertEqual(rendered.count(ROLE_MARKERS["user"]), 2)
        self.assertEqual(rendered.count(ROLE_MARKERS["assistant"]), 1)
        blocks = [block for block in rendered.split("\n\n") if block.strip()]
        self.assertTrue(blocks[0].startswith("[USER]"))
        self.assertTrue(blocks[1].startswith("[ASSISTANT]"))
        self.assertTrue(blocks[2].startswith("[USER]"))
        self.assertIn("Agent 可以理解为一个能感知环境", blocks[1])
        self.assertNotIn("Agent 可以理解为一个能感知环境", blocks[2])

    def test_5_system_tool_developer_and_unknown_roles(self) -> None:
        text = (
            "[System]\n你是助手。\n\n"
            "[User]\n请解释 RAG。\n\n"
            "[Tool]\nsearch(query='RAG') -> 3 hits\n\n"
            "[Developer]\n回答要简洁。\n"
        )

        parsed = parse_role_text(text)

        self.assertEqual(
            [str(message.role) for message in parsed.messages],
            ["system", "user", "tool", "developer"],
        )
        conversation = ChatConversation(conversation_id="c", messages=parsed.messages)
        rendered = conversation.render_role_text()
        for role in ("SYSTEM", "USER", "TOOL", "DEVELOPER"):
            self.assertIn(f"[{role}]", rendered)

        # unknown labels are refused, never silently mapped to "user"
        with self.assertRaises(UnsupportedChatRoleError) as caught:
            parse_role_text("[Moderator]\n欢迎。\n")
        self.assertIn("moderator", str(caught.exception).lower())
        self.assertIn("user", tuple(caught.exception.allowed))
        with self.assertRaises(UnsupportedChatRoleError):
            parse_chat_json({"messages": [{"role": "wizard", "content": "hi"}]})
        with self.assertRaises(ValidationError):
            ChatMessage(role="wizard", content="hi")

    def test_6_message_order_is_preserved(self) -> None:
        text = "\n\n".join(f"[User]\n第 {index} 条消息。" for index in range(1, 13))
        conversation = ChatImporter().load(self.write("many.txt", text))

        self.assertEqual(conversation.message_count, 12)
        self.assertEqual(
            [message.content for message in conversation.messages],
            [f"第 {index} 条消息。" for index in range(1, 13)],
        )

        payload = {
            "messages": [
                {"role": "assistant" if index % 2 else "user", "content": f"m{index}"} for index in range(6)
            ]
        }
        json_conversation = ChatImporter().load(self.write_json("order.json", payload))
        self.assertEqual([message.content for message in json_conversation.messages],
                         [f"m{index}" for index in range(6)])

    def test_7_timestamps_are_preserved(self) -> None:
        conversation = ChatImporter().load(self.write_json("t.json", JSON_PAYLOAD))

        self.assertEqual(conversation.messages[0].timestamp, "2026-10-04T10:00:00Z")
        self.assertEqual(conversation.messages[-1].timestamp, "2026-10-04T10:01:00Z")
        self.assertEqual(conversation.started_at, "2026-10-04T10:00:00Z")
        self.assertEqual(conversation.ended_at, "2026-10-04T10:01:00Z")

        explicit = dict(JSON_PAYLOAD, started_at="2026-01-01T00:00:00Z", ended_at="2026-01-02T00:00:00Z")
        overridden = ChatImporter().load(self.write_json("explicit.json", explicit))
        self.assertEqual(overridden.started_at, "2026-01-01T00:00:00Z")
        self.assertEqual(overridden.ended_at, "2026-01-02T00:00:00Z")

        # role text has no timestamps: None, not invented
        plain = ChatImporter().load(self.write("plain.txt", ROLE_TEXT))
        self.assertIsNone(plain.started_at)
        self.assertIsNone(plain.messages[0].timestamp)

    def test_8_blank_messages_are_dropped(self) -> None:
        text = "[User]\n\n\n[Assistant]\n有内容。\n\n[User]\n   \n"
        conversation = ChatImporter().load(self.write("blanks.txt", text))

        self.assertEqual(conversation.message_count, 1)
        self.assertEqual(str(conversation.messages[0].role), "assistant")

        with self.assertRaises(EmptyConversationError):
            ChatImporter().load(self.write("allblank.txt", "[User]\n   \n\n[Assistant]\n\n"))
        with self.assertRaises(ValidationError):
            ChatMessage(role="user", content="   \n\t ")
        with self.assertRaises(ValidationError):
            ChatConversation(conversation_id="c", messages=())

    def test_9_invalid_json_is_reported(self) -> None:
        with self.assertRaises(ChatParseError) as caught:
            ChatImporter().load(self.write("broken.json", "{ this is not json "))
        self.assertIn("invalid JSON", str(caught.exception))

        with self.assertRaises(ChatParseError):
            ChatImporter().load(self.write("array.json", "[1, 2, 3]"))
        with self.assertRaises(ChatParseError):
            parse_chat_json({"conversation_id": "x"})  # messages missing

    def test_10_missing_role_is_reported(self) -> None:
        with self.assertRaises(ChatParseError) as caught:
            ChatImporter().load(self.write_json("norole.json", {"messages": [{"content": "hi"}]}))
        self.assertIn("messages[0].role", str(caught.exception))

        with self.assertRaises(ChatParseError):
            parse_chat_json({"messages": [{"role": "   ", "content": "hi"}]})

    def test_11_missing_content_is_reported(self) -> None:
        with self.assertRaises(ChatParseError) as caught:
            ChatImporter().load(self.write_json("nocontent.json", {"messages": [{"role": "user"}]}))
        self.assertIn("messages[0].content", str(caught.exception))

        with self.assertRaises(ChatParseError) as caught_type:
            parse_chat_json({"messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}]})
        self.assertIn("must be a string", str(caught_type.exception))
        self.assertIn("adapter", str(caught_type.exception))  # block content needs a provider adapter

    def test_12_invalid_timestamp_is_reported(self) -> None:
        with self.assertRaises(ChatParseError) as caught:
            ChatImporter().load(
                self.write_json(
                    "badtime.json", {"messages": [{"role": "user", "content": "hi", "timestamp": "yesterday"}]}
                )
            )
        self.assertIn("messages[0].timestamp", str(caught.exception))

        with self.assertRaises(ValidationError):
            ChatMessage(role="user", content="hi", timestamp="not-a-time")


# --------------------------------------------------------------------------
# Metadata (13-17)
# --------------------------------------------------------------------------


class ChatMetadataTest(ChatTestCase):
    def test_13_conversation_id(self) -> None:
        payload = dict(JSON_PAYLOAD, conversation_id="conv-from-file")
        self.assertEqual(ChatImporter().load(self.write_json("a.json", payload)).conversation_id, "conv-from-file")

        explicit = ChatImporter().load(self.write_json("b.json", payload), conversation_id="conv-explicit")
        self.assertEqual(explicit.conversation_id, "conv-explicit")

        derived_first = ChatImporter().load(self.write("roles.txt", ROLE_TEXT))
        derived_second = ChatImporter().load(self.write("roles-copy.txt", ROLE_TEXT))
        self.assertTrue(derived_first.conversation_id.startswith("conv_"))
        self.assertEqual(derived_first.conversation_id, derived_second.conversation_id)  # stable

    def test_14_title_priority(self) -> None:
        messages = parse_role_text(ROLE_TEXT).messages

        self.assertEqual(resolve_title(explicit="显式标题", export_title="导出标题", messages=messages),
                         ("显式标题", "explicit"))
        self.assertEqual(resolve_title(explicit=None, export_title="导出标题", messages=messages),
                         ("导出标题", "export"))
        derived, source = resolve_title(explicit=None, export_title=None, messages=messages)
        self.assertEqual(source, "first_user_message")
        self.assertTrue(derived.startswith("我最近开始系统学习 Agent"))
        self.assertLessEqual(len(derived), 60)

        assistant_only = parse_role_text("[Assistant]\n只有助手在说话。\n").messages
        self.assertEqual(resolve_title(explicit=None, export_title=None, messages=assistant_only),
                         (UNTITLED_TITLE, "untitled"))

        # no model call is made just to name a conversation
        service, client = self.capture_service()
        conversation = ChatImporter().load(self.write("t.txt", ROLE_TEXT))
        self.assertEqual(client.transport.call_count, 0)
        self.assertEqual(conversation.title_source, "first_user_message")

    def test_15_provider(self) -> None:
        from_file = ChatImporter().load(self.write_json("p.json", JSON_PAYLOAD))
        self.assertEqual(from_file.provider, "generic-exporter")

        unknown = ChatImporter().load(self.write("roles.txt", ROLE_TEXT))
        self.assertIsNone(unknown.provider)  # never guessed

        override = ChatImporter().load(self.write("roles2.txt", ROLE_TEXT), provider="cli-provider")
        self.assertEqual(override.provider, "cli-provider")

        metadata = override.to_capture_request().metadata
        self.assertEqual(metadata["provider"], "cli-provider")
        self.assertIsNone(unknown.to_capture_request().metadata["provider"])

    def test_16_message_count(self) -> None:
        conversation = ChatImporter().load(self.write("count.txt", ROLE_TEXT))
        self.assertEqual(conversation.message_count, 3)
        self.assertEqual(conversation.to_capture_request().metadata["message_count"], 3)
        self.assertEqual(conversation.as_dict()["message_count"], 3)

    def test_17_started_and_ended_at_reach_the_capture_metadata(self) -> None:
        conversation = ChatImporter().load(self.write_json("meta.json", JSON_PAYLOAD))
        metadata = conversation.to_capture_request().metadata

        self.assertEqual(metadata["captured_from"], "chat")
        self.assertEqual(metadata["conversation_id"], "conv-001")
        self.assertEqual(metadata["started_at"], "2026-10-04T10:00:00Z")
        self.assertEqual(metadata["ended_at"], "2026-10-04T10:01:00Z")
        self.assertEqual(metadata["roles"], ["user", "assistant"])

        without = ChatImporter().load(self.write("no-time.txt", ROLE_TEXT)).to_capture_request().metadata
        self.assertIsNone(without["started_at"])
        self.assertIsNone(without["ended_at"])


# --------------------------------------------------------------------------
# Integration (18-30)
# --------------------------------------------------------------------------


class ChatIntegrationTest(ChatTestCase):
    def test_18_chat_to_capture(self) -> None:
        service, _ = self.capture_service(response_for(formation_payload(draft_payload())))
        path = self.write("conv.txt", ROLE_TEXT)

        result = import_chat_file(path, service, provider="cli-provider")

        request = result.capture_result.request
        self.assertEqual(str(request.source_type), "chat")
        self.assertIn("[USER]", request.content)
        self.assertIn("[ASSISTANT]", request.content)
        self.assertEqual(request.metadata["captured_from"], "chat")
        self.assertEqual(request.metadata["provider"], "cli-provider")
        self.assertEqual(request.metadata["message_count"], 3)
        self.assertIsNone(request.url)
        self.assertEqual(result.import_status, "imported")

    def test_19_chat_to_formation(self) -> None:
        service, client = self.capture_service(response_for(formation_payload(draft_payload())))
        import_chat_file(self.write("conv.txt", ROLE_TEXT), service)

        prompt = client.transport.requests[0].messages[-1].content
        self.assertIn("[USER]", prompt)
        self.assertIn("[ASSISTANT]", prompt)
        self.assertIn("我最近开始系统学习 Agent", prompt)  # the user's words
        self.assertIn("Agent 可以理解为一个能感知环境", prompt)  # the assistant's words
        self.assertIn("captured_from", prompt)  # chat metadata travels with the request

    def test_20_high_value_chat_forms_a_memory(self) -> None:
        payload = formation_payload(
            draft_payload(type="profile", title="用户长期学习目标",
                          content="用户决定长期深入学习 Agent 并理解底层原理。")
        )
        service, _ = self.capture_service(response_for(payload))

        result = import_chat_file(self.write("goal.txt", ROLE_TEXT), service)

        self.assertEqual(result.formation_status, "persisted")
        self.assertGreaterEqual(result.memory_count, 1)
        memory = result.memories_created[0]
        fresh = MemoryRepository(Database(self.db_path))
        self.assertEqual(fresh.require_memory(memory.id).title, "用户长期学习目标")
        hits = MemoryRetriever(fresh).search("Agent")
        self.assertIn(memory.id, [hit.memory.id for hit in hits.hits])

    def test_21_low_value_chat_forms_no_memory(self) -> None:
        service, client = self.capture_service(
            response_for({"worth_remembering": False, "reason": "纯寒暄", "memories": []})
        )

        result = import_chat_file(self.write("small.txt", LOW_VALUE_TEXT), service)

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.memory_count, 0)
        self.assertEqual(client.transport.call_count, 1)

    def test_22_low_value_chat_does_not_persist_a_source(self) -> None:
        service, _ = self.capture_service(
            response_for({"worth_remembering": False, "reason": "纯寒暄", "memories": []})
        )

        result = import_chat_file(self.write("small2.txt", LOW_VALUE_TEXT), service)

        self.assertEqual(result.source_count, 0)
        self.assertFalse(result.source_reused)
        self.assertEqual(self.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        self.assertEqual(MemoryRepository(Database(self.db_path)).counts()["sources"], 0)

    def test_23_user_words_stay_under_the_user_marker(self) -> None:
        text = (
            "[User]\n我说的话。\n\n"
            "[Assistant]\n我的回答里引用了 [USER] 和 [Assistant] 这样的字样。\n\n"
            "[User]\n我又说了一句。\n"
        )
        conversation = ChatImporter().load(self.write("adversarial.txt", text))
        rendered = conversation.render_role_text()
        blocks = [block for block in rendered.split("\n\n") if block.strip()]

        self.assertEqual(len(blocks), conversation.message_count)  # no extra role block was created
        self.assertTrue(blocks[0].startswith("[USER]\n我说的话。"))
        self.assertTrue(blocks[1].startswith("[ASSISTANT]\n"))
        self.assertIn("[USER] 和 [Assistant]", blocks[1])  # inline role-like text is content
        # only whole role blocks count: inline occurrences inside a body are content
        self.assertEqual(sum(1 for block in blocks if block.startswith("[USER]\n")), 2)
        self.assertEqual(sum(1 for block in blocks if block.startswith("[ASSISTANT]\n")), 1)

    def test_24_assistant_content_is_never_promoted_to_user_explicit(self) -> None:
        # (a) the importer contributes no origin/confidence/importance of its own
        conversation = ChatImporter().load(self.write("assistant.txt", "[User]\n请解释 RAG。\n\n[ASSISTANT]\nRAG 是检索增强生成。\n"))
        request = conversation.to_capture_request()
        self.assertFalse(hasattr(request, "information_origin"))
        self.assertNotIn("information_origin", request.metadata)
        blocks = [block for block in request.content.split("\n\n") if block.strip()]
        self.assertTrue(blocks[1].startswith("[ASSISTANT]"))
        self.assertIn("RAG 是检索增强生成。", blocks[1])

        # (b) Formation's own judgment is what reaches the Memory unchanged
        payload = formation_payload(
            draft_payload(information_origin="source_content", type="knowledge",
                          title="RAG 的定义", content="RAG 是检索增强生成。")
        )
        service, _ = self.capture_service(response_for(payload))
        result = import_chat_file(self.write("assistant2.txt", "[ASSISTANT]\nRAG 是检索增强生成。\n"), service)
        self.assertEqual(str(result.memories_created[0].information_origin), "source_content")

    def test_25_agent_inference_keeps_the_phase_2_confidence_policy(self) -> None:
        dropped_service, _ = self.capture_service(
            response_for(
                formation_payload(
                    draft_payload(information_origin="agent_inference", confidence=0.4, title="可能偏好")
                )
            )
        )
        dropped = import_chat_file(self.write("infer-low.txt", ROLE_TEXT), dropped_service)
        self.assertEqual(dropped.status, "skipped_by_policy")
        self.assertEqual(dropped.memory_count, 0)

        kept_service, _ = self.capture_service(
            response_for(
                formation_payload(
                    draft_payload(information_origin="agent_inference", confidence=0.9, title="可能偏好")
                )
            )
        )
        kept = import_chat_file(self.write("infer-high.txt", ROLE_TEXT), kept_service)
        self.assertEqual(kept.memory_count, 1)
        self.assertEqual(str(kept.memories_created[0].status), "pending")  # Phase 2/4 policy

    def test_26_one_chat_can_produce_several_memories(self) -> None:
        payload = formation_payload(
            draft_payload(type="profile", title="学习目标", content="用户要长期学习 Agent。", requires_source=True),
            draft_payload(type="knowledge", title="Agent 定义", content="Agent 能感知环境并采取行动。", requires_source=True),
            draft_payload(type="experience", title="框架取舍经验", content="先用现成框架跑通，再读底层实现。", requires_source=True),
        )
        service, _ = self.capture_service(response_for(payload))

        result = import_chat_file(self.write("multi.txt", ROLE_TEXT), service)

        self.assertEqual(result.memory_count, 3)
        self.assertEqual(result.source_count, 1)
        self.assertEqual({str(memory.type) for memory in result.memories_created},
                         {"profile", "knowledge", "experience"})
        self.assertEqual(self.counts(), {"sources": 1, "memories": 3, "memory_sources": 3})

    def test_27_memories_are_linked_to_the_chat_source(self) -> None:
        payload = formation_payload(
            draft_payload(title="A", content="结论 A。", requires_source=True),
            draft_payload(title="B", content="结论 B。", requires_source=True),
        )
        service, _ = self.capture_service(response_for(payload))

        result = import_chat_file(self.write("linked.txt", ROLE_TEXT), service)

        source = result.sources_created[0]
        self.assertEqual(str(source.source_type), "chat")
        self.assertEqual(source.metadata["captured_from"], "chat")
        for memory in result.memories_created:
            self.assertEqual([s.id for s in self.repo.get_sources_for_memory(memory.id)], [source.id])
        self.assertEqual({m.id for m in self.repo.get_memories_for_source(source.id)},
                         {m.id for m in result.memories_created})
        self.assertEqual(self.repo.count_links_for_memory(result.memories_created[0].id), 1)

    def test_28_importer_never_touches_sqlite(self) -> None:
        sources = [
            pathlib.Path(personal_memory.importers.chat.__file__).read_text(encoding="utf-8"),
            pathlib.Path(personal_memory.importers.chat_adapters.__file__).read_text(encoding="utf-8"),
        ]
        for source in sources:
            self.assertNotIn("sqlite3", source)
            self.assertNotIn("from ..store", source)
            for verb in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
                self.assertNotIn(verb, source)

        self.assertEqual(
            list(inspect.signature(ChatImporter.load).parameters),
            ["self", "path", "title", "provider", "conversation_id"],
        )
        self.assertEqual(
            list(inspect.signature(ChatImporter.import_conversation).parameters),
            ["self", "conversation", "capture", "source_type", "dry_run"],
        )
        with self.assertRaises(ValidationError):
            ChatImporter().import_conversation(
                ChatConversation(conversation_id="c", messages=(ChatMessage(role="user", content="hi"),)),
                self.repo,  # type: ignore[arg-type]  # a repository is not a CaptureService
            )

    def test_29_formation_failure_leaves_nothing_behind(self) -> None:
        conversation = ChatImporter().load(self.write("fail.txt", ROLE_TEXT))
        digest = compute_content_hash(conversation.render_role_text())
        service, _ = self.capture_service(LLMRequestError("provider unreachable", retryable=False))

        with self.assertRaises(LLMRequestError):
            service.capture_request(conversation.to_capture_request())

        self.assertEqual(self.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})
        self.assertFalse(self.repo.source_exists_by_hash(digest))
        self.assertTrue(self.repo.index_consistency()["consistent"])

    def test_30_duplicate_chat_reuses_the_existing_dedupe(self) -> None:
        path = self.write("dup.txt", ROLE_TEXT)
        payload = formation_payload(draft_payload(requires_source=True, evidence_quote="逐字依据"))

        # (a) no gate: the Source content_hash is reused (one Source row)
        service, _ = self.capture_service(response_for(payload), response_for(payload))
        first = import_chat_file(path, service)
        second = import_chat_file(path, service)
        self.assertEqual(first.source_count, 1)
        self.assertTrue(second.source_reused)
        self.assertEqual(second.sources_created[0].id, first.sources_created[0].id)
        self.assertEqual(self.counts()["sources"], 1)

        # (b) Phase 4 gate on: the Memory is recognised as an exact duplicate
        gated, _ = self.capture_service(response_for(payload), quality=True)
        third = import_chat_file(path, gated)
        self.assertEqual(third.status, "duplicate")
        self.assertEqual(third.memory_count, 0)
        self.assertTrue(self.repo.index_consistency()["consistent"])


# --------------------------------------------------------------------------
# Boundaries: long conversations, privacy, adapter seam
# --------------------------------------------------------------------------


class ChatBoundaryTest(ChatTestCase):
    def test_long_conversation_fails_explicitly_and_is_never_truncated(self) -> None:
        long_text = "[User]\n" + ("很长的内容。" * 200) + "\n\n[Assistant]\n最后一句。\n"
        path = self.write("long.txt", long_text)

        with self.assertRaises(FileTooLargeError) as caught:
            ChatImporter(max_bytes=500).load(path)
        self.assertGreater(caught.exception.size_bytes, 500)
        self.assertEqual(self.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

        # under the limit nothing is dropped: the last message is still there
        fits = ChatImporter().load(path)
        self.assertEqual(fits.message_count, 2)
        self.assertIn("最后一句。", fits.render_role_text())

    def test_default_views_and_errors_do_not_expose_the_conversation_body(self) -> None:
        # the secret sits in the SECOND user message: the derived title legitimately comes
        # from the first user message (spec §九), so this checks message bodies, not titles
        text = f"[User]\n你好，我有个问题。\n\n[Assistant]\n请讲。\n\n[User]\n{SECRET_SENTENCE}\n"
        service, _ = self.capture_service(
            response_for(formation_payload(draft_payload(requires_source=True, evidence_quote="逐字依据")))
        )

        result = import_chat_file(self.write("secret.txt", text), service)
        # title rule is explicit: it is the shortened first user message, and nothing else
        self.assertEqual(result.title, "你好，我有个问题。")
        self.assertEqual(result.title_source, "first_user_message")

        redacted = json.dumps(result.as_dict(), ensure_ascii=False)
        self.assertNotIn(SECRET_SENTENCE, redacted)
        self.assertNotIn(SECRET_SENTENCE, json.dumps(result.conversation.as_dict(), ensure_ascii=False))
        self.assertNotIn(SECRET_SENTENCE, json.dumps(result.conversation.messages[0].as_dict(), ensure_ascii=False))
        # no message body characters at all, not even from a short message
        self.assertNotIn("紫罗兰", redacted)
        for message in result.as_dict()["conversation"]["messages"]:
            self.assertNotIn("content", message)
            self.assertNotIn("content_preview", message)
        # ... but the explicit opt-ins still give the caller what it asked for
        self.assertIn("紫罗兰", json.dumps(result.as_dict(include_preview=True), ensure_ascii=False))
        self.assertIn(SECRET_SENTENCE, json.dumps(result.as_dict(include_content=True), ensure_ascii=False))

        # a parse error must not echo the conversation either
        with self.assertRaises(ChatParseError) as caught:
            ChatImporter().load(self.write("bad.json", json.dumps({"messages": [{"content": SECRET_SENTENCE}]})))
        self.assertNotIn(SECRET_SENTENCE, str(caught.exception))

    def test_adapter_seam_supports_a_new_platform_without_touching_the_core(self) -> None:
        class MinimalAdapter:
            name = "minimal-provider"
            provider = "minimal"
            extensions = (".mychat",)

            def matches(self, text: str, *, extension: str) -> bool:
                return text.startswith("MINIMAL ")

            def parse(self, text: str, *, source: str = ""):
                messages = tuple(
                    ChatMessage(role=part.split(":", 1)[0], content=part.split(":", 1)[1])
                    for part in text[len("MINIMAL ") :].split(" | ")
                )
                return ParsedChat(messages=messages, provider="minimal")

        path = self.write("custom.mychat", "MINIMAL user:你好 | assistant:你好，很高兴见到你。")
        importer = ChatImporter(adapters=[MinimalAdapter(), RoleTextAdapter(), GenericJsonAdapter()])

        conversation = importer.load(path)

        self.assertEqual(conversation.provider, "minimal")
        self.assertEqual([str(message.role) for message in conversation.messages], ["user", "assistant"])
        self.assertEqual(select_adapter("MINIMAL x", extension=".mychat", adapters=[MinimalAdapter()]).name,
                         "minimal-provider")
        # the shipped registry is untouched by that test double
        self.assertEqual([adapter.name for adapter in ADAPTERS],
                         ["provider-neutral-json", "role-text"])
        with self.assertRaises(ValidationError):
            ChatImporter(format="xml")
        with self.assertRaises(ValidationError):
            ChatImporter(adapters=[])

    def test_provider_specific_shapes_are_not_guessed(self) -> None:
        # a ChatGPT-"mapping"-style payload has no "messages" array: explicit failure,
        # not a guessed parse (the spec forbids guessing vendor formats)
        with self.assertRaises(ChatParseError) as caught:
            ChatImporter(format="json").load(
                self.write_json("chatgpt-like.json", {"title": "x", "mapping": {"a": {"message": {}}}})
            )
        self.assertIn("messages", str(caught.exception))

    def test_unsupported_extension_and_missing_file_are_typed(self) -> None:
        with self.assertRaises(FileMissingError):
            ChatImporter().load(self.tmpdir / "nope.txt")
        with self.assertRaises(UnsupportedFileTypeError):
            ChatImporter().load(self.write("conv.pdf", "not a chat"))

    def test_format_override_selects_the_parser(self) -> None:
        json_text = json.dumps({"messages": [{"role": "user", "content": "来自 JSON"}]})
        as_roles = ChatImporter(format="roles").load(self.write("weird.txt", "[User]\n来自 roles\n"))
        self.assertEqual(as_roles.messages[0].content, "来自 roles")

        mixed = ChatImporter(format="json").load(self.write("json-in-txt.txt", json_text))
        self.assertEqual(mixed.messages[0].content, "来自 JSON")
        # auto: a .txt that starts with "{" is JSON, a .json that is broken reports JSON
        auto = ChatImporter().load(self.write("json-in-other.txt", json_text))
        self.assertEqual(auto.messages[0].content, "来自 JSON")

    def test_dry_run_writes_nothing(self) -> None:
        service, _ = self.capture_service(response_for(formation_payload(draft_payload(requires_source=True))))
        result = import_chat_file(self.write("dry.txt", ROLE_TEXT), service, dry_run=True)

        self.assertEqual(result.import_status, "preview")
        self.assertEqual(result.status, "preview")
        self.assertEqual(self.counts(), {"sources": 0, "memories": 0, "memory_sources": 0})

    def test_short_title_helper_and_role_markers(self) -> None:
        messages = parse_role_text("[User]\n" + "很长的第一行" * 20 + "\n").messages
        derived = short_title_from_first_user_message(messages, limit=20)
        self.assertIsNotNone(derived)
        self.assertEqual(len(derived), 20)
        self.assertTrue(derived.endswith("…"))
        self.assertIsNone(short_title_from_first_user_message(messages[:0]))
        self.assertEqual(ROLE_MARKERS["system"], "[SYSTEM]")
        self.assertEqual(set(ROLE_MARKERS), {str(role) for role in ChatRole})

    def test_conversation_and_message_validation(self) -> None:
        with self.assertRaises(ValidationError):
            ChatConversation(conversation_id="  ", messages=(ChatMessage(role="user", content="hi"),))
        with self.assertRaises(ValidationError):
            ChatConversation(conversation_id="c", messages=("not a message",))  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            ChatConversation(conversation_id="c", messages=(ChatMessage(role="user", content="hi"),),
                             provider="   ")
        with self.assertRaises(ValidationError):
            ChatConversation(conversation_id="c", messages=(ChatMessage(role="user", content="hi"),),
                             started_at="not-a-time")
        with self.assertRaises(ValidationError):
            ChatImporter(max_bytes=0)
        self.assertTrue(issubclass(ChatImportError, MemorySystemError))
        self.assertTrue(issubclass(UnsupportedChatRoleError, ChatParseError))
        self.assertTrue(issubclass(EmptyConversationError, ChatImportError))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
