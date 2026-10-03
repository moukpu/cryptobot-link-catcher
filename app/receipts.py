from __future__ import annotations

import html
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

CHECK_PARAMETER_RE = re.compile(r"CQ[A-Za-z0-9_-]{1,62}\Z")
LABELS = {
    "queued": "⏳ В очереди", "awaiting": "⏳ Ожидается ответ",
    "received": "✅ Получено", "subscription": "➖ Обязательная подписка",
    "password": "➖ Требуется пароль", "captcha": "➖ Требуется капча",
    "unavailable": "➖ Чек недоступен", "already_used": "➖ Уже активирован",
    "payment": "⛔ Счёт на оплату заблокирован", "blocked": "⛔ Ссылка пропущена",
    "unconfirmed": "❔ Результат не подтверждён", "failed": "❌ Ошибка отправки",
    "legacy": "📋 Старый запуск: получение не подтверждено",
    "legacy_failed": "📋 Старый запуск: ошибка отправки",
    "unknown": "❔ Неизвестный ответ",
}


def is_receive_check(parameter: str) -> bool:
    return CHECK_PARAMETER_RE.fullmatch(parameter) is not None


@dataclass(frozen=True)
class BotReply:
    kind: str
    amount: str | None = None
    asset: str | None = None
    usd: str | None = None


def decimal_amount(raw: str) -> str | None:
    raw = raw.replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    if "," in raw and "." in raw:
        # English grouping, e.g. 1,000.25. Reject irregular grouping.
        if not re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d+", raw): return None
        raw = raw.replace(",", "")
    elif "," in raw:
        if raw.count(",") != 1: return None
        left, right = raw.split(",")
        if len(right) == 3 and left != "0": return None
        raw = raw.replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0 or value.adjusted() > 40: return None
    return format(value, "f")


RECEIVED_RE = re.compile(
    r"^[^\w]{0,12}(?:Вы получили|You (?:have )?received)\s*:?\s*"
    r"[^\w]{0,8}(?P<amount>\d[\d.,\u00a0\u202f ]*)\s+"
    r"(?P<asset>[A-Z][A-Z0-9]{1,11})"
    r"(?:\s*\(\$(?P<usd>\d[\d., ]*)\))?[.!]?\s*$", re.IGNORECASE,
)


def classify_bot_reply(text: str | None) -> BotReply:
    text = (text or "").replace("\u200b", "").replace("\ufe0f", "").strip()
    first = text.splitlines()[0] if text else ""
    lower = first.lower().replace("ё", "е")
    if re.search(r"для оплаты|оплатите|оплатить счет|счет на оплату|вы оплатили|pay (?:this |the )?invoice|choose.*(?:pay|payment)|you paid", lower):
        return BotReply("payment")
    if re.search(r"подпишитесь|требуется подписка|join.*(?:channel|group)|subscribe.*(?:channel|group)", lower):
        return BotReply("subscription")
    if re.search(r"введите пароль|требуется пароль|enter.*password|password.*required", lower):
        return BotReply("password")
    if re.search(r"капч|captcha|verify.*human", lower):
        return BotReply("captcha")
    if re.search(r"уже (?:был )?активирован|уже активировали|already (?:been )?(?:activated|redeemed)", lower):
        return BotReply("already_used")
    if re.search(r"чек.*(?:не найден|недоступен|законч|истек|не существует|удален|полностью активирован)|(?:check).*(?:not found|unavailable|expired|deleted|fully claimed)", lower):
        return BotReply("unavailable")
    match = RECEIVED_RE.fullmatch(first)
    if match:
        amount = decimal_amount(match['amount'])
        if amount:
            return BotReply('received', amount, match['asset'].upper(), decimal_amount(match['usd']) if match['usd'] else None)
    return BotReply("unknown")


def source_links(chat_id: int | None, message_id: int | None, username: str | None, chat_type: str) -> tuple[str | None, str | None]:
    if username and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        chat = 'https://t.me/'+username
        return chat, f'{chat}/{message_id}' if message_id and chat_type in {'channel','group'} else None
    if chat_id and chat_id < -1000000000000 and message_id:
        message = f'https://t.me/c/{-chat_id - 1000000000000}/{message_id}'
        return message, message  # A private message link also opens its parent chat.
    if chat_id and chat_id > 0:
        return f'tg://user?id={chat_id}', None
    return None, None


def source_html(row: dict) -> str:
    title = html.escape((row.get('source_title') or str(row.get('source_chat_id') or 'Неизвестный чат'))[:100])
    chat, message = row.get('chat_url'), row.get('message_url')
    label = f'<a href="{html.escape(chat, quote=True)}">{title}</a>' if chat else title
    if message:
        return f'Источник: {label}\n<a href="{html.escape(message, quote=True)}">Открыть сообщение с чеком</a>'
    return f'Источник: {label}\nСообщение: {row.get("source_message_id") or "неизвестно"}'
