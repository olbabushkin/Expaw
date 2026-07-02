-- Обратная связь по опубликованным совпадениям: реакции 👍/👎 на посты
-- в топиках. Живёт отдельно от matches, чтобы переживать /recalc
-- (пересчёт удаляет matches, но messages и feedback остаются).

CREATE TABLE feedback (
    id         bigserial PRIMARY KEY,
    message_id bigint NOT NULL REFERENCES messages(id),
    intent_id  int NOT NULL REFERENCES intents(id),
    verdict    smallint NOT NULL,      -- 1 = лайк (верно), -1 = дизлайк (ложное срабатывание)
    by_user    bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (message_id, intent_id)
);

CREATE INDEX feedback_intent_idx ON feedback (intent_id, verdict);
