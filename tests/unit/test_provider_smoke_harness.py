"""Offline checks for the opt-in live provider smoke harness (no network I/O)."""

from __future__ import annotations

import json
import os
import smtplib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest

from tests.live import smoke_harness as harness
from tests.live import test_provider_smoke as smoke

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_OPENAI_KEY = "sk-unit-test-not-a-real-key-0123456789"
FAKE_VOICE_KEY = "unit-test-voice-key-abcdef0123456789"

SMOKE_TESTS = {
    "openai": smoke.test_openai_smoke,
    "assemblyai": smoke.test_assemblyai_smoke,
    "elevenlabs": smoke.test_elevenlabs_smoke,
    "smtp": smoke.test_smtp_smoke,
}

FULL_CONFIG = {
    "openai": {"OPENAI_API_KEY": FAKE_OPENAI_KEY, "OPENAI_MODEL": "unit-test-model"},
    "assemblyai": {"ASSEMBLY_AI_API_KEY": FAKE_VOICE_KEY},
    "elevenlabs": {
        "ELEVEN_LABS_API_KEY": FAKE_VOICE_KEY,
        "ELEVEN_LABS_VOICE_ID": "unit-test-voice",
        "TTS_MODEL": "unit-test-tts-model",
    },
    "smtp": {
        "EMAIL_SMTP_HOST": "smtp.invalid",
        "EMAIL_SMTP_USERNAME": "unit-user",
        "EMAIL_SMTP_PASSWORD": "unit-test-smtp-password-123",
        "EMAIL_SMTP_FROM": "sender@example.invalid",
        "LIVE_SMOKE_SMTP_TEST_RECIPIENT": "smoke-recipient@example.invalid",
    },
}

ALL_GATING_VARS = {harness.GLOBAL_OPT_IN} | {
    name for spec in harness.PROVIDERS.values() for name in (spec.opt_in, *spec.required)
}


def _clear_gating_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_GATING_VARS:
        monkeypatch.delenv(name, raising=False)


def test_every_provider_has_a_smoke_test_and_cost_boundary():
    assert set(harness.PROVIDERS) == set(SMOKE_TESTS)
    for spec in harness.PROVIDERS.values():
        assert 0 <= spec.max_cost_usd <= 0.01
        assert set(spec.secrets) <= set(spec.required)
        assert spec.opt_in.startswith("LIVE_SMOKE_")


@pytest.mark.parametrize("provider", sorted(SMOKE_TESTS))
def test_smoke_tests_skip_by_default(provider, monkeypatch):
    _clear_gating_env(monkeypatch)
    assert harness.skip_reason(provider) is not None
    with pytest.raises(pytest.skip.Exception):
        SMOKE_TESTS[provider](capfd=None, caplog=None)


@pytest.mark.parametrize("provider", sorted(SMOKE_TESTS))
def test_partial_configuration_still_skips(provider, monkeypatch):
    spec = harness.PROVIDERS[provider]
    full = {harness.GLOBAL_OPT_IN: "1", spec.opt_in: "1", **FULL_CONFIG[provider]}
    assert harness.skip_reason(provider, full) is None

    partial_envs = [
        {k: v for k, v in full.items() if k != harness.GLOBAL_OPT_IN},
        {k: v for k, v in full.items() if k != spec.opt_in},
        {**full, harness.GLOBAL_OPT_IN: "true"},
    ] + [{k: v for k, v in full.items() if k != name} for name in spec.required]
    for partial in partial_envs:
        reason = harness.skip_reason(provider, partial)
        assert reason is not None
        for secret in harness.secret_values(provider, full):
            assert secret not in reason

    # The real test function must skip (before any I/O) under partial config too.
    _clear_gating_env(monkeypatch)
    for name, value in partial_envs[-1].items():
        monkeypatch.setenv(name, value)
    with pytest.raises(pytest.skip.Exception):
        SMOKE_TESTS[provider](capfd=None, caplog=None)


def test_placeholder_values_do_not_count_as_configured():
    env = {
        harness.GLOBAL_OPT_IN: "1",
        "LIVE_SMOKE_OPENAI": "1",
        "OPENAI_API_KEY": "sk-...",
        "OPENAI_MODEL": "gpt-test",
    }
    assert "OPENAI_API_KEY" in harness.skip_reason("openai", env)
    env = {
        harness.GLOBAL_OPT_IN: "1",
        "LIVE_SMOKE_ELEVENLABS": "1",
        **FULL_CONFIG["elevenlabs"],
        "ELEVEN_LABS_VOICE_ID": "default",
        "TTS_MODEL": "local-fake",
    }
    reason = harness.skip_reason("elevenlabs", env)
    assert "ELEVEN_LABS_VOICE_ID" in reason and "TTS_MODEL" in reason


def test_smtp_requires_an_explicit_recipient_address():
    env = {
        harness.GLOBAL_OPT_IN: "1",
        "LIVE_SMOKE_SMTP": "1",
        **FULL_CONFIG["smtp"],
        "LIVE_SMOKE_SMTP_TEST_RECIPIENT": "not-an-address",
    }
    assert harness.skip_reason("smtp", env) is not None


def test_sanitizer_strips_secrets_and_credential_shaped_tokens():
    text = (
        f"key={FAKE_OPENAI_KEY} Authorization: Bearer abcdefghijklmnop "
        f"xi-api-key: {FAKE_VOICE_KEY} password=hunter2hunter2 other sk-zzzzzzzzzzzz"
    )
    cleaned = harness.sanitize_text(text, [FAKE_OPENAI_KEY, FAKE_VOICE_KEY])
    for fragment in (FAKE_OPENAI_KEY, FAKE_VOICE_KEY, "abcdefghijklmnop", "hunter2", "sk-zzzz"):
        assert fragment not in cleaned
    assert harness.REDACTED in cleaned


def test_error_category_never_includes_exception_messages():
    from voice.real_providers import STTProviderError

    assert harness.error_category(RuntimeError(FAKE_OPENAI_KEY)) == "RuntimeError"
    assert (
        harness.error_category(STTProviderError("STT_AUTH_ERROR", FAKE_VOICE_KEY))
        == "STTProviderError:STT_AUTH_ERROR"
    )
    assert harness.error_category(STTProviderError("lower case " + FAKE_VOICE_KEY, "x")) == (
        "STTProviderError"
    )
    request = httpx.Request("GET", "https://provider.invalid/v1")
    response = httpx.Response(429, request=request)
    exc = httpx.HTTPStatusError(FAKE_OPENAI_KEY, request=request, response=response)
    assert harness.error_category(exc) == "HTTPStatusError:4xx"
    smtp_exc = smtplib.SMTPDataError(554, FAKE_VOICE_KEY.encode())
    assert harness.error_category(smtp_exc) == "SMTPDataError:5xx"
    assert harness.safe_label("retryable_not_accepted") == "retryable_not_accepted"
    assert harness.safe_label(FAKE_OPENAI_KEY) is None
    assert harness.safe_label(None) is None


def test_result_record_shape_and_emit(tmp_path, monkeypatch, capsys):
    result_file = tmp_path / "smoke-results.jsonl"
    monkeypatch.setenv(harness.RESULT_FILE_ENV, str(result_file))
    result = harness.SmokeResult(
        provider="openai",
        success=False,
        latency_ms=12.5,
        error_category="LLMConfigurationError",
        max_cost_usd=0.01,
        details={"model": "unit-test-model", "note": f"leak {FAKE_OPENAI_KEY}"},
    )
    record = result.to_record()
    assert set(record) == harness.RECORD_KEYS
    assert isinstance(record["success"], bool)
    assert isinstance(record["latency_ms"], float)

    line = harness.emit(result, [FAKE_OPENAI_KEY])
    out = capsys.readouterr().out
    assert out.startswith(harness.RESULT_PREFIX)
    assert FAKE_OPENAI_KEY not in out and FAKE_OPENAI_KEY not in line
    stored = json.loads(result_file.read_text(encoding="utf-8").strip())
    assert set(stored) == harness.RECORD_KEYS
    assert stored["provider"] == "openai" and stored["success"] is False
    assert stored["error_category"] == "LLMConfigurationError"
    assert FAKE_OPENAI_KEY not in json.dumps(stored)


def test_live_directory_reports_all_skipped_by_default(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ALL_GATING_VARS}
    junit = tmp_path / "live.xml"
    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest", "tests/live", "-q",
            "-p", "no:cacheprovider", f"--junitxml={junit}",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    suite = ET.parse(junit).getroot()
    suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
    assert int(suite.get("tests")) == len(SMOKE_TESTS)
    assert int(suite.get("skipped")) == len(SMOKE_TESTS)
    assert int(suite.get("failures")) == 0 and int(suite.get("errors")) == 0
