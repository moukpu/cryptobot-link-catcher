from __future__ import annotations

from telethon.tl import types

from app.main import entity_has_active_username


def test_accepts_secondary_active_cryptobot_username() -> None:
    entity = types.User(
        id=1559501630,
        is_self=False,
        contact=False,
        mutual_contact=False,
        deleted=False,
        bot=True,
        bot_chat_history=False,
        bot_nochats=False,
        verified=True,
        restricted=False,
        min=False,
        bot_inline_geo=False,
        support=False,
        scam=False,
        apply_min_photo=False,
        fake=False,
        bot_attach_menu=False,
        premium=False,
        attach_menu_enabled=False,
        bot_can_edit=False,
        close_friend=False,
        stories_hidden=False,
        stories_unavailable=False,
        contact_require_premium=False,
        bot_business=False,
        bot_has_main_app=False,
        access_hash=1,
        first_name="Crypto Bot",
        username="send",
        usernames=[
            types.Username(editable=False, active=True, username="CryptoBot")
        ],
    )
    assert entity_has_active_username(entity, "CryptoBot")


def test_rejects_inactive_or_unrelated_username() -> None:
    entity = types.User(
        id=1,
        is_self=False,
        contact=False,
        mutual_contact=False,
        deleted=False,
        bot=True,
        bot_chat_history=False,
        bot_nochats=False,
        verified=False,
        restricted=False,
        min=False,
        bot_inline_geo=False,
        support=False,
        scam=False,
        apply_min_photo=False,
        fake=False,
        bot_attach_menu=False,
        premium=False,
        attach_menu_enabled=False,
        bot_can_edit=False,
        close_friend=False,
        stories_hidden=False,
        stories_unavailable=False,
        contact_require_premium=False,
        bot_business=False,
        bot_has_main_app=False,
        access_hash=1,
        first_name="Other",
        username="OtherBot",
        usernames=[
            types.Username(editable=False, active=False, username="CryptoBot")
        ],
    )
    assert not entity_has_active_username(entity, "CryptoBot")
