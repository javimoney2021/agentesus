import asyncpg

from core.config import DATABASE_URL


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
                CREATE TABLE IF NOT EXISTS blacklist_ids (
                    external_id TEXT PRIMARY KEY,
                    nickname TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        print("✅ Conexion a PostgreSQL exitosa y tablas verificadas.")
        async with bot_pool.acquire() as conn:
            await conn.execute("""
                ALTER TABLE scheduled_posts ADD COLUMN IF NOT EXISTS thread_name TEXT DEFAULT NULL;
            """)
    except Exception as e:
        print(f"❌ Error conectando a la DB: {e}")


async def upsert_registro(user, nickname, external_id):
    async with bot_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO registros (user_id, discord_tag, nickname, external_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT(user_id) DO UPDATE SET
                discord_tag = excluded.discord_tag,
                nickname = excluded.nickname,
                external_id = excluded.external_id,
                updated_at = CURRENT_TIMESTAMP
        """, user.id, str(user), nickname, external_id)


async def get_registro(user_id):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM registros WHERE user_id=$1", user_id)


async def get_all_registros():
    async with bot_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM registros ORDER BY updated_at DESC")


async def delete_registro(user_id: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow("""
            DELETE FROM registros
            WHERE user_id=$1
            RETURNING user_id, discord_tag, nickname, external_id
        """, user_id)


async def get_by_external_id(external_id: str):
    async with bot_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM registros WHERE external_id=$1 ORDER BY updated_at DESC",
            external_id
        )


async def add_blacklist_id(external_id: str, nickname: str | None = None):
    async with bot_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO blacklist_ids (external_id, nickname)
            VALUES ($1, $2)
            ON CONFLICT (external_id) DO UPDATE SET
                nickname = COALESCE(EXCLUDED.nickname, blacklist_ids.nickname),
                updated_at = CURRENT_TIMESTAMP
        """, external_id, nickname)


async def get_blacklist_id(external_id: str):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM blacklist_ids WHERE external_id=$1",
            external_id
        )


async def get_all_blacklist_ids():
    async with bot_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM blacklist_ids ORDER BY created_at ASC, external_id ASC"
        )


async def delete_blacklist_id(external_id: str):
    async with bot_pool.acquire() as conn:
        return await conn.fetchrow("""
            DELETE FROM blacklist_ids
            WHERE external_id=$1
            RETURNING external_id, nickname
        """, external_id)


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
