from __future__ import annotations

from app.control_bot import ControlBot
from app.database import ProcessedStore
import asyncio
from telethon import types
from telethon.errors import FloodWaitError,RandomIdDuplicateError


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


def test_notification_retry_reuses_id_and_respects_floodwait(tmp_path):
    async def scenario():
        store = ProcessedStore(tmp_path/'db.sqlite3'); await store.open()
        await store.queue_notice('receipt:one','Получено <a href="https://t.me/test_channel/42">из сообщения</a>')
        ids = []
        class Client:
            async def get_input_entity(self,admin_id): return types.InputPeerUser(admin_id,0)
            async def __call__(self,request):
                ids.append(request.random_id)
                assert request.entities[0].url == 'https://t.me/test_channel/42'
                if len(ids)==1: raise FloodWaitError(request=request,capture=120)
                raise RandomIdDuplicateError(request=request)
        bot = object.__new__(ControlBot)
        bot.store = store; bot.admin_id = 123; bot.client = Client()
        delays = []
        async def wait(seconds):
            delays.append(seconds); bot.stop_event.set()
        bot._wait_or_stop = wait
        bot.stop_event = asyncio.Event()
        await bot._notice_loop()
        assert 121 in delays
        assert len(await store.pending_notices()) == 1
        bot.stop_event = asyncio.Event()
        await bot._notice_loop()
        assert ids[0] == ids[1]
        assert await store.pending_notices() == []
        await store.close()
    asyncio.run(scenario())
