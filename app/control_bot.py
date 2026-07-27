from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError

from app import __version__
from app.config import Settings
from app.database import ProcessedStore


logger = logging.getLogger("control_bot")


class ControlBot:
    def __init__(
        self,
        settings: Settings,
        store: ProcessedStore,
        admin_id: int,
    ) -> None:
        if not settings.control_bot_token:
            raise ValueError("Панель управления не настроена")
        self.settings = settings
        self.store = store
        self.admin_id = admin_id
        self.token = settings.control_bot_token
        self.bot_id = int(self.token.split(":", 1)[0])
        self.client: TelegramClient | None = None
        self.stop_event = asyncio.Event()

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
        await self.client.run_until_disconnected()
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
        elif command == "/recent":
            await event.respond(await self.recent_text(), buttons=self._menu())
        elif command == "/pause":
            await self.store.set_bool_setting("catcher_enabled", False)
            await event.respond("⏸ Ловец поставлен на паузу.", buttons=self._menu())
        elif command == "/resume":
            await self.store.set_bool_setting("catcher_enabled", True)
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
            await self.store.set_bool_setting("catcher_enabled", False)
            text = "⏸ Ловец поставлен на паузу."
        elif action == "resume":
            await self.store.set_bool_setting("catcher_enabled", True)
            text = "▶️ Ловец снова активен."
        elif action == "status":
            text = await self.status_text()
        elif action == "stats":
            text = await self.stats_text()
        elif action == "recent":
            text = await self.recent_text()
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
        return (
            f"Ловец: {worker_status}\n"
            f"Уведомления: {'🔔 включены' if notifications else '🔕 выключены'}\n"
            f"Получатель: только @CryptoBot\nВерсия: {__version__}"
        )

    async def stats_text(self) -> str:
        stats = await self.store.statistics()
        total = sum(stats.values())
        return (
            f"Всего уникальных параметров: {total}\n"
            f"✅ Успешно: {stats['sent']}\n"
            f"❌ Ошибки: {stats['failed']}\n"
            f"⏳ В обработке: {stats['processing']}"
        )

    async def recent_text(self) -> str:
        rows = await self.store.recent(10)
        if not rows:
            return "История пока пуста."
        icons = {"sent": "✅", "failed": "❌", "processing": "⏳"}
        lines = ["Последние срабатывания:"]
        for parameter, status, timestamp in rows:
            masked = (
                parameter
                if len(parameter) <= 8
                else f"{parameter[:4]}…{parameter[-4:]}"
            )
            lines.append(f"{icons.get(status, '•')} {masked} — {timestamp}")
        return "\n".join(lines)

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

    async def notify(self, text: str) -> None:
        if not await self.store.get_bool_setting("notifications_enabled", True):
            return
        if self.client is None or not self.client.is_connected():
            return
        try:
            await self.client.send_message(self.admin_id, text)
        except Exception:
            logger.exception("Не удалось отправить уведомление владельцу")

    async def _wait_or_stop(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def stop(self) -> None:
        self.stop_event.set()
        if self.client is not None:
            asyncio.ensure_future(self.client.disconnect())
