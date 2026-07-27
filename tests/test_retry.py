from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError, RandomIdDuplicateError

from app.main import Userbot


def make_userbot(client) -> Userbot:
    userbot = object.__new__(Userbot)
    userbot.client = client
    userbot.target_bot = object()
    userbot.stop_event = asyncio.Event()
    userbot.settings = SimpleNamespace(
        send_retries=3,
        retry_base_seconds=1,
        max_flood_wait_seconds=10,
    )

    async def no_wait(_seconds: int) -> None:
        return None

    userbot._wait_or_stop = no_wait
    return userbot


def test_network_retry_reuses_same_random_id() -> None:
    class Client:
        def __init__(self) -> None:
            self.random_ids = []

        async def __call__(self, request):
            self.random_ids.append(request.random_id)
            if len(self.random_ids) == 1:
                raise OSError("temporary")

    async def scenario() -> None:
        client = Client()
        userbot = make_userbot(client)
        await userbot._start_target_bot("PAYLOAD", 123456)
        assert client.random_ids == [123456, 123456]

    asyncio.run(scenario())


def test_duplicate_random_id_is_treated_as_success() -> None:
    class Client:
        async def __call__(self, _request):
            raise RandomIdDuplicateError(request=None)

    asyncio.run(make_userbot(Client())._start_target_bot("PAYLOAD", 321))


def test_flood_wait_above_limit_fails_without_sleeping() -> None:
    class Client:
        async def __call__(self, _request):
            raise FloodWaitError(request=None, capture=20)

    with pytest.raises(RuntimeError, match="превышает"):
        asyncio.run(make_userbot(Client())._start_target_bot("PAYLOAD", 321))
