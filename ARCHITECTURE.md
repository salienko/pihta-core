# pihta-core — единое ядро бронирования «Пихта»

Один сервис владеет базой, доменом бронирования, платежами и админкой.
Telegram- и MAX-боты — тонкие транспорты: получают обновление, спрашивают
ядро, рисуют ответ своими кнопками.

## Карточки Bothost

| Карточка | Что внутри | Домен |
|---|---|---|
| `pihta-core` | aiohttp: `/health`, `/api/v1/*`, админка, вебхук PayKeeper, воркеры | `core.bothost.tech` |
| `pihta-bot` (Telegram) | aiogram polling, рендер экранов, передача действий в ядро | `bot-1791828868-…bothost.tech` |
| `pihta-max-bot` (MAX) | вебхук MAX, рендер экранов, передача действий в ядро | `bot-1792941582-…bothost.tech` |

Ядро — единственный писатель в PostgreSQL. Транспорты не открывают БД.
Связь между карточками только по HTTPS: внутренней сети у Bothost нет.

## API

Все запросы — с заголовком `X-Core-Token: <CORE_API_TOKEN>`; без него 401.

- `GET /health` — проверка живости и БД (для Bothost).
- `GET /api/status` — сервис, БД, включён ли PayKeeper.
- `GET /api/v1/ping` — версия ядра.
- `POST /api/v1/screen` — экран для пользователя:
  ```json
  {"platform": "telegram|max", "user_id": 123, "action": "home|info:Цены|begin|…"}
  ```
  Ответ:
  ```json
  {"text": "…", "photo": "home", "rows": [[{"text": "…", "action": "begin"}]],
   "state": null, "reply_contact": false, "platform": "max", "actor": 123}
  ```
  Кнопка либо `action` (транспорт отправляет его назад), либо `url`.

Строки действий совпадают с нынешними callback-данными Telegram
(`home`, `begin`, `info:Цены`, `list:0:0`, `rulesfull`, `staffweb`,
`action:token:revision:value`), поэтому грамматика кнопок не меняется.

## Что уже перенесено

`domain.py`, `booking_domain.py`, `booking_flow.py`, `booking_store.py`,
`booking_reserve.py`, `staff_booking.py`, `admin_booking.py`, `admin_web.py`,
`booking_payments.py`, `paykeeper.py`, `paykeeper_store.py`,
`final_receipts.py`, `checkout_store.py` — без изменений, они не зависели
от мессенджеров. `core/screens.py` — новый транспортно-нейтральный слой
экранов (пока меню и информационные разделы). `core/api.py` — JSON API.

## Этапы

1. **Каркас** (готово): ядро поднимается, отдаёт админку, `/health`, API,
   вебхук PayKeeper; тесты зелёные на отдельной БД.
2. **Гостевой поток в ядре**: экраны `name → phone → guests → date → slot →
   review → submit` и действия `begin/save/submit/resume/resetask/discardask`,
   перенос `booking_ui.render` в `core/screens.py`.
3. **Telegram как клиент**: рендерер экранов на aiogram, флаг
   `CORE_MODE=remote`; локальный код остаётся путём отката.
4. **MAX как клиент**: рендерер на `maxapi`, вебхук MAX проксирует в ядро.
5. **Платежи и уведомления**: воркер PayKeeper и outbox-доставка переезжают
   в ядро (`core/notify.py`), ядро держит оба токена платформ.
6. **Переключение**: админка и подписанные ссылки на домене ядра,
   `WEB_BASE_URL` ядра, callback PayKeeper на домен ядра, затем удаление
   дублированного кода из ботов.

## Состояние этапов

- Этап 1 — готово (каркас, админка, health, API, Dockerfile).
- Этап 2 — готово: в `core/screens.py` перенесён весь гостевой путь
  (`name → phone → guests → date → slot → review → submit`), действия
  `begin/next/back/day/slot/guest/submit/discard/reset/resume`, список и
  карточка брони, отмена, профиль, разделы «Цены/Правила/Контакты/Помощь»,
  запрос платёжной ссылки и история платежей. Проверено сквозным тестом
  `test_core_flow.py` на реальной PostgreSQL.
- Этап 3 — следующий: рендерер экранов для Telegram за флагом
  `CORE_MODE=remote`.

## Переменные окружения карточки ядра

| Ключ | Значение |
|---|---|
| `DATABASE_URL` | DSN PostgreSQL (общая база бронирования) |
| `CORE_API_TOKEN` | общий секрет транспортов, ≥32 символов |
| `WEB_BASE_URL` | `https://core.bothost.tech` (подписанные ссылки админки) |
| `ADMIN_TELEGRAM_IDS` | ID администраторов |
| `PAYKEEPER_*` | как в боте; включать только после переноса воркера |
| `PORT` | 3000 (Bothost передаёт сам) |

## Риски

- Ядро — единая точка отказа: падение останавливает оба бота. Нужен внешний
  watchdog (проверка `/health` и перезапуск через Bothost API).
- Одновременно должно работать только одно ядро: `main.py` берёт
  `pg_try_advisory_lock` на отдельном соединении и раз в минуту перепроверяет,
  что лок всё ещё наш (`booking_store.WorkerLock.holds()`). Если связь с базой
  оборвалась, лок PostgreSQL отпускает молча, и второй экземпляр может
  подняться и слать те же уведомления дважды. При потере лока ядро сначала
  останавливает фоновые задачи (`delivery_loop`, `payments_loop`), затем
  завершается с ненулевым кодом — платформа поднимает новый экземпляр, и он
  либо снова берёт лок, либо откажется стартовать, увидев занятый лок.
- Каждый клик гостя — сетевой вызов; при 2 ГБ и одном процессе это
  приемлемо, но таймауты транспорта должны быть короткими (≤5 с) с
  понятным сообщением гостю.
- Идентичность: `(platform, external_user_id)`; нельзя склеивать
  Telegram-ID и MAX-ID по числу. В API платформа передаётся явно, в схеме
  для этого есть `migrations_002_platform_routing.sql`.
