from __future__ import annotations

import asyncio
import logging
import time
import html
from datetime import datetime, timezone, timedelta
from contextlib import suppress

from telethon import Button, TelegramClient, events, functions
from telethon.errors import MessageNotModifiedError, RandomIdDuplicateError, FloodWaitError
from telethon.extensions import html as telegram_html

from app import __version__
from app.config import Settings
from app.database import ProcessedStore
from app.receipts import LABELS, source_html


logger = logging.getLogger("control_bot")


class ControlBot:
    def __init__(
        self,
        settings: Settings,
        store: ProcessedStore,
        admin_id: int,
        secondary_store: ProcessedStore | None = None,
    ) -> None:
        if not settings.control_bot_token:
            raise ValueError("Панель управления не настроена")
        self.settings = settings
        self.store = store
        self.secondary_store = secondary_store
        self.admin_id = admin_id
        self.token = settings.control_bot_token
        self.bot_id = int(self.token.split(":", 1)[0])
        self.client: TelegramClient | None = None
        self.stop_event = asyncio.Event()

    def _stores(self) -> list[tuple[str, ProcessedStore]]:
        secondary = getattr(self, "secondary_store", None)
        return [("Аккаунт 1", self.store), ("Аккаунт 2", secondary)] if secondary is not None else [("", self.store)]

    async def _set_enabled(self, enabled: bool) -> None:
        for _, store in self._stores():
            await store.set_bool_setting("catcher_enabled", enabled)

    async def run(self) -> None:
        delay = 3
        while not self.stop_event.is_set():
            connected_at = time.monotonic()
            try:
                await self._run_once()
                delay = 3
            except asyncio.CancelledError:
                raise
            except Exception:
                if time.monotonic() - connected_at >= 30:
                    delay = 3
                logger.exception(
                    "Панель управления отключилась; повтор через %s сек.",
                    delay,
                )
                await self._wait_or_stop(delay)
                delay = min(delay * 2, 60)
            finally:
                if self.client is not None:
                    with suppress(Exception):
                        await self.client.disconnect()
                    self.client = None

    async def _run_once(self) -> None:
        self.client = TelegramClient(
            f"{self.settings.control_session_path}_{self.bot_id}",
            self.settings.api_id,
            self.settings.api_hash,
            auto_reconnect=True,
            connection_retries=-1,
            request_retries=5,
            retry_delay=3,
            catch_up=True,
        )
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._on_callback, events.CallbackQuery())
        await self.client.start(bot_token=self.token)
        me = await self.client.get_me()
        if me.id != self.bot_id or not me.bot:
            raise RuntimeError("Сессия панели принадлежит другому Telegram-боту")
        logger.info("Панель управления запущена")
        notifier = asyncio.create_task(self._notice_loop())
        try:
            await self.client.run_until_disconnected()
        finally:
            notifier.cancel()
            with suppress(asyncio.CancelledError): await notifier
        if not self.stop_event.is_set():
            raise ConnectionError("Клиент панели управления отключился")

    def _authorized(
        self,
        sender_id: int | None,
        chat_id: int | None,
        is_private: bool,
    ) -> bool:
        return (
            is_private
            and sender_id == self.admin_id
            and chat_id == self.admin_id
        )

    @staticmethod
    def _menu() -> list[list[Button]]:
        return [
            [
                Button.inline("📊 Статус", b"status"),
                Button.inline("📈 Статистика", b"stats"),
            ],
            [
                Button.inline("⏸ Пауза", b"pause"),
                Button.inline("▶️ Продолжить", b"resume"),
            ],
            [
                Button.inline("🕘 Последние", b"recent"),
                Button.inline("🔔 Уведомления", b"notifications"),
            ],
            [Button.inline("🗂 Прослушивание и исключения", b"view:all:0")],
        ]

    @staticmethod
    def _chat_buttons(
        chats: list[tuple[int, str, str | None, str, bool, bool]],
        page: int,
        total_pages: int,
        view: str,
    ) -> list[list[Button]]:
        type_icons = {
            "user": "👤",
            "bot": "🤖",
            "group": "👥",
            "channel": "📢",
        }
        rows: list[list[Button]] = []
        for chat_id, title, username, chat_type, ignored, locked in chats:
            kind = type_icons.get(chat_type, "💬")
            state = "🔒" if locked else "🚫" if ignored else "✅"
            label = f"{state} {kind} {' '.join(title.split())}"
            if username:
                label += f" (@{username})"
            rows.append(
                [
                    Button.inline(
                        label[:50],
                        f"toggle_chat:{chat_id}:{view}:{page}".encode(),
                    )
                ]
            )
        navigation: list[Button] = []
        if page > 0:
            navigation.append(
                Button.inline("⬅️", f"view:{view}:{page - 1}".encode())
            )
        navigation.append(
            Button.inline(
                f"{page + 1}/{max(total_pages, 1)}",
                f"view:{view}:{page}".encode(),
            )
        )
        if page + 1 < total_pages:
            navigation.append(
                Button.inline("➡️", f"view:{view}:{page + 1}".encode())
            )
        rows.append(navigation)
        rows.append(
            [
                Button.inline("Все", b"view:all:0"),
                Button.inline("👤", b"view:user:0"),
                Button.inline("🤖", b"view:bot:0"),
                Button.inline("👥", b"view:group:0"),
                Button.inline("📢", b"view:channel:0"),
                Button.inline("🚫", b"view:ignored:0"),
            ]
        )
        rows.append([Button.inline("↩️ Главное меню", b"status")])
        return rows

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        if not self._authorized(event.sender_id, event.chat_id, event.is_private):
            logger.warning("Отклонена попытка доступа к панели")
            return
        try:
            await self._handle_message(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка команды панели")
            with suppress(Exception):
                await event.respond("Не удалось выполнить команду. Попробуйте ещё раз.")

    async def _handle_message(self, event: events.NewMessage.Event) -> None:
        parts = (event.raw_text or "").strip().split(maxsplit=2)
        command = parts[0].split("@", 1)[0].lower() if parts else ""
        if command in {"/start", "/menu", "/help"}:
            await event.respond(
                "Панель управления ловцом @CryptoBot.\n"
                "Доступ разрешён только владельцу.",
                buttons=self._menu(),
            )
        elif command == "/status":
            await event.respond(await self.status_text(), buttons=self._menu())
        elif command == "/stats":
            await event.respond(await self.stats_text(), buttons=self._menu())
        elif command in {"/recent", "/history"}:
            await self._send_history_page(event,0,edit=False)
        elif command == "/pause":
            await self._set_enabled(False)
            await event.respond("⏸ Ловец поставлен на паузу.", buttons=self._menu())
        elif command == "/resume":
            await self._set_enabled(True)
            await event.respond("▶️ Ловец снова активен.", buttons=self._menu())
        elif command in {"/ignored", "/chats"}:
            view = "ignored" if command == "/ignored" else "all"
            await self._send_dialog_page(event, 0, view=view, edit=False)
        elif command in {"/ignore", "/unignore"}:
            if len(parts) < 2:
                await event.respond(
                    f"Формат: {command} CHAT_ID",
                    buttons=self._menu(),
                )
                return
            try:
                chat_id = int(parts[1])
            except ValueError:
                await event.respond("CHAT_ID должен быть числом.", buttons=self._menu())
                return
            ignored = command == "/ignore"
            changed = await self.store.set_chat_ignored(
                chat_id,
                ignored,
                parts[2] if len(parts) > 2 else None,
            )
            if not changed:
                await event.respond(
                    "🔒 Системное исключение уже заблокировано и не изменяется.",
                    buttons=self._menu(),
                )
                return
            await event.respond(
                f"{'🚫 Чат игнорируется' if ignored else '✅ Чат снова слушается'}: "
                f"{chat_id}",
                buttons=self._menu(),
            )
        else:
            await event.respond("Неизвестная команда. Нажмите /menu.", buttons=self._menu())

    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        if not self._authorized(event.sender_id, event.chat_id, event.is_private):
            await event.answer("Нет доступа", alert=True)
            return
        try:
            await self._handle_callback(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка кнопки панели")
            with suppress(Exception):
                await event.answer("Ошибка. Попробуйте ещё раз.", alert=True)

    async def _handle_callback(self, event: events.CallbackQuery.Event) -> None:
        action = event.data.decode("utf-8", errors="ignore")
        if action == "pause":
            await self._set_enabled(False)
            text = "⏸ Ловец поставлен на паузу."
        elif action == "resume":
            await self._set_enabled(True)
            text = "▶️ Ловец снова активен."
        elif action == "status":
            text = await self.status_text()
        elif action == "stats":
            text = await self.stats_text()
        elif action == "recent" or action.startswith('history:'):
            try: page = int(action.split(':')[1]) if ':' in action else 0
            except ValueError: page = 0
            await event.answer()
            await self._send_history_page(event,page,edit=True)
            return
        elif action.startswith('history2:'):
            try: page = int(action.split(':')[1])
            except ValueError: page = 0
            await event.answer()
            await self._send_history_page(event,page,edit=True,second=True)
            return
        elif action in {'unmatched','unmatched2'}:
            second = action == 'unmatched2'
            if second and getattr(self, 'secondary_store', None) is None:
                await event.answer('Второй аккаунт не подключён',alert=True)
                return
            await event.answer()
            await event.edit(await self.unmatched_text(second=second),buttons=[[Button.inline('↩️ История',b'history2:0' if second else b'recent')]])
            return
        elif action == "notifications":
            enabled = await self.store.toggle_bool_setting(
                "notifications_enabled",
                True,
            )
            text = f"🔔 Уведомления: {'включены' if enabled else 'выключены'}."
        elif (
            action == "ignored_chats"
            or action.startswith("dialogs:")
            or action.startswith("view:")
        ):
            try:
                if action.startswith("view:"):
                    _, view, page_raw = action.split(":", 2)
                    page = int(page_raw)
                elif action.startswith("dialogs:"):
                    view = "all"
                    page = int(action.split(":", 1)[1])
                else:
                    view, page = "all", 0
            except ValueError:
                await event.answer("Некорректная страница", alert=True)
                return
            await event.answer()
            await self._send_dialog_page(event, page, view=view, edit=True)
            return
        elif action.startswith("toggle_chat:"):
            try:
                _, chat_id_raw, view, page_raw = action.split(":", 3)
                chat_id = int(chat_id_raw)
                page = int(page_raw)
            except ValueError:
                await event.answer("Некорректный chat_id", alert=True)
                return
            ignored = await self.store.toggle_chat_ignored(chat_id)
            if ignored is None:
                await event.answer(
                    "Системное исключение нельзя включить",
                    alert=True,
                )
                return
            await event.answer("Чат отключён" if ignored else "Чат включён")
            await self._send_dialog_page(event, page, view=view, edit=True)
            return
        else:
            await event.answer("Неизвестное действие", alert=True)
            return
        await event.answer()
        with suppress(Exception):
            await event.edit(text, buttons=self._menu())

    async def status_text(self) -> str:
        enabled = await self.store.get_bool_setting("catcher_enabled", True)
        connected = await self.store.get_bool_setting("worker_connected", False)
        notifications = await self.store.get_bool_setting(
            "notifications_enabled",
            True,
        )
        if not enabled:
            worker_status = "⏸ на паузе"
        elif connected:
            worker_status = "🟢 активен"
        else:
            worker_status = "🟠 переподключается"
        correlation = await self.store.get_bool_setting('correlation_safe',True)
        text = (
            f"Ловец: {worker_status}\n"
            f"Уведомления: {'🔔 включены' if notifications else '🔕 выключены'}\n"
            f"Получатель: только @CryptoBot\nВерсия: {__version__}"
            + ('\nЕсть неоднозначные ответы: поступления без надёжной связи учитываются отдельно.' if not correlation else '')
        )
        if getattr(self, 'secondary_store', None) is not None:
            second_enabled = await self.secondary_store.get_bool_setting('catcher_enabled',True)
            second_connected = await self.secondary_store.get_bool_setting('worker_connected',False)
            state = '⏸ на паузе' if not second_enabled else '🟢 активен' if second_connected else '🟠 не подключён'
            text = 'Аккаунт 1\n' + text + '\n\nАккаунт 2: ' + state
        return text

    async def stats_text(self) -> str:
        sections = []
        for label,store in self._stores():
            sections.append((label+'\n' if label else '') + await self._stats_for_store(store))
        return '\n\n'.join(sections)

    async def _stats_for_store(self, store: ProcessedStore) -> str:
        now = datetime.now(timezone(timedelta(hours=5)))
        midnight = now.replace(hour=0,minute=0,second=0,microsecond=0)
        lines = ['Подтверждённые получения чекера']
        for label,since in [('Сегодня',midnight),('Последние 7 дней',midnight-timedelta(days=6)),('Всё время',None)]:
            income = await store.income(since.astimezone(timezone.utc).isoformat(timespec='seconds') if since else None)
            amounts = ', '.join(f'{amount} {asset}' for asset,amount in income['totals'].items()) or '0'
            lines.append(f'{label}: {income["count"]} чеков · {amounts}')
        income = await store.income()
        if income['unmatched_count']:
            amounts = ', '.join(f'{amount} {asset}' for asset,amount in income['unmatched'].items())
            lines += ['',f'Отдельно, источник не установлен: {income["unmatched_count"]} поступлений · {amounts}', 'Это могут быть ручные получения или задержанные ответы.']
        counts = await store.outcome_counts()
        lines += ['',f'Обязательная подписка: {counts.get("subscription",0)}',f'Пароль: {counts.get("password",0)} · Капча: {counts.get("captcha",0)}',f'Заблокированные ссылки/счета: {counts.get("blocked",0)+counts.get("payment",0)}',f'Без подтверждения: {counts.get("legacy",0)+counts.get("unconfirmed",0)}',f'В очереди/ожидании: {counts.get("queued",0)+counts.get("awaiting",0)}','Дни считаются по UTC+5. Учёт поступлений — с версии 1.1.0.']
        return '\n'.join(lines)

    async def recent_text(self) -> str:
        rows,_ = await self.store.history_page(0)
        if not rows:
            return "История пока пуста."
        return self._history_text(rows)

    @staticmethod
    def _history_text(rows: list[dict]) -> str:
        lines = ['История чеков']
        for row in rows:
            label = LABELS.get(row['outcome'],LABELS['unconfirmed'])
            if row['outcome'] == 'received': label += f" {row['amount']} {row['asset']}"
            timestamp = datetime.fromisoformat(row['claimed_at']).astimezone(timezone(timedelta(hours=5))).strftime('%d.%m %H:%M')
            lines += ['',f'{html.escape(label)} · {timestamp}',source_html(row)]
        return '\n'.join(lines)

    async def _send_history_page(self,event,page: int,*,edit: bool,second: bool=False) -> None:
        secondary = getattr(self, 'secondary_store', None)
        if second and secondary is None:
            await event.answer('Второй аккаунт не подключён',alert=True)
            return
        store = secondary if second else self.store
        prefix = 'history2' if second else 'history'
        rows,count = await store.history_page(max(0,page))
        pages = max(1,(count+4)//5)
        page = max(0,min(page,pages-1))
        if not rows and count: rows,_ = await store.history_page(page)
        text = self._history_text(rows) if rows else 'История пока пуста.'
        if secondary is not None: text = ('Аккаунт 2\n' if second else 'Аккаунт 1\n') + text
        navigation = []
        if page: navigation.append(Button.inline('⬅️',f'{prefix}:{page-1}'.encode()))
        navigation.append(Button.inline(f'{page+1}/{pages}',f'{prefix}:{page}'.encode()))
        if page+1<pages: navigation.append(Button.inline('➡️',f'{prefix}:{page+1}'.encode()))
        buttons = [navigation,[Button.inline('💰 Без установленного источника',b'unmatched2' if second else b'unmatched')]]
        if secondary is not None: buttons.append([Button.inline('Аккаунт 1',b'history:0'),Button.inline('Аккаунт 2',b'history2:0')])
        buttons.append([Button.inline('↩️ Главное меню',b'status')])
        if edit:
            with suppress(MessageNotModifiedError): await event.edit(text,buttons=buttons,parse_mode='html',link_preview=False)
        else:
            await event.respond(text,buttons=buttons,parse_mode='html',link_preview=False)

    async def unmatched_text(self,second: bool=False) -> str:
        store = self.secondary_store if second else self.store
        cursor = await store._connection().execute("SELECT amount,asset,received_at,message_id FROM bot_receipts WHERE kind='received' AND parameter IS NULL ORDER BY received_at DESC LIMIT 15")
        rows = await cursor.fetchall()
        lines = ['Поступления без установленного источника','Не включены в доход чекера с известным источником.']
        if getattr(self, 'secondary_store', None) is not None:
            lines.insert(0,'Аккаунт 2' if second else 'Аккаунт 1')
        for amount,asset,timestamp,message_id in rows:
            date = datetime.fromisoformat(timestamp).astimezone(timezone(timedelta(hours=5))).strftime('%d.%m %H:%M')
            lines.append(f'{date}: {amount} {asset} · сообщение CryptoBot #{message_id}')
        if not rows: lines.append('Пока нет.')
        return '\n'.join(lines)

    async def ignored_text(self) -> str:
        ignored = await self.store.ignored_chats()
        lines = [
            "Прослушивание Telegram",
            "🔒 системное исключение · 🚫 выключено · ✅ слушается",
        ]
        if ignored:
            shown = ignored[:15]
            lines.extend(
                f"{'🔒' if locked else '🚫'} {' '.join(title.split())} — {chat_id}"
                for chat_id, title, locked in shown
            )
            if len(ignored) > len(shown):
                lines.append(f"…и ещё {len(ignored) - len(shown)}")
        else:
            lines.append("Исключений пока нет.")
        lines.extend(
            [
                "",
                "Ниже все диалоги аккаунта. Нажмите, чтобы переключить.",
                "Команды: /ignore CHAT_ID и /unignore CHAT_ID",
            ]
        )
        return "\n".join(lines)

    async def _send_dialog_page(
        self,
        event: events.NewMessage.Event | events.CallbackQuery.Event,
        page: int,
        *,
        view: str,
        edit: bool,
    ) -> None:
        allowed_views = {"all", "user", "bot", "group", "channel", "ignored"}
        if view not in allowed_views:
            view = "all"
        page_size = 8
        total = await self.store.chat_count(view)
        total_pages = max(1, (total + page_size - 1) // page_size)
        safe_page = max(0, min(page, total_pages - 1))
        chats = await self.store.chats_page(safe_page, page_size, view)
        view_names = {
            "all": "все",
            "user": "люди",
            "bot": "боты",
            "group": "группы",
            "channel": "каналы",
            "ignored": "исключения",
        }
        text = (
            f"{await self.ignored_text()}\n\n"
            f"Фильтр: {view_names[view]}. Диалогов: {total}. "
            f"Страница {safe_page + 1}/{total_pages}.\n"
            "👤 люди · 🤖 боты · 👥 группы · 📢 каналы"
        )
        buttons = self._chat_buttons(chats, safe_page, total_pages, view)
        if edit:
            with suppress(MessageNotModifiedError):
                await event.edit(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)

    async def notify(self, text: str, *, key: str) -> None:
        if not await self.store.get_bool_setting("notifications_enabled", True):
            return
        await self.store.queue_notice(key,text)

    async def _notice_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                if await self.store.get_bool_setting('notifications_enabled',True):
                    for label,store in self._stores():
                        for key,body,random_id in await store.pending_notices():
                            text,entities = telegram_html.parse((label+'\n' if label else '')+body)
                            peer = await self.client.get_input_entity(self.admin_id)
                            try:
                                await self.client(functions.messages.SendMessageRequest(peer=peer,message=text,entities=entities,random_id=random_id,no_webpage=True))
                            except RandomIdDuplicateError:
                                pass
                            await store.notice_sent(key)
            except asyncio.CancelledError: raise
            except FloodWaitError as exc:
                logger.warning('Telegram отложил доставку уведомления на %s сек.',exc.seconds)
                await self._wait_or_stop(int(exc.seconds)+1)
            except Exception:
                logger.warning('Доставка уведомления отложена')
                await self._wait_or_stop(10)
            await self._wait_or_stop(1)

    async def _wait_or_stop(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def stop(self) -> None:
        self.stop_event.set()
        if self.client is not None:
            asyncio.ensure_future(self.client.disconnect())
