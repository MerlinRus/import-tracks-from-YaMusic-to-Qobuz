# QobuzSync

Web-сервис для переноса и синхронизации треков из Яндекс.Музыки в плейлисты Qobuz.

## Возможности

- загрузка списка треков из `tracks.txt` или текстового поля;
- импорт треков по ссылкам Яндекс.Музыки: трек, альбом, плейлист, лайкнутые треки, артист;
- авторизация Яндекс.Музыки через device code;
- поиск соответствий треков в Qobuz через WebSocket с прогрессом;
- ручной выбор соответствия, если автоматический поиск не нашел трек;
- создание нового плейлиста Qobuz или добавление в существующий;
- пропуск дублей при добавлении в существующий плейлист;
- локальный кэш результатов поиска.

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

Минимальный файл:

```env
QOBUZ_TOKEN=''
QOBUZ_APP_ID='30650571'
QOBUZ_APP_SECRET=''
YANDEX_MUSIC_TOKEN=''
```

`YANDEX_MUSIC_TOKEN` можно получить через встроенный вход Яндекс.Музыки.

`QOBUZ_TOKEN` можно ввести вручную или попробовать получить кнопкой входа через браузер. Браузерный вход открывает отдельное окно и сохраняет локальный профиль в `.qobuz_login_profile/`.

## Важное Про Qobuz И Капчу

Qobuz может блокировать автоматизированные браузеры invisible captcha/anti-bot проверкой. Если окно входа говорит, что капча не пройдена, но капчи не видно, это означает, что сайт отклонил браузерный score.

В таком случае надежнее получить `QOBUZ_TOKEN` локально и прописать его в `.env` на сервере. На headless-сервере браузерный вход обычно не подходит без полноценного GUI/Xvfb.

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

Проверочный запуск:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Systemd

Создайте сервис:

```bash
sudo nano /etc/systemd/system/qobuzsync.service
```

Пример содержимого:

```ini
[Unit]
Description=QobuzSync
After=network.target

[Service]
Type=simple
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

Пример конфига:

```nginx
server {
    server_name your-domain.example;

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
    }
}
```

После настройки домена можно подключить TLS через certbot.

## Локальные Файлы

Эти файлы не коммитятся:

- `.env` — токены и секреты;
- `.qobuz_login_profile/` — cookies браузерного входа Qobuz;
- `tracks.txt` — личный список треков;
- `search_cache.json` — локальный кэш поиска;
- `.codegraph/` — локальный индекс.

## Ограничения

Qobuz API добавляет новые треки в конец существующего плейлиста. В текущей безопасной реализации сервис пропускает дубли и добавляет только новые треки, но не перестраивает весь плейлист для перемещения новых треков наверх.
