"""Typed email delivery boundary and explicitly selected adapters."""

from __future__ import annotations

import hashlib
import smtplib
import socket
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Literal, Protocol

from pydantic import SecretStr

from config import settings

EmailOutcome = Literal[
    "simulated",
    "accepted",
    "definitively_rejected",
    "retryable_not_accepted",
    "unknown",
]


@dataclass(frozen=True)
class EmailDeliveryRequest:
    communication_id: str
    idempotency_key: str
    recipient: str
    subject: str
    body: str


@dataclass(frozen=True)
class EmailDeliveryResult:
    outcome: EmailOutcome
    provider_name: str
    provider_message_id: str | None = None
    error_class: str | None = None
    idempotency_supported: bool = False
    safe_metadata: Mapping[str, str | int | bool] = field(default_factory=dict)


class EmailProvider(Protocol):
    def deliver(self, request: EmailDeliveryRequest) -> EmailDeliveryResult: ...


class EmailProviderConfigurationError(RuntimeError):
    """Raised when an explicitly selected real provider is not configured."""


class OfflineSimulationEmailProvider:
    """Deterministic adapter; never performs network I/O."""

    def deliver(self, request: EmailDeliveryRequest) -> EmailDeliveryResult:
        reference = "sim-" + hashlib.sha256(request.communication_id.encode()).hexdigest()[:24]
        return EmailDeliveryResult(
            outcome="simulated",
            provider_name="offline_simulation",
            provider_message_id=reference,
        )


class SmtpEmailProvider:
    """SMTP adapter using mandatory STARTTLS and conservative outcome classification."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: SecretStr,
        from_address: str,
        timeout_seconds: float,
        smtp_factory=smtplib.SMTP,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_address = from_address
        self._timeout_seconds = timeout_seconds
        self._smtp_factory = smtp_factory

    def deliver(self, request: EmailDeliveryRequest) -> EmailDeliveryResult:
        provider_result: EmailDeliveryResult | None = None
        provider_accepted = False
        try:
            with self._smtp_factory(
                self._host, self._port, timeout=self._timeout_seconds
            ) as client:
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
                client.login(self._username, self._password.get_secret_value())
                message = EmailMessage()
                message["From"] = self._from_address
                message["To"] = request.recipient
                message["Subject"] = request.subject
                message["Message-ID"] = (
                    f"<{request.communication_id}@{self._host}>"
                )
                message.set_content(request.body)
                refused = self._submit_message(client, message)
                if isinstance(refused, EmailDeliveryResult):
                    provider_result = refused
                elif refused:
                    provider_result = self._recipient_refusal_result(refused)
                else:
                    provider_accepted = True
        except smtplib.SMTPAuthenticationError:
            known_result = self._known_result(provider_result, provider_accepted)
            if known_result is not None:
                return known_result
            return EmailDeliveryResult(
                outcome="definitively_rejected",
                provider_name="smtp",
                error_class="authentication_rejected",
                idempotency_supported=False,
            )
        except (
            TimeoutError,
            socket.timeout,
            OSError,
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPException,
        ):
            known_result = self._known_result(provider_result, provider_accepted)
            if known_result is not None:
                return known_result
            return EmailDeliveryResult(
                outcome="retryable_not_accepted",
                provider_name="smtp",
                error_class="connection_failure",
                idempotency_supported=False,
            )
        except Exception:
            known_result = self._known_result(provider_result, provider_accepted)
            if known_result is not None:
                return known_result
            return EmailDeliveryResult(
                outcome="retryable_not_accepted",
                provider_name="smtp",
                error_class="connection_failure",
                idempotency_supported=False,
            )
        if provider_result is not None:
            return provider_result
        return EmailDeliveryResult(
            outcome="accepted",
            provider_name="smtp",
            idempotency_supported=False,
            safe_metadata={"smtp_acceptance": True},
        )

    @staticmethod
    def _known_result(
        provider_result: EmailDeliveryResult | None, provider_accepted: bool
    ) -> EmailDeliveryResult | None:
        if provider_result is not None:
            return provider_result
        if provider_accepted:
            return EmailDeliveryResult(
                outcome="accepted",
                provider_name="smtp",
                idempotency_supported=False,
                safe_metadata={"smtp_acceptance": True},
            )
        return None

    def _submit_message(self, client, message: EmailMessage):
        try:
            return client.send_message(message)
        except smtplib.SMTPDataError as exc:
            return self._data_error_result(exc.smtp_code)
        except smtplib.SMTPRecipientsRefused as exc:
            return self._recipient_refusal_result(exc.recipients)
        except Exception:
            return EmailDeliveryResult(
                outcome="unknown",
                provider_name="smtp",
                error_class="transport_outcome_unknown",
                idempotency_supported=False,
            )

    @staticmethod
    def _recipient_refusal_result(
        refusals: Mapping[str, tuple[int, bytes]],
    ) -> EmailDeliveryResult:
        codes = [int(item[0]) for item in refusals.values()]
        if all(code >= 500 for code in codes):
            outcome: EmailOutcome = "definitively_rejected"
        elif all(code < 500 for code in codes):
            outcome = "retryable_not_accepted"
        else:
            outcome = "unknown"
        return EmailDeliveryResult(
            outcome=outcome,
            provider_name="smtp",
            error_class="recipient_refused",
            idempotency_supported=False,
            safe_metadata={"refused_recipient_count": len(refusals)},
        )

    @staticmethod
    def _data_error_result(code: int) -> EmailDeliveryResult:
        temporary = 400 <= code < 500
        return EmailDeliveryResult(
            outcome="retryable_not_accepted" if temporary else "definitively_rejected",
            provider_name="smtp",
            error_class="temporary_smtp_rejection" if temporary else "permanent_smtp_rejection",
            idempotency_supported=False,
            safe_metadata={"smtp_status_class": code // 100},
        )


def create_email_provider() -> EmailProvider:
    provider_name = settings.EMAIL_PROVIDER.strip().lower()
    if provider_name == "offline_simulation":
        return OfflineSimulationEmailProvider()
    if provider_name != "smtp":
        raise EmailProviderConfigurationError("EMAIL_PROVIDER must be offline_simulation or smtp")
    if settings.ENVIRONMENT.casefold() == "test":
        raise EmailProviderConfigurationError("Real email providers are disabled in test environments")
    password = settings.EMAIL_SMTP_PASSWORD
    if (
        not settings.EMAIL_SMTP_HOST.strip()
        or not settings.EMAIL_SMTP_USERNAME.strip()
        or not isinstance(password, SecretStr)
        or not password.get_secret_value()
        or not settings.EMAIL_SMTP_FROM.strip()
    ):
        raise EmailProviderConfigurationError(
            "SMTP requires EMAIL_SMTP_HOST, EMAIL_SMTP_USERNAME, "
            "EMAIL_SMTP_PASSWORD, and EMAIL_SMTP_FROM"
        )
    return SmtpEmailProvider(
        host=settings.EMAIL_SMTP_HOST.strip(),
        port=settings.EMAIL_SMTP_PORT,
        username=settings.EMAIL_SMTP_USERNAME,
        password=password,
        from_address=settings.EMAIL_SMTP_FROM.strip(),
        timeout_seconds=settings.EMAIL_SMTP_TIMEOUT_SECONDS,
    )
