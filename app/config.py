from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.getenv("CRYPTOBOT_ENV_PATH") or PROJECT_ROOT / ".env").expanduser().resolve()
TARGET_BOT_USERNAME = "CryptoBot"
TARGET_BOT_ID = 1559501630


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"В .env не задано обязательное значение {name}")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} должно быть целым числом") from exc
    if value < 1:
        raise ValueError(f"{name} должно быть больше нуля")
    return value


def _project_path(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default).strip()).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    phone_number: str | None
    session_path: Path
    db_path: Path
    log_file: Path | None
    log_level: str
    send_retries: int
    retry_base_seconds: int
    max_flood_wait_seconds: int
    control_bot_token: str | None
    control_admin_id: int | None
    control_session_path: Path
    secondary_db_path: Path | None = None


def load_settings(*, require_phone: bool = False) -> Settings:
    load_dotenv(ENV_PATH, override=True)
    api_id_raw = _required("API_ID")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise ValueError("API_ID должен быть целым числом") from exc

    log_file_raw = os.getenv("LOG_FILE", "data/userbot.log").strip()
    log_file = _project_path("LOG_FILE", "data/userbot.log") if log_file_raw else None

    phone_number = os.getenv("PHONE_NUMBER", "").strip() or None
    if require_phone and phone_number is None:
        raise ValueError("В .env не задано обязательное значение PHONE_NUMBER")
    if phone_number is not None and not phone_number.startswith("+"):
        raise ValueError("PHONE_NUMBER должен быть в международном формате с +")

    control_bot_token = os.getenv("CONTROL_BOT_TOKEN", "").strip() or None
    if control_bot_token:
        token_parts = control_bot_token.split(":", 1)
        if len(token_parts) != 2 or not token_parts[0].isdigit() or not token_parts[1]:
            raise ValueError("CONTROL_BOT_TOKEN имеет неверный формат")
    control_admin_raw = os.getenv("CONTROL_ADMIN_ID", "").strip()
    control_admin_id: int | None = None
    if control_admin_raw:
        try:
            control_admin_id = int(control_admin_raw)
        except ValueError as exc:
            raise ValueError("CONTROL_ADMIN_ID должен быть целым числом") from exc
        if control_admin_id <= 0:
            raise ValueError("CONTROL_ADMIN_ID должен быть больше нуля")
    settings …4042 tokens truncated…        )
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
