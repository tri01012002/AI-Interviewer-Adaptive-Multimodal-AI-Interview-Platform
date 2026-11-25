"""Secrets must never reach console output, logs, reprs, or error messages.

All values below are synthetic sentinels, never real credentials.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from config.settings import Settings
from integrations.llm_providers.provider import (
    LLMConfigurationError,
    LLMProviderError,
    OpenAIProvider,
    OpenAISettings,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "sentinel-secret-value-7f3a9c"

_SECRET_NAME_PARTS = ("api_key", "secret", "password", "credential", "dsn")
_SKIP_DIRS = {".git", ".venv", "venv", "tests", "node_modules", "__pycache__"}


def test_importing_offline_llm_config_prints_no_secret():
    env = {
        **os.environ,
        # The offline indexing stack only accepts its own providers, not "none".
        "LLM_PROVIDER": "groq",
        "GROQ_API_KEY": SENTINEL,
        "ANTHROPIC_API_KEY": SENTINEL,
        "OPENAI_API_KEY": SENTINEL,
        "OPENROUTER_API_KEY": SENTINEL,
        "QDRANT_API_KEY": SENTINEL,
    }
    result = subprocess.run(
        [sys.executable, "-c", "import core.configs as c; print(repr(c.env_config))"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr


def test_offline_llm_config_keeps_key_values_usable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    from core.configs import EnvConfig

    config = EnvConfig(groq_api_key=SENTINEL, anthropic_api_key=SENTINEL)
    assert config.groq_api_key == SENTINEL
    assert config.anthropic_api_key == SENTINEL
    assert SENTINEL not in repr(config)
    assert SENTINEL not in str(config)


def test_application_settings_repr_hides_secrets():
    settings = Settings(
        SECRET_KEY=SENTINEL,
        BOOTSTRAP_ADMIN_PASSWORD=SENTINEL,
        OPENAI_API_KEY=SENTINEL,
        ANTHROPIC_API_KEY=SENTINEL,
        ELEVEN_LABS_API_KEY=SENTINEL,
        ASSEMBLY_AI_API_KEY=SENTINEL,
        LANGWATCH_API_KEY=SENTINEL,
        SENTRY_DSN=f"https://{SENTINEL}@example.invalid/1",
    )
    assert settings.OPENAI_API_KEY == SENTINEL
    assert SENTINEL not in repr(settings)
    assert SENTINEL not in str(settings)


def test_openai_provider_settings_repr_hides_key():
    provider_settings = OpenAISettings(api_key=SENTINEL, model="test-model")
    assert provider_settings.api_key == SENTINEL
    assert SENTINEL not in repr(provider_settings)


@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
def test_openai_provider_errors_do_not_echo_credentials(status_code: int):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {SENTINEL}"
        return httpx.Response(status_code, json={"error": {"message": "synthetic"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(
        OpenAISettings(api_key=SENTINEL, model="test-model", max_retries=0),
        client=client,
    )
    with pytest.raises(LLMProviderError) as excinfo:
        provider.generate("system", "user")
    rendered = f"{excinfo.value!s} {excinfo.value!r}"
    assert SENTINEL not in rendered
    if status_code in {401, 403}:
        assert isinstance(excinfo.value, LLMConfigurationError)


def _iter_source_files():
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        yield path


def _references_secret(node: ast.AST) -> str | None:
    for child in ast.walk(node):
        name = None
        if isinstance(child, ast.Name):
            name = child.id
        elif isinstance(child, ast.Attribute):
            name = child.attr
        if name and any(part in name.lower() for part in _SECRET_NAME_PARTS):
            return name
    return None


def _is_output_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    if isinstance(func, ast.Attribute):
        return func.attr in {"debug", "info", "warning", "error", "exception", "critical", "log"}
    return False


def test_no_source_prints_or_logs_secret_values():
    offenders: list[str] = []
    for path in _iter_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_output_call(node):
                continue
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            for argument in arguments:
                # Conditional expressions such as "configured" if key else "missing"
                # only reveal presence, not the value.
                if isinstance(argument, ast.JoinedStr):
                    values = [
                        value.value
                        for value in argument.values
                        if isinstance(value, ast.FormattedValue)
                        and not isinstance(value.value, ast.IfExp)
                    ]
                elif isinstance(argument, ast.IfExp):
                    values = []
                else:
                    values = [argument]
                for value in values:
                    name = _references_secret(value)
                    if name:
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno} ({name})"
                        )
    assert offenders == []
