"""Aplica las migraciones SQL pendientes antes de iniciar el worker.

Uso:
    python scripts/migrate.py

Es idempotente: cada archivo se registra en `schema_migrations` y solo se
ejecuta una vez. No importa core.config para no exigir DISCORD_TOKEN.
"""
import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = BASE_DIR / "migrations"

load_dotenv(dotenv_path=BASE_DIR / ".env")


def database_url() -> str | None:
    value = os.getenv("DATABASE_URL")
    if value and value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql://", 1)
    return value


async def run_migrations(url: str | None = None) -> int:
    url = url or database_url()
    if not url:
        raise RuntimeError("DATABASE_URL no configurada; no se pueden aplicar migraciones.")

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("⚠️ No hay archivos de migración.")
        return 0

    connection = await asyncpg.connect(url, timeout=20, command_timeout=60)
    applied = 0
    try:
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await connection.execute(
            "SELECT pg_advisory_lock(hashtext('agentesus:schema_migrations'))"
        )
        for path in files:
            version = path.stem
            exists = await connection.fetchval(
                "SELECT 1 FROM schema_migrations WHERE version=$1",
                version,
            )
            if exists:
                continue

            sql = path.read_text(encoding="utf-8")
            async with connection.transaction():
                await connection.execute(sql)
                await connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)",
                    version,
                )
            applied += 1
            print(f"✅ Migración aplicada: {version}")
    finally:
        try:
            await connection.execute(
                "SELECT pg_advisory_unlock(hashtext('agentesus:schema_migrations'))"
            )
        except Exception:
            pass
        await connection.close()

    if applied == 0:
        print("✅ Base de datos al día.")
    else:
        print(f"✅ {applied} migración(es) aplicada(s).")
    return applied


if __name__ == "__main__":
    asyncio.run(run_migrations())
