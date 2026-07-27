# CryptoBot Link Catcher

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Telethon](https://img.shields.io/badge/Telethon-1.44-26A5E4?logo=telegram&logoColor=white)
![Ubuntu](https://img.shields.io/badge/Ubuntu-22.04%20%7C%2024.04-E95420?logo=ubuntu&logoColor=white)
[![Tests](https://github.com/moukpu/cryptobot-link-catcher/actions/workflows/tests.yml/badge.svg)](https://github.com/moukpu/cryptobot-link-catcher/actions/workflows/tests.yml)
[![Release](https://img.shields.io/github/v/release/moukpu/cryptobot-link-catcher?display_name=tag)](https://github.com/moukpu/cryptobot-link-catcher/releases/latest)

Надёжный Telegram-юзербот для поиска и обработки deep link на
`@CryptoBot`: все диалоги, inline-кнопки, SQLite-очередь, закрытая
панель управления и автозапуск через systemd.

Проект от Moukpu. Я собрал его для простой задачи: держать Telegram-клиент на
сервере, замечать новые ссылки запуска `@CryptoBot` и обрабатывать их без дублей.
Настроил один раз — дальше сервис живёт сам.

Версия: **1.0.0**

## Что умеет

- Слушает весь Telegram авторизованного аккаунта: лички, ботов, группы,
  супергруппы и каналы.
- Читает замьюченные и архивные диалоги. Mute влияет на уведомления приложения,
  а не на Telegram updates.
- Обрабатывает входящие, исходящие и отредактированные сообщения.
- Достаёт ссылки из обычного текста, скрытой гиперссылки и inline-кнопки.
- Понимает `http`, `https`, `tg://resolve`, `t.me`, `telegram.me`,
  `telegram.dog` и username-поддомены.
- Принимает активные алиасы только той Telegram-сущности, которая подтверждена
  как `@CryptoBot`. Целевой ID дополнительно зафиксирован.
- Никогда не запускает постороннего бота и не открывает внешние сайты.
- Сохраняет каждый параметр и его `random_id` в SQLite до отправки.
- После обрыва, рестарта или потерянного ответа продолжает незавершённую
  операцию с тем же ключом идемпотентности.
- Переподключается после потери сети, учитывает FloodWait и догружает доступные
  Telegram updates после простоя.
- Автоматически стартует вместе с Ubuntu через systemd.

Ловец не сканирует старую историю целиком. Он работает с новыми Telegram
updates и теми updates, которые Telegram отдаёт через catch-up после простоя.

## Панель управления

Панель создаётся отдельным ботом через `@BotFather`. Она доступна только
владельцу и только в личном чате.

В панели есть:

- реальное состояние клиента: активен, пауза или переподключение;
- статистика и последние срабатывания с маскированными параметрами;
- включение и выключение уведомлений;
- полный список диалогов с пагинацией;
- фильтры: все, люди, боты, группы, каналы и исключения;
- пользовательские исключения через кнопки или команды;
- `/ignore CHAT_ID`, `/unignore CHAT_ID`, `/ignored`, `/chats`.

`@CryptoBot` и сама панель всегда показываются как `🔒`. Это системные
исключения: их нельзя включить кнопкой или командой. Все остальные диалоги по
умолчанию имеют состояние `✅` и прослушиваются, включая mute и архив.

## Требования

- Ubuntu 22.04 или 24.04;
- Python 3.10+;
- сервер с постоянным исходящим интернетом;
- `API_ID` и `API_HASH` с <https://my.telegram.org/apps>;
- отдельный BotFather-бот, если нужна панель управления.

Входящие порты приложению не нужны. Для SSH лучше разрешить `22/tcp` только со
своего IP.

## Быстрая установка

Распакуйте архив на сервере и выполните:

```bash
cd cryptobot-link-catcher
sudo bash scripts/install_ubuntu.sh
sudo nano /opt/cryptobot-userbot/.env
```

Минимальная конфигурация:

```env
API_ID=PASTE_API_ID_HERE
API_HASH=PASTE_API_HASH_HERE
PHONE_NUMBER=+000000000000

SESSION_PATH=session/telegram_user
DB_PATH=data/processed.sqlite3
LOG_FILE=data/userbot.log
LOG_LEVEL=INFO

SEND_RETRIES=5
RETRY_BASE_SECONDS=3
MAX_FLOOD_WAIT_SECONDS=3600

CONTROL_BOT_TOKEN=PASTE_TOKEN_HERE
CONTROL_ADMIN_ID=
CONTROL_SESSION_PATH=session/control_bot
```

`CONTROL_ADMIN_ID` можно оставить пустым. Тогда панель автоматически разрешит
доступ аккаунту, который авторизован в пользовательской сессии.

Файл сохраняется через `Ctrl+O`, `Enter`, `Ctrl+X`.

## Первая авторизация

```bash
cd /opt/cryptobot-userbot
sudo -u telegram-userbot .venv/bin/python -m app.auth
```

Введите одноразовый код Telegram и облачный пароль 2FA, если он включён. После
успешной авторизации номер больше не нужен:

```bash
sudo nano /opt/cryptobot-userbot/.env
```

Поле можно оставить пустым:

```env
PHONE_NUMBER=
```

Теперь запускаем:

```bash
sudo systemctl enable --now cryptobot-userbot
sudo systemctl status cryptobot-userbot
```

Откройте управляющего бота, нажмите Start и отправьте `/menu`.

## Проверка

Полная диагностика без вывода секретов:

```bash
cd /opt/cryptobot-userbot
sudo bash scripts/healthcheck_ubuntu.sh
```

Ожидаемый результат:

```text
SERVICE=active
DATABASE=ok
PERMISSIONS=ok
HEALTHCHECK=passed
```

Логи:

```bash
sudo journalctl -u cryptobot-userbot -f
tail -f /opt/cryptobot-userbot/data/userbot.log
```

INFO-логи не выводят номер, username, ID аккаунта, BotFather-токен, полные
start-параметры и ID диалогов.

## Обновление

Запустите установщик из новой распакованной папки:

```bash
sudo bash scripts/install_ubuntu.sh
```

`.env`, Telegram-сессии, SQLite и логи сохраняются. Если обновление завершится
ошибкой, установщик попытается вернуть ранее работавшую службу.

## Резервная копия

Приватные данные находятся только здесь:

```text
/opt/cryptobot-userbot/.env
/opt/cryptobot-userbot/session/
/opt/cryptobot-userbot/data/
```

Копировать их нужно только в зашифрованное хранилище. Session-файл фактически
даёт доступ к Telegram-аккаунту.

## Удаление

Отключить сервис, но сохранить конфигурацию и сессии:

```bash
sudo bash scripts/uninstall_ubuntu.sh
```

Полностью стереть сервис вместе с приватными данными:

```bash
sudo bash scripts/uninstall_ubuntu.sh --purge-data
```

Второй вариант необратим.

## Что переживает автоматически

- кратковременную и длительную потерю интернета;
- разрыв TCP и переподключение Telegram;
- перезапуск процесса, systemd, Ubuntu или EC2;
- доступные offline-updates после простоя;
- повтор одной ссылки одновременно в нескольких местах;
- падение после сохранения ссылки и до отправки;
- потерю ответа после принятого Telegram-запроса;
- FloodWait в настроенном диапазоне;
- временные RPC, timeout и сетевые ошибки;
- повреждённую ссылку, неизвестный формат URL и чужого бота;
- ошибку чтения entities или inline-кнопок одного сообщения;
- ошибочную команду, callback или попытку доступа к панели;
- замену BotFather-бота: сессии разделяются по его ID;
- большой список диалогов и исключений без превышения лимита сообщения;
- повторный запуск второй копии через штатный systemd.

Требуют вмешательства владельца: отозванная Telegram-сессия, бан аккаунта,
неверные API-данные или BotFather-токен, заполненный диск, повреждённая SQLite,
недоступность Telegram/AWS и изменение правил стороннего сервиса. Тут никакой
код не должен делать вид, что всё нормально: healthcheck завершится ошибкой, а
systemd сохранит процесс в контролируемом цикле перезапуска.

## Приватность релиза

В исходном ZIP нет `.env`, session-файлов, SQLite, логов, SSH-ключей, API-данных,
BotFather-токена, номера телефона, ID или username аккаунта. Release собирается
по белому списку файлов, затем автоматически проверяется на типовые секреты.

Локальная проверка проекта:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Автоматизация пользовательского аккаунта может подпадать под правила Telegram
и стороннего сервиса. Используйте проект только со своим аккаунтом и учитывайте
актуальные ограничения.
