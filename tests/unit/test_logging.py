from app.core.logging import redact_secrets


def test_sensitive_fields_are_masked() -> None:
    event = {
        "event": "x",
        "telegram_bot_token": "123:abc",
        "Authorization": "Basic zzz",
        "gigachat_auth_key": "key",
        "user_id": "42",
    }
    redacted = redact_secrets(None, "info", event)
    assert redacted["telegram_bot_token"] == "***"
    assert redacted["Authorization"] == "***"
    assert redacted["gigachat_auth_key"] == "***"
    assert redacted["user_id"] == "42"
    assert redacted["event"] == "x"
