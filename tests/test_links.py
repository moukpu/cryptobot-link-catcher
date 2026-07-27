from __future__ import annotations

import asyncio

from telethon.tl import types

from app.links import (
    extract_start_parameters,
    find_urls_in_text,
    parse_cryptobot_start_link,
)


def test_supported_links() -> None:
    expected = "TEST_PARAMETER_1"
    links = [
        "http://t.me/CryptoBot?start=TEST_PARAMETER_1",
        "https://t.me/CryptoBot?start=TEST_PARAMETER_1",
        "https://telegram.me/cryptobot?start=TEST_PARAMETER_1",
        "https://telegram.dog/CryptoBot?start=TEST_PARAMETER_1",
        "tg://resolve?domain=CryptoBot&start=TEST_PARAMETER_1",
        "tg:resolve?domain=@CryptoBot&start=TEST_PARAMETER_1",
        "https://CryptoBot.t.me/?start=TEST_PARAMETER_1",
        "t.me/CryptoBot?start=TEST_PARAMETER_1",
        "www.telegram.me/CryptoBot?start=TEST_PARAMETER_1",
    ]
    assert {parse_cryptobot_start_link(link) for link in links} == {expected}


def test_rejects_other_destinations_and_invalid_payloads() -> None:
    links = [
        "https://t.me/OtherBot?start=TEST_PARAMETER_1",
        "https://evil.example/CryptoBot?start=TEST_PARAMETER_1",
        "https://t.me/CryptoBot?start=",
        "https://t.me/CryptoBot?start=contains%20space",
        f"https://t.me/CryptoBot?start={'a' * 65}",
        "tg://resolve?domain=NotCryptoBot&start=ok",
        "tg://user?domain=CryptoBot&start=ok",
        "https://t.me/CryptoBot?startapp=ok",
        "https://t.me/CryptoBot/anything?start=ok",
        "https://CryptoBot.t.me/anything?start=ok",
    ]
    assert all(parse_cryptobot_start_link(link) is None for link in links)


def test_accepts_verified_alias_only_when_explicitly_allowed() -> None:
    link = "http://t.me/send?start=ALIAS_PARAMETER"
    assert parse_cryptobot_start_link(link) is None
    assert (
        parse_cryptobot_start_link(link, {"cryptobot", "send", "calc"})
        == "ALIAS_PARAMETER"
    )


def test_extracts_links_from_surrounding_text_and_html_ampersand() -> None:
    text = (
        "Первая: (https://t.me/CryptoBot?start=ONE). "
        "Вторая: tg://resolve?domain=CryptoBot&amp;start=TWO!"
    )
    parameters = {
        parameter
        for url in find_urls_in_text(text)
        if (parameter := parse_cryptobot_start_link(url))
    }
    assert parameters == {"ONE", "TWO"}


def test_extracts_hidden_entity_and_inline_button_links() -> None:
    class Button:
        url = "tg://resolve?domain=CryptoBot&start=BUTTON"

    class Message:
        raw_text = "Видимого URL здесь нет"

        @staticmethod
        def get_entities_text():
            return [
                (
                    types.MessageEntityTextUrl(
                        offset=0,
                        length=5,
                        url="https://t.me/CryptoBot?start=HIDDEN",
                    ),
                    "нажми",
                )
            ]

        @staticmethod
        async def get_buttons():
            return [[Button()]]

    assert asyncio.run(extract_start_parameters(Message())) == {"HIDDEN", "BUTTON"}


def test_extracts_raw_inline_button_without_network_lookup() -> None:
    class Button:
        url = "https://t.me/send?start=RAW_BUTTON"

    class Row:
        buttons = [Button()]

    class Markup:
        rows = [Row()]

    class Message:
        raw_text = ""
        reply_markup = Markup()

    assert asyncio.run(
        extract_start_parameters(Message(), {"cryptobot", "send"})
    ) == {"RAW_BUTTON"}
