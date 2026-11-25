"""Opt-in live provider smoke tests: one tiny, bounded request per provider.

Every test is marked ``live`` and SKIPS unless all of these are set:

* ``RUN_LIVE_PROVIDER_SMOKE=1`` (global opt-in),
* the provider opt-in (``LIVE_SMOKE_OPENAI`` / ``LIVE_SMOKE_ASSEMBLYAI`` /
  ``LIVE_SMOKE_ELEVENLABS`` / ``LIVE_SMOKE_SMTP`` = ``1``),
* every provider variable listed in ``tests/live/smoke_harness.py``.

Inputs are fixed and synthetic (no candidate data), retries are 0, each test
has a hard pytest timeout plus provider-level timeouts, and each run emits one
sanitized ``LIVE_SMOKE_RESULT`` JSON record (provider, latency, success, error
category). Documented maximum cost per run is in ``PROVIDERS[...].max_cost_usd``
and docs/PROVIDER_SMOKE_RUNBOOK.md.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import struct
import sys
from collections.abc import Callable
from time import perf_counter
from uuid import uuid4

import pytest

from tests.live.smoke_harness import (
    PROVIDERS,
    SmokeResult,
    emit,
    error_category,
    safe_label,
    sanitize_text,
    secret_values,
    skip_reason,
)

pytestmark = pytest.mark.live

OPENAI_SMOKE_MAX_TOKENS = 16
TTS_SMOKE_TEXT = "Provider smoke check."  # fixed 3-word input
TTS_MAX_BYTES = 512 * 1024
SMTP_SMOKE_SUBJECT = "AI Interviewer SMTP smoke test"
SMTP_SMOKE_BODY = (
    "This is an automated AI Interviewer SMTP smoke test.\n"
    "No action is required. No candidate data is included.\n"
)
_TRANSCRIPT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _require(provider: str) -> None:
    reason = skip_reason(provider)
    if reason is not None:
        pytest.skip(reason)


def _short(value: str, limit: int = 64) -> str:
    return sanitize_text(value)[:limit]


def _run(provider: str, call: Callable[[], dict], capfd, caplog) -> None:
    """Run one bounded provider call, emit a sanitized record, check for key leaks."""
    spec = PROVIDERS[provider]
    secrets = secret_values(provider)
    started = perf_counter()
    details: dict = {}
    try:
        details = dict(call() or {})
        success = bool(details.pop("_success", True))
        category = details.pop("_error_category", None)
    except Exception as exc:  # noqa: BLE001 - reduced to a safe category below
        success = False
        category = error_category(exc)
    result = SmokeResult(
        provider=provider,
        success=success,
        latency_ms=round((perf_counter() - started) * 1000, 2),
        error_category=category,
        max_cost_usd=spec.max_cost_usd,
        details=details,
    )
    emit(result, secrets)

    out, err = capfd.readouterr()
    leaked = any(secret in out or secret in err or secret in caplog.text for secret in secrets)
    # Re-display only sanitized output (captured text was consumed above).
    sys.stdout.write(sanitize_text(out, secrets))
    if leaked:
        pytest.fail("A credential value appeared in captured output or logs.", pytrace=False)
    if not success:
        pytest.fail(f"{provider} smoke test failed: {category}", pytrace=False)


@pytest.mark.timeout(60)
def test_openai_smoke(capfd, caplog):
    """One structured chat completion, max 16 output tokens. Max cost < USD 0.01."""
    _require("openai")
    import httpx
    from pydantic import BaseModel

    from integrations.llm_providers.provider import OpenAIProvider, OpenAISettings

    class SmokeReply(BaseModel):
        ok: bool

    model = os.environ["OPENAI_MODEL"].strip()

    def call() -> dict:
        with httpx.Client(timeout=20.0) as client:
            provider = OpenAIProvider(
                OpenAISettings(
                    api_key=os.environ["OPENAI_API_KEY"].strip(),
                    model=model,
                    base_url=os.environ.get("OPENAI_BASE_URL", "").strip()
                    or "https://api.openai.com/v1",
                    timeout_seconds=20.0,
                    max_tokens=OPENAI_SMOKE_MAX_TOKENS,
                    temperature=0.0,
                    max_retries=0,
                    prompt_version="live-smoke-v1",
                ),
                client=client,
            )
            reply = provider.generate_structured(
                "You are a connectivity health check.",
                'Respond with exactly {"ok": true}.',
                SmokeReply,
            )
        return {"model": _short(model), "schema_validated": True, "ok": reply.ok}

    _run("openai", call, capfd, caplog)


def _synthetic_pcm(seconds: float = 1.0, rate: int = 16000, freq: float = 440.0) -> bytes:
    """1 s of a quiet 440 Hz tone, PCM16 mono 16 kHz. Synthetic: no candidate audio."""
    frames = int(seconds * rate)
    amplitude = 0.1 * 32767
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(frames)
    )


@pytest.mark.timeout(90)
def test_assemblyai_smoke(capfd, caplog):
    """Upload 1 s of synthetic audio and transcribe it once. Max cost < USD 0.01."""
    _require("assemblyai")
    import httpx

    from voice.contracts import AudioFormat
    from voice.real_providers import ASSEMBLYAI_BASE_URL, AssemblyAISTTProvider

    api_key = os.environ["ASSEMBLY_AI_API_KEY"].strip()
    model = os.environ.get("STT_MODEL", "").strip() or "best"
    provider = AssemblyAISTTProvider(
        api_key=api_key,
        model=model,
        language=os.environ.get("STT_LANGUAGE", "").strip() or "en-US",
        timeout_seconds=15.0,
        max_retries=0,
        poll_interval_seconds=1.0,
        transcript_deadline_seconds=45.0,
    )
    created: dict[str, str] = {}
    original_request = provider._request

    async def recording_request(client, method, path, deadline, **kwargs):
        body = await original_request(client, method, path, deadline, **kwargs)
        if method == "POST" and path == "/v2/transcript" and isinstance(body.get("id"), str):
            created["id"] = body["id"]
        return body

    provider._request = recording_request

    async def transcribe() -> dict:
        utterance_id = f"live-smoke-{uuid4().hex[:12]}"
        await provider.start_session(utterance_id, AudioFormat())
        async for _ in provider.send_audio(utterance_id, _synthetic_pcm()):
            pass
        events = [event async for event in provider.end_utterance(utterance_id)]
        await provider.close()
        final = [event for event in events if event.is_final]
        return {"final_events": len(final), "transcript_chars": len(final[-1].text) if final else 0}

    def call() -> dict:
        details: dict = {"model": _short(model), "audio_seconds": 1}
        try:
            details.update(asyncio.run(transcribe()))
        finally:
            transcript_id = created.get("id")
            details["transcript_id"] = (
                transcript_id if transcript_id and _TRANSCRIPT_ID.fullmatch(transcript_id) else None
            )
            details["transcript_deleted"] = False
            if details["transcript_id"] and os.environ.get(
                "LIVE_SMOKE_ASSEMBLYAI_KEEP_TRANSCRIPT", ""
            ).strip() != "1":
                try:
                    response = httpx.delete(
                        f"{ASSEMBLYAI_BASE_URL}/v2/transcript/{details['transcript_id']}",
                        headers={"authorization": api_key},
                        timeout=10.0,
                    )
                    details["transcript_deleted"] = response.status_code < 300
                    details["delete_status_class"] = f"{response.status_code // 100}xx"
                except httpx.HTTPError as exc:
                    details["delete_error_category"] = error_category(exc)
        return details

    _run("assemblyai", call, capfd, caplog)


@pytest.mark.timeout(60)
def test_elevenlabs_smoke(capfd, caplog):
    """Synthesize a fixed 3-word phrase (~21 characters). Max cost < USD 0.01."""
    _require("elevenlabs")
    from voice.real_providers import ElevenLabsTTSProvider

    model = os.environ["TTS_MODEL"].strip()
    provider = ElevenLabsTTSProvider(
        api_key=os.environ["ELEVEN_LABS_API_KEY"].strip(),
        voice_id=os.environ["ELEVEN_LABS_VOICE_ID"].strip(),
        model=model,
        timeout_seconds=20.0,
        max_retries=0,
    )

    async def synthesize() -> dict:
        received = 0
        chunks = 0
        async for chunk in provider.stream(TTS_SMOKE_TEXT):
            received += len(chunk)
            chunks += 1
            if received >= TTS_MAX_BYTES:
                break
        await provider.close()
        return {
            "model": _short(model),
            "input_characters": len(TTS_SMOKE_TEXT),
            "audio_bytes": received,
            "chunks": chunks,
            "_success": received > 0,
            "_error_category": None if received > 0 else "EmptyAudio",
        }

    _run("elevenlabs", lambda: asyncio.run(synthesize()), capfd, caplog)


@pytest.mark.timeout(60)
def test_smtp_smoke(capfd, caplog):
    """Send one fixed message to the explicitly configured test recipient. No spend."""
    _require("smtp")
    from pydantic import SecretStr

    from services.email_provider import EmailDeliveryRequest, SmtpEmailProvider

    recipient = os.environ["LIVE_SMOKE_SMTP_TEST_RECIPIENT"].strip()
    provider = SmtpEmailProvider(
        host=os.environ["EMAIL_SMTP_HOST"].strip(),
        port=int(os.environ.get("EMAIL_SMTP_PORT", "").strip() or 587),
        username=os.environ["EMAIL_SMTP_USERNAME"].strip(),
        password=SecretStr(os.environ["EMAIL_SMTP_PASSWORD"]),
        from_address=os.environ["EMAIL_SMTP_FROM"].strip(),
        timeout_seconds=10.0,
    )

    def call() -> dict:
        smoke_id = f"live-smoke-{uuid4().hex}"
        result = provider.deliver(
            EmailDeliveryRequest(
                communication_id=smoke_id,
                idempotency_key=smoke_id,
                recipient=recipient,
                subject=SMTP_SMOKE_SUBJECT,
                body=SMTP_SMOKE_BODY,
            )
        )
        accepted = result.outcome == "accepted"
        return {
            "outcome": safe_label(result.outcome),
            "error_class": safe_label(result.error_class),
            "recipient_domain": _short(recipient.rsplit("@", 1)[-1]),
            "_success": accepted,
            "_error_category": None
            if accepted
            else f"smtp:{safe_label(result.outcome)}:{safe_label(result.error_class)}",
        }

    _run("smtp", call, capfd, caplog)
