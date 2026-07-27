from __future__ import annotations

import html
import logging
import re
from urllib.parse import parse_qs, unquote, urlsplit

from telethon.tl import types


TARGET_USERNAME = "cryptobot"
logger = logging.getLogger(__name__)
TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
START_PARAMETER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
URL_RE = re.compile(
    r"""(?ix)
    (?:
        https?://
        | tg:(?://)?
        | (?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/
        | [A-Za-z0-9_]{5,32}\.t\.me(?:/|\?)
    )
    [^\s<>"'\[\]{}]+
    """
)
TRAILING_PUNCTUATION = ".,;:!?)]}»”’"


def _clean_candidate(value: str) -> str:
    return html.unescape(value.strip()).rstrip(TRAILING_PUNCTUATION)


def _query_value(query: str, key: str) -> str | None:
    values = parse_qs(query, keep_blank_values=True)
    for current_key, current_values in values.items():
        if current_key.lower() == key and current_values:
            return current_values[0]
    return None


def parse_cryptobot_start_link(
    candidate: str,
    allowed_usernames: set[str] | None = None,
) -> str | None:
    value = _clean_candidate(candidate)
    if not value:
        return None

    lowered = value.lower()
    if lowered.startswith(
        (
            "t.me/",
            "telegram.me/",
            "telegram.dog/",
            "www.t.me/",
            "www.telegram.me/",
            "www.telegram.dog/",
        )
    ):
        value = f"https://{value}"
    elif re.match(r"(?i)^[A-Za-z0-9_]{5,32}\.t\.me(?:/|\?)", value):
        value = f"https://{value}"

    try:
        parsed = urlsplit(value)
    except ValueError:
        return None

    username: str | None = None
    if parsed.scheme.lower() in {"http", "https"}:
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]

        if host in TELEGRAM_HOSTS:
            parts = [unquote(part) for part in parsed.path.split("/") if part]
            if len(parts) == 1:
                username = parts[0]
        elif host.endswith(".t.me"):
            subdomain = host[: -len(".t.me")]
            if "." not in subdomain and not parsed.path.strip("/"):
                username = unquote(subdomain)
        else:
            return None
    elif parsed.scheme.lower() == "tg":
        action = (parsed.netloc or parsed.path).strip("/").lower()
        if action != "resolve":
            return None
        username = _query_value(parsed.query, "domain")
    else:
        return None

    allowed = {
        value.lstrip("@").lower()
        for value in (allowed_usernames or {TARGET_USERNAME})
    }
    if not username or username.lstrip("@").lower() not in allowed:
        return None

    start_parameter = _query_value(parsed.query, "start")
    if not start_parameter or not START_PARAMETER_RE.fullmatch(start_parameter):
        return None
    return start_parameter


def find_urls_in_text(text: str | None) -> set[str]:
    if not text:
        return set()
    return {_clean_candidate(match.group(0)) for match in URL_RE.finditer(text)}


async def collect_message_urls(message: object) -> set[str]:
    candidates = find_urls_in_text(getattr(message, "raw_text", None))
    reply_markup = getattr(message, "reply_markup", None)
    for row in getattr(reply_markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            url = getattr(button, "url", None)
            if url:
                candidates.add(_clean_candidate(url))

    get_entities_text = getattr(message, "get_entities_text", None)
    if callable(get_entities_text):
        try:
            for entity, visible_text in get_entities_text():
                if isinstance(entity, types.MessageEntityTextUrl):
                    candidates.add(_clean_candidate(entity.url))
                elif isinstance(entity, types.MessageEntityUrl):
                    candidates.add(_clean_candidate(visible_text))
        except Exception:
            logger.exception("Не удалось прочитать URL-entities сообщения")

    get_buttons = getattr(message, "get_buttons", None)
    if callable(get_buttons):
        try:
            buttons = await get_buttons()
            for row in buttons or []:
                for button in row:
                    url = getattr(button, "url", None)
                    if url:
                        candidates.add(_clean_candidate(url))
        except Exception:
            logger.exception("Не удалось прочитать inline-кнопки сообщения")
    return candidates


async def extract_start_parameters(
    message: object,
    allowed_usernames: set[str] | None = None,
) -> set[str]:
    candidates = await collect_message_urls(message)
    return {
        parameter
        for candidate in candidates
        if (
            parameter := parse_cryptobot_start_link(
                candidate,
                allowed_usernames,
            )
        )
        is not None
    }
