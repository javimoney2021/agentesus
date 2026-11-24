import asyncio
from core.activity import record

import asyncpg

from core.config import DATABASE_URL


bot_pool = None
_db_ready = False
_heartbeat_lock = asyncio.Lock()
_health_failures = 0
_POOL_REBUILD_AFTER_FAILURES = 2


def is_ready() -> bool:
    """La app solo debe consultar la DB si el pool existe y respondió."""
    return _db_ready and bot_pool is not None


async def _close_pool(pool) -> None:
    try:
        await asyncio.wait_for(pool.close(), timeout=10)
    except Exception:
        try:
            pool.terminate()
        except Exception:
            pass


async def init_db():
    global bot_pool, _db_ready
    new_pool = None
    if not DATABASE_URL:
        print("❌ Error conectando a la DB: falta DATABASE_URL.")
        _db_ready = False
        return False

    try:
        new_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            command_timeout=15,
            timeout=20,
        )
        async with new_pool.acquire() as conn:
            schema_ready = await conn.fetchval(
                "SELECT to_regclass('registros') IS NOT NULL"
            )

        previous_pool = bot_pool
        bot_pool = new_pool
        new_pool = None
        if previous_pool is not None:
            await _close_pool(previous_pool)

        _db_ready = True
        if schema_ready:
            print("✅ Conexion a PostgreSQL exitosa.")
        else:
            print(
                "⚠️ Conexion a PostgreSQL exitosa, pero falta el esquema. "
                "Ejecuta `python scripts/migrate.py`."
            )
        return True
    except Exception as e:
        _db_ready = False
        if new_pool is not None:
            await _close_pool(new_pool)
        print(f"Error conectando a PostgreSQL: {type(e).__name__}")
        return False


async def heartbeat_db() -> bool:
    """Comprueba PostgreSQL y reconstruye el pool tras fallos repetidos."""
    global bot_pool, _db_ready, _health_failures

    async with _heartbeat_lock:
        record('db.healthcheck')
        pool = bot_pool
        if pool is not None:
            try:
                async with pool.acquire(timeout=10) as conn:
                    await conn.fetchval("SELECT 1", timeout=10)
            except Exception as error:
                _health_failures += 1
                _db_ready = False
                record('db.healthcheck_failed')
                print(
                    "PostgreSQL healthcheck: "
                    f"{type(error).__name__}; fallo {_health_failures}/"
                    f"{_POOL_REBUILD_AFTER_FAILURES}."
                )
                if _health_failures < _POOL_REBUILD_AFTER_FAILURES:
                    return False
                print("🔄 Reconstruyendo el pool de PostgreSQL...")
                await _close_pool(pool)
                bot_pool = None
            else:
                _health_failures = 0
                _db_ready = True
                return True

        print("🔄 Intentando reconectar PostgreSQL...")
        record('db.connect_attempt')
        _health_failures = 0
        return await init_db()


async def get_registro(user_id):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM registros WHERE user_id=$1", user_id)


async def get_all_registros():
    async with bot_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM registros ORDER BY updated_at DESC")


async def get_by_external_id(external_id: str, limit: int = 10):
    async with bot_pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT * FROM registros
            WHERE external_id=$1
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            external_id,
            limit,
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
        async with conn.transaction():
            event = await conn.fetchrow(
                "DELETE FROM event_instances WHERE id=$1 RETURNING *",
                event_id,
            )
            if event:
                await conn.execute("""
                    DELETE FROM event_users AS users
                    WHERE users.guild_id=$1
                      AND users.retain_profile=FALSE
                      AND NOT EXISTS (
                          SELECT 1
                          FROM event_registrations AS registrations
                          WHERE registrations.guild_id=users.guild_id
                            AND registrations.user_id=users.user_id
                      )
                """, event["guild_id"])
            return event


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


async def register_event_participant(
    guild_id: int,
    user_id: int,
    discord_tag: str,
    nickname: str | None,
    external_id: str | None,
    country: str | None,
    retain_profile: bool = True,
):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", guild_id)
            event = await conn.fetchrow("""
                SELECT
                    instances.*,
                    (
                        SELECT cooldowns.can_register_at
                        FROM data_deletion_cooldowns AS cooldowns
                        WHERE cooldowns.guild_id=$1
                          AND cooldowns.user_id=$2
                          AND cooldowns.can_register_at>CURRENT_TIMESTAMP
                    ) AS cooldown_until
                FROM event_instances AS instances
                WHERE instances.guild_id=$1
                  AND instances.status IN ('open', 'closed')
                ORDER BY instances.id DESC
                LIMIT 1
                FOR UPDATE
            """, guild_id, user_id)
            if not event:
                return "no_event", None
            if event["status"] != "open":
                return "closed", None
            if event["cooldown_until"] is not None:
                return "cooldown", {"can_register_at": event["cooldown_until"]}

            registration_state = await conn.fetchrow("""
                SELECT
                    COUNT(*)::INTEGER AS participant_count,
                    COUNT(*) FILTER (WHERE user_id=$2) > 0 AS already_registered
                FROM event_registrations
                WHERE event_id=$1
            """, event["id"], user_id)
            if registration_state["already_registered"]:
                return "duplicate", None

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
                profile = await conn.fetchrow("""
                    INSERT INTO event_users (
                        guild_id, user_id, discord_tag, nickname, external_id, country,
                        retain_profile
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (guild_id, external_id) DO NOTHING
                    RETURNING *
                """, guild_id, user_id, discord_tag, nickname, external_id, country,
                     retain_profile)
                if profile is None:
                    return "external_id_duplicate", None
            else:
                await conn.execute("""
                    UPDATE event_users
                    SET discord_tag=$3, updated_at=CURRENT_TIMESTAMP
                    WHERE guild_id=$1 AND user_id=$2
                """, guild_id, user_id, discord_tag)

            position = registration_state["participant_count"] + 1
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


async def get_registration_context(
    guild_id: int,
    user_id: int,
    external_id: str | None = None,
):
    """Reúne en una sola conexión los datos que necesita el botón Registrarse."""
    async with bot_pool.acquire() as conn:
        cooldown = await conn.fetchrow("""
            SELECT requested_at, can_register_at
            FROM data_deletion_cooldowns
            WHERE guild_id=$1 AND user_id=$2
              AND can_register_at>CURRENT_TIMESTAMP
        """, guild_id, user_id)
        profile = await conn.fetchrow("""
            SELECT * FROM event_users WHERE guild_id=$1 AND user_id=$2
        """, guild_id, user_id)
        event = await conn.fetchrow("""
            SELECT * FROM event_instances
            WHERE guild_id=$1 AND status IN ('open', 'closed')
            ORDER BY id DESC
            LIMIT 1
        """, guild_id)
        if event is None:
            return {
                "cooldown": cooldown,
                "profile": profile,
                "event": None,
                "registration": None,
                "blacklist": None,
                "participant_count": 0,
            }

        registration = await conn.fetchrow("""
            SELECT * FROM event_registrations
            WHERE event_id=$1 AND user_id=$2
        """, event["id"], user_id)
        effective_external_id = (
            profile["external_id"] if profile is not None else external_id
        )
        blacklist = await conn.fetchrow("""
            SELECT * FROM event_blacklist
            WHERE guild_id=$1
              AND (external_id=$2 OR user_id=$3)
            ORDER BY (user_id=$3) DESC
            LIMIT 1
        """, guild_id, effective_external_id, user_id)
        participant_count = await conn.fetchval(
            "SELECT COUNT(*) FROM event_registrations WHERE event_id=$1",
            event["id"],
        )
        return {
            "cooldown": cooldown,
            "profile": profile,
            "event": event,
            "registration": registration,
            "blacklist": blacklist,
            "participant_count": participant_count,
        }


async def cleanup_expired_data_deletion_cooldowns():
    if not is_ready():
        return
    async with bot_pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM data_deletion_cooldowns
            WHERE can_register_at<=CURRENT_TIMESTAMP
        """)


async def delete_user_data_and_start_cooldown(
    guild_id: int,
    user_id: int,
    cooldown_days: int,
):
    async with bot_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", guild_id)
            affected_events = await conn.fetch("""
                SELECT DISTINCT event_id
                FROM event_registrations
                WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id)
            registration_result = await conn.execute("""
                DELETE FROM event_registrations
                WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id)

            for event in affected_events:
                await conn.execute("""
                    UPDATE event_registrations
                    SET position=-position
                    WHERE event_id=$1
                """, event["event_id"])
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
                    UPDATE event_registrations AS registrations
                    SET position=ordered.new_position,
                        is_overflow=(
                            instances.participant_limit > 0
                            AND ordered.new_position > instances.participant_limit
                        )
                    FROM ordered, event_instances AS instances
                    WHERE registrations.event_id=$1
                      AND registrations.user_id=ordered.user_id
                      AND instances.id=$1
                """, event["event_id"])

            profile_result = await conn.execute("""
                DELETE FROM event_users
                WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id)
            history_result = await conn.execute(
                "DELETE FROM registros WHERE user_id=$1",
                user_id,
            )
            deleted_count = sum(
                int(result.rsplit(" ", 1)[-1])
                for result in (registration_result, profile_result, history_result)
            )
            if deleted_count:
                cooldown = await conn.fetchrow("""
                    INSERT INTO data_deletion_cooldowns (
                        guild_id, user_id, requested_at, can_register_at
                    )
                    VALUES (
                        $1, $2, CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP + ($3 * INTERVAL '1 day')
                    )
                    ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET requested_at=EXCLUDED.requested_at,
                        can_register_at=EXCLUDED.can_register_at
                    RETURNING requested_at, can_register_at
                """, guild_id, user_id, cooldown_days)
            else:
                cooldown = None

            return {
                "deleted_count": deleted_count,
                "affected_event_ids": [row["event_id"] for row in affected_events],
                "cooldown": cooldown,
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
