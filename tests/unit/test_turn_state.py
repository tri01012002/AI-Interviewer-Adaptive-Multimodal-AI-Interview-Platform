import pytest

from services.turn_state import InvalidTurnTransitionError, TurnStatus, validate_turn_transition


def test_valid_turn_transitions_are_allowed():
    validate_turn_transition(TurnStatus.RECEIVED, TurnStatus.PROCESSING)
    validate_turn_transition(TurnStatus.PROCESSING, TurnStatus.COMPLETED)
    validate_turn_transition(TurnStatus.PROCESSING, TurnStatus.FAILED_RETRYABLE)
    validate_turn_transition(TurnStatus.FAILED_RETRYABLE, TurnStatus.PROCESSING)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TurnStatus.COMPLETED, TurnStatus.PROCESSING),
        (TurnStatus.COMPLETED, TurnStatus.COMPLETED),
        (TurnStatus.FAILED_FINAL, TurnStatus.PROCESSING),
        (TurnStatus.RECEIVED, TurnStatus.COMPLETED),
    ],
)
def test_invalid_turn_transitions_are_rejected(current, target):
    with pytest.raises(InvalidTurnTransitionError):
        validate_turn_transition(current, target)