"""Teacher runtime (KB 1.1 Phase 2C-4): one user input -> one complete turn.

Architecture (one direction only, orchestration layer)
------------------------------------------------------
::

    user_message + session_id
            │
            ▼
    TeacherRuntime.turn()                     ← this module: orchestration ONLY
            │
            ├─ 1..4  LearningService.get_context(session_id)
            │        LearningService.get_learning_overview(source_id)
            │        TeacherContext.from_learning(...)          (read-only projection)
            │
            ├─ 5..6  TeacherTurnRequest  →  TeacherLLMAdapter.generate(request)
            │                                 │
            │                                 ▼
            │                          TeacherTurnResponse        (validated contract)
            │                                 │  actions, in order
            ├─ 8     TeacherActionExecutor.execute(session_id=…, action=…)
            │                                 │
            │                                 ▼
            │                          LearningService   ← every rule still lives here
            │                                 │
            │                                 ▼
            │                          LearningRepository → SQLite
            │
            └─ 9..10 LearningService.get_context(session_id)   (re-read: latest state)
                     │
                     ▼
              TeacherTurnResult(response, context)

The runtime is a **thin composition** of four existing collaborators.  It decides
nothing of its own:

* it does not validate a session, a level, a Memory or a plan -- the service does
* it does not build actions, add actions, drop actions, reorder actions or repeat
  them: ``response.actions`` is executed exactly as the contract parsed it
* it does not own transactions and does not fake atomicity: if action *B* fails,
  *A*'s real effect stays (the service committed it) and *C* is simply never
  reached -- the exception travels up unchanged
* it never catches an exception: ``TeacherModelError`` (call failed),
  ``ValidationError`` (illegal output), ``ConflictError`` / ``NotFoundError``
  (the service refused) all propagate unwrapped
* it never retries and never calls the model twice for one turn: one
  ``turn()`` = at most one model call
* it never touches a repository, a database, SQL, HTTP or a provider SDK -- its only
  dependencies are the learning service and the teacher layers, and both are injected

Order of operations per turn (the numbering above is the code order):

1. read the session-scoped ``LearningContext`` (a missing session fails here, before
   the model is involved at all)
2. take ``source_id`` from that context
3. read the source-scoped ``LearningOverview``
4. project both into a read-only ``TeacherContext``
5. build the ``TeacherTurnRequest``
6. ask the adapter for one response (which asks the model exactly once, strictly
   parses the answer and validates the contract + session ids)
7. execute each action, in order, through the P2C-2 executor
8. re-read the ``LearningContext``, so what the caller gets back is the state *after*
   the turn -- never the pre-turn snapshot the model was shown

That last point is the whole reason this layer exists: the model reasons about the
turn's *input* state, while the caller must be handed the turn's *output* state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import ValidationError
from .teacher import TeacherAction, TeacherContext, TeacherTurnRequest, TeacherTurnResponse
from .teacher_executor import TeacherActionExecutor
from .teacher_llm import TeacherLLMAdapter, TeacherModel, TeacherPromptBuilder

if TYPE_CHECKING:  # annotations only -- the runtime never imports the engine at runtime
    from .learning import LearningContext, LearningService

__all__ = ["TeacherRuntime", "TeacherTurnResult"]


@dataclass(frozen=True)
class TeacherTurnResult:
    """One completed turn: what the model said, plus the state after execution.

    Two stored fields, on purpose:

    ``response``
        the validated model output (``assistant_message`` + the proposed actions, in
        the model's order).
    ``context``
        the ``LearningContext`` re-read **after** the actions ran -- the caller never
        receives the pre-turn snapshot the model was shown (that snapshot is not
        returned at all, so there is exactly one context in the result).

    ``executed_actions`` is a read-only alias of ``response.actions`` rather than a
    third stored field: a successful return means every proposed action was executed
    in order (any failure raises before a result is built), so storing the same tuple
    twice could only give the two copies a chance to disagree.
    """

    response: TeacherTurnResponse
    context: "LearningContext"

    def __post_init__(self) -> None:
        problems = []
        if not isinstance(self.response, TeacherTurnResponse):
            problems.append(("response", f"must be a TeacherTurnResponse, got {type(self.response).__name__}"))
        if self.context is None or not hasattr(self.context, "session"):
            problems.append(("context", f"must be a LearningContext, got {type(self.context).__name__}"))
        if problems:
            raise ValidationError(
                "invalid teacher turn result: "
                + "; ".join(f"{name}: {message}" for name, message in problems),
                problems=tuple(problems),
            )

    @property
    def executed_actions(self) -> tuple[TeacherAction, ...]:
        """The actions that ran, in order (== ``response.actions`` on any result)."""
        return self.response.actions

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view of the whole turn (no engine objects, plain data only)."""
        return {"response": self.response.as_dict(), "context": self.context.as_dict()}


class TeacherRuntime:
    """Single-turn orchestration: one user message in, one turn result out.

    Constructed with the service and the model it must use (dependency injection);
    the adapter and the executor are built from them here, so both validate their own
    dependencies exactly once and the runtime holds no repository, no database, no
    connection and no provider client.

    There is deliberately **one** public method: :meth:`turn`.  No loop, no session
    queue, no resumption, no approval flow.  A caller that wants a second turn calls
    ``turn()`` again -- two turns are two calls, each with its own fresh context.
    """

    def __init__(
        self,
        learning: "LearningService",
        model: TeacherModel,
        *,
        prompt_builder: TeacherPromptBuilder | None = None,
    ) -> None:
        if learning is None:
            raise ValidationError("the runtime needs a LearningService", field="learning")
        self.learning = learning
        self.adapter = TeacherLLMAdapter(model, prompt_builder=prompt_builder)
        self.executor = TeacherActionExecutor(learning)

    # ==================================================================
    # the only public method
    # ==================================================================
    def turn(self, *, session_id: str, user_message: str) -> TeacherTurnResult:
        """Run exactly one turn of one session and return its result.

        Nothing is caught, retried, skipped or rolled back:

        * the session does not exist          -> ``NotFoundError`` (before any model call)
        * the model call fails                -> ``TeacherModelError``
        * the model output is illegal         -> ``ValidationError``
        * the service refuses an action       -> ``ConflictError`` / ``NotFoundError``
          / ``ValidationError``, and the remaining actions are **not** executed
        """
        context = self._teacher_context(session_id)
        request = TeacherTurnRequest(
            session_id=session_id,
            user_message=user_message,
            context=context,
        )

        response = self.adapter.generate(request)

        for action in response.actions:                      # model order, no reordering
            self.executor.execute(session_id=session_id, action=action)

        # re-read: the caller must see the state *after* this turn, not the snapshot
        # the model was shown
        return TeacherTurnResult(response=response, context=self.learning.get_context(session_id))

    # ==================================================================
    # internals: read-only context assembly (no business rule of its own)
    # ==================================================================
    def _teacher_context(self, session_id: str) -> TeacherContext:
        """The read-only projection the model is allowed to see for this turn.

        Two reads that already exist (session-scoped context, source-scoped overview)
        and the contract's own pure projection -- no extra query layer, no writes, and
        no rule "computed" here.
        """
        learning_context = self.learning.get_context(session_id)
        overview = self.learning.get_learning_overview(learning_context.source.id)
        return TeacherContext.from_learning(learning_context, overview)
