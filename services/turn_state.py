"""Turn lifecycle states and transition rules."""

from __future__ import annotations

from enum import StrEnum


class TurnStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"


ALLOWED_TURN_TRANSITIONS: dict[TurnStatus, set[TurnStatus]] = {
    TurnStatus.RECEIVED: {TurnStatus.PROCESSING},
    TurnStatus.PROCESSING: {
        TurnStatus.COMPLETED,
        TurnStatus.FAILED_RETRYABLE,
        TurnStatus.FAILED_FINAL,
    },
    TurnStatus.FAILED_RETRYABLE: {TurnStatus.PROCESSING},
    TurnStatus.COMPLETED: set(),
    TurnStatus.FAILED_FINAL: set(),
}


class InvalidTurnTransitionError(ValueError):
    """Raised when a turn lifecycle transition is not allowed."""


def validate_turn_transition(current: str, target: str) -> None:
    try:
        current_status = TurnStatus(current)
        target_status = TurnStatus(target)
    except ValueError as exc:
        raise InvalidTurnTransitionError(f"Unknown turn transition: {current} -> {target}") from exc
    if target_status not in ALLOWED_TURN_TRANSITIONS[current_status]:
        raise InvalidTurnTransitionError(f"Invalid turn transition: {current} -> {target}")