from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.getenv("CRYPTOBOT_ENV_PATH") or PROJECT_ROOT / ".env").expanduser().resolve()
TARGET_BOT_USERNAME = "CryptoBot"
TARGET_BOT_ID = 1559501630


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"В .env не задано обязательное значение {name}")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} должно быть целым числом") from exc
    if value < 1:
        raise ValueError(f"{name} должно быть больше нуля")
    return value


def _project_path(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default).strip()).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    phone_number: str | None
    session_path: Path
    db_path: Path
    log_file: Path | None
    log_level: str
    send_retries: int
    retry_base_seconds: int
    max_flood_wait_seconds: int
    control_bot_token: str | None
    control_admin_id: int | None
    control_session_path: Path
    secondary_db_path: Path | None = None


def load_settings(*, require_phone: bool = False) -> Settings:
    load_dotenv(ENV_PATH, override=True)
    api_id_raw = _required("API_ID")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise ValueError("API_ID должен быть целым числом") from exc

    log_file_raw = os.getenv("LOG_FILE", "data/userbot.log").strip()
    log_file = _project_path("LOG_FILE", "data/userbot.log") if log_file_raw else None

    phone_number = os.getenv("PHONE_NUMBER", "").strip() or None
    if require_phone and phone_number is None:
        raise ValueError("В .env не задано обязательное значение PHONE_NUMBER")
    if phone_number is not None and not phone_number.startswith("+"):
        raise ValueError("PHONE_NUMBER должен быть в международном формате с +")

    control_bot_token = os.getenv("CONTROL_BOT_TOKEN", "").strip() or None
    if control_bot_token:
        token_parts = control_bot_token.split(":", 1)
        if len(token_parts) != 2 or not token_parts[0].isdigit() or not token_parts[1]:
            raise ValueError("CONTROL_BOT_TOKEN имеет неверный формат")
    control_admin_raw = os.getenv("CONTROL_ADMIN_ID", "").strip()
    control_admin_id: int | None = None
    if control_admin_raw:
        try:
            control_admin_id = int(control_admin_raw)
        except ValueError as exc:
            raise ValueError("CONTROL_ADMIN_ID должен быть целым числом") from exc
        if control_admin_id <= 0:
            raise ValueError("CONTROL_ADMIN_ID должен быть больше нуля")
    settings = Settings(
        api_id=api_id,
        api_hash=_required("API_HASH"),
        phone_number=phone_number,
        session_path=_project_path("SESSION_PATH", "session/telegram_user"),
        db_path=_project_path("DB_PATH", "data/processed.sqlite3"),
        log_file=log_file,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        send_retries=_positive_int("SEND_RETRIES", 5),
        retry_base_seconds=_positive_int("RETRY_BASE_SECONDS", 3),
        max_flood_wait_seconds=_positive_int("MAX_FLOOD_WAIT_SECONDS", 3600),
        control_bot_token=control_bot_token,
        control_admin_id=control_admin_id,
        control_session_path=_project_path(
            "CONTROL_SESSION_PATH",
            "session/control_bot",
        ),
        secondary_db_path=_project_path("SECONDARY_DB_PATH", "data_second/processed.sqlite3")
        if os.getenv("SECONDARY_DB_PATH", "").strip() else None,
    )
    if settings.secondary_db_path == settings.db_path:
        raise ValueError("Аккаунты должны использовать разные базы данных")
    settings.session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.control_session_path.parent.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )
    settings.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return settings


def session_file(session_path: Path) -> Path:
    if session_path.suffix == ".session":
        return session_path
    return Path(f"{session_path}.session")


def enforce_private_permissions(settings: Settings) -> list[str]:
    warnings: list[str] = []
    for directory in {
        settings.session_path.parent,
        settings.control_session_path.parent,
        settings.db_path.parent,
    }:
        try:
            if stat.S_IMODE(directory.stat().st_mode) != 0o700:
                directory.chmod(0o700)
        except OSError as exc:
            warnings.append(f"не удалось установить chmod 700 для {directory}: {exc}")

    protected_paths: list[Path | None] = [
        ENV_PATH,
        session_file(settings.session_path),
        session_file(settings.control_session_path),
        settings.db_path,
        settings.log_file,
    ]
    protected_paths.extend(
        settings.control_session_path.parent.glob(
            f"{settings.control_session_path.name}_*.session*"
        )
    )
    for path in protected_paths:
        if path is None or not path.exists():
            continue
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                path.chmod(0o600)
                mode = stat.S_IMODE(path.stat().st_mode)
                if mode & 0o077:
                    warnings.append(f"небезопасные права {oct(mode)} у {path}")
        except OSError as exc:
            warnings.append(f"не удалось установить chmod 600 для {path}: {exc}")
    return warnings
