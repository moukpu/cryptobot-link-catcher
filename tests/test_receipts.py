import asyncio
import sqlite3
from datetime import datetime, timezone, timedelta
from app.database import Claim, ProcessedStore
from app.receipts import BotReply, classify_bot_reply, is_receive_check, source_links, source_html
import pytest


@pytest.mark.parametrize('parameter',['IV123456','invoice-IV123','CQ','cq123','PAYLOAD','CQ bad','CQ'+'a'*63])
def test_only_receive_prefix_is_allowed(parameter):
    assert not is_receive_check(parameter)


def test_receive_prefix():
    assert is_receive_check('CQ123456abcDEF_-')


@pytest.mark.parametrize('text,kind',[
    ('📥 Вы получили 🪙 17 USDT ($16.99).','received'),
    ('Выберите монету для оплаты счета #IV65967563 на сумму 1.4 USD от Shop.','payment'),
    ('Чтобы активировать этот чек, подпишитесь на канал(ы).','subscription'),
    ('Введите пароль от чека для получения 🪙 0.5 USDT ($0.51).','password'),
    ('Пройдите капчу для активации чека.','captcha'),
    ('Этот чек уже активирован.','already_used'),
    ('Чек не найден.','unavailable'),
    ('👛 Кошелёк\n🪙 Tether: 17.457852 USDT ($17.46)','unknown'),
    ('Вы оплатили счёт #IV123 на сумму 1.4 USD (1.400311 USDT).','payment'),
    ('Описание: Вы получили 500 USDT','unknown'),
    ('Введите пароль\nВы получили 500 USDT','password'),
    ('You received 0.00000001 BTC ($0.01).','received'),
    ('Вы получили 1,000 USDT','unknown'),
    ('Вы получили 1,000.25 USDT','received'),
    ('Вы получили 0,001 TON','received'),
])
def test_reply_templates(text,kind):
    assert classify_bot_reply(text).kind == kind


def test_source_links_and_escaping():
    assert source_links(-1001234567890,42,'test_channel','channel') == ('https://t.me/test_channel','https://t.me/test_channel/42')
    assert source_links(-1001234567890,42,None,'group')[1] == 'https://t.me/c/1234567890/42'
    assert source_links(-12345,42,None,'group') == (None,None)
    assert source_links(12345,42,None,'user') == ('tg://user?id=12345',None)
    assert '&lt;b&gt;' in source_html({'source_title':'<b>fake</b>','chat_url':'https://t.me/test_channel','message_url':None})


def test_receipt_dedup_and_restart(tmp_path):
    async def scenario():
        path = tmp_path/'db.sqlite3'
        store = ProcessedStore(path)
        await store.open()
        claim = Claim('CQfirst',-1001234567890,42,777,'Source','test_channel','channel')
        await store.claim(claim)
        await store.begin_attempt(claim.parameter,100)
        await store.register_request(claim.parameter,101)
        received = BotReply('received','0.00000001','BTC','0.01')
        assert (await store.record_bot_reply(102,received,101))['outcome'] == 'received'
        assert await store.record_bot_reply(102,received,101) is None
        assert await store.record_bot_reply(102,BotReply('received','999','BTC'),101) is None
        assert await store.record_bot_reply(103,received,101) is None
        await store.close()
        store = ProcessedStore(path)
        await store.open()
        assert (await store.income())['totals'] == {'BTC':'0.00000001'}
        assert (await store.history_page())[0][0]['message_url'] == 'https://t.me/test_channel/42'
        await store.close()
    asyncio.run(scenario())


def test_manual_and_delayed_income_not_assigned_to_next_check(tmp_path):
    async def scenario():
        store = ProcessedStore(tmp_path/'db.sqlite3')
        await store.open()
        await store.claim(Claim('CQold',1,2,3))
        await store.begin_attempt('CQold',100)
        await store.register_request('CQold',101)
        await store.finish_outcome('CQold','unconfirmed',uncertain=True)
        await store.claim(Claim('CQnew',2,3,4))
        await store.begin_attempt('CQnew',104)
        await store.register_request('CQnew',105)
        receipt = BotReply('received','17','USDT')
        row = await store.record_bot_reply(106,receipt,105)
        assert row['parameter'] is None
        row = await store.record_bot_reply(107,receipt,999)
        assert row['parameter'] is None
        assert (await store.income())['totals'] == {}
        assert (await store.income())['unmatched'] == {'USDT':'34'}
        # An explicit Telegram reply can establish a source even after uncertainty.
        row = await store.record_bot_reply(108,BotReply('received','1','USDT'),105,explicit=True)
        assert row['parameter'] == 'CQnew'
        await store.close()
    asyncio.run(scenario())


def test_stale_reply_not_success(tmp_path):
    async def scenario():
        store = ProcessedStore(tmp_path/'db.sqlite3'); await store.open()
        await store.claim(Claim('CQnew',1,2,3)); await store.begin_attempt('CQnew',100)
        await store.register_request('CQnew',101)
        stale = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        row = await store.record_bot_reply(102,BotReply('received','17','USDT'),101,received_at=stale)
        assert row['parameter'] is None
        await store.close()
    asyncio.run(scenario())


def test_recover_inflight_without_resending_or_erasing_legacy(tmp_path):
    async def scenario():
        path = tmp_path/'db.sqlite3'; store = ProcessedStore(path); await store.open()
        await store.claim(Claim('CQinflight',1,2,3)); await store.begin_attempt('CQinflight',100)
        await store.register_request('CQinflight',101)
        await store.close()
        store = ProcessedStore(path); await store.open()
        assert await store.recover_attempts() == 1
        assert await store.pending_claims() == []
        assert (await store.detail('CQinflight'))['outcome'] == 'unconfirmed'
        assert not await store.get_bool_setting('correlation_safe',True)
        await store.close()
    asyncio.run(scenario())


def test_migrate_original_schema(tmp_path):
    path = tmp_path/'db.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE processed_starts(parameter TEXT PRIMARY KEY,status TEXT NOT NULL CHECK(status IN ('processing','sent','failed')),source_chat_id INTEGER,source_message_id INTEGER,claimed_at TEXT NOT NULL,finished_at TEXT,error TEXT,random_id INTEGER)")
        db.execute("INSERT INTO processed_starts VALUES('IVold','sent',1,2,'2026-09-01T00:00:00+00:00',NULL,NULL,3)")
    async def scenario():
        store = ProcessedStore(path); await store.open()
        assert (await store.detail('IVold'))['outcome'] == 'legacy'
        assert (await store.statistics())['sent'] == 1
        assert (await store.income())['count'] == 0
        assert not await store.claim(Claim('IVold',1,2,3))
        await store.close()
    asyncio.run(scenario())


def test_outbox_survives_reopen_and_reuses_id(tmp_path):
    async def scenario():
        path = tmp_path/'db.sqlite3'; store = ProcessedStore(path); await store.open()
        await store.queue_notice('one','first')
        notice = (await store.pending_notices())[0]
        await store.queue_notice('one','duplicate')
        await store.close()
        store = ProcessedStore(path); await store.open()
        assert await store.pending_notices() == [notice]
        await store.notice_sent('one')
        assert await store.pending_notices() == []
        await store.close()
    asyncio.run(scenario())
