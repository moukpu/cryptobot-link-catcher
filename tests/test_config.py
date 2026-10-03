from __future__ import annotations

import pytest

from app.config import load_settings


def configure_minimum(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("API_ID", "123456")
    monkeypatch.setenv("API_HASH", "a" * 32)
    monkeypatch.setenv("SESSION_PATH", str(tmp_path / "session" / "user"))
    monkeypatch.setenv("CONTROL_SESSION_PATH", str(tmp_path / "session" / "control"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "db.sqlite3"))
    monkeypatch.setenv("LOG_FILE", "")
    monkeypatch.delenv("PHONE_NUMBER", raising=False)
    monkeypatch.delenv("CONTROL_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CONTROL_ADMIN_ID", raising=False)


def test_phone_is_not_required_after_authorization(monkeypatch, tmp_path) -> None:
    configure_minimum(monkeypatch, tmp_path)
    assert load_settings().phone_number is None


def test_authorization_requires_phone(monkeypatch, tmp_path) -> None:
    configure_minimum(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="PHONE_NUMBER"):
        load_settings(require_phone=True)


def test_control_token_format_is_validated(monkeypatch, tmp_path) -> None:
    configure_minimum(monkeypatch, tmp_path)
    monkeypatch.setenv("CONTROL_BOT_TOKEN", "not-a-token")
    with pytest.raises(ValueError, match="CONTROL_BOT_TOKEN"):
        load_settings()


def test_two_accounts_cannot_share_database(monkeypatch, tmp_path) -> None:
    configure_minimum(monkeypatch, tmp_path)
    monkeypatch.setenv("SECONDARY_DB_PATH", str(tmp_path / "data" / "db.sqlite3"))
    with pytest.raises(ValueError, match="разные базы"):
        load_settings()
