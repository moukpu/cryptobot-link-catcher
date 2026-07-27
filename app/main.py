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


logger = logging.getLogger("cryptobot_userbot")


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
        self.control_bot = ControlBot(self.settings, self.store, admin_id)
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
                logger.debug(
                    "Пропущено сообщение %s из чата @%s",
                    event.id,
                    TARGET_BOT_USERNAME,
                )
                return
            control_bot_id = (
                int(self.settings.control_bot_token.split(":", 1)[0])
                if self.settings.control_bot_token
                else None
            )
            if event.chat_id == control_bot_id:
                logger.debug("Пропущен системный чат панели управления")
                return

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
                )
                if await self.store.claim(claim):
                    await self._enqueue_claim(claim)
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
            finally:
                self.queued_parameters.discard(claim.parameter)
                queue.task_done()

    async def _process_claim(self, claim: Claim) -> None:
        try:
            await self._start_target_bot(claim.parameter, claim.random_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.store.mark_failed(claim.parameter, repr(exc))
            logger.exception("Запуск не выполнен")
            if self.control_bot is not None:
                await self.control_bot.notify("❌ Не удалось запустить @CryptoBot")
            return

        await self.store.mark_sent(claim.parameter)
        logger.info("Успешно запущен только @%s", TARGET_BOT_USERNAME)
        if self.control_bot is not None:
            await self.control_bot.notify("✅ @CryptoBot успешно запущен")

    async def _start_target_bot(self, parameter: str, random_id: int) -> None:
        if self.client is None or self.target_bot is None:
            raise RuntimeError("Telegram-клиент не готов")

        for attempt in range(1, self.settings.send_retries + 1):
            try:
                await self.client(
                    functions.messages.StartBotRequest(
                        bot=self.target_bot,
                        peer=self.target_bot,
                        random_id=random_id,
                        start_param=parameter,
                    )
                )
                return
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
