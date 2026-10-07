"""Teacher application boundary (KB 1.1 Phase P2D-1): the thin entry point for callers.

Where it sits
-------------
::

    UI / Web / CLI / a future agent host        ← only three calls, no wiring
            │  start · active_session · turn
            ▼
    TeacherApplication                          ← this module: application boundary ONLY
            ├─ start()          → LearningService.start_session(...)
            ├─ active_session() → LearningService.get_active_session(...)
            └─ turn()           → TeacherAgent.run(...)
                                        ▼
                                TeacherRuntime.turn()
                                        ▼
                                TeacherLLMAdapter
                                        ▼
                                TeacherProvider

The pattern is always the same four steps: *receive input → find or create the existing
object → call the layer that already owns the operation → return that layer's own
result*.  There is deliberately nothing else in this module:

* **no business rule** -- an active/completed/abandoned session, the current Memory, the
  plan cursor, assessment legality and ``memory_id`` validation all stay in
  :class:`~personal_memory.learning.LearningService`
* **no action validation, no prompt decision, no model call, no HTTP** -- those live in
  the executor, the prompt builders (whose version is chosen by the runtime, P2C-7), the
  adapter and the provider
* **no database access at all** -- not sqlite, not a repository, not SQL: the facade
  holds a ``LearningService`` and an agent, nothing else
* **no retry, no fallback, no rollback, no swallowed exception** -- the module contains
  no ``try``/``except``; ``ValidationError``, ``ConflictError``, ``NotFoundError`` and
  ``TeacherModelError`` travel to the caller exactly as they were raised
* **no loop and no state**: one ``turn()`` is one ``TeacherAgent.run``; a facade instance
  keeps no session, no cursor and no history between calls, and nothing is persisted

Two ways in, so that no caller ever has to write ``TeacherProvider(...)``,
``TeacherLLMAdapter(...)`` or a prompt-builder class:

``TeacherApplication(learning, agent)``
    Bring your own stack: whatever object provides ``run(...)`` (a real
    :class:`~personal_memory.teacher_agent.TeacherAgent`, or a stub in a test).  This
    constructor knows nothing about providers, prompts or transports.
``TeacherApplication.compose(learning, model=... | config=... | config_path=...)``
    The real-world path for an application: either hand it a ``TeacherModel`` (a real
    provider, a local model, a fake) or let it read the project's existing LLM config
    (:func:`~personal_memory.llm.load_config`) and build provider → runtime → agent
    internally.  The prompt version is whatever the runtime's default is (P2C-7:
    ``teacher_prompt_v2``); this module never names a prompt builder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from .errors import ValidationError
from .llm import LLMConfig, Transport, load_config
from .teacher_agent import DEFAULT_MAX_STEPS, TeacherAgent, TeacherAgentResult
from .teacher_provider import TeacherProvider
from .teacher_runtime import TeacherRuntime

if TYPE_CHECKING:  # annotations only -- the engine is never imported at runtime
    from .learning import LearningContext, LearningService, LearningSession
    from .teacher_llm import TeacherModel

__all__ = ["TeacherApplication"]


class TeacherApplication:
    """Session lifecycle plus one bounded teacher turn, for application code.

    Every method is a delegation, so the caller gets the *same* objects and the *same*
    exceptions the layers below already produce -- there is no second result model, no
    second learning service and no hidden state to keep in sync.
    """

    def __init__(self, learning: "LearningService", agent: "TeacherAgent") -> None:
        if learning is None:
            raise ValidationError("the application needs a LearningService", field="learning")
        if agent is None:
            raise ValidationError("the application needs a TeacherAgent", field="agent")
        if not callable(getattr(agent, "run", None)):
            raise ValidationError(
                f"an agent must provide run(...), got {type(agent).__name__}", field="agent"
            )
        for method in ("start_session", "get_active_session"):
            if not callable(getattr(learning, method, None)):
                raise ValidationError(
                    f"a LearningService must provide {method}(...), got "
                    f"{type(learning).__name__}",
                    field="learning",
                )
        self.learning = learning
        self.agent = agent

    # ==================================================================
    # composition (only used by a composition root -- never by a request handler)
    # ==================================================================
    @classmethod
    def compose(
        cls,
        learning: "LearningService",
        *,
        model: "TeacherModel | None" = None,
        config: LLMConfig | None = None,
        config_path: Any = None,
        transport: Transport | None = None,
    ) -> "TeacherApplication":
        """Build the production stack (provider → runtime → agent) and wrap it.

        * ``model`` -- use this ``TeacherModel`` as is (a real provider, a local model or
          a fake); ``config``/``config_path`` are then ignored, so an offline test never
          needs a credential.
        * ``config`` -- an already-loaded :class:`~personal_memory.llm.LLMConfig`.
        * ``config_path`` -- otherwise the project's own
          :func:`~personal_memory.llm.load_config` is used (it resolves the key from
          overrides, the environment, the JSON file, then provider defaults, and raises
          ``LLMConfigError`` when there is none).  No key, base URL or model name is
          written here.
        * ``transport`` -- injected into the real provider (offline wire tests, or a
          different protocol), passed straight through to
          :class:`~personal_memory.teacher_provider.TeacherProvider`.

        The prompt version is not a parameter: the runtime's default applies, which keeps
        this phase's boundary free of prompt decisions.
        """
        if model is not None:
            return cls(learning, TeacherAgent(TeacherRuntime(learning, model)))

        resolved = load_config(config_path) if config is None else config
        provider = TeacherProvider(resolved, transport=transport)
        return cls(learning, TeacherAgent(TeacherRuntime(learning, provider)))

    # ==================================================================
    # the application API (three calls, three delegations)
    # ==================================================================
    def start(
        self,
        *,
        source_id: str,
        memory_ids: Sequence[str] | None = None,
    ) -> "LearningContext":
        """Start the Source's learning session and return the service's own context.

        Pure delegation to :meth:`LearningService.start_session`: the plan is the
        Source's linked Memories (or exactly ``memory_ids``, in that order), and every
        refusal -- empty Source, Memory not linked to the Source, a Source that already
        has a running session -- is raised by the service, unwrapped.
        """
        return self.learning.start_session(source_id=source_id, memory_ids=memory_ids)

    def active_session(self, source_id: str) -> "LearningSession | None":
        """The Source's running learning session, or ``None`` (for a resume flow).

        Pure delegation to :meth:`LearningService.get_active_session`; "active" keeps
        the engine's meaning and is not re-decided here.
        """
        return self.learning.get_active_session(source_id)

    def turn(
        self,
        *,
        session_id: str,
        user_message: str,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> TeacherAgentResult:
        """Run one bounded teacher turn sequence and return the agent's own result.

        Pure delegation to :meth:`TeacherAgent.run`: the agent owns the step budget, the
        stop conditions and the ``runtime.turn`` calls.  ``max_steps`` is forwarded
        verbatim (its default is the agent's own constant), and every failure --
        ``TeacherModelError``, ``ValidationError``, ``ConflictError``,
        ``NotFoundError`` -- reaches the caller unchanged, with no retry and no rollback.
        """
        return self.agent.run(
            session_id=session_id,
            user_message=user_message,
            max_steps=max_steps,
        )
