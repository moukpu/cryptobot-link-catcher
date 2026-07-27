from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(level_name: str, log_file: Path | None) -> None:
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handlers.append(file_handler)

    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    logging.getLogger("telethon").setLevel(max(level, logging.WARNING))

