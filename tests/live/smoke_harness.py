"""Opt-in gating, sanitizing and result recording for live provider smoke tests.

Nothing in this module performs network I/O. It decides whether a live smoke
test may run (a global opt-in AND a provider opt-in AND every required
variable), reduces exceptions to a credential-free error category, and emits
one JSON result record per smoke test. See docs/PROVIDER_SMOKE_RUNBOOK.md.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

GLOBAL_OPT_IN = "RUN_LIVE_PROVIDER_SMOKE"
RESULT_FILE_ENV = "LIVE_SMOKE_RESULT_FILE"
RESULT_PREFIX = "LIVE_SMOKE_RESULT "
REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    opt_in: str
    required: tuple[str, ...]
    secrets: tuple[str, ...]
    # Documented upper bound for one smoke run; enforced by fixed tiny inputs,
    # a hard timeout and zero retries, not by the provider.
    max_cost_usd: float
    # Values that look configured but are placeholders/fakes never count.
    placeholders: Mapping[str, frozenset[str]] = field(default_factory=dict)


PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        opt_in="LIVE_SMOKE_OPENAI",
        required=("OPENAI_API_KEY", "OPENAI_MODEL"),
        secrets=("OPENAI_API_KEY",),
        max_cost_usd=0.01,
        placeholders={"OPENAI_API_KEY": frozenset({"sk-..."})},
    ),
    "assemblyai": ProviderSpec(
        name="assemblyai",
        opt_in="LIVE_SMOKE_ASSEMBLYAI",
        required=("ASSEMBLY_AI_API_KEY",),
        secrets=("ASSEMBLY_AI_API_KEY",),
        max_cost_usd=0.01,
        placeholders={"ASSEMBLY_AI_API_KEY": frozenset({"your-assembly-ai-key"})},
    ),
    "elevenlabs": ProviderSpec(
        name="elevenlabs",
        opt_in="LIVE_SMOKE_ELEVENLABS",
        required=("ELEVEN_LABS_API_KEY", "ELEVEN_LABS_VOICE_ID", "TTS_MODEL"),
        secrets=("ELEVEN_LABS_API_KEY",),
        max_cost_usd=0.01,
        placeholders={
            "ELEVEN_LABS_API_KEY": frozenset({"your-eleven-labs-key"}),
            "ELEVEN_LABS_VOICE_ID": frozenset({"default", "default-voice-id"}),
            "TTS_MODEL": frozenset({"local-fake", "fake"}),
        },
    ),
    "smtp": ProviderSpec(
        name="smtp",
        opt_in="LIVE_SMOKE_SMTP",
        required=(
            "EMAIL_SMTP_HOST",
            "EMAIL_SMTP_USERNAME",
            "EMAIL_SMTP_PASSWORD",
            "EMAIL_SMTP_FROM",
            "LIVE_SMOKE_SMTP_TEST_RECIPIENT",
        ),
        secrets=("EMAIL_SMTP_PASSWORD", "EMAIL_SMTP_USERNAME"),
        max_cost_usd=0.0,
    ),
}


def skip_reason(provider: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Return why a smoke test must not run, or None when fully opted in.

    The message names variables only; it never includes their values.
    """
    env = os.environ if environ is None else environ
    spec = PROVIDERS[provider]
    if env.get(GLOBAL_OPT_IN, "").strip() != "1":
        return f"Set {GLOBAL_OPT_IN}=1 to allow any live provider smoke test."
    if env.get(spec.opt_in, "").strip() != "1":
        return f"Set {spec.opt_in}=1 to opt into the {provider} smoke test."
    missing = []
    for name in spec.required:
        value = env.get(name, "").strip()
        if not value or value in spec.placeholders.get(name, frozenset()):
            missing.append(name)
    if missing:
        return f"{provider} smoke test is not fully configured; missing: {', '.join(missing)}"
    if provider == "smtp" and "@" not in env.get("LIVE_SMOKE_SMTP_TEST_RECIPIENT", ""):
        return "LIVE_SMOKE_SMTP_TEST_RECIPIENT must be an explicit test mailbox address."
    return None


def secret_values(provider: str, environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return [
        value
        for name in PROVIDERS[provider].secrets
        if (value := env.get(name, "").strip())
    ]


_TOKEN_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|authorization|xi-api-key|password)\s*[=:]\s*)\S+"),
)


def sanitize_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove known secret values and credential-shaped tokens from text."""
    cleaned = str(text)
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        cleaned = cleaned.replace(secret, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        if pattern.groups:
            cleaned = pattern.sub(lambda m: m.group(1) + REDACTED, cleaned)
        else:
            cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned


_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,47}")
_SAFE_LABEL = re.compile(r"[a-z][a-z0-9_]{1,47}")


def error_category(exc: BaseException) -> str:
    """Exception type plus, when known, a fixed error code or HTTP status class.

    Exception messages are never included: they can echo request data.
    """
    parts = [type(exc).__name__]
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        parts.append(code)
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "smtp_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        parts.append(f"{status // 100}xx")
    return ":".join(parts)


def safe_label(value: object) -> str | None:
    """Accept only short snake_case enum-like labels (e.g. SMTP outcome classes)."""
    if isinstance(value, str) and _SAFE_LABEL.fullmatch(value):
        return value
    return None


@dataclass
class SmokeResult:
    provider: str
    success: bool
    latency_ms: float
    error_category: str | None
    max_cost_usd: float
    details: dict[str, str | int | float | bool | None] = field(default_factory=dict)
    recorded_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    def to_record(self) -> dict[str, object]:
        return asdict(self)


RECORD_KEYS = frozenset(
    {"provider", "success", "latency_ms", "error_category", "max_cost_usd", "details", "recorded_at"}
)


def emit(result: SmokeResult, secrets: Iterable[str] = ()) -> str:
    """Print one sanitized JSON record (and append it to the optional result file)."""
    secrets = [s for s in secrets if s]
    line = sanitize_text(json.dumps(result.to_record(), sort_keys=True), secrets)
    for secret in secrets:
        if secret in line:  # pragma: no cover - sanitize_text guarantees this
            raise AssertionError("refusing to emit a smoke record containing a credential")
    print(RESULT_PREFIX + line, flush=True)
    result_file = os.environ.get(RESULT_FILE_ENV, "").strip()
    if result_file:
        with Path(result_file).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return line
