# Telegram Intent Monitor

Мониторинг Telegram-чатов с классификацией сообщений по интентам и публикацией
совпадений в форум-группу (один топик = один интент).

**Стек:** Python 3.12, Telethon (юзербот, MTProto push), aiogram 3 (управляющий
бот), PostgreSQL (и как БД, и как очередь), Claude API (Haiku) для
классификации, Docker Compose.

## Архитектура

```
[Источники: чаты/каналы]
        │ push (MTProto)
        ▼
  [userbot: Telethon] ──► PostgreSQL ◄── [worker: префильтр + LLM]
        ▲                    ▲  │
        │ join/leave         │  ▼ publish
  [bot: aiogram] ────────────┘ [форум-группа, топики по интентам]
```

Три процесса общаются **только через БД**: бот ставит статусы-задачи
(`pending_join` / `leaving`), юзербот их исполняет; воркер забирает сообщения
через `SELECT ... FOR UPDATE SKIP LOCKED` (Postgres как очередь, без
Redis/Celery); бот публикует `matches WHERE published_at IS NULL`.

## Подготовка (этап 0)

1. **Аккаунт юзербота.** Отдельный номер, получить `api_id`/`api_hash` на
   [my.telegram.org](https://my.telegram.org). Аккаунт «прогреть»: аватар, имя,
   пара дней обычной активности; не вступать сразу в десятки чатов.
2. **Бот.** Создать через @BotFather, сохранить токен.
3. **Форум-группа.** Создать супергруппу, включить Topics. Добавить бота
   админом с правами Manage Topics и постинга.
4. **Конфиг.** `cp .env.example .env`, заполнить.
5. **Session-файл** (единственный интерактивный шаг, локально):

   ```bash
   pip install -r requirements.txt
   TG_API_ID=... TG_API_HASH=... TG_SESSION=./data/session/userbot python -m userbot.login
   chmod 600 ./data/session/userbot.session
   ```

   ⚠️ Session-файл = полный доступ к аккаунту: не коммитить (в `.gitignore`),
   права 600, каталог `data/` монтируется volume'ом.

## Деплой (CI/CD)

Пуш в `main` → GitHub Actions: syntax-check → rsync кода на сервер →
`docker compose up -d --build` → health check. Ручной запуск — кнопка
Run workflow во вкладке Actions.

**Настройка один раз:**

1. На сервере (Ubuntu, под root):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/deploy/setup-server.sh | bash
   ```

   Скрипт ставит Docker и rsync, создаёт пользователя `deploy`, каталог
   `/opt/expaw` и SSH-ключ для Actions; в конце печатает, что куда положить.

2. В GitHub: Settings → Secrets and variables → Actions:
   - `SSH_HOST` — IP сервера
   - `SSH_USER` — `deploy`
   - `SSH_KEY` — приватный ключ из `/home/deploy/.ssh/github_actions`
   - `SSH_PORT` — опционально, по умолчанию 22

3. На сервер положить `/opt/expaw/.env` (из `.env.example`) и
   `/opt/expaw/data/session/userbot.session` (сгенерирован локально).

`.env` и `data/` (сессия + данные Postgres) деплоем **не трогаются** —
rsync их исключает. Миграции применяются автоматически (сервис `migrate`,
идемпотентен), пересобираются только изменившиеся сервисы. Убийство любого
контейнера самовосстанавливается без потери сообщений (`catch_up=True` +
страховочная сверка).

Локальный запуск без CI — те же два файла + `docker compose up -d --build`.

## Использование

В личке с ботом (только для `ADMIN_IDS`):

| Команда | Действие |
|---|---|
| `/add_chat @name` или `t.me/+hash` | отслеживать чат (вступи в него сам — мониторинг включится автоматически) |
| `/remove_chat @name` | перестать отслеживать; из чата аккаунт не выходит |
| `/purge_chat @name` | удалить чат со всеми сообщениями и совпадениями |
| `/list_chats` | статусы и дата последнего сообщения |
| `/add_intent dog_boarding Передержка собак` | интент + топик в форуме |
| `/set_prompt dog_boarding <критерии>` | критерии для LLM; префильтр подберётся автоматически |
| `/set_prefilter dog_boarding передержк, выгул, /собак[аеу]/` | префильтр вручную (перезаписывает) |
| `/suggest_prefilter dog_boarding` | перегенерировать префильтр через LLM |
| `/set_threshold dog_boarding 0.75` | порог уверенности |
| `/toggle_intent`, `/list_intents` | управление интентами |
| `/stats` | сообщения/совпадения за сутки, % дошедших до LLM |
| `/retry_errors` | вернуть сообщения со статусом `error` в очередь |

Сообщения собираются **с момента вступления юзербота** в чат: при join
сохраняется последнее сообщение как точка отсчёта, история до вступления не
читается. Интент начинает работать, когда у него есть **и** промпт, **и** префильтр
(сообщения без кандидатов по префильтру до LLM не доходят — это главный
контроль стоимости). После `/set_prompt` бот сам просит LLM подобрать
ключевые слова (основы слов, синонимы, англ. варианты) — руками их задавать
не обязательно, но можно поправить через `/set_prefilter`.

## Пайплайн классификации

1. **Префильтр (бесплатно):** нормализация (lowercase, ё→е); короче
   `MIN_TEXT_LEN`, от ботов → `skipped`; по ключевым словам/regex каждого
   интента отбираются кандидаты, нет ни одного → `skipped`.
2. **LLM:** один запрос на сообщение со всеми интентами-кандидатами; модель —
   `claude-haiku-4-5` (задача бинарной классификации, дорогая модель не нужна);
   строгий JSON через structured outputs; ретраи 429/5xx делает SDK;
   `confidence >= threshold` → `matches`. Полный ответ LLM сохраняется в
   `matches.llm_response` для тюнинга промптов.

## Публикация и антидубль

- UNIQUE `(message_id, intent_id)` — одно сообщение не публикуется дважды в
  один топик.
- Хэш нормализованного текста (`published_hashes`): повтор того же текста из
  другого чата в окне `DEDUP_WINDOW_HOURS` не публикуется (у такого match
  `published_at` задан, `published_msg_id` — NULL).
- Троттлинг `PUBLISH_MIN_INTERVAL_SEC` + обработка `TelegramRetryAfter`
  (Bot API ~20 сообщений/мин в группу).

## Надёжность

- Топик «Система» в той же группе: старт/стоп сервисов, FloodWait, ошибки
  join, watchdog-алерты (heartbeat каждого сервиса, порог 10 мин).
- Аккаунт юзербота **сам никуда не вступает и не выходит** — владелец
  вступает в чаты сам, система только проверяет членство (раз в ~5 мин) и
  слушает. Это сводит риск бана к минимуму: никакой активности от имени
  аккаунта, кроме чтения. Сверка раз в 2 часа с паузами между чатами.
- Ретеншн: `skipped`-сообщения удаляются через `SKIPPED_RETENTION_DAYS`.
- Бэкап: `docker compose exec postgres pg_dump -U $POSTGRES_USER $POSTGRES_DB | gzip > backup.sql.gz`
  (повесить на крон с ротацией).

## Структура

```
common/     конфиг, пул asyncpg, JSON-логи, нормализация текста
db/         миграции (sql) + раннер (python -m db.migrate)
userbot/    Telethon: push-слушатель, join/leave-менеджер, сверка, login-скрипт
bot/        aiogram 3: команды админа, публикатор, топик «Система», watchdog
worker/     префильтр + классификация Claude, ретеншн
```
