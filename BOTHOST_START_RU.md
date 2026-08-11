# Lorenzo: точный запуск на Bothost

## 1. Что загружать

Загружайте содержимое этого архива так, чтобы в корне проекта Bothost были:

- `main.py`
- `Dockerfile`
- `requirements.txt`
- `web_app.py`
- `bot_app.py`
- `templates/`
- `static/`

Не должно быть дополнительной папки над `main.py`.

## 2. Настройки проекта Bothost

В настройках проекта:

1. Тип/шаблон: Telegram / Python.
2. Главный файл: `main.py`.
3. Включите `Использовать собственный Dockerfile`.
4. Включите домен.
5. Внутренний порт: `8080`.
6. Не задавайте `PORT` вручную: Bothost передаст его сам.
7. После изменения этих параметров выполните именно новый Deploy/Redeploy.

Dockerfile использует Python 3.11 и сам устанавливает все зависимости.

## 3. Переменные окружения

Добавляйте их в разделе `Переменные окружения` Bothost, а не только в локальный `.env`.

Обязательно:

```env
OWNER_ID=123456789
WEB_ADMIN_USERNAME=admin
WEB_ADMIN_PASSWORD=ЗАМЕНИТЬ_НА_СЛОЖНЫЙ_ПАРОЛЬ
WEB_SECRET_KEY=ЗАМЕНИТЬ_НА_ДЛИННУЮ_СЛУЧАЙНУЮ_СТРОКУ
TELETHON_ENABLED=1
```

`OWNER_ID` — только числовой Telegram ID, без `@`.

`BOT_TOKEN` Bothost обычно добавляет автоматически для Telegram-проекта. Если `/health` покажет `bot_token_set: false`, добавьте вручную:

```env
BOT_TOKEN=1234567890:AA...
```

Для Telethon пока ничего больше не задавайте. API ID/API Hash/телефон/код/2FA можно пройти через Web Admin.

Не задавайте вручную `PORT` и `DOMAIN`, если Bothost уже создаёт их автоматически.

## 4. Первый Deploy

Откройте логи сборки. Должны быть строки установки пакетов из `requirements.txt`, включая:

- aiogram 3.22.0
- Telethon 1.44.0
- fastapi 0.128.2
- uvicorn 0.48.0

Если сборка завершилась успешно, откройте runtime-логи.

Нормальный старт выглядит примерно так:

```text
Starting Lorenzo runtime
Python 3.11.x
Web Admin listening on 0.0.0.0:8080
Persistent data dir: /app/data
OWNER_ID configured: True
BOT_TOKEN configured: True
WEB_ADMIN_PASSWORD configured: True
Telegram Bot API connected: @your_bot (id=...)
```

## 5. Проверка Web Admin

Откройте:

```text
https://ВАШ-ДОМЕН/health
```

Полностью рабочий Bot API выглядит так:

```json
{
  "ok": true,
  "ready": true,
  "config": {
    "bot_token_set": true,
    "owner_id_set": true,
    "web_password_set": true,
    "data_dir_exists": true,
    "database_exists": true
  },
  "runtime": {
    "bot": "running",
    "bot_error": ""
  }
}
```

После этого откройте корень домена. Он должен перевести на `/login`.

Вход: значения `WEB_ADMIN_USERNAME` и `WEB_ADMIN_PASSWORD` из Bothost.

## 6. Проверка Telegram-бота

В личном чате с ботом отправьте:

```text
/myid
```

Бот вернёт ваш реальный числовой Telegram ID. Он должен полностью совпадать с `OWNER_ID` в Bothost.

Затем отправьте:

```text
/admin
```

Для владельца должна открыться админ-панель.

Если `/myid` не отвечает — проблема не в OWNER_ID: Bot API polling не работает. Смотрите `/health` -> `runtime.bot_error`.

Если `/myid` отвечает, но `/admin` молчит — сравните ID из `/myid` с `OWNER_ID`, затем сделайте Redeploy после исправления ENV.

## 7. Подключение Telethon

Только после того как `/health` показывает `bot: running`:

1. Войдите в Web Admin.
2. Откройте `Telethon`.
3. Введите API ID.
4. Введите API Hash.
5. Введите телефон с кодом страны.
6. Введите код Telegram.
7. Если включена 2FA — введите пароль.

После авторизации в `/health` Telethon должен перейти из `waiting/setup` в `running`.

## 8. Если не работает

### `/health` не открывается

Проверьте:

- включён домен;
- собственный Dockerfile включён;
- порт в Bothost = 8080;
- выполнен новый Deploy;
- в runtime-логах есть `Web Admin listening on 0.0.0.0:8080`.

### `bot_error = ModuleNotFoundError: No module named 'aiogram'`

Dockerfile/requirements не были применены. Проверьте, что `Dockerfile` и `requirements.txt` лежат в корне, включите `Использовать собственный Dockerfile` и выполните новый Deploy.

### `bot_error` говорит о неверном токене

Пересоздайте/проверьте токен в BotFather и значение `BOT_TOKEN` в Bothost.

### Ошибка `Conflict` / другой getUpdates

Тот же Bot Token уже запущен в другом месте. Остановите старую копию бота на другом сервере/хостинге и перезапустите текущую.

### Web работает, Telegram нет

Смотрите только `/health` -> `runtime.bot` и `runtime.bot_error`. Web и Telegram запускаются в одном приложении, но web специально остаётся доступным при ошибке Bot API, чтобы показать причину.

### Подключение Telethon на Bothost

После запуска Web Admin используйте **Telethon → Создать QR-код**. Это предпочтительнее входа по номеру на облачном сервере. На телефоне: **Telegram → Настройки → Устройства → Подключить устройство**, затем отсканируйте QR из панели. Если включён 2FA, панель автоматически перейдёт к вводу пароля.

Если используете вход по номеру, Web Admin покажет реальный способ доставки, выбранный Telegram. SMS для сторонних клиентов не гарантируется; не делайте много повторных запросов кода подряд.
