import os
import unicodedata
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

_owner = os.getenv("OWNER_ID", "0")
OWNER_ID = int(_owner) if _owner.isdigit() else 0

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

REQUIRED_REGISTER_ROLE_ID = 1409401827065204786
VERIFY_CHANNEL_ID = 1200056404208263219

BLACKLIST_ALERT_CHANNEL_ID = 1466994193263231250
BLACKLIST_ALERT_ROLE_ID = 1241815781453594678

TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True

DB_NO_DISPONIBLE = "⚠️ La base de datos no está disponible temporalmente. Intenta de nuevo más tarde."


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    nfd_form = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd_form if unicodedata.category(c) != "Mn")


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


def has_required_register_role(member: discord.Member | discord.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return any(role.id == REQUIRED_REGISTER_ROLE_ID for role in member.roles)


async def db_unavailable(interaction: discord.Interaction) -> bool:
    from core import database

    if database.bot_pool is None:
        await interaction.response.send_message(DB_NO_DISPONIBLE, ephemeral=False)
        return True
    return False
