-- Схема Telegram Intent Monitor.
-- source_chats.id — суррогатный ключ: при /add_chat реальный tg chat_id ещё
-- неизвестен (юзербот узнаёт его в момент join), поэтому chat_id — nullable UNIQUE.

CREATE TABLE source_chats (
    id             bigserial PRIMARY KEY,
    chat_id        bigint UNIQUE,            -- tg chat id (-100... для каналов), заполняет юзербот
    username       text,
    invite_hash    text,                     -- hash из t.me/+... / t.me/joinchat/...
    title          text,
    status         text NOT NULL DEFAULT 'pending_join',  -- pending_join / active / leaving / left / error
    last_error     text,
    added_by       bigint,                   -- tg user id админа
    joined_at      timestamptz,
    last_synced_at timestamptz,              -- страховочная сверка
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE intents (
    id         serial PRIMARY KEY,
    code       text UNIQUE NOT NULL,         -- 'dog_boarding'
    title      text NOT NULL,
    topic_id   int,                          -- message_thread_id топика в форуме
    llm_prompt text,                         -- критерии интента для классификатора
    prefilter  text[] NOT NULL DEFAULT '{}', -- ключевые слова; /.../ — regex
    threshold  real NOT NULL DEFAULT 0.7,
    enabled    boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id                bigserial PRIMARY KEY,
    chat_id           bigint NOT NULL REFERENCES source_chats(chat_id),
    tg_msg_id         bigint NOT NULL,
    sender_id         bigint,
    text              text,
    tg_date           timestamptz,
    raw               jsonb,
    status            text NOT NULL DEFAULT 'new',  -- new / prefiltered / classified / skipped / error
    intent_candidates text[],
    error             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chat_id, tg_msg_id)                     -- дедупликация push + catch-up
);

CREATE INDEX messages_status_idx ON messages (status);
CREATE INDEX messages_chat_date_idx ON messages (chat_id, tg_date);

CREATE TABLE matches (
    id               bigserial PRIMARY KEY,
    message_id       bigint NOT NULL REFERENCES messages(id),
    intent_id        int NOT NULL REFERENCES intents(id),
    score            real,
    llm_response     jsonb,
    published_msg_id bigint,                 -- id поста в форум-группе; NULL при published_at != NULL => дубль
    published_at     timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (message_id, intent_id)
);

CREATE INDEX matches_unpublished_idx ON matches (id) WHERE published_at IS NULL;

CREATE TABLE audit_log (
    id         bigserial PRIMARY KEY,
    actor      bigint,
    action     text NOT NULL,
    payload    jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Антидубль уровня 2: хэш нормализованного текста, окно задаётся конфигом.
CREATE TABLE published_hashes (
    hash         text PRIMARY KEY,
    published_at timestamptz NOT NULL DEFAULT now()
);

-- Служебные события: пишут все сервисы, бот постит их в топик «Система».
CREATE TABLE system_events (
    id         bigserial PRIMARY KEY,
    level      text NOT NULL DEFAULT 'info',  -- info / warning / error
    message    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    posted_at  timestamptz
);

CREATE INDEX system_events_unposted_idx ON system_events (id) WHERE posted_at IS NULL;

-- Heartbeat для watchdog.
CREATE TABLE service_heartbeats (
    service text PRIMARY KEY,
    beat_at timestamptz NOT NULL
);

-- Настройки/состояние (id служебного топика и т.п.).
CREATE TABLE kv (
    key   text PRIMARY KEY,
    value text
);
