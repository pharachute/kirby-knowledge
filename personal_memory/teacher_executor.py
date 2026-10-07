"""Teacher action execution (KB 1.1 Phase 2C-2): intent -> LearningService call.

Architecture (one direction only)
---------------------------------
::

    TeacherTurnResponse          (contract, P2C-1)
            │ actions
            ▼
    TeacherActionExecutor        ← this module: the ONLY place that turns an action
            │                      into a business call
            ▼
    LearningService              ← owns every rule (active session, current memory,
            │                      level legality, plan exhaustion, …)
            ▼
    LearningRepository → SQLite

The executor is deliberately thin:

* it maps one already-parsed :class:`~personal_memory.teacher.TeacherAction` onto the
  matching :class:`~personal_memory.learning.LearningService` method and returns that
  method's own :class:`~personal_memory.learning.LearningContext`
* it **decides nothing**: no state/session reads, no ``plan_cursor`` maths, no
  "understanding" inference, no extra action (no implicit ``record_learning`` after an
  assessment, no implicit new session after a finish)
* it **swallows no exception**: ``ConflictError`` / ``NotFoundError`` /
  ``ValidationError`` from the service travel up unchanged, so an agent runtime can
  still tell what actually happened
* it never touches SQLite / a repository / a transaction -- and it does not import
  the store, database or model modules at all

The one rule the executor *does* own is a safety boundary that belongs to the
transport layer, not to the learning domain: an action carrying its **own**
``session_id`` (finish/abandon) must name the session the caller is executing in.
Otherwise a model could end somebody else's session.

Single action per call, on purpose: "one agent intent -> one business operation" is
what this phase verifies.  Batching, atomicity across several actions, previews and
approval flows are explicitly out of scope (P2C-3+).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import ValidationError
from .teacher import (
    ALLOWED_ACTION_TYPES,
    AbandonSessionAction,
    FinishSessionAction,
    RecordAssessmentAction,
    RecordLearningAction,
    TeacherAction,
)

if TYPE_CHECKING:  # annotations only -- the executor never imports the engine at runtime
    from .learning import LearningContext, LearningService

__all__ = ["SUPPORTED_ACTION_TYPES", "TeacherActionExecutor"]

#: The action types this executor can map (identical to the contract's allowlist --
#: reused deliberately instead of being re-typed, so the two can never drift).
SUPPORTED_ACTION_TYPES: tuple[str, ...] = ALLOWED_ACTION_TYPES


class TeacherActionExecutor:
    """Maps one :class:`TeacherAction` onto the matching ``LearningService`` call.

    Constructed with the service it must use (dependency injection, same style as
    ``LearningService(learning, memory)``); it holds no repository, no database and no
    state of its own.
    """

    def __init__(self, learning: "LearningService") -> None:
        if learning is None:
            raise ValidationError("the executor needs a LearningService", field="learning")
        self.learning = learning

    # ==================================================================
    # public API
    # ==================================================================
    def execute(self, *, session_id: str, action: TeacherAction) -> "LearningContext":
        """Execute exactly one action in the context of ``session_id``.

        The returned :class:`LearningContext` is the service's own result (updated
        session, states and current memory included) -- this method adds nothing to it.

        Everything about *whether* the action is allowed stays inside
        :class:`~personal_memory.learning.LearningService`; the executor only refuses
        what it cannot map at all (a non-action, an unknown action subclass) and the
        session-id mismatch described in the module docstring.
        """
        self._check_session_id(session_id)
        if not isinstance(action, TeacherAction):
            raise ValidationError(
                f"action must be a TeacherAction, got {type(action).__name__}", field="action"
            )

        if isinstance(action, RecordLearningAction):
            return self.learning.record_learning(session_id, memory_id=action.memory_id)

        if isinstance(action, RecordAssessmentAction):
            return self.learning.record_assessment(
                session_id,
                understanding_level=action.understanding_level,
                known_aspects=action.known_aspects,
                weak_aspects=action.weak_aspects,
                misconceptions=action.misconceptions,
                memory_id=action.memory_id,
            )

        if isinstance(action, (FinishSessionAction, AbandonSessionAction)):
            self._check_action_session_id(session_id, action)
            if isinstance(action, FinishSessionAction):
                return self.learning.finish_session(session_id)
            return self.learning.abandon_session(session_id)

        # a subclass the contract never defined (e.g. a hand-rolled "delete_memory"):
        # refused here too, so no unknown action can ever reach the engine
        raise ValidationError(
            f"unsupported teacher action {type(action).__name__!r} (kind={getattr(action, 'kind', '?')!r}); "
            f"the executor only handles {list(SUPPORTED_ACTION_TYPES)}",
            field="action",
        )

    # ==================================================================
    # guards (transport-level only -- no domain rules)
    # ==================================================================
    @staticmethod
    def _check_session_id(session_id: Any) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValidationError(
                f"session_id must be a non-empty string, got {session_id!r}", field="session_id"
            )

    @staticmethod
    def _check_action_session_id(session_id: str, action: TeacherAction) -> None:
        """An action may not carry a different session than the one being executed."""
        action_session_id = getattr(action, "session_id", None)
        if str(action_session_id) != str(session_id):
            raise ValidationError(
                f"action names session {action_session_id!r} but the executor is running in "
                f"{session_id!r}; an action cannot act on another session",
                field="session_id",
            )
