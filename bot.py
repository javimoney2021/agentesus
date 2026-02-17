print("🔥 [DEBUG] ESTE bot.py SE ESTÁ EJECUTANDO")

import os
import io
import discord
from discord import app_commands
from discord.ext import commands
import asyncpg  # Usamos asyncpg para conectar con PostgreSQL
import aiosqlite
from dotenv import load_dotenv


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
STAFF_ROLES = [r.strip() for r in os.getenv("STAFF_ROLES", "Admin,Moderador,Mod").split(",") if r.strip()]
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Usamos la variable DATABASE_URL que es proporcionada por Railway
DATABASE_URL = os.getenv("DATABASE_URL")

DB_FILE = "registros.db"
# Cada 5 mensajes -> 1 punto contabilizado
COOLDOWN_MESSAGES = 4

# contadores en memoria: (guild_id, user_id) -> parcial
partial_bucket = {}

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix=None, intents=intents)

# ---------- BASE DE DATOS ----------
async def init_db():
    # Conectamos a PostgreSQL usando asyncpg
    conn = await asyncpg.connect(DATABASE_URL)
    
    # Crear las tablas necesarias
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS registros (
            user_id BIGINT PRIMARY KEY,
            discord_tag TEXT NOT NULL,
            nickname TEXT NOT NULL,
            external_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


    await conn.execute("""
        CREATE TABLE IF NOT EXISTS permitted_channels (
            guild_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS msg_counts (
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            counted INT NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        )
    """)

    await conn.close()

async def upsert_registro(user, nickname, external_id):
    # Usamos asyncpg para insertar o actualizar registros en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT INTO registros (user_id, discord_tag, nickname, external_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT(user_id) DO UPDATE SET
            discord_tag = excluded.discord_tag,
            nickname = excluded.nickname,
            external_id = excluded.external_id,
            updated_at = CURRENT_TIMESTAMP
    """, user.id, str(user), nickname, external_id)
    await conn.close()

async def get_registro(user_id):
    # Usamos asyncpg para consultar un registro en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("""
        SELECT user_id, discord_tag, nickname, external_id, created_at, updated_at
        FROM registros WHERE user_id=$1
    """, user_id)
    await conn.close()
    return row

async def get_all_registros():
    # Obtener todos los registros de la base de datos PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT user_id, discord_tag, nickname, external_id, created_at, updated_at
        FROM registros ORDER BY updated_at DESC
    """)
    await conn.close()
    return rows

async def delete_registro(user_id: int):
    # Eliminar un registro de PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DELETE FROM registros WHERE user_id=$1", user_id)
    await conn.close()

async def permit_channel(guild_id: int, channel_id: int):
    # Permitir un canal en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT OR IGNORE INTO permitted_channels (guild_id, channel_id)
        VALUES ($1, $2)
    """, guild_id, channel_id)
    await conn.close()

async def unpermit_channel(guild_id: int, channel_id: int):
    # Desautorizar un canal en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        DELETE FROM permitted_channels
        WHERE guild_id=$1 AND channel_id=$2
    """, guild_id, channel_id)
    await conn.close()


async def is_channel_permitted(guild_id: int, channel_id: int) -> bool:
    # Verificar si un canal está autorizado en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("""
        SELECT 1 FROM permitted_channels
        WHERE guild_id=$1 AND channel_id=$2
    """, guild_id, channel_id)
    await conn.close()
    return row is not None

async def reset_table(guild_id: int):
    # Resetear la tabla de mensajes para un servidor en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DELETE FROM msg_counts WHERE guild_id=$1", guild_id)
    await conn.close()

async def reset_user_count(guild_id: int, user_id: int):
    # Resetear el contador de mensajes para un usuario en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DELETE FROM msg_counts WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)
    await conn.close()

async def add_counted_points(guild_id: int, user_id: int, points: int):
    # Agregar puntos a un usuario en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT INTO msg_counts (guild_id, user_id, counted)
        VALUES ($1, $2, $3)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            counted = counted + excluded.counted,
            updated_at = CURRENT_TIMESTAMP
    """, guild_id, user_id, points)
    await conn.close()

async def get_leaderboard(guild_id: int, limit: int, offset: int):
    # Obtener el leaderboard desde PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT user_id, counted
        FROM msg_counts
        WHERE guild_id=$1
        ORDER BY counted DESC, updated_at DESC
        LIMIT $2 OFFSET $3
    """, guild_id, limit, offset)
    await conn.close()
    return rows

async def get_leaderboard_total(guild_id: int) -> int:
    # Obtener el total de registros en el leaderboard
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("""
        SELECT COUNT(*) FROM msg_counts WHERE guild_id=$1
    """, guild_id)
    await conn.close()
    return int(row[0]) if row else 0

async def get_by_external_id(external_id: str):
    # Obtener registros por ID espacial desde PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT user_id, discord_tag, nickname, external_id, created_at, updated_at
        FROM registros
        WHERE external_id=$1
        ORDER BY updated_at DESC
    """, external_id)
    await conn.close()
    return rows

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS registros (
                user_id INTEGER PRIMARY KEY,
                discord_tag TEXT NOT NULL,
                nickname TEXT NOT NULL,
                external_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


    await conn.execute("""
        CREATE TABLE IF NOT EXISTS permitted_channels (
            guild_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS msg_counts (
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            counted INT NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        )
    """)

    await conn.close()

async def upsert_registro(user, nickname, external_id):
    # Usamos asyncpg para insertar o actualizar registros en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT INTO registros (user_id, discord_tag, nickname, external_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT(user_id) DO UPDATE SET
            discord_tag = excluded.discord_tag,
            nickname = excluded.nickname,
            external_id = excluded.external_id,
            updated_at = CURRENT_TIMESTAMP
    """, user.id, str(user), nickname, external_id)
    await conn.close()

async def get_registro(user_id):
    # Usamos asyncpg para consultar un registro en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("""
        SELECT user_id, discord_tag, nickname, external_id, created_at, updated_at
        FROM registros WHERE user_id=$1
    """, user_id)
    await conn.close()
    return row

async def get_all_registros():
    # Obtener todos los registros de la base de datos PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT user_id, discord_tag, nickname, external_id, created_at, updated_at
        FROM registros ORDER BY updated_at DESC
    """)
    await conn.close()
    return rows

async def delete_registro(user_id: int):
    # Eliminar un registro de PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DELETE FROM registros WHERE user_id=$1", user_id)
    await conn.close()

async def permit_channel(guild_id: int, channel_id: int):
    # Permitir un canal en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT OR IGNORE INTO permitted_channels (guild_id, channel_id)
        VALUES ($1, $2)
    """, guild_id, channel_id)
    await conn.close()

async def unpermit_channel(guild_id: int, channel_id: int):
    # Desautorizar un canal en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        DELETE FROM permitted_channels
        WHERE guild_id=$1 AND channel_id=$2
    """, guild_id, channel_id)
    await conn.close()


async def is_channel_permitted(guild_id: int, channel_id: int) -> bool:
    # Verificar si un canal está autorizado en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("""
        SELECT 1 FROM permitted_channels
        WHERE guild_id=$1 AND channel_id=$2
    """, guild_id, channel_id)
    await conn.close()
    return row is not None

async def reset_table(guild_id: int):
    # Resetear la tabla de mensajes para un servidor en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DELETE FROM msg_counts WHERE guild_id=$1", guild_id)
    await conn.close()

async def reset_user_count(guild_id: int, user_id: int):
    # Resetear el contador de mensajes para un usuario en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DELETE FROM msg_counts WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)
    await conn.close()

async def add_counted_points(guild_id: int, user_id: int, points: int):
    # Agregar puntos a un usuario en PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT INTO msg_counts (guild_id, user_id, counted)
        VALUES ($1, $2, $3)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            counted = counted + excluded.counted,
            updated_at = CURRENT_TIMESTAMP
    """, guild_id, user_id, points)
    await conn.close()

async def get_leaderboard(guild_id: int, limit: int, offset: int):
    # Obtener el leaderboard desde PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT user_id, counted
        FROM msg_counts
        WHERE guild_id=$1
        ORDER BY counted DESC, updated_at DESC
        LIMIT $2 OFFSET $3
    """, guild_id, limit, offset)
    await conn.close()
    return rows

async def get_leaderboard_total(guild_id: int) -> int:
    # Obtener el total de registros en el leaderboard
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("""
        SELECT COUNT(*) FROM msg_counts WHERE guild_id=$1
    """, guild_id)
    await conn.close()
    return int(row[0]) if row else 0

async def get_by_external_id(external_id: str):

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("""
            SELECT user_id, discord_tag, nickname, external_id, created_at, updated_at
            FROM registros
            WHERE external_id = ?
            ORDER BY updated_at DESC
        """, (external_id,)) as cur:
            return await cur.fetchall()


    # Obtener registros por ID espacial desde PostgreSQL
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT user_id, discord_tag, nickname, external_id, created_at, updated_at
        FROM registros
        WHERE external_id=$1
        ORDER BY updated_at DESC
    """, external_id)
    await conn.close()
    return rows


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

# ---------- EVENTO ----------
@bot.event
async def on_ready():
    await init_db()

    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print("✅ Comandos sincronizados en tu servidor")
        else:
            await bot.tree.sync()
            print("✅ Comandos sincronizados globalmente")
    except Exception as e:
        print("❌ Error sincronizando comandos:", e)

    print(f"🤖 Bot conectado como {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not message.guild:
        return

    permitted = await is_channel_permitted(message.guild.id, message.channel.id)
    if not permitted:
        return

    key = (message.guild.id, message.author.id)
    partial_bucket[key] = partial_bucket.get(key, 0) + 1

    if partial_bucket[key] >= COOLDOWN_MESSAGES:
        partial_bucket[key] = 0
        await add_counted_points(message.guild.id, message.author.id, 1)

    await bot.process_commands(message)



async def dm_owner(text: str):
    if not OWNER_ID:
        return
    try:
        user = await bot.fetch_user(OWNER_ID)
        await user.send(text)
    except Exception:
        pass



# ---------- COMANDOS ----------
@bot.tree.command(name="registrar", description="Registra Nickname e ID Espacial")
async def registrar(interaction: discord.Interaction, nickname: str, external_id: str):
    # 1️⃣ Verificar si ya está registrado
    existing = await get_registro(interaction.user.id)
    if existing:
        embed = discord.Embed(
            description="🚫 **YA TE ENCUENTRAS REGISTRADO**",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # 2️⃣ Registrar solo si NO existe
    await upsert_registro(
        interaction.user,
        nickname.strip(),
        external_id.strip()
    )

    embed = discord.Embed(
        title="✅ Registro Completado - Ahora ya puedes participar en Eventos",
        description=f"Nick: `{nickname}`\nID: `{external_id}`",
        color=discord.Color.green()
    )

 # ✅ ENVIAR INFO AL OWNER POR DM
    await dm_owner(
        f"🆕 Nuevo registro\n"
        f"Usuario: {interaction.user} (ID {interaction.user.id})\n"
        f"Nick: {nickname.strip()}\n"
        f"ID espacial: {external_id.strip()}\n"
        f"Servidor: {interaction.guild.name if interaction.guild else 'DM'}"
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="consultar", description="(Staff) Consultar Registro de miembro SUS")
@require_staff()
async def consultar(interaction, usuario: discord.Member):
    row = await get_registro(usuario.id)
    if not row:
        return await interaction.response.send_message("No registrado.", ephemeral=True)

    _, tag, nick, ext_id, created, updated = row
    embed = discord.Embed(title="📄 Registro", color=discord.Color.blue())
    embed.set_thumbnail(url=usuario.display_avatar.url)
    embed.add_field(name="Usuario", value=tag, inline=False)
    embed.add_field(name="Nick", value=nick, inline=True)
    embed.add_field(name="ID", value=ext_id, inline=True)
    embed.set_footer(text=f"Actualizado: {updated}")

    await interaction.response.send_message(embed=embed, ephemeral=False)

@bot.tree.command(name="userid", description="(Staff) Busca a qué usuario pertenece un ID Espacial")
@app_commands.describe(id_espacial="ID Espacial a buscar")
@require_staff()
async def userid(interaction: discord.Interaction, id_espacial: str):
    id_espacial = id_espacial.strip()

    rows = await get_by_external_id(id_espacial)
    if not rows:
        return await interaction.response.send_message(
            f"ℹ️ No encontré ningún usuario registrado con **ID Espacial** `{id_espacial}`.",
            ephemeral=False
        )

    # Si por alguna razón hay más de un usuario con el mismo ID, los listamos
    embed = discord.Embed(
        title="🔎 Resultado de búsqueda por ID Espacial",
        description=f"ID Espacial: `{id_espacial}`\nCoincidencias: **{len(rows)}**",
        color=discord.Color.blurple()
    )

    for user_id, discord_tag, nickname, external_id, created_at, updated_at in rows[:10]:
        embed.add_field(
            name=f"{discord_tag}",
            value=f"Nick: `{nickname}`\nDiscord ID: `{user_id}`\nActualizado: `{updated_at}`",
            inline=False
        )

    if len(rows) > 10:
        embed.set_footer(text="Mostrando solo las primeras 10 coincidencias.")

    await interaction.response.send_message(embed=embed, ephemeral=False)


@bot.tree.command(name="editar", description="(Staff) Editar Registro")
@require_staff()
async def editar(interaction, usuario: discord.Member, nickname: str, external_id: str):
    await upsert_registro(usuario, nickname.strip(), external_id.strip())
    await interaction.response.send_message("✏️ Registro Actualizado.", ephemeral=False)


@bot.tree.command(name="eliminar_registro", description="(Staff) Elimina el registro de un usuario")
@require_staff()
async def eliminar_registro(interaction: discord.Interaction, usuario: discord.Member):
    row = await get_registro(usuario.id)
    if not row:
        return await interaction.response.send_message(
            "❌ Ese usuario no tiene registro.",
            ephemeral=False
        )

    await delete_registro(usuario.id)
    await interaction.response.send_message(
        f"🗑️ Registro eliminado para **{usuario}**.",
        ephemeral=False
    )


@bot.tree.command(name="permitchannel", description="(Admin) Autoriza un canal para contabilizar mensajes")
@app_commands.describe(canal="Canal donde se contarán mensajes")
@require_staff()
async def permitchannel_cmd(interaction: discord.Interaction, canal: discord.TextChannel):
    await permit_channel(interaction.guild.id, canal.id)
    await interaction.response.send_message(
        f"✅ Canal autorizado para contabilización: {canal.mention}",
        ephemeral=True
    )
    
@bot.tree.command(name="borrarchannel", description="(Admin) Desautoriza un canal para contabilizar mensajes")
@app_commands.describe(canal="Canal a quitar de contabilización")
@require_staff()
async def borrarchannel_cmd(interaction: discord.Interaction, canal: discord.TextChannel):
    await unpermit_channel(interaction.guild.id, canal.id)
    await interaction.response.send_message(
        f"🗑️ Canal desautorizado para contabilización: {canal.mention}",
        ephemeral=True
    )


@bot.tree.command(name="reset_tabla", description="(Staff) Resetea el Ranking")
@require_staff()
async def reset_tabla_cmd(interaction: discord.Interaction):
    await reset_table(interaction.guild.id)
    await interaction.response.send_message("✅ Tabla de mensajes reseteada.", ephemeral=False)

@bot.tree.command(name="reset_user", description="(Staff) Resetea los puntos de un usuario")
@app_commands.describe(usuario="Usuario a resetear")
@require_staff()
async def reset_user_cmd(interaction: discord.Interaction, usuario: discord.Member):
    await reset_user_count(interaction.guild.id, usuario.id)
    await interaction.response.send_message(
        f"✅ Contador reseteado para {usuario.mention}.",
        ephemeral=False
    )

class TablaView(discord.ui.View):
    def __init__(self, guild_id: int, page: int = 0):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.page = page

    async def build_embed(self, interaction: discord.Interaction) -> discord.Embed:
        page_size = 10
        offset = self.page * page_size
        rows = await get_leaderboard(self.guild_id, page_size, offset)
        total = await get_leaderboard_total(self.guild_id)

        embed = discord.Embed(title="🏆 RANKING DE CONVERSADORES 🏆", color=discord.Color.blurple())
        embed.description = f"Mostrando posiciones **{offset+1}–{min(offset+page_size, total)}** de **{total}** registrados.\n" \

        if not rows:
            embed.add_field(name="Sin datos", value="Aún no hay mensajes contabilizados.", inline=False)
            return embed

        lines = []
        pos = offset + 1
        for user_id, counted in rows:
            # Intentar usar apodo del servidor (display_name)
            name = f"Usuario {user_id}"
            try:
                member = await interaction.guild.fetch_member(int(user_id))
                name = member.display_name  # apodo del servidor si existe
            except Exception:
                pass
            lines.append(f"**#{pos}** — {name} — **{counted}**")
            pos += 1

        embed.add_field(name="Ranking", value="\n".join(lines), inline=False)
        embed.set_footer(text="Usa los botones para cambiar de página.")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        embed = await self.build_embed(interaction)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Solo avanzamos si existe otra página
        total = await get_leaderboard_total(self.guild_id)
        if (self.page + 1) * 10 < total:
            self.page += 1
        embed = await self.build_embed(interaction)
        await interaction.response.edit_message(embed=embed, view=self)


@bot.tree.command(name="tabla", description="Muestra el Ranking de los más conversadores")
async def tabla_cmd(interaction: discord.Interaction):
    view = TablaView(interaction.guild.id, page=0)
    embed = await view.build_embed(interaction)
    # PUBLICO: ephemeral=False
    await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

class UserbaseView(discord.ui.View):
    def __init__(self, registros, page=0, page_size=10):
        super().__init__(timeout=120)  # tiempo para que los botones expiren
        self.registros = registros
        self.page = page
        self.page_size = page_size

    async def build_embed(self, interaction: discord.Interaction):  # <-- requiere interaction
        embed = discord.Embed(
            title="📋 Registro de Miembros Super Sus LATAM",
            color=discord.Color.green()
        )

        total = len(self.registros)
        start = self.page * self.page_size
        end = start + self.page_size
        page_regs = self.registros[start:end]

        if not page_regs:
            embed.description = "No hay registros en esta página."
            return embed

        lines = []
        for i, r in enumerate(page_regs, start=start + 1):
            discord_tag = r[1]
            nick = r[2]
            ext_id = r[3]
            lines.append(f"**{i}.** {discord_tag} | Nick={nick} | ID={ext_id}")

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Página {self.page + 1} de {(total - 1) // self.page_size + 1}")

        return embed

    @discord.ui.button(label="❮", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            embed = await self.build_embed(interaction)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()  # No hace nada si ya está en la primera página

    @discord.ui.button(label="❯", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        max_page = (len(self.registros) - 1) // self.page_size
        if self.page < max_page:
            self.page += 1
            embed = await self.build_embed(interaction)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()  # No hace nada si ya está en la última página
  # No pasa nada si ya está en la última página
@bot.tree.command(name="userbase", description="(Staff) Ver todos los jugadores registrados")
@require_staff()
async def userbase(interaction: discord.Interaction):
    registros = await get_all_registros()
    if not registros:
        return await interaction.response.send_message("No hay registros.", ephemeral=True)

    view = UserbaseView(registros=registros, page=0)
    embed = await view.build_embed(interaction)

    # NO es efímero para que todos lo vean
    await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN en .env")

bot.run(TOKEN)
