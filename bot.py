print("🌲º'º [DEBUG] ESTE bot.py SE ESTÁ EJECUTANDO º'º🌲")

import os
import io
import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

STAFF_ROLES = [
    r.strip()
    for r in os.getenv("STAFF_ROLES", "Admin,Moderador ES").split(",")
    if r.strip()
]

_owner = os.getenv("OWNER_ID", "0")
OWNER_ID = int(_owner) if _owner.isdigit() else 0

raw_url = os.getenv("DATABASE_URL")
if raw_url and raw_url.startswith("postgres://"):
    DATABASE_URL = raw_url.replace("postgres://", "postgresql://", 1)
else:
    DATABASE_URL = raw_url

COOLDOWN_MESSAGES = 4
partial_bucket = {}

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True


# Pool global para evitar saturar conexiones en Railway
bot_pool = None


# ---------- BASE DE DATOS ----------
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
                CREATE TABLE IF NOT EXISTS permitted_channels (
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    PRIMARY KEY (guild_id, channel_id)
                );
                CREATE TABLE IF NOT EXISTS msg_counts (
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    counted INT NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, user_id)
                );
            """)
        print("✅ Conexión a PostgreSQL exitosa y tablas verificadas.")
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
        await conn.execute("DELETE FROM registros WHERE user_id=$1", user_id)


async def permit_channel(guild_id: int, channel_id: int):
    async with bot_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO permitted_channels (guild_id, channel_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            guild_id, channel_id
        )


async def unpermit_channel(guild_id: int, channel_id: int):
    async with bot_pool.acquire() as conn:
        await conn.execute("DELETE FROM permitted_channels WHERE guild_id=$1 AND channel_id=$2", guild_id, channel_id)


async def is_channel_permitted(guild_id: int, channel_id: int) -> bool:
    if bot_pool is None:
        return False
    async with bot_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM permitted_channels WHERE guild_id=$1 AND channel_id=$2",
            guild_id, channel_id
        )
        return row is not None


async def reset_table(guild_id: int):
    async with bot_pool.acquire() as conn:
        await conn.execute("DELETE FROM msg_counts WHERE guild_id=$1", guild_id)


async def reset_user_count(guild_id: int, user_id: int):
    async with bot_pool.acquire() as conn:
        await conn.execute("DELETE FROM msg_counts WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)


async def add_counted_points(guild_id: int, user_id: int, points: int):
    if bot_pool is None:
        return
    async with bot_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO msg_counts (guild_id, user_id, counted)
            VALUES ($1, $2, $3)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                counted = msg_counts.counted + excluded.counted,
                updated_at = CURRENT_TIMESTAMP
        """, guild_id, user_id, points)


async def get_leaderboard(guild_id: int, limit: int, offset: int):
    async with bot_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT user_id, counted FROM msg_counts WHERE guild_id=$1 ORDER BY counted DESC, updated_at DESC LIMIT $2 OFFSET $3",
            guild_id, limit, offset
        )


async def get_leaderboard_total(guild_id: int) -> int:
    async with bot_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) FROM msg_counts WHERE guild_id=$1", guild_id)
        return int(row[0]) if row else 0


async def get_by_external_id(external_id: str):
    async with bot_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM registros WHERE external_id=$1 ORDER BY updated_at DESC",
            external_id
        )


async def dm_owner(text: str):
    if not OWNER_ID:
        return
    try:
        user = await bot.fetch_user(OWNER_ID)
        await user.send(text)
    except:
        pass


# ---------- PERMISOS ----------
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
            await interaction.response.send_message("⛔ No tienes permisos.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


# ---------- BOT (FIX SYNC) ----------
class MyBot(commands.Bot):
    async def setup_hook(self):
        await init_db()

        guild_id = os.getenv("GUILD_ID")
        if not guild_id:
            print("❌ Falta GUILD_ID en Railway Variables. No sincronizo comandos.")
            return

        guild = discord.Object(id=int(guild_id))

        # Forzar que los slash commands se publiquen en el servidor (dev rápido)
        self.tree.copy_global_to(guild=guild)

        synced = await self.tree.sync(guild=guild)
        print(f"✅ Comandos sincronizados en tu servidor (guild): {len(synced)}")

        # Si vuelve a salir 0, imprimimos lo que realmente hay cargado
        if len(synced) == 0:
            cmds = [c.name for c in self.tree.get_commands()]
            print("🧾 Comandos cargados en el árbol:", cmds)
            print("👉 Si esta lista sale vacía, tus comandos no están registrándose antes del sync.")


bot = MyBot(command_prefix="_", intents=intents)


# ---------- EVENTOS ----------
@bot.event
async def on_ready():
    print(f"🤖 Bot conectado como {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    if bot_pool is None:
        return

    if await is_channel_permitted(message.guild.id, message.channel.id):
        key = (message.guild.id, message.author.id)
        partial_bucket[key] = partial_bucket.get(key, 0) + 1

        if partial_bucket[key] >= COOLDOWN_MESSAGES:
            partial_bucket[key] = 0
            await add_counted_points(message.guild.id, message.author.id, 1)

    await bot.process_commands(message)


# ---------- COMANDOS ----------
@bot.tree.command(name="registrar", description="Registra Nickname e ID Espacial")
async def registrar(interaction: discord.Interaction, nickname: str, external_id: str):
    existing = await get_registro(interaction.user.id)
    if existing:
        return await interaction.response.send_message(
            embed=discord.Embed(description="🚫 **YA TE ENCUENTRAS REGISTRADO**", color=discord.Color.red()),
            ephemeral=True
        )
    await upsert_registro(interaction.user, nickname.strip(), external_id.strip())
    embed = discord.Embed(
        title="✅ Registro Completado",
        description=f"Nick: `{nickname}`\nID: `{external_id}`",
        color=discord.Color.green()
    )
    await dm_owner(f"🆕 Nuevo registro: {interaction.user} | Nick: {nickname} | ID: {external_id}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="consultar", description="(Staff) Consultar Registro")
@require_staff()
async def consultar(interaction: discord.Interaction, usuario: discord.Member):
    row = await get_registro(usuario.id)
    if not row:
        return await interaction.response.send_message("No registrado.", ephemeral=True)
    embed = discord.Embed(title="📄 Registro", color=discord.Color.blue())
    embed.set_thumbnail(url=usuario.display_avatar.url)
    embed.add_field(name="Usuario", value=row['discord_tag'], inline=False)
    embed.add_field(name="Nick", value=row['nickname'], inline=True)
    embed.add_field(name="ID", value=row['external_id'], inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="userid", description="(Staff) Busca por ID Espacial")
@require_staff()
async def userid(interaction: discord.Interaction, id_espacial: str):
    rows = await get_by_external_id(id_espacial.strip())
    if not rows:
        return await interaction.response.send_message(f"ℹ️ No encontré registros con ID `{id_espacial}`.")
    embed = discord.Embed(title="🔎 Resultados", color=discord.Color.blurple())
    for r in rows[:10]:
        embed.add_field(
            name=r['discord_tag'],
            value=f"Nick: `{r['nickname']}`\nDiscord ID: `{r['user_id']}`",
            inline=False
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="editar", description="(Staff) Editar Registro")
@require_staff()
async def editar(interaction: discord.Interaction, usuario: discord.Member, nickname: str, external_id: str):
    await upsert_registro(usuario, nickname.strip(), external_id.strip())
    await interaction.response.send_message("✏️ Registro Actualizado.")


@bot.tree.command(name="eliminar_registro", description="(Staff) Elimina registro")
@require_staff()
async def eliminar_registro(interaction: discord.Interaction, usuario: discord.Member):
    await delete_registro(usuario.id)
    await interaction.response.send_message(f"🗑️ Registro eliminado para **{usuario}**.")


@bot.tree.command(name="permitchannel", description="(Admin) Autoriza canal")
@require_staff()
async def permitchannel_cmd(interaction: discord.Interaction, canal: discord.TextChannel):
    await permit_channel(interaction.guild.id, canal.id)
    await interaction.response.send_message(f"✅ Canal autorizado: {canal.mention}", ephemeral=True)


@bot.tree.command(name="borrarchannel", description="(Admin) Desautoriza canal")
@require_staff()
async def borrarchannel_cmd(interaction: discord.Interaction, canal: discord.TextChannel):
    await unpermit_channel(interaction.guild.id, canal.id)
    await interaction.response.send_message(f"🗑️ Canal desautorizado: {canal.mention}", ephemeral=True)


@bot.tree.command(name="reset_tabla", description="(Staff) Resetea Ranking")
@require_staff()
async def reset_tabla_cmd(interaction: discord.Interaction):
    await reset_table(interaction.guild.id)
    await interaction.response.send_message("✅ Tabla reseteada.")


class TablaView(discord.ui.View):
    def __init__(self, guild_id: int, page: int = 0):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.page = page

    async def build_embed(self, interaction) -> discord.Embed:
        offset = self.page * 10
        rows = await get_leaderboard(self.guild_id, 10, offset)
        total = await get_leaderboard_total(self.guild_id)
        embed = discord.Embed(title="🏆 RANKING 🏆", color=discord.Color.blurple())
        if not rows:
            embed.description = "Sin datos aún."
            return embed
        lines = []
        for i, r in enumerate(rows, start=offset + 1):
            lines.append(f"**#{i}** — <@{r['user_id']}> — **{r['counted']}**")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Página {self.page + 1}")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction, button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=await self.build_embed(interaction), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        if (self.page + 1) * 10 < await get_leaderboard_total(self.guild_id):
            self.page += 1
        await interaction.response.edit_message(embed=await self.build_embed(interaction), view=self)


@bot.tree.command(name="tabla", description="Muestra el Ranking")
async def tabla_cmd(interaction: discord.Interaction):
    view = TablaView(interaction.guild.id)
    await interaction.response.send_message(embed=await view.build_embed(interaction), view=view)


class UserbaseView(discord.ui.View):
    def __init__(self, registros, page=0):
        super().__init__(timeout=120)
        self.registros = registros
        self.page = page

    async def build_embed(self):
        start = self.page * 10
        end = start + 10
        page_regs = self.registros[start:end]

        embed = discord.Embed(
            title="📋 Lista de Usuarios Registrados",
            color=discord.Color.green()
        )

        def esc(s: str) -> str:
            return discord.utils.escape_markdown(str(s))

        lines = []
        for i, r in enumerate(page_regs, start=start + 1):
            discord_tag = esc(r.get("discord_tag", "N/A"))
            nickname = esc(r.get("nickname", "N/A"))
            external_id = esc(r.get("external_id", "N/A"))
            lines.append(
                f"**{i}.** {discord_tag} | Nick=`{nickname}` | ID=`{external_id}`"
            )

        embed.description = "\n".join(lines) if lines else "Fin de la lista."
        return embed

    @discord.ui.button(label="❮", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction, button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="❯", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        if (self.page + 1) * 10 < len(self.registros):
            self.page += 1
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)


@bot.tree.command(name="userbase", description="(Staff) Ver registrados")
@require_staff()
async def userbase(interaction: discord.Interaction):
    regs = await get_all_registros()
    if not regs:
        return await interaction.response.send_message("No hay registros.", ephemeral=True)
    view = UserbaseView(regs)
    await interaction.response.send_message(embed=await view.build_embed(), view=view)


if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN")

bot.run(TOKEN)
