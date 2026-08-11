# Lorenzo — Bothost-ready Telegram + Telethon + Web Admin

Сборка состоит из трёх компонентов, запускаемых одним `main.py`:

- **Aiogram Bot** — онбординг, уведомления и закрытая Telegram-админка.
- **Telethon Sync** — состав групп, события вступления/выхода, Telegram presence и статистика сообщений.
- **FastAPI Web Admin** — чаты, участники, активность, администраторы, уведомления, настройки и мастер подключения Telethon.


## ВАЖНО: как загружать ZIP на Bothost

В этом релизе файлы в ZIP лежат **сразу в корне архива**. После распаковки в `/app` должны быть:

```text
/app/main.py
/app/requirements.txt
/app/web_app.py
/app/templates/
/app/static/
```

Не должно быть лишнего уровня вида `/app/LorenzoNew_bothost/main.py`. Если в настройках Bothost есть поле **«Главный файл (точка входа)»**, укажите `main.py` и выполните **новый деплой**, а не только рестарт.

`main.py` теперь также экспортирует FastAPI-объект `app`. Поэтому сборка работает и при прямом запуске `python main.py`, и если платформа использует ASGI-режим `main:app`. Telegram polling и Telethon worker запускаются вместе с FastAPI.

Для диагностики откройте `/health`. В рабочем состоянии поле `runtime.bot` должно стать `running`. Если там `error`, рядом будет безопасный текст ошибки без токена.

## Что сделано специально для Bothost

- `main.py` является единой точкой входа, экспортирует FastAPI `app` и запускает Bot API + Web Admin + Telethon worker.
- Web Admin слушает `0.0.0.0` и автоматически использует переменную `PORT`, которую передаёт Bothost.
- Если Bothost передал `DOMAIN`, публичный URL web-панели определяется автоматически как `https://<DOMAIN>`; вручную задавать `WEB_PUBLIC_URL` обычно не нужно.
- Рабочие данные хранятся в `<project>/data`. На Bothost проект размещается в `/app`, поэтому это автоматически становится `/app/data` — persistent storage хостинга.
- В persistent storage находятся база, настройки, Telethon-конфиг, web-secret и лог.
- Старые `bot.db`, `settings.json` и `.telethon_config.json` из корня автоматически копируются в `data` **только если persistent-версии ещё нет**. При следующих деплоях существующие данные не перезаписываются.
- Telethon использует **StringSession**, а не SQLite `.session`. Это соответствует контейнерной схеме Bothost и не требует интерактивного терминала.
- Телефон, одноразовый Telegram-код и 2FA-пароль не сохраняются.
- Web Admin имеет HttpOnly-cookie, CSRF, security headers и ограничение неудачных попыток входа.
- Если `WEB_SECRET_KEY` не задан, безопасный ключ создаётся один раз в `data/.web_secret_key`, а не меняется на каждом рестарте.

## Постоянные файлы

По умолчанию:

```text
/app/data/bot.db
/app/data/settings.json
/app/data/.telethon_config.json
/app/data/.web_secret_key
/app/data/bot.log
```

Папка `data/` находится в `.gitignore`.

При необходимости пути можно переопределить:

```env
DATA_DIR=/app/data
DATABASE_PATH=/app/data/bot.db
SETTINGS_PATH=/app/data/settings.json
TELETHON_CONFIG_PATH=/app/data/.telethon_config.json
LOG_PATH=/app/data/bot.log
```

Обычно на Bothost это не требуется.

## Деплой на Bothost

### 1. Загрузите проект

Используйте эту сборку как проект/репозиторий. Главный файл — **`main.py`**.

### 2. Включите домен

В настройках Bothost включите **«Использовать домен»** и выберите внутренний порт, например `8080`.

Код не хардкодит этот порт: Bothost передаёт выбранное значение в `PORT`, а Lorenzo читает его автоматически.

### 3. Переменные окружения

Минимум для первого запуска:

```env
OWNER_ID=123456789
WEB_ADMIN_USERNAME=admin
WEB_ADMIN_PASSWORD=очень_сложный_уникальный_пароль
WEB_SECRET_KEY=длинная_случайная_строка
```

`BOT_TOKEN` Bothost обычно передаёт автоматически для Telegram-бота. Если в конкретном способе деплоя он не создаётся — добавьте вручную:

```env
BOT_TOKEN=123456:ABCDEF...
```

Не нужно вручную задавать `PORT` и `DOMAIN`, если их уже передаёт Bothost.

### 4. Откройте Web Admin

После запуска откройте домен Bothost. Проверка состояния доступна по:

```text
https://ваш-домен/health
```

Web Admin находится на корневом URL:

```text
https://ваш-домен/
```

### 5. Подключите Telethon через web-панель

Откройте **🔐 Telethon** и пройдите мастер:

1. `API ID`
2. `API Hash`
3. номер Telegram
4. код Telegram
5. 2FA-пароль, если включён

После успешного входа создаётся **StringSession**. Она сохраняется внутри `/app/data/.telethon_config.json` с ограниченными правами и автоматически используется worker'ом после рестартов/обновлений.

API ID/API Hash можно получить в `my.telegram.org` → **API development tools**.

### Альтернативный вариант Telethon через Bothost ENV

Если StringSession уже существует, поддерживаются стандартные переменные Bothost:

```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SESSION_STRING=1AAB...
```

Также поддерживаются имена Lorenzo:

```env
TELETHON_API_ID=12345678
TELETHON_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELETHON_SESSION_STRING=1AAB...
```

Реальную `SESSION_STRING` нельзя публиковать, коммитить в Git или отправлять посторонним — она даёт доступ к Telegram-сессии аккаунта.

## Локальный запуск

Python 3.11+:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

На Windows активация окружения:

```text
venv\Scripts\activate
```

Локально, если `PORT` не задан, Web Admin использует `WEB_PORT`, а затем fallback `8080`.

## Основные переменные

```env
BOT_TOKEN=...
OWNER_ID=123456789

WEB_ENABLED=1
WEB_HOST=0.0.0.0
WEB_ADMIN_USERNAME=admin
WEB_ADMIN_PASSWORD=...
WEB_SECRET_KEY=...
WEB_SESSION_TTL_SECONDS=43200

TELETHON_ENABLED=1
TELETHON_SYNC_INTERVAL_SECONDS=300
TELETHON_DISCOVERY_INTERVAL_SECONDS=1800
TELETHON_HISTORY_DAYS=30
TELETHON_HISTORY_MAX_MESSAGES=50000
TELETHON_HISTORY_OVERLAP_SECONDS=600
```

На Bothost `WEB_PUBLIC_URL` обычно оставляется пустым: приложение использует `DOMAIN`. `WEB_COOKIE_SECURE` также определяется автоматически для HTTPS. При необходимости оба значения можно переопределить вручную.

## Как считается активность

Основной показатель — активность **в конкретной группе**, а не Telegram online-status:

- последнее сообщение пользователя;
- количество сообщений за 7/30 дней;
- дневные агрегаты;
- дата вступления/выхода;
- текущий membership status;
- Telegram presence (`online`, `offline`, `recently`, `last_week`, `last_month`, `hidden`).

Тексты сообщений в статистическую БД не сохраняются.

## Какие чаты отслеживаются

Только:

- `group`
- `supergroup`

Личные аккаунты и broadcast-каналы не добавляются в tracking. Старые ошибочные positive `chat_id` автоматически удаляются из tracking/settings при миграции.

## Первая синхронизация

1. Запустите приложение.
2. Подключите Telethon в Web Admin.
3. Откройте **Чаты**.
4. Дождитесь обнаружения групп.
5. Включите отслеживание нужной группы.
6. Запустите полный sync либо дождитесь автоматического.

Worker синхронизирует участников и историю за `TELETHON_HISTORY_DAYS`, затем поддерживает данные через Telegram events и периодические сверки.

## Обновление старой сборки

Текущая сборка содержит совместимость со старой структурой:

- корневой `bot.db` один раз переносится в persistent storage;
- `settings.json` переносится аналогично;
- `members_panel.sqlite3`, если присутствует, используется как legacy-источник для миграции групп/участников;
- положительные ID личных пользователей исключаются;
- текущие администраторы и история участников остаются в `bot.db`;
- старая файловая Telethon-сессия `*.session` намеренно не используется новой Bothost-сборкой. Один раз переподключите Telethon через Web Admin, чтобы получить persistent StringSession, либо задайте `SESSION_STRING` через окружение.

После первого успешного запуска на Bothost убедитесь, что `/app/data/bot.db` существует. Именно `/app/data` нужно резервировать перед серьёзными изменениями.

## Web Admin безопасность

Для публичной панели обязательно используйте длинный уникальный `WEB_ADMIN_PASSWORD`.

Рекомендуется также задать `WEB_SECRET_KEY`, например локально сгенерировать:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Если переменная не задана, приложение создаст постоянный secret в `/app/data/.web_secret_key` автоматически.

После 10 неправильных попыток входа с одного адреса вход временно блокируется на 15 минут.

## Диагностика

Health endpoint:

```text
GET /health
```

Ожидаемый ответ:

```json
{"ok": true, "time": 1234567890}
```

Если домен Bothost отдаёт `502/504`, сначала проверьте:

- домен включён;
- порт в панели Bothost выбран;
- приложение действительно запущено через `main.py`;
- в логах Uvicorn видно `0.0.0.0:<PORT>`;
- `/health` открывается.

## Важное ограничение Telegram

Telethon не может обходить privacy-настройки пользователей. Если Telegram отдаёт `recently`, `last_week`, `last_month` или `hidden`, точный last seen получить нельзя. Поэтому решение об активности/неактивности строится прежде всего на сообщениях пользователя внутри отслеживаемой группы.
