import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from telethon import functions
from app.main import Userbot
from app.database import Claim,ProcessedStore
from app.config import TARGET_BOT_ID


def msg(id,text,*,out=False,sender=TARGET_BOT_ID,forwarded=None):
    return SimpleNamespace(id=id,raw_text=text,message=text,out=out,sender_id=sender,
        date=datetime.now(timezone.utc),reply_to_msg_id=None,fwd_from=forwarded,
        peer_id=SimpleNamespace(user_id=TARGET_BOT_ID))


class Client:
    def __init__(self,response=None):
        self.messages = [msg(10,'Кошелёк')]
        self.requests = []
        self.response = response
    async def get_messages(self,peer,limit=1): return sorted(self.messages,key=lambda m:m.id,reverse=True)[:limit]
    async def __call__(self,request):
        self.requests.append(request)
        assert isinstance(request,functions.messages.StartBotRequest)
        outgoing = msg(20,'/start '+request.start_param,out=True)
        self.messages.append(outgoing)
        if self.response: self.messages.append(msg(21,self.response))
        return SimpleNamespace(updates=[SimpleNamespace(message=outgoing)])
    async def iter_messages(self,peer,limit=30,min_id=0,max_id=None,reverse=False):
        rows = [m for m in self.messages if m.id>min_id and (max_id is None or m.id<max_id)]
        for m in sorted(rows,key=lambda m:m.id,reverse=not reverse)[:limit]: yield m


class Panel:
    def __init__(self): self.notices = []
    async def notify(self,text,*,key): self.notices.append((text,key))


def userbot(store,client):
    user = object.__new__(Userbot)
    user.store = store; user.client = client; user.target_bot = SimpleNamespace(id=TARGET_BOT_ID)
    user.target_peer_id = TARGET_BOT_ID
    user.stop_event = asyncio.Event(); user.reply_lock = asyncio.Lock(); user.result_event = asyncio.Event()
    user.active_parameter = None; user.control_bot = Panel()
    user.settings = SimpleNamespace(send_retries=1,retry_base_seconds=1,max_flood_wait_seconds=10)
    return user


@pytest.mark.parametrize('response,outcome,amount',[
    ('📥 Вы получили 🪙 17 USDT ($16.99).','received',{'USDT':'17'}),
    ('Чтобы активировать этот чек, подпишитесь на канал(ы).','subscription',{}),
    ('Введите пароль от чека для получения 🪙 0.5 USDT ($0.51).','password',{}),
    ('Пройдите капчу.','captcha',{}),
    ('Выберите монету для оплаты счета #IV123.','payment',{}),
    ('Этот чек уже активирован.','already_used',{}),
])
def test_request_then_real_result_and_source(tmp_path,response,outcome,amount):
    async def scenario():
        store = ProcessedStore(tmp_path/'db.sqlite3'); await store.open()
        claim = Claim('CQtoken',-1001234567890,42,123,'Original <chat>','original_chat','channel')
        await store.claim(claim)
        client = Client(response); user = userbot(store,client)
        await user._process_claim(claim)
        assert (await store.detail(claim.parameter))['outcome'] == outcome
        assert (await store.income())['totals'] == amount
        assert len(client.requests) == 1
        notice = user.control_bot.notices[0][0]
        assert 'https://t.me/original_chat/42' in notice
        assert 'Original &lt;chat&gt;' in notice
        assert ('✅ Получено' in notice) == (outcome=='received')
        # Replayed updates do not generate another notice or more income.
        await user._on_bot_message(client.messages[-1])
        assert len(user.control_bot.notices) == 1
        await store.close()
    asyncio.run(scenario())


def test_invoice_in_old_queue_and_last_line_guard(tmp_path):
    async def scenario():
        store = ProcessedStore(tmp_path/'db.sqlite3'); await store.open()
        claim = Claim('IV123',1,2,3); await store.claim(claim)
        client = Client(); user = userbot(store,client)
        await user._process_claim(claim)
        assert not client.requests
        assert (await store.detail('IV123'))['outcome'] == 'blocked'
        with pytest.raises(ValueError): await user._start_target_bot('IV123',123)
        assert not client.requests
        await store.close()
    asyncio.run(scenario())


def test_no_response_is_not_success(tmp_path,monkeypatch):
    monkeypatch.setattr('app.main.BOT_REPLY_TIMEOUT_SECONDS',0.01)
    async def scenario():
        store = ProcessedStore(tmp_path/'db.sqlite3'); await store.open()
        claim = Claim('CQsilent',1,2,3); await store.claim(claim)
        user = userbot(store,Client())
        await user._process_claim(claim)
        assert (await store.detail('CQsilent'))['outcome'] == 'unconfirmed'
        assert '✅' not in user.control_bot.notices[0][0]
        assert (await store.income())['count'] == 0
        await store.close()
    asyncio.run(scenario())


def test_manual_outgoing_cancels_attribution_and_spoof_is_ignored(tmp_path):
    async def scenario():
        store = ProcessedStore(tmp_path/'db.sqlite3'); await store.open()
        claim = Claim('CQpending',1,2,3); await store.claim(claim)
        await store.begin_attempt('CQpending',10); await store.register_request('CQpending',20)
        user = userbot(store,Client()); user.active_parameter = 'CQpending'
        await user._on_bot_message(msg(21,'manual',out=True))
        assert (await store.detail('CQpending'))['outcome'] == 'unconfirmed'
        await user._on_bot_message(msg(22,'Вы получили 999 USDT',sender=777))
        await user._on_bot_message(msg(23,'Вы получили 999 USDT',forwarded=object()))
        assert (await store.income())['unmatched_count'] == 0
        await store.close()
    asyncio.run(scenario())
