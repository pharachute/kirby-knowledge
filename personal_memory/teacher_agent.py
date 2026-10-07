"""Bounded teacher agent loop (KB 1.1 Phase 2C-6): one input, a few internal steps.

Architecture (one direction only)
---------------------------------
::

    user_message + session_id
            │
            ▼
    TeacherAgent.run(max_steps)         ← this module: bounded orchestration ONLY
            │  step 1 … N   (N <= max_steps <= MAX_STEPS_LIMIT)
            ▼
    TeacherRuntime.turn()               (P2C-4: read context → ONE model call →
            │                            execute actions in order → re-read context)
            ▼
    TeacherLLMAdapter → TeacherActionExecutor → LearningService → LearningRepository

One loop step *is* one :meth:`TeacherRuntime.turn`, so the loop inherits every
guarantee of the single-turn layer instead of restating it: one model call per step,
strict parsing, ordered execution, no retry, unwrapped errors, and a context that is
re-read after execution.  This module therefore adds exactly one thing to the system:
**a bound plus explicit stop rules**.

Stop conditions (any one ends the run -- the model does not get a vote)
----------------------------------------------------------------------
``no_actions``
    the step proposed nothing (``actions == ()``): answering is not a state change.
``session_ended``
    the step proposed ``finish_session`` / ``abandon_session`` (contract-derived
    :data:`STOP_ACTION_TYPES`).
``max_steps``
    the budget is used up -- including the case where the model keeps proposing the
    same action forever.  This is why no de-duplication policy is needed: repetition
    cannot extend a run beyond ``max_steps``, and the loop never decides *whether* an
    action is sensible (that stays in ``LearningService``).

What the loop deliberately does **not** do
------------------------------------------
* no retry, no skipping, no rollback, no error wrapping: a failure anywhere (model,
  adapter, executor, service) travels up unchanged and ends the run, while the steps
  that already committed keep their real effect
* no direct service/repository/database access: its only collaborator is the injected
  runtime (no ``get_context`` call of its own, no SQL, no sqlite)
* no second learning engine and no business rule of its own: it does not judge
  "should this be learned", does not touch the session plan, and does not create
  sessions
* no hidden chat history: every step sends the **same** ``user_message`` (still
  untrusted data) and the *fresh* context the runtime hands back.  The previous
  ``assistant_message`` is never pasted into a user message, and no system state is
  disguised as user input -- the observable effect of what was already executed is
  already part of the next step's context (progress, counts, levels), which is the
  loop's only state channel.
* no recursion and no ``while``: a ``for`` over a validated positive ``int``

Failure semantics (unchanged from P2C-4, and no better in this layer)
--------------------------------------------------------------------
If step *k* proposes ``[A, B]`` and *B* fails, then *A*'s effect is committed, *B*
raises and the loop stops -- that is what "the service owns the transaction" means.
The loop never tries to make the run atomic across steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from .errors import ValidationError
from .teacher import (
    AbandonSessionAction,
    FinishSessionAction,
    TeacherAction,
    TeacherTurnResponse,
)
from .teacher_runtime import TeacherRuntime

if TYPE_CHECKING:  # annotation only -- the loop never imports the engine at runtime
    from .learning import LearningContext

__all__ = [
    "DEFAULT_MAX_STEPS",
    "MAX_STEPS_LIMIT",
    "STOP_ACTION_TYPES",
    "TeacherAgent",
    "TeacherAgentResult",
]

#: Steps a single run uses when the caller does not say otherwise.
DEFAULT_MAX_STEPS: int = 3

#: Absolute ceiling for one run.  A caller may lower the budget but never raise it
#: beyond this, so "bounded" holds even for a caller that passes a huge number.
MAX_STEPS_LIMIT: int = 10

#: The contract actions that *by definition* end a learning run.  Derived from the
#: contract classes so a rename cannot silently turn a stop condition into a no-op.
STOP_ACTION_TYPES: tuple[str, ...] = (
    FinishSessionAction.kind,
    AbandonSessionAction.kind,
)


def _check_max_steps(value: Any) -> int:
    """A step budget: a positive ``int``, never a bool, never above the ceiling."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            f"max_steps must be a positive integer, got {value!r}", field="max_steps"
        )
    if value < 1:
        raise ValidationError(f"max_steps must be at least 1, got {value}", field="max_steps")
    if value > MAX_STEPS_LIMIT:
        raise ValidationError(
            f"max_steps must not exceed {MAX_STEPS_LIMIT} (hard run limit), got {value}",
            field="max_steps",
        )
    return value


def _ends_the_turn(response: TeacherTurnResponse) -> bool:
    """Stop conditions A and B/C, read from the response only (no domain judgement)."""
    if not response.actions:
        return True
    return any(action.kind in STOP_ACTION_TYPES for action in response.actions)


@dataclass(frozen=True)
class TeacherAgentResult:
    """One bounded run: every step's response, plus the state after the last step.

    Two stored fields, for the same reason :class:`~personal_memory.teacher_runtime.TeacherTurnResult`
    stores two:

    ``responses``
        one :class:`~personal_memory.teacher.teacher.TeacherTurnResponse` per executed
        step, in order; ``responses[-1]`` is what the user should be told.
    ``context``
        the ``LearningContext`` re-read after the **last** step's actions -- the real
        final state, never a pre-turn snapshot (and never duplicated: a run keeps
        exactly one context).

    ``steps``, ``final_response``, ``executed_actions`` and ``stop_reason`` are read-only
    views derived from ``responses``: a run that returned normally executed every
    action it proposed, so storing them again could only let the copies disagree.
    """

    responses: tuple[TeacherTurnResponse, ...]
    context: "LearningContext"

    def __post_init__(self) -> None:
        problems: list[tuple[str, str]] = []
        if isinstance(self.responses, (str, bytes)) or not isinstance(self.responses, Sequence):
            problems.append(
                ("responses", f"must be a sequence of TeacherTurnResponse, got {type(self.responses).__name__}")
            )
        elif not self.responses:
            problems.append(("responses", "a run always has at least one response"))
        else:
            for index, response in enumerate(self.responses):
                if not isinstance(response, TeacherTurnResponse):
                    problems.append(
                        ("responses", f"entry {index} must be a TeacherTurnResponse, "
                                      f"got {type(response).__name__}")
                    )
        if self.context is None or not hasattr(self.context, "session"):
            problems.append(
                ("context", f"must be a LearningContext, got {type(self.context).__name__}")
            )
        if problems:
            raise ValidationError(
                "invalid teacher agent result: "
                + "; ".join(f"{name}: {message}" for name, message in problems),
                problems=tuple(problems),
            )
        object.__setattr__(self, "responses", tuple(self.responses))

    # -- derived views -----------------------------------------------------
    @property
    def steps(self) -> int:
        """How many internal steps ran (== model calls made, == ``len(responses)``)."""
        return len(self.responses)

    @property
    def final_response(self) -> TeacherTurnResponse:
        return self.responses[-1]

    @property
    def executed_actions(self) -> tuple[TeacherAction, ...]:
        """Every executed action of the run, in step order and within-step order."""
        return tuple(action for response in self.responses for action in response.actions)

    @property
    def stop_reason(self) -> str:
        """Why the loop ended: ``no_actions`` / ``session_ended`` / ``max_steps``."""
        last = self.responses[-1]
        if not last.actions:
            return "no_actions"
        if any(action.kind in STOP_ACTION_TYPES for action in last.actions):
            return "session_ended"
        return "max_steps"

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view of the whole run (plain data only)."""
        return {
            "responses": [response.as_dict() for response in self.responses],
            "context": self.context.as_dict(),
            "steps": self.steps,
            "stop_reason": self.stop_reason,
        }


class TeacherAgent:
    """Runs at most ``max_steps`` teacher steps for one user message.

    The runtime is injected: the agent owns no model, no service, no database and no
    prompt -- choosing the prompt version (for example
    :class:`~personal_memory.teacher_llm.TeacherPromptV2Builder`) stays a decision of
    whoever builds the runtime.

    ::

        runtime = TeacherRuntime(service, provider, prompt_builder=TeacherPromptV2Builder())
        result  = TeacherAgent(runtime).run(session_id=sid, user_message="…")
    """

    def __init__(self, runtime: TeacherRuntime) -> None:
        if runtime is None:
            raise ValidationError("the agent needs a TeacherRuntime", field="runtime")
        if not callable(getattr(runtime, "turn", None)):
            raise ValidationError(
                f"a TeacherRuntime must provide turn(...), got {type(runtime).__name__}",
                field="runtime",
            )
        self.runtime = runtime

    # ==================================================================
    # the only public method
    # ==================================================================
    def run(
        self,
        *,
        session_id: str,
        user_message: str,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> TeacherAgentResult:
        """Run one bounded turn sequence and return every step plus the final state.

        At most ``max_steps`` model calls happen: steps stop early on
        ``actions == ()`` or on ``finish_session`` / ``abandon_session``, and the
        budget stops them otherwise.  Nothing is caught, retried, skipped or rolled
        back -- the first failure ends the run with its own exception.
        """
        budget = _check_max_steps(max_steps)          # before any model call

        responses: list[TeacherTurnResponse] = []
        final_context: "LearningContext | None" = None
        for _ in range(budget):
            turn = self.runtime.turn(session_id=session_id, user_message=user_message)
            responses.append(turn.response)
            final_context = turn.context
            if _ends_the_turn(turn.response):
                break

        # budget >= 1 (validated above), so the loop always assigned a final context
        return TeacherAgentResult(responses=tuple(responses), context=final_context)
