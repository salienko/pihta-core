# pihta-core

Ядро системы бронирования бани «Пихта»: PostgreSQL, домен бронирования,
платежи PayKeeper, админка, JSON API для транспортов Telegram и MAX.

## Локальный запуск

```bash
set -a; . ./.env; set +a
export PORT=8087 WEB_BASE_URL=https://core.bothost.tech ADMIN_TELEGRAM_IDS=530433212
export CORE_API_TOKEN=<32+ символов>
python main.py
```

`.env` (не коммитится) содержит `DATABASE_URL` и `TEST_DATABASE_URL`.

## Тесты

```bash
set -a; . ./.env; set +a
WEB_BASE_URL=https://core.bothost.tech python -m unittest test_core_api -v
```

Тесты домена/хранилища требуют `TEST_DATABASE_URL` и создают собственную
схему на каждый тест, затем удаляют её. Боевую базу не трогают.

## Деплой

Карточка Bothost: Python 3.11, свой Dockerfile, «Использовать домен»,
внутренний порт 3000, Git-репозиторий или архив. После сборки:
`curl https://<домен>/health` → `{"status":"ok"}`.
