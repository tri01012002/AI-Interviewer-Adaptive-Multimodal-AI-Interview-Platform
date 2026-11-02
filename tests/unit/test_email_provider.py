from __future__ import annotations

import smtplib
import socket

import pytest
from pydantic import SecretStr

from config import settings
from services.email_provider import (
    EmailDeliveryRequest,
    EmailProviderConfigurationError,
    OfflineSimulationEmailProvider,
    SmtpEmailProvider,
    create_email_provider,
)


def delivery_request() -> EmailDeliveryRequest:
    return EmailDeliveryRequest(
        communication_id="communication-id",
        idempotency_key="stable-communication-id",
        recipient="candidate@example.test",
        subject="Interview details",
        body="Please reply with your availability.",
    )


def test_offline_provider_is_deterministic_and_never_opens_smtp(monkeypatch):
    def reject_network(*args, **kwargs):
        pytest.fail("offline email adapter attempted network I/O")

    monkeypatch.setattr(smtplib, "SMTP", reject_network)
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "offline_simulation")
    provider = create_email_provider()

    first = provider.deliver(delivery_request())
    second = provider.deliver(delivery_request())

    assert isinstance(provider, OfflineSimulationEmailProvider)
    assert first.outcome == "simulated"
    assert first.provider_name == "offline_simulation"
    assert first.provider_message_id == second.provider_message_id
    assert not first.idempotency_supported


def test_explicit_smtp_selection_fails_closed_in_test_environment(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "smtp")
    monkeypatch.setattr(settings, "EMAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(settings, "EMAIL_SMTP_USERNAME", "test-user")
    monkeypatch.setattr(settings, "EMAIL_SMTP_PASSWORD", SecretStr("test-secret"))
    monkeypatch.setattr(settings, "EMAIL_SMTP_FROM", "noreply@example.test")

    with pytest.raises(EmailProviderConfigurationError, match="disabled in test"):
        create_email_provider()


def test_missing_smtp_credentials_raise_configuration_error_without_fallback(
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "smtp")
    monkeypatch.setattr(settings, "EMAIL_SMTP_HOST", "")
    monkeypatch.setattr(settings, "EMAIL_SMTP_USERNAME", "")
    monkeypatch.setattr(settings, "EMAIL_SMTP_PASSWORD", SecretStr(""))
    monkeypatch.setattr(settings, "EMAIL_SMTP_FROM", "")

    with pytest.raises(EmailProviderConfigurationError, match="SMTP requires"):
        create_email_provider()


class StubSmtp:
    def __init__(self, send_error=None, refusals=None, exit_error=None):
        self.send_error = send_error
        self.refusals = refusals or {}
        self.exit_error = exit_error
        self.sent = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.exit_error is not None:
            raise self.exit_error
        return False

    def ehlo(self):
        return None

    def starttls(self, *, context):
        return None

    def login(self, username, password):
        return None

    def send_message(self, message):
        self.sent = True
        if self.send_error is not None:
            raise self.send_error
        return self.refusals


def smtp_provider(stub):
    return SmtpEmailProvider(
        host="smtp.example.test",
        port=587,
        username="test-user",
        password=SecretStr("test-secret"),
        from_address="noreply@example.test",
        timeout_seconds=1,
        smtp_factory=lambda *args, **kwargs: stub,
    )


def test_smtp_acceptance_is_not_claimed_as_recipient_delivery():
    stub = StubSmtp()

    result = smtp_provider(stub).deliver(delivery_request())

    assert result.outcome == "accepted"
    assert result.provider_message_id is None
    assert result.safe_metadata == {"smtp_acceptance": True}
    assert stub.sent


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (smtplib.SMTPDataError(451, b"temporary"), "retryable_not_accepted"),
        (smtplib.SMTPDataError(550, b"rejected"), "definitively_rejected"),
        (socket.timeout("possible acceptance"), "unknown"),
    ],
)
def test_smtp_submission_outcomes_are_classified_conservatively(error, expected):
    result = smtp_provider(StubSmtp(send_error=error)).deliver(delivery_request())

    assert result.outcome == expected
    assert result.error_class


@pytest.mark.parametrize(
    ("send_error", "expected"),
    [
        (None, "accepted"),
        (socket.timeout("possible acceptance"), "unknown"),
    ],
)
def test_smtp_shutdown_error_cannot_downgrade_submission_outcome(
    send_error, expected
):
    result = smtp_provider(
        StubSmtp(
            send_error=send_error,
            exit_error=OSError("connection closed during QUIT"),
        )
    ).deliver(delivery_request())

    assert result.outcome == expected


def test_smtp_connection_failure_proves_message_was_not_submitted():
    def fail_to_connect(*args, **kwargs):
        raise OSError("private connection details")

    provider = SmtpEmailProvider(
        host="smtp.example.test",
        port=587,
        username="test-user",
        password=SecretStr("test-secret"),
        from_address="noreply@example.test",
        timeout_seconds=1,
        smtp_factory=fail_to_connect,
    )

    result = provider.deliver(delivery_request())

    assert result.outcome == "retryable_not_accepted"
    assert result.error_class == "connection_failure"
    assert "private" not in str(result)
