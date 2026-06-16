# QobuzSync

Web-сервис для переноса и синхронизации треков из Яндекс.Музыки в плейлисты Qobuz.

## Возможности

- загрузка списка треков из файла, текстового поля или ссылки Яндекс.Музыки;
- импорт треков по ссылкам Яндекс.Музыки: трек, альбом, плейлист, лайкнутые треки, артист;
- авторизация Яндекс.Музыки через device code;
- авторизация Qobuz через вставку токена или браузерный вход;
- поиск соответствий треков в Qobuz через WebSocket с прогрессом;
- ручной выбор соответствия, если автоматический поиск не нашел трек;
- создание нового плейлиста Qobuz или добавление в существующий;
- пропуск дублей при добавлении в существующий плейлист;
- отдельные Qobuz/Yandex аккаунты для каждого посетителя через cookie-сессии;
- локальный кэш результатов поиска Qobuz.

## Как Работают Пользовательские Сессии

В web-режиме `.env` больше не хранит аккаунт конкретного пользователя.

При первом заходе сервис выдает браузеру cookie `qsync_sid`. По этому идентификатору в SQLite-файле `qobuzsync.db` хранятся токены именно этого посетителя:

- `qobuz_token`;
- выбранные `qobuz_app_id` и `qobuz_app_secret`;
- рабочий `qobuz_working_app_id`;
- `yandex_token`.

Это значит, что пользователь A и пользователь B на `qobuz.rentalall.ru` видят свои аккаунты, пока у них разные браузеры, cookie или сессии. Токены не возвращаются обратно во frontend через `/api/config`; UI получает только флаги авторизации и профиль.

Важно: это anonymous-session модель, а не полноценная регистрация. Если пользователь очистит cookie или зайдет с другого устройства, он получит новую сессию и должен будет авторизоваться заново. Для продакшена обязательно включайте HTTPS и `QSYNC_COOKIE_SECURE=true`.

## Быстрый Старт

Требуется Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env
python app.py
```

Откройте:

```text
http://127.0.0.1:8000
```

На Linux/macOS активация окружения:

```bash
source .venv/bin/activate
```

## Настройка `.env`

Минимальный файл для web-сервиса:

```env
QOBUZ_APP_ID=30650571
QOBUZ_APP_SECRET=
QSYNC_DB_PATH=qobuzsync.db
QSYNC_COOKIE_SECURE=false
QSYNC_LOGIN_PROFILE_DIR=.qobuz_login_profiles
QSYNC_BROWSER_RUNTIME_DIR=
QSYNC_BROWSER_LOGIN_ENABLED=true
```

`QOBUZ_APP_ID` и `QOBUZ_APP_SECRET` используются как серверные значения по умолчанию. Пользователь может заменить их в форме Qobuz в своей сессии.

`QSYNC_DB_PATH` задает путь к SQLite-базе с пользовательскими сессиями. Этот файл содержит токены пользователей, поэтому его нельзя коммитить, отдавать через nginx или класть в публичные бэкапы.

`QSYNC_COOKIE_SECURE=true` нужно ставить на сервере за HTTPS. Для локального `http://127.0.0.1:8000` оставьте `false`.

`QSYNC_LOGIN_PROFILE_DIR` задает директорию для профилей браузерного входа Qobuz. В продакшене лучше хранить ее рядом с базой в `/var/lib/qobuzsync`, а не внутри директории приложения.

`QSYNC_BROWSER_RUNTIME_DIR` задает домашнюю runtime-директорию для серверного Chromium. На сервере укажите `/var/lib/qobuzsync`, чтобы cache/config/crashpad не пытались писать в недоступный home.

`QSYNC_BROWSER_LOGIN_ENABLED=false` отключает серверный браузерный вход Qobuz. Для публичного сервиса это рекомендуемый режим: пользователь вставляет Qobuz token вручную, а сервер не пытается запускать Chromium.

## Вход В Qobuz И Капча

Qobuz может блокировать автоматизированные браузеры invisible captcha/anti-bot проверкой. Если окно входа говорит, что капча не пройдена, но самой капчи не видно, это значит, что сайт отклонил browser score.

Надежный вариант для публичного сервиса - дать пользователю вставить Qobuz token вручную в форме. Браузерный вход запускается на стороне сервера и больше подходит для локального использования или сервера с полноценным GUI/Xvfb.

Профили браузерного входа хранятся отдельно для каждой сессии в `QSYNC_LOGIN_PROFILE_DIR/<session_id>/`.

## Запуск Для Разработки

```bash
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

## Запуск На Сервере

Пример для Ubuntu/Debian.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
git clone https://github.com/MerlinRus/import-tracks-from-YaMusic-to-Qobuz.git
cd import-tracks-from-YaMusic-to-Qobuz
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
nano .env
```

Для публичного HTTPS-домена в `.env`:

```env
QSYNC_COOKIE_SECURE=true
QSYNC_DB_PATH=/var/lib/qobuzsync/qobuzsync.db
QSYNC_LOGIN_PROFILE_DIR=/var/lib/qobuzsync/qobuz_login_profiles
QSYNC_BROWSER_RUNTIME_DIR=/var/lib/qobuzsync
QSYNC_BROWSER_LOGIN_ENABLED=false
```

Создайте директорию для базы и браузерных профилей, затем отдайте ее пользователю сервиса:

```bash
sudo mkdir -p /var/lib/qobuzsync/qobuz_login_profiles
sudo chown -R qobuzsync:qobuzsync /var/lib/qobuzsync
sudo chmod 700 /var/lib/qobuzsync
sudo chmod 700 /var/lib/qobuzsync/qobuz_login_profiles
```

Проверочный запуск:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Systemd

Пример сервиса:

```ini
[Unit]
Description=QobuzSync
After=network.target

[Service]
Type=simple
User=qobuzsync
Group=qobuzsync
WorkingDirectory=/opt/import-tracks-from-YaMusic-to-Qobuz
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/import-tracks-from-YaMusic-to-Qobuz/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Если проект лежит не в `/opt/import-tracks-from-YaMusic-to-Qobuz`, поменяйте `WorkingDirectory` и `ExecStart`.

Запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable qobuzsync
sudo systemctl start qobuzsync
sudo systemctl status qobuzsync
```

Логи:

```bash
journalctl -u qobuzsync -f
```

## Nginx Reverse Proxy

Пример конфига для `qobuz.rentalall.ru`:

```nginx
server {
    server_name qobuz.rentalall.ru;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600;
        proxy_send_timeout 3600;
    }
}
```

После настройки домена подключите TLS через certbot и включите `QSYNC_COOKIE_SECURE=true`.

## Локальные Файлы

Эти файлы не коммитятся:

- `.env` - серверные секреты;
- `qobuzsync.db` и `qobuzsync.db-*` - пользовательские сессии и токены;
- `.qobuz_login_profile/` - старый локальный профиль браузерного входа Qobuz;
- `.qobuz_login_profiles/` - профили браузерного входа по сессиям;
- `/var/lib/qobuzsync/qobuz_login_profiles/` - рекомендуемое место для профилей браузерного входа на сервере;
- `tracks.txt` - личный список треков;
- `search_cache.json` - локальный кэш поиска;
- `.codegraph/` - локальный индекс.

## Ограничения

Qobuz API добавляет новые треки в конец существующего плейлиста. В текущей реализации сервис пропускает дубли и добавляет только новые треки, но не перестраивает весь плейлист для перемещения новых треков наверх.

Для настоящих пользовательских аккаунтов с паролями, админкой, удалением данных и аудитом действий нужно добавить полноценную систему пользователей поверх текущей cookie-сессионной модели.
