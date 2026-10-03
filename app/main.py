from __future__ import annotations

import asyncio
import fcntl
import logging
import secrets
import signal
import time
from contextlib import suppress
from datetime import datetime, timezone

from telethon import TelegramClient, events, functions, utils
from telethon.errors import FloodWaitError, RPCError, RandomIdDuplicateError
from telethon.tl import types

from app import __version__
from app.config import (
    TARGET_BOT_ID,
    TARGET_BOT_USERNAME,
    enforce_private_permissions,
    load_settings,
)
from app.control_bot import ControlBot
from app.database import Claim, ProcessedStore
from app.links import extract_start_parameters
from app.logging_setup import configure_logging
from app.receipts import is_receive_check, classify_bot_reply, source_html, LABELS


logger = logging.getLogger("cryptobot_userbot")
BOT_REPLY_TIMEOUT_SECONDS = 20


def entity_has_active_username(entity: types.User, username: str) -> bool:
    expected = username.lstrip("@").lower()
    active_names = {
        entity.username.lower()
        for _ in (0,)
        if entity.username
    }
    active_names.update(
        item.username.lower()
        for item in (entity.usernames or [])
        if item.active and item.username
    )
    return expected in active_names


def active_usernames(entity: types.User) -> set[str]:
    names = {entity.username.lower()} if entity.username else set()
    names.update(
        item.username.lower()
        for item in (entity.usernames or [])
        if item.active and item.username
    )
    return names


def entity_chat_type(entity: object) -> str:
    if isinstance(entity, types.User):
        return "bot" if entity.bot else "user"
    if isinstance(entity, types.Channel):
        return "channel" if entity.broadcast else "group"
    if isinstance(entity, types.Chat):
        return "group"
    return "unknown"


class Userbot:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.store = ProcessedStore(self.settings.db_path)
        self.secondary_store: ProcessedStore | None = None
        self.stop_event = asyncio.Event()
        self.client: TelegramClient | None = None
        self.target_bot: types.User | None = None
        self.target_peer_id: int | None = None
        self.target_usernames: set[str] = set()
        self.control_bot: ControlBot | None = None
        self.control_task: asyncio.Task[None] | None = None
        self.ready_event = asyncio.Event()
        self.work_queue: asyncio.Queue[Claim] | None = None
        self.worker_task: asyncio.Task[None] | None = None
        self.queued_parameters: set[str] = set()
        self.lock_file = None
        self.reply_lock = asyncio.Lock()
        self.result_event = asyncio.Event()
        self.active_parameter: str | None = None

    async def start(self) -> None:
        lock_path = self.settings.db_path.parent / "userbot.lock"
        self.lock_file = lock_path.open("a+")
        try:
            fcntl.flock(
                self.lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise RuntimeError("Уже запущена другая копия процесса") from exc
        lock_path.chmod(0o600)
        await self.store.open()
        await self.store.recover_attempts()
        for warning in enforce_private_permissions(self.settings):
            logger.warning(warning)

        try:
            reconnect_delay = 3
            while not self.stop_event.is_set():
                connected_at = time.monotonic()
                try:
                    await self._run_connected()
                    reconnect_delay = 3
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if time.monotonic() - connected_at >= 30:
                        reconnect_delay = 3
                    logger.exception(
                        "Соединение завершилось с ошибкой; повтор через %s сек.",
                        reconnect_delay,
                    )
                    await self._wait_or_stop(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)
                finally:
                    with suppress(Exception):
                        await self.store.set_bool_setting("worker_connected", False)
                    if self.client is not None:
                        with suppress(Exception):
                            await self.client.disconnect()
                        self.client = None
        finally:
            if self.control_bot is not None:
                self.control_bot.stop()
            if self.control_task is not None:
                try:
                    await asyncio.wait_for(self.control_task, timeout=10)
                except TimeoutError:
                    self.control_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self.control_task
            await self.store.close()
            if self.secondary_store is not None:
                await self.secondary_store.close()
            if self.lock_file is not None:
                self.lock_file.close()
                self.lock_file = None

    async def _run_connected(self) -> None:
        self.ready_event.clear()
        self.client = TelegramClient(
            str(self.settings.session_path),
            self.settings.api_id,
            self.settings.api_hash,
            auto_reconnect=True,
            connection_retries=-1,
            request_retries=0,
            retry_delay=3,
            flood_sleep_threshold=0,
            raise_last_call_error=True,
            catch_up=True,
            device_model="AWS CryptoBot Userbot",
            system_version="Ubuntu",
            app_version=__version__,
        )
        self.client.add_event_handler(
            self._on_new_message,
            events.NewMessage(),
        )
        self.client.add_event_handler(
            self._on_new_message,
            events.MessageEdited(),
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telegram-сессия не авторизована. Выполните: python -m app.auth"
            )

        entity = await self.client.get_entity(f"@{TARGET_BOT_USERNAME}")
        if not isinstance(entity, types.User) or not entity.bot:
            raise RuntimeError(f"@{TARGET_BOT_USERNAME} не является Telegram-ботом")
        if not entity_has_active_username(entity, TARGET_BOT_USERNAME):
            raise RuntimeError("У Telegram-сущности нет активного username @CryptoBot")
        if entity.id != TARGET_BOT_ID:
            raise RuntimeError("Telegram вернул неожиданный ID целевого бота")

        self.target_bot = entity
        self.target_peer_id = utils.get_peer_id(entity)
        self.target_usernames = active_usernames(entity)
        me = await self.client.get_me()
        system_ignored = [
            (self.target_peer_id, "@CryptoBot"),
        ]
        if self.settings.control_bot_token:
            system_ignored.append(
                (
                    int(self.settings.control_bot_token.split(":", 1)[0]),
                    "Панель управления",
                )
            )
        await self.store.replace_system_ignored(system_ignored)
        await self._ensure_control_bot(me.id)
        for row in await self.store.results_without_notice():
            await self._notify_result(row)
        self.work_queue = asyncio.Queue(maxsize=1000)
        self.queued_parameters.clear()
        self.worker_task = asyncio.create_task(self._work_loop())
        self.ready_event.set()
        try:
            for pending in await self.store.pending_claims():
                await self._enqueue_claim(pending)
            try:
                await self._sync_dialogs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Не удалось обновить список диалогов; мониторинг продолжает работать"
                )
            await self.store.set_bool_setting("worker_connected", True)
            logger.info("Ловец запущен; получатель проверен")
            await self.client.run_until_disconnected()
            if not self.stop_event.is_set():
                raise ConnectionError("Telegram-клиент отключился")
        finally:
            self.ready_event.clear()
            if self.worker_task is not None:
                self.worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.worker_task
            self.worker_task = None
            self.work_queue = None
            self.queued_parameters.clear()

    async def _ensure_control_bot(self, account_id: int) -> None:
        if not self.settings.control_bot_token or self.control_task is not None:
            return
        admin_id = self.settings.control_admin_id or account_id
        if self.settings.secondary_db_path is not None:
            if self.settings.secondary_db_path == self.settings.db_path:
                raise ValueError("Аккаунты должны использовать разные базы данных")
            self.settings.secondary_db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.secondary_store = ProcessedStore(self.settings.secondary_db_path)
            await self.secondary_store.open()
        self.control_bot = ControlBot(self.settings, self.store, admin_id, self.secondary_store)
        self.control_task = asyncio.create_task(self.control_bot.run())

    async def _sync_dialogs(self) -> None:
        if self.client is None:
            return
        dialogs: list[tuple[int, str, str | None, str, str]] = []
        async for dialog in self.client.iter_dialogs():
            dialog_entity = dialog.entity
            seen_at = dialog.date or datetime.now(timezone.utc)
            dialogs.append(
                (
                    utils.get_peer_id(dialog_entity),
                    dialog.name or utils.get_display_name(dialog_entity),
                    getattr(dialog_entity, "username", None),
                    entity_chat_type(dialog_entity),
                    seen_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
                )
            )
        await self.store.sync_dialogs(dialogs)
        logger.info("Синхронизировано диалогов Telegram: %s", len(dialogs))

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        try:
            await self.ready_event.wait()
            if self.stop_event.is_set():
                return
            if event.chat_id == self.target_peer_id:
                await self._on_bot_message(event.message)
                return
            control_bot_id = (
                int(self.settings.control_bot_token.split(":", 1)[0])
                if self.settings.control_bot_token
                else None
            )
            if event.chat_id == control_bot_id:
                logger.debug("Пропущен системный чат панели управления")
                return

            chat = event.chat
            title = utils.get_display_name(chat) if chat is not None else str(event.chat_id)
            username = getattr(chat,'username',None) if chat is not None else None
            if event.chat_id is not None:
                if await self.store.is_chat_ignored(event.chat_id):
                    logger.debug("Исключённый диалог пропущен")
                    return
                chat = event.chat
                title = (
                    utils.get_display_name(chat)
                    if chat is not None
                    else str(event.chat_id)
                )
                username = getattr(chat, "username", None) if chat is not None else None
                try:
                    await self.store.remember_chat(
                        event.chat_id,
                        title,
                        username,
                        entity_chat_type(chat),
                    )
                except Exception:
                    logger.exception("Не удалось обновить каталог диалогов")
            if not await self.store.get_bool_setting("catcher_enabled", True):
                logger.debug("Ловец на паузе; сообщение %s пропущено", event.id)
                return

            parameters = await extract_start_parameters(
                event.message,
                self.target_usernames,
            )
            if not parameters:
                return

            logger.info(
                "Найдены подходящие ссылки: %s",
                len(parameters),
            )
            for parameter in sorted(parameters):
                claim = Claim(
                    parameter=parameter,
                    source_chat_id=event.chat_id,
                    source_message_id=event.id,
                    random_id=secrets.randbits(63),
                    source_title=title,
                    source_username=username,
                    source_type=entity_chat_type(chat),
                )
                if await self.store.claim(claim):
                    if is_receive_check(parameter):
                        await self._enqueue_claim(claim)
                    else:
                        await self.store.finish_outcome(parameter,'blocked')
                        await self.store.mark_failed(parameter,'Non-receive link blocked')
                else:
                    logger.info("Повторный параметр пропущен")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Ошибка обработки сообщения",
            )

    async def _enqueue_claim(self, claim: Claim) -> None:
        if claim.parameter in self.queued_parameters:
            return
        queue = self.work_queue
        if queue is None:
            return
        self.queued_parameters.add(claim.parameter)
        await queue.put(claim)

    async def _work_loop(self) -> None:
        queue = self.work_queue
        if queue is None:
            return
        while True:
            claim = await queue.get()
            try:
                await self._process_claim(claim)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Ошибка обработки результата; очередь продолжает работать')
                row = await self.store.finish_outcome(claim.parameter,'unconfirmed',uncertain=True)
                await self.store.mark_sent(claim.parameter)
                await self._notify_result(row)
            finally:
                self.queued_parameters.discard(claim.parameter)
                queue.task_done()

    async def _process_claim(self, claim: Claim) -> None:
        if not is_receive_check(claim.parameter):
            await self.store.finish_outcome(claim.parameter,'blocked')
            await self.store.mark_failed(claim.parameter,'Non-receive link blocked')
            return
        if (await self.store.detail(claim.parameter)).get('outcome') != 'queued':
            return
        while not await self.store.get_bool_setting('catcher_enabled',True):
            await self._wait_or_stop(1)
            if self.stop_event.is_set(): raise asyncio.CancelledError
        self.result_event.clear()
        self.active_parameter = claim.parameter
        try:
            async with self.reply_lock:
                latest = await self.client.get_messages(self.target_bot,limit=1)
                boundary = latest[0].id if latest else 0
                await self.store.begin_attempt(claim.parameter,boundary)
                result = await self._start_target_bot(claim.parameter, claim.random_id)
                request_id = None
                request_date = None
                for update in getattr(result,'updates',[]) or []:
                    message = getattr(update,'message',None)
                    if (message is not None and getattr(message,'out',False)
                        and getattr(message,'message',None) == '/start '+claim.parameter
                        and getattr(getattr(message,'peer_id',None),'user_id',None) == self.target_peer_id):
                        request_id = message.id
                        request_date = message.date
                    if isinstance(update,types.UpdateMessageID) and update.random_id == claim.random_id:
                        request_id = update.id
                if request_id is None:
                    async for message in self.client.iter_messages(self.target_bot,limit=12,min_id=boundary):
                        if message.out and message.raw_text == '/start '+claim.parameter:
                            request_id = message.id
                            request_date = message.date
                            break
                if request_id is not None:
                    if request_date is None:
                        outgoing = await self.client.get_messages(self.target_bot,ids=request_id)
                        request_date = outgoing.date if outgoing else None
                    await self.store.register_request(claim.parameter,request_id,
                        request_date.astimezone(timezone.utc).isoformat(timespec='seconds') if request_date else None)
                else:
                    await self.store.finish_outcome(claim.parameter,'unconfirmed',uncertain=True)
                await self.store.mark_sent(claim.parameter)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.store.mark_failed(claim.parameter, repr(exc))
            row = await self.store.finish_outcome(claim.parameter,'failed',uncertain=True)
            logger.exception("Запуск не выполнен")
            await self._notify_result(row)
            self.active_parameter = None
            return
        try:
            # Read recent responses as well: an update may have arrived before registration.
            async for message in self.client.iter_messages(self.target_bot,limit=15,min_id=boundary,reverse=True):
                if not message.out: await self._on_bot_message(message)
            if (await self.store.detail(claim.parameter)).get('outcome') == 'awaiting':
                try:
                    await asyncio.wait_for(self.result_event.wait(),timeout=BOT_REPLY_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    row = await self.store.finish_outcome(claim.parameter,'unconfirmed',uncertain=True)
                    await self._notify_result(row)
            elif request_id is None:
                await self._notify_result(await self.store.detail(claim.parameter))
        finally:
            self.active_parameter = None

    async def _on_bot_message(self, message) -> None:
        if getattr(message,'out',False):
            async with self.reply_lock:
                if self.active_parameter:
                    row = await self.store.detail(self.active_parameter)
                    if row.get('outcome') == 'awaiting' and message.id > (row.get('boundary_id') or 0) and message.id != row.get('request_message_id'):
                        row = await self.store.finish_outcome(self.active_parameter,'unconfirmed',uncertain=True)
                        self.result_event.set()
                        await self._notify_result(row)
            return
        # Never parse forwarded messages as financial confirmations from the bot itself.
        if getattr(message,'sender_id',None) != self.target_peer_id or getattr(message,'fwd_from',None): return
        reply = classify_bot_reply(message.raw_text)
        request_id = getattr(message,'reply_to_msg_id',None)
        explicit = request_id is not None
        if request_id is None:
            async for previous in self.client.iter_messages(self.target_bot,limit=30,max_id=message.id):
                if previous.out:
                    request_id = previous.id
                    break
        async with self.reply_lock:
            row = await self.store.record_bot_reply(message.id,reply,request_id,explicit=explicit,
                received_at=message.date.astimezone(timezone.utc).isoformat(timespec='seconds'))
            if row and row.get('parameter') == self.active_parameter:
                self.result_event.set()
            elif self.active_parameter and reply.kind != 'unknown':
                active = await self.store.detail(self.active_parameter)
                if active.get('outcome') == 'awaiting' and active.get('request_message_id') == request_id:
                    unconfirmed = await self.store.finish_outcome(self.active_parameter,'unconfirmed',uncertain=True)
                    self.result_event.set()
                    if row is None: row = unconfirmed
        if row: await self._notify_result(row)

    async def _notify_result(self, row: dict | None) -> None:
        if not row or self.control_bot is None: return
        if row['outcome'] == 'received':
            text = f"✅ Получено {row['amount']} {row['asset']}"
            if row.get('usd'): text += f" (≈ ${row['usd']} по ответу бота)"
        else:
            text = LABELS.get(row['outcome'],LABELS['unconfirmed'])
        if row.get('parameter'):
            text += '\n'+source_html(row)
        else:
            text += '\nИсточник не установлен. Возможно ручное получение или задержанный ответ.'
        key = f"result:{row.get('parameter') or row.get('response_message_id')}:{row['outcome']}"
        await self.control_bot.notify(text,key=key)

    async def _start_target_bot(self, parameter: str, random_id: int):
        if not is_receive_check(parameter):
            raise ValueError('Запуск разрешён только для CQ-чеков на получение')
        if self.client is None or self.target_bot is None:
            raise RuntimeError("Telegram-клиент не готов")

        for attempt in range(1, self.settings.send_retries + 1):
            try:
                result = await self.client(
                    functions.messages.StartBotRequest(
                        bot=self.target_bot,
                        peer=self.target_bot,
                        random_id=random_id,
                        start_param=parameter,
                    )
                )
                return result
            except RandomIdDuplicateError:
                logger.warning("Telegram подтвердил повтор уже принятого запроса")
                return
            except FloodWaitError as exc:
                wait_seconds = int(exc.seconds) + 1
                if wait_seconds > self.settings.max_flood_wait_seconds:
                    raise RuntimeError(
                        f"FloodWait {wait_seconds} сек. превышает разрешённый предел"
                    ) from exc
                logger.warning(
                    "FloodWait %s сек.; ожидание без новой заявки",
                    wait_seconds,
                )
                await self._wait_or_stop(wait_seconds)
                if self.stop_event.is_set():
                    raise asyncio.CancelledError
            except (OSError, ConnectionError, asyncio.TimeoutError, RPCError) as exc:
                if attempt >= self.settings.send_retries:
                    raise
                delay = min(
                    self.settings.retry_base_seconds * (2 ** (attempt - 1)),
                    60,
                )
                logger.warning(
                    "Временная ошибка отправки (%s/%s): %s; повтор через %s сек.",
                    attempt,
                    self.settings.send_retries,
                    type(exc).__name__,
                    delay,
                )
                await self._wait_or_stop(delay)
                if self.stop_event.is_set():
                    raise asyncio.CancelledError

        raise RuntimeError("Исчерпаны попытки запуска бота")

    async def _wait_or_stop(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def stop(self) -> None:
        logger.info("Получен сигнал остановки")
        self.stop_event.set()
        self.ready_event.set()
        if self.client is not None:
            asyncio.ensure_future(self.client.disconnect())
        if self.control_bot is not None:
            self.control_bot.stop()


async def async_main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_file)
    userbot = Userbot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, userbot.stop)
    await userbot.start()


if __name__ == "__main__":
    asyncio.run(async_main())
