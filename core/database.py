import json

import asyncpg

from core.config import ANTIFRAUD_RETENTION_DAYS, DATABASE_URL


bot_pool = None


async def init_db():
    global bot_pool
    try:
        bot_pool = await asyncpg.create_pool(DATABASE_URL)
        async with bot_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS registros (
                    user_id BIGINT PRIMARY KEY,
                    discord_tag TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS scheduled_posts (
                    id SERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    title TEXT,
                    content TEXT,
                    attachment_urls TEXT[],
                    scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    author_id BIGINT NOT NULL,
                    thread_name TEXT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
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
                CREATE TABLE IF NOT EXISTS verification_tokens (
                    token_id UUID PRIMARY KEY,
                    token_digest CHAR(64) NOT NULL UNIQUE,
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'issued'
                        CHECK (status IN ('issued', 'used', 'expired', 'revoked')),
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    used_at TIMESTAMP WITH TIME ZONE,
                    CHECK (expires_at > created_at)
                );
                CREATE INDEX IF NOT EXISTS verification_tokens_user_status_idx
                    ON verification_tokens (guild_id, user_id, status);
                CREATE INDEX IF NOT EXISTS verification_tokens_expiration_idx
                    ON verification_tokens (expires_at)
                    WHERE status = 'issued';
                CREATE UNIQUE INDEX IF NOT EXISTS verification_tokens_one_issued_user_uidx
                    ON verification_tokens (guild_id, user_id)
                    WHERE status = 'issued';
                CREATE TABLE IF NOT EXISTS verification_attempts (
                    id BIGSERIAL PRIMARY KEY,
                    token_id UUID UNIQUE
                        REFERENCES verification_tokens(token_id) ON DELETE SET NULL,
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    discord_tag TEXT,
                    ip_hash CHAR(64) NOT NULL,
                    ip_network_hash CHAR(64),
                    fingerprint_hash CHAR(64),
                    country_code VARCHAR(2),
                    region TEXT,
                    timezone TEXT,
                    language TEXT,
                    browser_family TEXT,
                    os_family TEXT,
                    device_type TEXT,
                    vpn_detected BOOLEAN,
                    proxy_detected BOOLEAN,
                    hosting_detected BOOLEAN,
                    risk_score SMALLINT NOT NULL DEFAULT 0
                        CHECK (risk_score BETWEEN 0 AND 100),
                    risk_level TEXT NOT NULL DEFAULT 'pending'
                        CHECK (risk_level IN ('pending', 'low', 'medium', 'high')),
                    decision TEXT NOT NULL DEFAULT 'pending'
                        CHECK (decision IN ('pending', 'approved', 'review', 'rejected', 'error')),
                    role_granted BOOLEAN NOT NULL DEFAULT FALSE,
                    possible_main_user_id BIGINT,
                    risk_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
                    consent_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    signals JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    retention_until TIMESTAMP WITH TIME ZONE NOT NULL
                );
                ALTER TABLE verification_attempts
                    ADD COLUMN IF NOT EXISTS consent_at TIMESTAMP WITH TIME ZONE
                    NOT NULL DEFAULT CURRENT_TIMESTAMP;
                ALTER TABLE verification_attempts
                    ADD COLUMN IF NOT EXISTS ip_network_hash CHAR(64);
                ALTER TABLE verification_attempts
                    ADD COLUMN IF NOT EXISTS possible_main_user_id BIGINT;
                ALTER TABLE verification_attempts
                    ADD COLUMN IF NOT EXISTS risk_reasons JSONB
                    NOT NULL DEFAULT '[]'::jsonb;
                CREATE INDEX IF NOT EXISTS verification_attempts_user_idx
                    ON verification_attempts (guild_id, user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS verification_attempts_ip_idx
                    ON verification_attempts (guild_id, ip_hash, created_at DESC);
                CREATE INDEX IF NOT EXISTS verification_attempts_network_idx
                    ON verification_attempts
                    (guild_id, ip_network_hash, created_at DESC)
                    WHERE ip_network_hash IS NOT NULL;
                CREATE INDEX IF NOT EXISTS verification_attempts_retention_idx
                    ON verification_attempts (retention_until);
                CREATE TABLE IF NOT EXISTS verified_users (
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    first_verified_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    last_verified_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    last_country_code VARCHAR(2),
                    status TEXT NOT NULL DEFAULT 'verified'
                        CHECK (status IN ('verified', 'revoked')),
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS verified_users_last_verified_idx
                    ON verified_users (guild_id, last_verified_at DESC);
                CREATE TABLE IF NOT EXISTS verification_antifraud_signals (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    ip_hash CHAR(64) NOT NULL,
                    ip_network_hash CHAR(64) NOT NULL,
                    fingerprint_hash CHAR(64) NOT NULL,
                    first_seen TIMESTAMP WITH TIME ZONE NOT NULL,
                    last_seen TIMESTAMP WITH TIME ZONE NOT NULL,
                    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    UNIQUE (
                        guild_id,
                        user_id,
                        ip_hash,
                        ip_network_hash,
                        fingerprint_hash
                    )
                );
                CREATE INDEX IF NOT EXISTS verification_antifraud_ip_idx
                    ON verification_antifraud_signals
                    (guild_id, ip_hash, expires_at DESC);
                CREATE INDEX IF NOT EXISTS verification_antifraud_network_idx
                    ON verification_antifraud_signals
                    (guild_id, ip_network_hash, expires_at DESC);
                CREATE INDEX IF NOT EXISTS verification_antifraud_fingerprint_idx
                    ON verification_antifraud_signals
                    (guild_id, fingerprint_hash, expires_at DESC);
                CREATE INDEX IF NOT EXISTS verification_antifraud_expiration_idx
                    ON verification_antifraud_signals (expires_at);
            """)
            await conn.execute(
                """
                WITH approved AS (
                    SELECT
                        guild_id,
                        user_id,
                        country_code,
                        created_at,
                        MIN(created_at) OVER (
                            PARTITION BY guild_id, user_id
                        ) AS first_verified_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY guild_id, user_id
                            ORDER BY created_at DESC
                        ) AS row_number
                    FROM verification_attempts
                    WHERE decision='approved' AND role_granted=TRUE
                )
                INSERT INTO verified_users (
                    guild_id,
                    user_id,
                    first_verified_at,
                    last_verified_at,
                    last_country_code,
                    status
                )
                SELECT
                    guild_id,
                    user_id,
                    first_verified_at,
                    created_at,
                    country_code,
                    'verified'
                FROM approved
                WHERE row_number=1
                ON CONFLICT (guild_id, user_id) DO UPDATE
                SET
                    first_verified_at=LEAST(
                        verified_users.first_verified_at,
                        EXCLUDED.first_verified_at
                    ),
                    last_country_code=CASE
                        WHEN EXCLUDED.last_verified_at >=
                             verified_users.last_verified_at
                        THEN COALESCE(
                            EXCLUDED.last_country_code,
                            verified_users.last_country_code
                        )
                        ELSE verified_users.last_country_code
                    END,
                    last_verified_at=GREATEST(
                        verified_users.last_verified_at,
                        EXCLUDED.last_verified_at
                    ),
                    status='verified',
                    updated_at=CURRENT_TIMESTAMP
                """
            )
            await conn.execute(
                """
                INSERT INTO verification_antifraud_signals (
                    guild_id,
                    user_id,
                    ip_hash,
                    ip_network_hash,
                    fingerprint_hash,
                    first_seen,
                    last_seen,
                    expires_at
                )
                SELECT
                    guild_id,
                    user_id,
                    ip_hash,
                    ip_network_hash,
                    fingerprint_hash,
                    MIN(created_at),
                    MAX(created_at),
                    MAX(created_at) + ($1 * INTERVAL '1 day')
                FROM verification_attempts
                WHERE decision='approved'
                  AND role_granted=TRUE
                  AND ip_network_hash IS NOT NULL
                  AND fingerprint_hash IS NOT NULL
                  AND created_at + ($1 * INTERVAL '1 day') > CURRENT_TIMESTAMP
                GROUP BY
                    guild_id,
                    user_id,
                    ip_hash,
                    ip_network_hash,
                    fingerprint_hash
                ON CONFLICT (
                    guild_id,
                    user_id,
                    ip_hash,
                    ip_network_hash,
                    fingerprint_hash
                ) DO UPDATE
                SET
                    first_seen=LEAST(
                        verification_antifraud_signals.first_seen,
                        EXCLUDED.first_seen
                    ),
                    last_seen=GREATEST(
                        verification_antifraud_signals.last_seen,
                        EXCLUDED.last_seen
                    ),
                    expires_at=GREATEST(
                        verification_antifraud_signals.expires_at,
                        EXCLUDED.expires_at
                    )
                """,
                ANTIFRAUD_RETENTION_DAYS,
            )
            cleanup = await _purge_expired_verification_data(conn)
        print("✅ Conexion a PostgreSQL exitosa y tablas verificadas.")
        if any(cleanup.values()):
            print(
                "🧹 Retencion de verificacion aplicada: "
                f"{cleanup['attempts']} intento(s), "
                f"{cleanup['tokens']} token(s) y "
                f"{cleanup['antifraud']} señal(es) eliminados."
            )
        async with bot_pool.acquire() as conn:
            await conn.execute("""
                ALTER TABLE scheduled_posts ADD COLUMN IF NOT EXISTS thread_name TEXT DEFAULT NULL;
            """)
    except Exception as e:
        print(f"❌ Error conectando a la DB: {e}")


async def get_registro(user_id):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM registros WHERE user_id=$1", user_id)


async def create_verification_token(
    token_id,
    token_digest,
    guild_id,
    user_id,
    expires_at,
):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1::BIGINT)",
                user_id,
            )
            await conn.execute(
                """
                UPDATE verification_tokens
                SET status = CASE
                    WHEN expires_at <= CURRENT_TIMESTAMP THEN 'expired'
                    ELSE 'revoked'
                END
                WHERE guild_id=$1 AND user_id=$2 AND status='issued'
                """,
                guild_id,
                user_id,
            )
            return await conn.fetchrow(
                """
                INSERT INTO verification_tokens (
                    token_id,
                    token_digest,
                    guild_id,
                    user_id,
                    expires_at
                )
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
                """,
                token_id,
                token_digest,
                guild_id,
                user_id,
                expires_at,
            )


async def consume_verification_token(token_id, token_digest):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE verification_tokens
                SET status='expired'
                WHERE token_id=$1
                  AND token_digest=$2
                  AND status='issued'
                  AND expires_at <= CURRENT_TIMESTAMP
                """,
                token_id,
                token_digest,
            )
            return await conn.fetchrow(
                """
                UPDATE verification_tokens
                SET status='used', used_at=CURRENT_TIMESTAMP
                WHERE token_id=$1
                  AND token_digest=$2
                  AND status='issued'
                  AND expires_at > CURRENT_TIMESTAMP
                RETURNING *
                """,
                token_id,
                token_digest,
            )


async def revoke_verification_token(token_id, token_digest):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow(
            """
            UPDATE verification_tokens
            SET status='revoked'
            WHERE token_id=$1
              AND token_digest=$2
              AND status='issued'
            RETURNING *
            """,
            token_id,
            token_digest,
        )


async def get_verification_submission_counts(
    guild_id,
    user_id,
    ip_hash,
    since,
):
    async with bot_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                (
                    SELECT COUNT(*)
                    FROM verification_attempts
                    WHERE guild_id=$1 AND user_id=$2 AND created_at >= $4
                ) AS user_count,
                (
                    SELECT COUNT(*)
                    FROM verification_attempts
                    WHERE guild_id=$1 AND ip_hash=$3 AND created_at >= $4
                ) AS ip_count
            """,
            guild_id,
            user_id,
            ip_hash,
            since,
        )
        return int(row["user_count"]), int(row["ip_count"])


async def record_pending_verification_attempt(
    *,
    token_id,
    token_digest,
    guild_id,
    user_id,
    discord_tag,
    ip_hash,
    ip_network_hash,
    fingerprint_hash,
    country_code,
    region,
    timezone_name,
    language,
    browser_family,
    os_family,
    device_type,
    signals,
    retention_until,
):
    signals_json = json.dumps(
        signals,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE verification_tokens
                SET status='expired'
                WHERE token_id=$1
                  AND token_digest=$2
                  AND status='issued'
                  AND expires_at <= CURRENT_TIMESTAMP
                """,
                token_id,
                token_digest,
            )
            consumed_token = await conn.fetchrow(
                """
                UPDATE verification_tokens
                SET status='used', used_at=CURRENT_TIMESTAMP
                WHERE token_id=$1
                  AND token_digest=$2
                  AND guild_id=$3
                  AND user_id=$4
                  AND status='issued'
                  AND expires_at > CURRENT_TIMESTAMP
                RETURNING token_id
                """,
                token_id,
                token_digest,
                guild_id,
                user_id,
            )
            if consumed_token is None:
                return None

            return await conn.fetchrow(
                """
                INSERT INTO verification_attempts (
                    token_id,
                    guild_id,
                    user_id,
                    discord_tag,
                    ip_hash,
                    ip_network_hash,
                    fingerprint_hash,
                    country_code,
                    region,
                    timezone,
                    language,
                    browser_family,
                    os_family,
                    device_type,
                    signals,
                    retention_until
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15::jsonb, $16
                )
                RETURNING *
                """,
                token_id,
                guild_id,
                user_id,
                discord_tag,
                ip_hash,
                ip_network_hash,
                fingerprint_hash,
                country_code,
                region,
                timezone_name,
                language,
                browser_family,
                os_family,
                device_type,
                signals_json,
                retention_until,
            )


async def get_verification_match_candidates(
    guild_id,
    user_id,
    ip_hash,
    ip_network_hash,
    fingerprint_hash,
    limit=100,
):
    async with bot_pool.acquire() as conn:
        return await conn.fetch(
            """
            WITH candidates AS (
                SELECT
                    id,
                    user_id,
                    discord_tag,
                    ip_hash,
                    ip_network_hash,
                    fingerprint_hash,
                    country_code,
                    timezone,
                    language,
                    browser_family,
                    os_family,
                    device_type,
                    decision,
                    role_granted,
                    created_at
                FROM verification_attempts
                WHERE guild_id=$1
                  AND user_id<>$2
                  AND retention_until > CURRENT_TIMESTAMP
                  AND decision IN ('pending', 'approved', 'review')
                  AND (
                        ip_hash=$3
                        OR ip_network_hash=$4
                        OR fingerprint_hash=$5
                  )

                UNION ALL

                SELECT
                    antifraud.id,
                    antifraud.user_id,
                    NULL::TEXT AS discord_tag,
                    antifraud.ip_hash,
                    antifraud.ip_network_hash,
                    antifraud.fingerprint_hash,
                    users.last_country_code AS country_code,
                    NULL::TEXT AS timezone,
                    NULL::TEXT AS language,
                    NULL::TEXT AS browser_family,
                    NULL::TEXT AS os_family,
                    NULL::TEXT AS device_type,
                    'approved'::TEXT AS decision,
                    TRUE AS role_granted,
                    antifraud.last_seen AS created_at
                FROM verification_antifraud_signals AS antifraud
                LEFT JOIN verified_users AS users
                  ON users.guild_id=antifraud.guild_id
                 AND users.user_id=antifraud.user_id
                WHERE antifraud.guild_id=$1
                  AND antifraud.user_id<>$2
                  AND antifraud.expires_at > CURRENT_TIMESTAMP
                  AND (
                        antifraud.ip_hash=$3
                        OR antifraud.ip_network_hash=$4
                        OR antifraud.fingerprint_hash=$5
                  )
            )
            SELECT *
            FROM candidates
            ORDER BY created_at ASC
            LIMIT $6
            """,
            guild_id,
            user_id,
            ip_hash,
            ip_network_hash,
            fingerprint_hash,
            limit,
        )


async def finalize_verification_attempt(
    attempt_id,
    *,
    risk_score,
    risk_level,
    decision,
    role_granted,
    possible_main_user_id,
    risk_reasons,
):
    reasons_json = json.dumps(
        risk_reasons,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchrow(
                """
                UPDATE verification_attempts
                SET risk_score=$2,
                    risk_level=$3,
                    decision=$4,
                    role_granted=$5,
                    possible_main_user_id=$6,
                    risk_reasons=$7::jsonb,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=$1
                RETURNING *
                """,
                attempt_id,
                risk_score,
                risk_level,
                decision,
                role_granted,
                possible_main_user_id,
                reasons_json,
            )
            if updated is None or decision != "approved" or not role_granted:
                return updated

            await conn.execute(
                """
                INSERT INTO verified_users (
                    guild_id,
                    user_id,
                    first_verified_at,
                    last_verified_at,
                    last_country_code,
                    status
                )
                VALUES ($1, $2, $3, $3, $4, 'verified')
                ON CONFLICT (guild_id, user_id) DO UPDATE
                SET
                    first_verified_at=LEAST(
                        verified_users.first_verified_at,
                        EXCLUDED.first_verified_at
                    ),
                    last_verified_at=GREATEST(
                        verified_users.last_verified_at,
                        EXCLUDED.last_verified_at
                    ),
                    last_country_code=COALESCE(
                        EXCLUDED.last_country_code,
                        verified_users.last_country_code
                    ),
                    status='verified',
                    updated_at=CURRENT_TIMESTAMP
                """,
                updated["guild_id"],
                updated["user_id"],
                updated["created_at"],
                updated["country_code"],
            )
            if (
                updated["ip_network_hash"] is not None
                and updated["fingerprint_hash"] is not None
            ):
                await conn.execute(
                    """
                    INSERT INTO verification_antifraud_signals (
                        guild_id,
                        user_id,
                        ip_hash,
                        ip_network_hash,
                        fingerprint_hash,
                        first_seen,
                        last_seen,
                        expires_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $6,
                        $6 + ($7 * INTERVAL '1 day')
                    )
                    ON CONFLICT (
                        guild_id,
                        user_id,
                        ip_hash,
                        ip_network_hash,
                        fingerprint_hash
                    ) DO UPDATE
                    SET
                        first_seen=LEAST(
                            verification_antifraud_signals.first_seen,
                            EXCLUDED.first_seen
                        ),
                        last_seen=GREATEST(
                            verification_antifraud_signals.last_seen,
                            EXCLUDED.last_seen
                        ),
                        expires_at=GREATEST(
                            verification_antifraud_signals.expires_at,
                            EXCLUDED.expires_at
                        )
                    """,
                    updated["guild_id"],
                    updated["user_id"],
                    updated["ip_hash"],
                    updated["ip_network_hash"],
                    updated["fingerprint_hash"],
                    updated["created_at"],
                    ANTIFRAUD_RETENTION_DAYS,
                )
            return updated


async def clear_verification_records(guild_id, user_id):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1::BIGINT)",
                user_id,
            )
            deleted_attempts = await conn.fetch(
                """
                DELETE FROM verification_attempts
                WHERE guild_id=$1 AND user_id=$2
                RETURNING id
                """,
                guild_id,
                user_id,
            )
            deleted_tokens = await conn.fetch(
                """
                DELETE FROM verification_tokens
                WHERE guild_id=$1 AND user_id=$2
                RETURNING token_id
                """,
                guild_id,
                user_id,
            )
            deleted_antifraud = await conn.fetch(
                """
                DELETE FROM verification_antifraud_signals
                WHERE guild_id=$1 AND user_id=$2
                RETURNING id
                """,
                guild_id,
                user_id,
            )
            deleted_profile = await conn.fetchrow(
                """
                DELETE FROM verified_users
                WHERE guild_id=$1 AND user_id=$2
                RETURNING user_id
                """,
                guild_id,
                user_id,
            )
            return {
                "attempts": len(deleted_attempts),
                "tokens": len(deleted_tokens),
                "antifraud": len(deleted_antifraud),
                "profiles": int(deleted_profile is not None),
            }


async def _purge_expired_verification_data(conn):
    deleted_attempts = await conn.fetchval(
        """
        WITH deleted AS (
            DELETE FROM verification_attempts
            WHERE retention_until <= CURRENT_TIMESTAMP
            RETURNING id
        )
        SELECT COUNT(*) FROM deleted
        """
    )
    deleted_antifraud = await conn.fetchval(
        """
        WITH deleted AS (
            DELETE FROM verification_antifraud_signals
            WHERE expires_at <= CURRENT_TIMESTAMP
            RETURNING id
        )
        SELECT COUNT(*) FROM deleted
        """
    )
    deleted_tokens = await conn.fetchval(
        """
        WITH deleted AS (
            DELETE FROM verification_tokens
            WHERE expires_at <= CURRENT_TIMESTAMP
            RETURNING token_id
        )
        SELECT COUNT(*) FROM deleted
        """
    )
    return {
        "attempts": int(deleted_attempts or 0),
        "tokens": int(deleted_tokens or 0),
        "antifraud": int(deleted_antifraud or 0),
    }


async def purge_expired_verification_data():
    if bot_pool is None:
        return {"attempts": 0, "tokens": 0, "antifraud": 0}
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            return await _purge_expired_verification_data(conn)


async def get_verified_users_count(guild_id):
    async with bot_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM verified_users WHERE guild_id=$1",
            guild_id,
        )


async def get_verified_users_page(guild_id, limit, offset):
    async with bot_pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT
                users.guild_id,
                users.user_id,
                users.first_verified_at,
                users.last_verified_at,
                users.last_country_code,
                users.status,
                latest.risk_score,
                latest.risk_level,
                latest.created_at AS latest_attempt_at
            FROM verified_users AS users
            LEFT JOIN LATERAL (
                SELECT risk_score, risk_level, created_at
                FROM verification_attempts
                WHERE guild_id=users.guild_id
                  AND user_id=users.user_id
                  AND decision='approved'
                  AND role_granted=TRUE
                ORDER BY created_at DESC
                LIMIT 1
            ) AS latest ON TRUE
            WHERE users.guild_id=$1
            ORDER BY users.last_verified_at DESC, users.user_id ASC
            LIMIT $2 OFFSET $3
            """,
            guild_id,
            limit,
            offset,
        )


async def get_all_registros():
    async with bot_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM registros ORDER BY updated_at DESC")


async def get_by_external_id(external_id: str):
    async with bot_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM registros WHERE external_id=$1 ORDER BY updated_at DESC",
            external_id
        )


async def list_event_catalog(guild_id: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM event_catalog WHERE guild_id=$1 ORDER BY name ASC",
            guild_id,
        )


async def add_event_catalog(
    guild_id: int,
    name: str,
    name_key: str,
    created_by: int,
    max_items: int,
):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", guild_id)
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM event_catalog WHERE guild_id=$1",
                guild_id,
            )
            if count >= max_items:
                return "full", None

            duplicate = await conn.fetchval("""
                SELECT EXISTS(
                    SELECT 1 FROM event_catalog
                    WHERE guild_id=$1 AND name_key=$2
                )
            """, guild_id, name_key)
            if duplicate:
                return "duplicate", None

            row = await conn.fetchrow("""
                INSERT INTO event_catalog (guild_id, name, name_key, created_by)
                VALUES ($1, $2, $3, $4)
                RETURNING *
            """, guild_id, name, name_key, created_by)
            return "created", row


async def remove_event_catalog(guild_id: int, catalog_event_id: int):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            active = await conn.fetchval("""
                SELECT EXISTS(
                    SELECT 1
                    FROM event_instances
                    WHERE guild_id=$1
                      AND catalog_event_id=$2
                      AND status IN ('open', 'closed')
                )
            """, guild_id, catalog_event_id)
            if active:
                return "active", None

            row = await conn.fetchrow("""
                DELETE FROM event_catalog
                WHERE guild_id=$1 AND id=$2
                RETURNING *
            """, guild_id, catalog_event_id)
            return ("deleted", row) if row else ("missing", None)


async def get_active_event(guild_id: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT *
            FROM event_instances
            WHERE guild_id=$1 AND status IN ('open', 'closed')
            ORDER BY id DESC
            LIMIT 1
        """, guild_id)


async def create_active_event(
    guild_id: int,
    catalog_event_id: int,
    participant_limit: int,
    registration_channel_id: int,
    created_by: int,
):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", guild_id)
            active = await conn.fetchrow("""
                SELECT * FROM event_instances
                WHERE guild_id=$1 AND status IN ('open', 'closed')
                LIMIT 1
            """, guild_id)
            if active:
                return "active", active

            catalog_event = await conn.fetchrow("""
                SELECT * FROM event_catalog
                WHERE guild_id=$1 AND id=$2
            """, guild_id, catalog_event_id)
            if not catalog_event:
                return "missing", None

            row = await conn.fetchrow("""
                INSERT INTO event_instances (
                    guild_id, catalog_event_id, event_name, participant_limit,
                    status, registration_channel_id, created_by
                )
                VALUES ($1, $2, $3, $4, 'open', $5, $6)
                RETURNING *
            """, guild_id, catalog_event_id, catalog_event["name"], participant_limit,
                 registration_channel_id, created_by)
            return "created", row


async def set_event_message(event_id: int, message_id: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow("""
            UPDATE event_instances
            SET registration_message_id=$2
            WHERE id=$1
            RETURNING *
        """, event_id, message_id)


async def delete_event(event_id: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow(
            "DELETE FROM event_instances WHERE id=$1 RETURNING *",
            event_id,
        )


async def close_active_event(guild_id: int):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", guild_id)
            return await conn.fetchrow("""
                UPDATE event_instances
                SET status='closed', closed_at=CURRENT_TIMESTAMP
                WHERE guild_id=$1 AND status='open'
                RETURNING *
            """, guild_id)


async def get_event_user(guild_id: int, user_id: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT * FROM event_users WHERE guild_id=$1 AND user_id=$2
        """, guild_id, user_id)


async def get_event_user_count(guild_id: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM event_users WHERE guild_id=$1",
            guild_id,
        )


async def get_event_users_page(guild_id: int, limit: int, offset: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetch("""
            SELECT * FROM event_users
            WHERE guild_id=$1
            ORDER BY updated_at DESC, user_id ASC
            LIMIT $2 OFFSET $3
        """, guild_id, limit, offset)


async def get_event_blacklist(guild_id: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetch("""
            SELECT * FROM event_blacklist
            WHERE guild_id=$1
            ORDER BY created_at ASC, external_id ASC
        """, guild_id)


async def add_event_blacklist(
    guild_id: int,
    user_id: int,
    external_id: str,
    created_by: int,
    max_items: int,
):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", guild_id)
            existing = await conn.fetchrow("""
                SELECT * FROM event_blacklist
                WHERE guild_id=$1 AND (external_id=$2 OR user_id=$3)
            """, guild_id, external_id, user_id)
            if existing:
                return "duplicate", existing

            count = await conn.fetchval(
                "SELECT COUNT(*) FROM event_blacklist WHERE guild_id=$1",
                guild_id,
            )
            if count >= max_items:
                return "full", None

            row = await conn.fetchrow("""
                INSERT INTO event_blacklist (guild_id, user_id, external_id, created_by)
                VALUES ($1, $2, $3, $4)
                RETURNING *
            """, guild_id, user_id, external_id, created_by)
            return "created", row


async def remove_event_blacklist(guild_id: int, external_id: str):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", guild_id)
            return await conn.fetchrow("""
                DELETE FROM event_blacklist
                WHERE guild_id=$1 AND external_id=$2
                RETURNING *
            """, guild_id, external_id)


async def is_event_blacklisted(
    guild_id: int,
    external_id: str | None,
    user_id: int,
) -> bool:
    return await get_event_blacklist_match(guild_id, external_id, user_id) is not None


async def get_event_blacklist_match(
    guild_id: int,
    external_id: str | None,
    user_id: int,
):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT * FROM event_blacklist
            WHERE guild_id=$1
              AND (external_id=$2 OR user_id=$3)
            ORDER BY (user_id=$3) DESC
            LIMIT 1
        """, guild_id, external_id, user_id)


async def update_event_user_profile(
    guild_id: int,
    user_id: int,
    nickname: str,
    external_id: str,
    country: str,
):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", guild_id)
            profile = await conn.fetchrow("""
                SELECT * FROM event_users
                WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id)
            if not profile:
                return "missing", None

            duplicate = await conn.fetchval("""
                SELECT EXISTS(
                    SELECT 1 FROM event_users
                    WHERE guild_id=$1 AND external_id=$2 AND user_id<>$3
                )
            """, guild_id, external_id, user_id)
            if duplicate:
                return "external_id_duplicate", None

            updated = await conn.fetchrow("""
                UPDATE event_users
                SET nickname=$3,
                    external_id=$4,
                    country=$5,
                    updated_at=CURRENT_TIMESTAMP
                WHERE guild_id=$1 AND user_id=$2
                RETURNING *
            """, guild_id, user_id, nickname, external_id, country)
            return "updated", updated


async def delete_event_user_profile(guild_id: int, user_id: int):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", guild_id)
            profile = await conn.fetchrow("""
                SELECT * FROM event_users
                WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id)
            if not profile:
                return "missing", None

            affected_events = await conn.fetch("""
                SELECT
                    e.id,
                    e.event_name,
                    e.participant_limit
                FROM event_registrations AS r
                JOIN event_instances AS e ON e.id=r.event_id
                WHERE r.guild_id=$1 AND r.user_id=$2
                FOR UPDATE OF e
            """, guild_id, user_id)

            await conn.execute("""
                DELETE FROM event_registrations
                WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id)

            for event in affected_events:
                event_id = event["id"]
                participant_limit = event["participant_limit"]
                await conn.execute("""
                    UPDATE event_registrations
                    SET position=-position
                    WHERE event_id=$1
                """, event_id)
                await conn.execute("""
                    WITH ordered AS (
                        SELECT
                            user_id,
                            ROW_NUMBER() OVER (
                                ORDER BY -position ASC, registered_at ASC, user_id ASC
                            )::INTEGER AS new_position
                        FROM event_registrations
                        WHERE event_id=$1
                    )
                    UPDATE event_registrations AS r
                    SET
                        position=ordered.new_position,
                        is_overflow=(
                            $2::INTEGER > 0
                            AND ordered.new_position > $2::INTEGER
                        )
                    FROM ordered
                    WHERE r.event_id=$1 AND r.user_id=ordered.user_id
                """, event_id, participant_limit)

            deleted = await conn.fetchrow("""
                DELETE FROM event_users
                WHERE guild_id=$1 AND user_id=$2
                RETURNING *
            """, guild_id, user_id)
            return "deleted", {
                "profile": deleted,
                "affected_events": affected_events,
            }


async def get_event_registration(event_id: int, user_id: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT * FROM event_registrations
            WHERE event_id=$1 AND user_id=$2
        """, event_id, user_id)


async def get_event_participant_count(event_id: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM event_registrations WHERE event_id=$1",
            event_id,
        )


async def register_event_participant(
    guild_id: int,
    user_id: int,
    discord_tag: str,
    nickname: str | None,
    external_id: str | None,
    country: str | None,
):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", guild_id)
            event = await conn.fetchrow("""
                SELECT * FROM event_instances
                WHERE guild_id=$1 AND status IN ('open', 'closed')
                LIMIT 1
                FOR UPDATE
            """, guild_id)
            if not event:
                return "no_event", None
            if event["status"] != "open":
                return "closed", None

            existing_registration = await conn.fetchrow("""
                SELECT * FROM event_registrations
                WHERE event_id=$1 AND user_id=$2
            """, event["id"], user_id)
            if existing_registration:
                return "duplicate", existing_registration

            profile = await conn.fetchrow("""
                SELECT * FROM event_users WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id)
            effective_external_id = (
                profile["external_id"] if profile is not None else external_id
            )
            blacklisted = await conn.fetchval("""
                SELECT EXISTS(
                    SELECT 1 FROM event_blacklist
                    WHERE guild_id=$1
                      AND (external_id=$2 OR user_id=$3)
                )
            """, guild_id, effective_external_id, user_id)
            if blacklisted:
                return "blacklisted", None

            if profile is None:
                duplicate_external_id = await conn.fetchval("""
                    SELECT EXISTS(
                        SELECT 1 FROM event_users
                        WHERE guild_id=$1 AND external_id=$2
                    )
                """, guild_id, external_id)
                if duplicate_external_id:
                    return "external_id_duplicate", None

                profile = await conn.fetchrow("""
                    INSERT INTO event_users (
                        guild_id, user_id, discord_tag, nickname, external_id, country
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING *
                """, guild_id, user_id, discord_tag, nickname, external_id, country)
            else:
                await conn.execute("""
                    UPDATE event_users
                    SET discord_tag=$3, updated_at=CURRENT_TIMESTAMP
                    WHERE guild_id=$1 AND user_id=$2
                """, guild_id, user_id, discord_tag)

            participant_count = await conn.fetchval(
                "SELECT COUNT(*) FROM event_registrations WHERE event_id=$1",
                event["id"],
            )
            position = participant_count + 1
            is_overflow = (
                event["participant_limit"] > 0
                and position > event["participant_limit"]
            )
            registration = await conn.fetchrow("""
                INSERT INTO event_registrations (
                    event_id, guild_id, user_id, position, is_overflow
                )
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
            """, event["id"], guild_id, user_id, position, is_overflow)
            return "registered", {
                "event": event,
                "profile": profile,
                "registration": registration,
            }


async def get_event_participants(event_id: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetch("""
            SELECT
                r.position,
                r.is_overflow,
                r.registered_at,
                u.user_id,
                u.discord_tag,
                u.nickname,
                u.external_id,
                u.country
            FROM event_registrations AS r
            JOIN event_users AS u
              ON u.guild_id=r.guild_id AND u.user_id=r.user_id
            WHERE r.event_id=$1
            ORDER BY r.position ASC
        """, event_id)


async def add_scheduled_post(guild_id, channel_id, title, content, attachment_urls, scheduled_at, author_id, thread_name=None):
    async with bot_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO scheduled_posts (guild_id, channel_id, title, content, attachment_urls, scheduled_at, author_id, thread_name)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
        """, guild_id, channel_id, title, content, attachment_urls, scheduled_at, author_id, thread_name)
        return row["id"] if row else None


async def get_scheduled_posts(guild_id):
    async with bot_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM scheduled_posts WHERE guild_id=$1 ORDER BY scheduled_at ASC", guild_id)


async def delete_scheduled_post(post_id):
    async with bot_pool.acquire() as conn:
        await conn.execute("DELETE FROM scheduled_posts WHERE id=$1", post_id)


async def update_scheduled_post(post_id, title, content, attachment_urls, scheduled_at, thread_name=None):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow("""
            UPDATE scheduled_posts
            SET title=$2,
                content=$3,
                attachment_urls=$4,
                scheduled_at=$5,
                thread_name=$6
            WHERE id=$1
            RETURNING *
        """, post_id, title, content, attachment_urls, scheduled_at, thread_name)


async def get_expired_posts():
    async with bot_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM scheduled_posts WHERE scheduled_at <= NOW()")


async def load_scheduled_posts(guild_id=None):
    if bot_pool is None:
        print("Cache de agendamientos no cargado: DB no disponible.")
        return []

    async with bot_pool.acquire() as conn:
        if guild_id:
            rows = await conn.fetch(
                "SELECT * FROM scheduled_posts WHERE guild_id=$1 ORDER BY scheduled_at ASC",
                guild_id
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM scheduled_posts ORDER BY scheduled_at ASC"
            )

    posts = [dict(r) for r in rows]
    print(f"📦 Cache de agendamientos cargado: {len(posts)} post(s) pendiente(s).")
    return posts
