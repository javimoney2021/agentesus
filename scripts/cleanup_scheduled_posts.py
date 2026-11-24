"""Elimina manualmente los restos del sistema de publicaciones agendadas.

La migración `migrations/0002_remove_scheduled_posts.sql` ya realiza esta
limpieza en la fase release. Este script queda para ejecuciones manuales:

    python scripts/cleanup_scheduled_posts.py

No importa core.config para no requerir DISCORD_TOKEN en este proceso.
"""
import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


async def cleanup() -> None:
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL no configurada; se omite la limpieza.")
        return

    connection = await asyncpg.connect(
        DATABASE_URL,
        timeout=20,
        command_timeout=30,
    )
    try:
        await connection.execute("DROP TABLE IF EXISTS scheduled_posts")
        print("✅ Tabla scheduled_posts eliminada (si existía).")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(cleanup())
