"""SMTP outcome classification by protocol phase (stubbed SMTP, no network).

Rule: a failure before the message is handed to the server is
``retryable_not_accepted`` (definitely not sent) or ``definitively_rejected``;
a failure while sending may follow server acceptance and is ``unknown``,
which the communication service routes to needs_attention, never to retry.
"""

from __future__ import annotations

import smtplib
import socket

import pytest
from pydantic import SecretStr

from services.email_provider import EmailDeliveryRequest, SmtpEmailProvider

SECRET = "sentinel-smtp-password-93be"


class PhasedSmtp:
    """SMTP stub that fails in one named phase."""

    def __init__(self, fail_phase: str | None = None, error: BaseException | None = None):
        self.fail_phase = fail_phase
        self.error = error
        self.calls: list[str] = []
        self.messages = []

    def _step(self, phase: str):
        self.calls.append(phase)
        if phase == self.fail_phase:
            raise self.error

    def __enter__(self):
        self._step("connect")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._step("quit")
        return False

    def ehlo(self):
        self._step("ehlo")

    def starttls(self, *, context):
        self._step("starttls")

    def login(self, username, password):
        assert password == SECRET
        self._step("login")

    def send_message(self, message):
        self.messages.append(message)
        self._step("send")
        return {}


def _deliver(stub: PhasedSmtp):
    provider = SmtpEmailProvider(
        host="smtp.example.test",
        port=587,
        username="ci-user",
        password=SecretStr(SECRET),
        from_address="noreply@example.test",
        timeout_seconds=1,
        smtp_factory=lambda *args, **kwargs: stub,
    )
    return provider.deliver(
        EmailDeliveryRequest(
            communication_id="comm-123",
            idempotency_key="comm-123",
            recipient="candidate@example.test",
            subject="Interview invitation",
            body="Synthetic body",
        )
    )


@pytest.mark.parametrize(
    "phase", ["connect", "ehlo", "starttls", "login"]
)
@pytest.mark.parametrize(
    "error",
    [socket.timeout("timed out"), OSError("reset"), smtplib.SMTPServerDisconnected("gone")],
)
def test_failures_before_send_are_definitely_not_sent(phase, error):
    stub = PhasedSmtp(phase, error)
    result = _deliver(stub)
    assert result.outcome == "retryable_not_accepted"
    assert stub.messages == []


def test_authentication_rejection_is_definitive():
    result = _deliver(PhasedSmtp("login", smtplib.SMTPAuthenticationError(535, b"bad")))
    assert result.outcome == "definitively_rejected"


@pytest.mark.parametrize(
    ("code", "expected"),
    [(451, "retryable_not_accepted"), (550, "definitively_rejected")],
)
def test_sender_refused_happens_before_data_so_is_not_unknown(code, expected):
    error = smtplib.SMTPSenderRefused(code, b"sender refused", "noreply@example.test")
    result = _deliver(PhasedSmtp("send", error))
    assert result.outcome == expected
    assert result.error_class.startswith("sender_refused")


@pytest.mark.parametrize(
    "error",
    [
        socket.timeout("timeout while waiting for the final DATA reply"),
        smtplib.SMTPServerDisconnected("disconnected during DATA"),
        OSError("connection reset during DATA"),
    ],
)
def test_failures_during_send_are_unknown_never_retryable(error):
    result = _deliver(PhasedSmtp("send", error))
    assert result.outcome == "unknown"
    assert result.error_class == "transport_outcome_unknown"


def test_disconnect_after_accepted_data_is_still_accepted():
    result = _deliver(PhasedSmtp("quit", smtplib.SMTPServerDisconnected("gone after 250")))
    assert result.outcome == "accepted"


def test_successful_submission_is_accepted_with_stable_message_id():
    first, second = PhasedSmtp(), PhasedSmtp()
    assert _deliver(first).outcome == "accepted"
    assert _deliver(second).outcome == "accepted"
    ids = {stub.messages[0]["Message-ID"] for stub in (first, second)}
    assert ids == {"<comm-123@smtp.example.test>"}


@pytest.mark.parametrize(
    ("phase", "error"),
    [
        ("login", OSError(f"server echoed {SECRET}")),
        ("send", OSError(f"server echoed {SECRET}")),
    ],
)
def test_results_never_carry_credentials(phase, error):
    result = _deliver(PhasedSmtp(phase, error))
    assert SECRET not in repr(result)
