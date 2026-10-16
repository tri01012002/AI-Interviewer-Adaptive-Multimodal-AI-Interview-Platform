import pytest

from apps.api import main as api_main
from config.settings import Settings


def test_settings_do_not_enable_known_password_admin_bootstrap_by_default(monkeypatch):
    monkeypatch.delenv("API_HOST", raising=False)
    monkeypatch.delenv("ENABLE_DEV_ADMIN_BOOTSTRAP", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)

    settings = Settings(_env_file=None)

    assert settings.API_HOST == "127.0.0.1"
    assert settings.ENABLE_DEV_ADMIN_BOOTSTRAP is False
    assert settings.BOOTSTRAP_ADMIN_EMAIL == ""
    assert settings.BOOTSTRAP_ADMIN_PASSWORD == ""
    assert settings.LLM_PROVIDER == "none"
    assert settings.EMBEDDING_PROVIDER == "none"


def test_explicit_dev_bootstrap_requires_operator_supplied_credentials(monkeypatch):
    monkeypatch.setattr(api_main.settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(api_main.settings, "ENABLE_DEV_ADMIN_BOOTSTRAP", True)
    monkeypatch.setattr(api_main.settings, "BOOTSTRAP_ADMIN_EMAIL", "")
    monkeypatch.setattr(api_main.settings, "BOOTSTRAP_ADMIN_PASSWORD", "")
    monkeypatch.setattr(
        api_main.UserStore,
        "ensure_default_admin",
        lambda *args, **kwargs: pytest.fail("bootstrap must validate credentials first"),
    )

    with pytest.raises(RuntimeError, match="requires explicit credentials"):
        api_main.ensure_dev_admin_bootstrap()
