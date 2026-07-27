from __future__ import annotations

from app.control_bot import ControlBot


def test_panel_access_requires_private_owner_chat() -> None:
    bot = object.__new__(ControlBot)
    bot.admin_id = 123
    assert bot._authorized(123, 123, True)
    assert not bot._authorized(123, -100500, False)
    assert not bot._authorized(999, 999, True)


def test_locked_dialog_has_lock_icon() -> None:
    rows = ControlBot._chat_buttons(
        [(100, "Crypto Bot", "CryptoBot", "bot", False, True)],
        page=0,
        total_pages=1,
        view="all",
    )
    assert rows[0][0].text.startswith("🔒 🤖")
