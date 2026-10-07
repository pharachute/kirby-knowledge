"""Teacher product entry for the Knowledge Base UI (P2D-2).

Where it sits
-------------
::

    /learn/<memory_id> 页面（UI）
            │  只有这些调用
            ▼
    TeacherEntry                     ← this module: the product's only Teacher dependency
            ├─ active_session(source_id)  → TeacherApplication.active_session(...)
            ├─ start(memory_id)           → TeacherApplication.start(source_id, memory_ids=[memory])
            └─ turn(memory_id, message)   → TeacherApplication.turn(session_id, user_message)
                                            ▼
                                    TeacherAgent → TeacherRuntime → Adapter → Provider

The UI layer never touches ``LearningService``, ``TeacherAgent``, ``TeacherRuntime``,
``TeacherLLMAdapter``, ``TeacherProvider``, the executor or a prompt builder: everything
goes through :class:`~personal_memory.teacher_application.TeacherApplication`, which
P2D-1 already froze.

What this module owns (and nothing more):

* **the product's Teacher composition** -- one lazily built ``TeacherApplication`` per
  context.  Lazy on purpose: browsing memories must keep working when no model is
  configured, exactly like Capture/Import do today.
* **"learn this Memory"** -- the engine's session API is Source-scoped, so a Memory
  entry point resolves the Memory's real Source (existing read API) and starts a session
  whose plan is exactly that one Memory (``memory_ids=[memory_id]``, an existing
  parameter).  No new engine API, no plan invented in the UI.
* **the presentation of Teacher state** -- a read-only snapshot for the page and a JSON
  view of one turn result.  All learning facts come from the objects the application
  boundary returned (``LearningSession`` / ``LearningContext`` / ``TeacherAgentResult``);
  this module never decides whether a learning action is legal, never writes a learning
  row and never re-implements a session rule.
* **error presentation** -- human wording plus the original exception text, so the user
  can understand and retry while the underlying semantics stay visible (P2D-1 showed a
  real model can produce an illegal ``memory_id`` and be refused: that refusal must never
  render as success).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from ..errors import MemorySystemError, ValidationError
from ..learning import LearningService
from ..learning_store import LearningRepository
from ..llm import LLMConfigError
from ..teacher_application import TeacherApplication
from ..teacher_llm import TeacherModelError
from .components import back_link, empty_state, section, status_chip
from .views import Flash, escape, layout

if TYPE_CHECKING:  # annotations only
    from ..learning import LearningContext, LearningSession
    from ..teacher_agent import TeacherAgentResult

__all__ = ["TeacherEntry", "describe_error", "error_payload", "learn_href", "learn_page",
           "turn_payload"]

#: Base route of the Memory -> Teacher pages (``/learn/<memory_id>``).
LEARN_ROUTE = "/learn"

#: Session status -> user wording (the enum itself keeps its engine meaning).
SESSION_LABELS: dict[str, str] = {
    "active": "进行中",
    "completed": "已完成",
    "abandoned": "已放弃",
}

#: Why the agent loop stopped -> whether the user can keep talking.
STOP_LABELS: dict[str, str] = {
    "no_actions": "这一轮结束了（模型没有提出学习状态变化）",
    "session_ended": "这次学习已经结束",
    "max_steps": "达到本轮步数上限，可以继续发下一条消息",
}

#: Action kind -> user wording for "what this turn changed".
ACTION_LABELS: dict[str, str] = {
    "record_learning": "记录了一次学习",
    "record_assessment": "更新了理解程度",
    "finish_session": "结束了这次学习",
    "abandon_session": "放弃了这次学习",
}


def _text(value: Any, limit: int = 4000) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit:
        text = text[:limit] + f"\n…（共 {len(text)} 字符，已截断显示）"
    return text


def describe_error(exc: BaseException) -> tuple[str, str]:
    """Map a failure to ``(中文说明, 原始技术信息)``.

    The wording is friendly; the original text is kept verbatim so the page never hides
    *why* something was refused (and never turns a refusal into a success).
    """
    detail = _text(str(exc), 600)
    if isinstance(exc, TeacherModelError):
        return "模型这次没有回答成功，学习状态没有变化。", detail
    if isinstance(exc, ValidationError):
        return "这次请求不满足当前状态的要求，学习状态没有变化。", detail
    if isinstance(exc, MemorySystemError):
        name = type(exc).__name__
        if name == "ConflictError":
            return "卡比提出的学习动作没有被接受，学习状态没有变化。", detail
        if name == "NotFoundError":
            return "找不到这条记忆、来源或这次学习。", detail
        return "这次操作没有完成，学习状态没有变化。", detail
    return "发生了未预期的错误，操作没有完成。", f"{type(exc).__name__}: {detail}"


class TeacherEntry:
    """The product's Teacher entry point: Memory -> session -> turn.

    One instance per :class:`~personal_memory.web.server.WebContext`; it builds the
    ``TeacherApplication`` once, on first use.
    """

    def __init__(self, context: Any) -> None:
        self.context = context
        self._application: TeacherApplication | None = None

    # ==================================================================
    # composition (lazy: the UI keeps working without a model)
    # ==================================================================
    @property
    def application(self) -> TeacherApplication:
        if self._application is None:
            self._application = self._build_application()
        return self._application

    def _build_application(self) -> TeacherApplication:
        repository = self.context.repository
        # the same Database object the web context already uses, shared with the
        # learning repository -- this is the composition root, not a second database.
        learning = LearningService(LearningRepository(repository.database), repository)
        model = getattr(self.context, "teacher_model", None)
        if model is not None:
            # injected TeacherModel (tests, a local model): no credential needed
            return TeacherApplication.compose(learning, model=model)
        if self.context.llm_config is None:
            raise LLMConfigError(
                "模型未配置，无法开始学习：" + (self.context.llm_error or "未知原因")
            )
        return TeacherApplication.compose(learning, config=self.context.llm_config)

    def available(self) -> tuple[bool, str]:
        """``(ready, reason)`` -- a missing model is a normal, explainable state."""
        try:
            self.application
        except LLMConfigError as exc:
            return False, str(exc)
        return True, ""

    # ==================================================================
    # the three product operations
    # ==================================================================
    def sources_for_memory(self, memory_id: str) -> list[Any]:
        """The Memory's real Sources (existing read API; no new query semantics)."""
        return list(self.context.repository.get_sources_for_memory(memory_id))

    def active_session(self, source_id: str) -> "LearningSession | None":
        """Delegated to the application boundary -- never to ``LearningService``."""
        return self.application.active_session(source_id)

    def start(self, memory_id: str) -> "LearningContext":
        """Start a learning session over this Memory's material, beginning at this Memory.

        The plan is the engine's own default (every Memory linked to the Source, in the
        Source's order) *rotated* so the Memory the user clicked comes first -- the
        existing ``memory_ids`` parameter already means "this list, in this order", so no
        new plan semantics are invented here.  Starting from the whole material keeps the
        session usable for more than one learning event.
        """
        memory = self.context.repository.require_memory(memory_id)
        sources = self.sources_for_memory(memory_id)
        if not sources:
            raise ValidationError(
                "这条记忆没有关联来源，无法开始一次以材料为单位的学习", field="source_id"
            )
        source = sources[0]
        plan = [memory.id] + [
            other.id
            for other in self.context.repository.get_memories_for_source(source.id)
            if other.id != memory.id
        ]
        return self.application.start(source_id=source.id, memory_ids=plan)

    def turn(self, memory_id: str, user_message: str) -> "TeacherAgentResult":
        """Send one message to the session that is running for this Memory's Source."""
        sources = self.sources_for_memory(memory_id)
        if not sources:
            raise ValidationError("这条记忆没有关联来源，无法进行学习", field="source_id")
        session = self.application.active_session(sources[0].id)
        if session is None:
            raise ValidationError(
                "这条记忆还没有进行中的学习，请先点「开始学习」", field="session_id"
            )
        return self.application.turn(session_id=session.id, user_message=user_message)


# ==========================================================================
# presentation (pure functions of the data the HTTP layer already read)
# ==========================================================================

def _paragraphs(text: Any) -> str:
    blocks = [block.strip() for block in ("" if text is None else str(text)).split("\n\n")
              if block.strip()]
    return "".join(f"<p>{escape(block)}</p>" for block in blocks)


def _session_line(session: Any, current_memory: Any) -> str:
    if session is None:
        return "<p class=\"muted\">这条记忆还没有开始学习。</p>"
    status = str(session.status)
    label = SESSION_LABELS.get(status, status)
    total = len(session.plan)
    cursor = int(session.plan_cursor)
    current = (
        escape(current_memory.title)
        if current_memory is not None
        else "（这次学习已经走完了全部内容）"
    )
    return (
        f"<p>学习状态：<strong>{escape(label)}</strong>"
        f"（计划 {total} 条，进行到第 {min(cursor + 1, total)} 条）</p>"
        f"<p>当前正在学：<strong>{current}</strong></p>"
        f'<p class="muted">这些状态来自这次学习返回的真实数据，页面不做任何推断。</p>'
    )


def _sources_html(sources: Sequence[Any]) -> str:
    if not sources:
        return '<p class="muted">这条记忆没有关联来源，因此无法开始一次以材料为单位的学习。</p>'
    items = "".join(
        f'<li><a href="/sources/{escape(source.id)}"><span>{escape(source.title or "无标题来源")}'
        f"</span></a></li>"
        for source in sources
    )
    return f'<ul class="source-list">{items}</ul>'


def _actions_html(result: Any) -> str:
    kinds = [action.kind for action in result.executed_actions]
    if not kinds:
        return '<p class="muted">这一轮没有修改学习状态。</p>'
    labels = "、".join(ACTION_LABELS.get(kind, kind) for kind in kinds)
    return f"<p>这一轮的动作：<strong>{escape(labels)}</strong></p>"


def _turn_html(result: Any) -> str:
    context = result.context
    session = context.session
    current_id = session.current_memory_id
    current = context.memory_for(current_id) if current_id else None
    steps = list(result.responses)
    messages = []
    for index, response in enumerate(steps, start=1):
        prefix = f'<p class="muted">第 {index} 步</p>' if len(steps) > 1 else ""
        messages.append(prefix + _paragraphs(response.assistant_message))
    stop = STOP_LABELS.get(result.stop_reason, result.stop_reason)
    return section(
        "卡比的回答",
        "".join(messages)
        + _actions_html(result)
        + f'<p class="muted">{escape(stop)}</p>'
        + _session_line(session, current),
    )


def _error_html(error: BaseException | None) -> str:
    if error is None:
        return ""
    friendly, detail = describe_error(error)
    return (
        f'<div class="err"><strong>✗ {escape(friendly)}</strong>'
        f'<div class="muted">{escape(detail)}</div>'
        f'<div class="muted">你可以直接再发一条消息，或者先自己看看当前状态。</div></div>'
        f"<!-- teacher error kind: {escape(type(error).__name__)} -->"
    )


def _turn_form(memory: Any, *, message: str, enabled: bool) -> str:
    if not enabled:
        return ""
    return (
        f'<form method="post" action="{LEARN_ROUTE}/{escape(memory.id)}/turn">'
        '<label for="message">想对卡比说什么</label>'
        f'<textarea id="message" name="message" required placeholder="例如：给我讲讲这条">'
        f"{escape(message)}</textarea>"
        '<p><button class="btn primary" type="submit">发送</button></p>'
        "</form>"
        '<p class="muted">这一步会真的调用模型；如果模型提出的学习动作不合法，'
        "会被引擎拒绝，页面会如实告诉你，不会假装成功。</p>"
    )


def _start_form(memory: Any) -> str:
    return (
        f'<form method="post" action="{LEARN_ROUTE}/{escape(memory.id)}/start">'
        '<button class="btn primary" type="submit">开始学习</button>'
        "</form>"
        '<p class="muted">开始后卡比会以这条记忆为内容和你对话；'
        "学习状态只由既有学习引擎写入。</p>"
    )


def learn_page(
    *,
    memory: Any,
    sources: Sequence[Any],
    session: Any | None,
    current_memory: Any | None,
    ready: bool,
    reason: str = "",
    turn: Any | None = None,
    error: BaseException | None = None,
    message: str = "",
    flash: Flash | None = None,
) -> str:
    """The Memory -> Teacher page (开始学习 / 对话 / 状态)."""
    summary_html = (
        f'<div class="summary"><strong>一句话摘要</strong>：{escape(memory.summary)}</div>'
        if (memory.summary or "").strip()
        else ""
    )
    if not ready:
        chat_html = empty_state(
            "还不能开始学习",
            reason or "模型未配置，配置好模型后就能在这里和卡比对话。",
            action_label="回到我的记忆",
            action_href="/memories",
        )
        state_html = '<p class="muted">没有可用的模型，所以这次学习无法开始。</p>'
    elif session is None:
        state_html = _session_line(None, None)
        chat_html = _start_form(memory) if sources else (
            '<p class="muted">这条记忆没有关联来源，无法开始学习。</p>'
        )
    else:
        state_html = _session_line(session, current_memory)
        chat_html = _turn_form(memory, message=message, enabled=True)

    body = f"""
{back_link(f"/memories/{escape(memory.id)}", "← 返回这条记忆")}
<article class="memory-detail">
  <div class="kicker"><span class="glyph">🎓</span> 学习 · {status_chip(memory.status)}</div>
  <h1>{escape(memory.title)}</h1>
  <div class="body">{_paragraphs(memory.content) or "<p>（这条记忆没有正文。）</p>"}</div>
  {summary_html}
</article>
{_error_html(error)}
{section("这条记忆的当前状态", state_html)}
{section("来自", _sources_html(sources))}
{section("和卡比聊聊", chat_html)}
{_turn_html(turn) if turn is not None else ""}
"""
    return layout(title=f"学习：{memory.title}", body=body, current="memories", flash=flash)


def turn_payload(result: "TeacherAgentResult") -> dict[str, Any]:
    """JSON view of one turn (for ``fetch`` clients); plain data only, no new queries."""
    context = result.context
    session = context.session
    current_id = session.current_memory_id
    current = context.memory_for(current_id) if current_id else None
    return {
        "ok": True,
        "assistant_message": result.final_response.assistant_message,
        "messages": [response.assistant_message for response in result.responses],
        "steps": result.steps,
        "stop_reason": result.stop_reason,
        "actions": [action.kind for action in result.executed_actions],
        "action_types": list(result.final_response.action_types),
        "session": {
            "id": session.id,
            "status": str(session.status),
            "plan_size": len(session.plan),
            "plan_cursor": session.plan_cursor,
            "current_memory_id": current_id,
        },
        "current_memory": (
            {"id": current.id, "title": current.title} if current is not None else None
        ),
        "learning": [
            {
                "memory_id": state.memory_id,
                "learn_count": state.learn_count,
                "understanding_level": str(state.understanding_level),
            }
            for state in context.states
        ],
    }


def learn_href(memory_id: str) -> str:
    """The Memory → Teacher URL the Memory detail page links to (P2D-2 entry point)."""
    return f"{LEARN_ROUTE}/{memory_id}"


def error_payload(exc: BaseException) -> dict[str, Any]:
    """JSON view of a refused turn/start (same shape as :func:`turn_payload`, ``ok=False``)."""
    friendly, detail = describe_error(exc)
    return {
        "ok": False,
        "error": type(exc).__name__,
        "message": friendly,
        "detail": detail,
    }
