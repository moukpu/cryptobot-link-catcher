import asyncio
from telethon import types
from app.control_bot import ControlBot
from app.database import ProcessedStore, Claim


def test_accounts_have_independent_deduplication_and_shared_pause(tmp_path):
    async def scenario():
        one=ProcessedStore(tmp_path/'one.sqlite');two=ProcessedStore(tmp_path/'two.sqlite')
        await one.open();await two.open()
        try:
            bot=object.__new__(ControlBot);bot.store=one;bot.secondary_store=two
            claim=Claim('CQsame',100,42,123)
            assert await one.claim(claim)
            assert not await one.claim(claim)
            assert await two.claim(claim)
            assert not await two.claim(claim)
            await one.queue_notice('same-check','Первое получение')
            await two.queue_notice('same-check','Второе получение')
            await one.notice_sent('same-check')
            assert await one.pending_notices()==[]
            assert len(await two.pending_notices())==1
            await bot._set_enabled(False)
            assert not await one.get_bool_setting('catcher_enabled',True)
            assert not await two.get_bool_setting('catcher_enabled',True)
            await bot._set_enabled(True)
            assert await one.get_bool_setting('catcher_enabled',False)
            assert await two.get_bool_setting('catcher_enabled',False)
        finally:await one.close();await two.close()
    asyncio.run(scenario())


def test_second_receipt_notification_labels_account_and_preserves_link(tmp_path):
    async def scenario():
        one=ProcessedStore(tmp_path/'one.sqlite');two=ProcessedStore(tmp_path/'two.sqlite')
        await one.open();await two.open()
        try:
            await two.queue_notice('receipt:second','Получено <a href="https://t.me/test_channel/42">из сообщения</a>')
            sent=[]
            class Client:
                async def get_input_entity(self,admin_id):return types.InputPeerUser(admin_id,0)
                async def __call__(self,request):sent.append(request)
            bot=object.__new__(ControlBot);bot.store=one;bot.secondary_store=two;bot.admin_id=123;bot.client=Client();bot.stop_event=asyncio.Event()
            async def stop(seconds):bot.stop_event.set()
            bot._wait_or_stop=stop
            await bot._notice_loop()
            assert len(sent)==1
            assert sent[0].message.startswith('Аккаунт 2\n')
            assert sent[0].peer.user_id==123
            assert sent[0].entities[0].url=='https://t.me/test_channel/42'
            assert await two.pending_notices()==[]
            stats=await bot.stats_text()
            assert 'Аккаунт 1\n' in stats and 'Аккаунт 2\n' in stats
        finally:await one.close();await two.close()
    asyncio.run(scenario())
