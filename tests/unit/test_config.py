import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_empty_secrets_are_treated_as_missing() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="", gigachat_auth_key="   ", telegram_proxy="")
    assert settings.telegram_bot_token is None
    assert settings.gigachat_auth_key is None
    assert settings.telegram_proxy is None


def test_secrets_are_hidden_in_repr() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="123:secret-value")
    assert "secret-value" not in repr(settings)
    assert settings.telegram_bot_token is not None
    assert settings.telegram_bot_token.get_secret_value() == "123:secret-value"


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, default_timezone="Mars/Olympus")


def test_ca_bundle_for_gigachat_exists() -> None:
    settings = Settings(_env_file=None)
    assert settings.gigachat_ca_bundle.is_file()
    assert settings.gigachat_ca_bundle.read_text().count("BEGIN CERTIFICATE") == 2
