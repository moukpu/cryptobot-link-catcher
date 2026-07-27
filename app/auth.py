from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient

from app.config import enforce_private_permissions, load_settings, session_file
from app.logging_setup import configure_logging


async def authorize() -> None:
    settings = load_settings(require_phone=True)
    configure_logging(settings.log_level, settings.log_file)
    logger = logging.getLogger("auth")

    client = TelegramClient(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        device_model="AWS CryptoBot Userbot",
        system_version="Ubuntu",
        app_version="1.0",
    )
    try:
        assert settings.phone_number is not None
        await client.start(phone=settings.phone_number)
        logger.info("Авторизация успешна")
    finally:
        await client.disconnect()
        for warning in enforce_private_permissions(settings):
            logger.warning(warning)
        logger.info("Файл сессии: %s", session_file(settings.session_path))


if __name__ == "__main__":
    asyncio.run(authorize())
