-- Esquema base de Agente SUS.
-- Todas las sentencias son idempotentes para poder marcar esta migracion
-- como aplicada tambien en bases de datos que ya existian.

CREATE TABLE IF NOT EXISTS event_catalog (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (guild_id, name_key)
);

CREATE TABLE IF NOT EXISTS event_users (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    discord_tag TEXT NOT NULL,
    nickname TEXT NOT NULL,
    external_id TEXT NOT NULL,
    country TEXT NOT NULL,
    retain_profile BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id),
    UNIQUE (guild_id, external_id)
);

CREATE TABLE IF NOT EXISTS event_blacklist (
    guild_id BIGINT NOT NULL,
    user_id BIGINT,
    external_id TEXT NOT NULL,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, external_id)
);

ALTER TABLE event_blacklist
    ADD COLUMN IF NOT EXISTS user_id BIGINT;

UPDATE event_blacklist AS blacklist
SET user_id = users.user_id
FROM event_users AS users
WHERE blacklist.guild_id = users.guild_id
  AND blacklist.external_id = users.external_id
  AND blacklist.user_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS event_blacklist_guild_user_uidx
    ON event_blacklist (guild_id, user_id)
    WHERE user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS event_instances (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    catalog_event_id INTEGER NOT NULL REFERENCES event_catalog(id) ON DELETE RESTRICT,
    event_name TEXT NOT NULL,
    participant_limit INTEGER NOT NULL CHECK (participant_limit IN (0, 10, 15, 20)),
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    registration_channel_id BIGINT NOT NULL,
    registration_message_id BIGINT,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP WITH TIME ZONE
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_event_per_guild
    ON event_instances (guild_id)
    WHERE status IN ('open', 'closed');

CREATE TABLE IF NOT EXISTS event_registrations (
    event_id INTEGER NOT NULL REFERENCES event_instances(id) ON DELETE CASCADE,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    position INTEGER NOT NULL,
    is_overflow BOOLEAN NOT NULL DEFAULT FALSE,
    registered_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, user_id),
    UNIQUE (event_id, position),
    FOREIGN KEY (guild_id, user_id)
        REFERENCES event_users(guild_id, user_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS data_deletion_cooldowns (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    requested_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    can_register_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

ALTER TABLE event_users
    ADD COLUMN IF NOT EXISTS retain_profile BOOLEAN NOT NULL DEFAULT TRUE;

-- Indices de lectura. Sin ellos, las consultas por usuario y las listas
-- paginadas recorren tablas completas.
CREATE INDEX IF NOT EXISTS event_registrations_guild_user_idx
    ON event_registrations (guild_id, user_id);

CREATE INDEX IF NOT EXISTS event_users_guild_updated_idx
    ON event_users (guild_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS data_deletion_cooldowns_expiry_idx
    ON data_deletion_cooldowns (can_register_at);
