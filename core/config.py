import os
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_PATH)

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

STAFF_ROLES = [
    role.strip()
    for role in os.getenv("STAFF_ROLES", "Admin,Moderador ES,Equipo de Eventos").split(",")
    if role.strip()
]

raw_url = os.getenv("DATABASE_URL")
if raw_url and raw_url.startswith("postgres://"):
    DATABASE_URL = raw_url.replace("postgres://", "postgresql://", 1)
else:
    DATABASE_URL = raw_url

if not TOKEN:
    raise ValueError("No se encontró DISCORD_TOKEN en el archivo .env")

if not GUILD_ID:
    print("⚠️ Aviso: no se encontró GUILD_ID en las variables de entorno.")

R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET = os.getenv("R2_BUCKET")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")

def env_int(name: str, default: int = 0) -> int:
    value = os.getenv(name, str(default))
    return int(value) if value.isdigit() else default


# Registro y gestion de eventos.
EVENT_VERIFIED_ROLE_ID = 1409401827065204786
EVENT_PARTICIPANT_ROLE_ID = 1528613617090429009
EVENT_VERIFICATION_CHANNEL_ID = 1491206205648015360
EVENT_ALLOWED_CHANNEL_IDS = frozenset({
    1242886260461142116,
    1528637027619180644,
})
EVENT_PARTICIPANT_LIMITS = (0, 10, 15, 20)
EVENT_CATALOG_MAX_ITEMS = 25


TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True

DB_NO_DISPONIBLE = "⚠️ La base de datos no está disponible temporalmente. Intenta de nuevo más tarde."


def is_staff(interaction):
    perms = interaction.user.guild_permissions
    if perms.administrator or perms.manage_guild or perms.moderate_members:
        return True
    for role in interaction.user.roles:
        if role.name in STAFF_ROLES:
            return True
    return False


def require_staff():
    async def predicate(interaction):
        if not is_staff(interaction):
            await interaction.response.send_message("⛔ No tienes permisos.", ephemeral=False)
            return False
        return True
    return app_commands.check(predicate)


async def db_unavailable(interaction: discord.Interaction) -> bool:
    from core import database

    if database.bot_pool is None:
        await interaction.response.send_message(DB_NO_DISPONIBLE, ephemeral=False)
        return True
    return False
