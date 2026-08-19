from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

from .turn_runtime import TurnRuntimeContext


_CURRENT_TURN: ContextVar[TurnRuntimeContext | None] = ContextVar(
    "current_turn",
    default=None,
)


def set_current_turn(
    context: TurnRuntimeContext,
) -> Token[TurnRuntimeContext | None]:
    return _CURRENT_TURN.set(context)


def get_current_turn() -> TurnRuntimeContext | None:
    return _CURRENT_TURN.get()


def reset_current_turn(token: Token[TurnRuntimeContext | None]) -> None:
    _CURRENT_TURN.reset(token)


@contextmanager
def runtime_stage(name: str) -> Iterator[None]:
    context = get_current_turn()
    if context is not None:
        context.start_stage(name)
    try:
        yield
    finally:
        if context is not None:
            context.finish_stage(name)


def register_tray_call() -> None:
    context = get_current_turn()
    if context is not None:
        context.tray_call_count += 1


def register_database_call() -> None:
    context = get_current_turn()
    if context is not None:
        context.database_call_count += 1


def register_integration_failure(provider: str) -> None:
    context = get_current_turn()
    if context is not None:
        context.register_integration_failure(provider)


def register_avoided_llm_call(
    reason: str,
    *,
    intended_call_type: str | None = None,
    intended_call_types: list[str] | None = None,
) -> None:
    context = get_current_turn()
    if context is not None:
        context.register_avoided_llm_call(
            reason,
            intended_call_type=intended_call_type,
            intended_call_types=intended_call_types,
        )
