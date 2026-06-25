import discord
from discord import app_commands

from core.config import (
    BLACKLIST_ALERT_CHANNEL_ID,
    BLACKLIST_ALERT_ROLE_ID,
    OWNER_ID,
    db_unavailable,
    has_required_register_role,
    require_staff,
)
from core.database import (
    add_blacklist_id,
    delete_blacklist_id,
    delete_registro,
    get_all_blacklist_ids,
    get_all_registros,
    get_blacklist_id,
    get_by_external_id,
    get_registro,
    upsert_registro,
)


async def dm_owner(bot, text: str):
    if not OWNER_ID:
        return
    try:
        user = await bot.fetch_user(OWNER_ID)
        await user.send(text)
    except Exception:
        pass


async def notify_blacklist_attempt(bot, interaction: discord.Interaction, attempted_id: str):
    if not interaction.guild:
        return

    channel = interaction.guild.get_channel(BLACKLIST_ALERT_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(BLACKLIST_ALERT_CHANNEL_ID)
        except Exception as e:
            print(f"⚠️ No se pudo obtener el canal de alertas de blacklist: {e}")
            return

    try:
        await channel.send(
            f"Hey! No tan rapido...👀\n\n"
            f"<@&{BLACKLIST_ALERT_ROLE_ID}> **Ojo!** El usuario <@{interaction.user.id}> "
            f"Intentó registrarse con una **ID prohibida.**\n"
            f"**ID detectada:** `{attempted_id}`",
            allowed_mentions=discord.AllowedMentions(roles=True, users=True)
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar la alerta de intento con ID prohibida: {e}")


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


def setup(bot):
    @bot.tree.command(name="registrar", description="Registra Nickname e ID Espacial")
    @app_commands.rename(nickname="nickname", external_id="id_espacial")
    @app_commands.describe(
        nickname="Tu Nickname de SuperSus",
        external_id="Tu ID Espacial"
    )
    async def registrar(interaction: discord.Interaction, nickname: str, external_id: str):
        if await db_unavailable(interaction):
            return
        nickname = nickname.strip()
        external_id = external_id.strip()

        if not interaction.guild or not has_required_register_role(interaction.user):
            return await interaction.response.send_message(
                "📢 Para completar tu registro es necesario que poseas el rol <@&1409401827065204786> verifica tu cuenta ahora en el canal <#1200056404208263219> con el comando **/panel** Pulsando en el boton verde, luego en el mensaje **Click me to verify!**",
                ephemeral=True
            )

        existing = await get_registro(interaction.user.id)
        if existing:
            return await interaction.response.send_message(
                embed=discord.Embed(
                    description="🚫 **YA TE ENCUENTRAS REGISTRADO**",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )

        blacklisted = await get_blacklist_id(external_id)
        if blacklisted:
            await notify_blacklist_attempt(bot, interaction, external_id)
            return await interaction.response.send_message(
                embed=discord.Embed(
                    description="🚫 **Registro no Aceptado: Esta ID espacial No tiene permitido participar en Eventos del servidor!**",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )

        existing_rows = await get_by_external_id(external_id)
        if existing_rows:
            return await interaction.response.send_message(
                embed=discord.Embed(
                    description="🚫 **Error! ID Espacial Ya se encuentra Registrada.**",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )

        await upsert_registro(interaction.user, nickname, external_id)

        embed = discord.Embed(
            title="✅ Registro Completado",
            description=f"Nick: `{nickname}`\nID: `{external_id}`",
            color=discord.Color.green()
        )

        await dm_owner(
            bot,
            f"🆕 Nuevo registro: {interaction.user} | Nick: {nickname} | ID: {external_id}"
        )

        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="consultar", description="(Staff) Consultar Registro")
    @require_staff()
    async def consultar(interaction: discord.Interaction, usuario: discord.Member):
        if await db_unavailable(interaction):
            return
        row = await get_registro(usuario.id)
        if not row:
            return await interaction.response.send_message("No registrado.", ephemeral=True)

        embed = discord.Embed(title="📄 Registro", color=discord.Color.blue())
        embed.set_thumbnail(url=usuario.display_avatar.url)
        embed.add_field(name="Usuario", value=f"<@{usuario.id}>", inline=False)
        embed.add_field(name="Nick", value=row["nickname"], inline=True)
        embed.add_field(name="ID", value=row["external_id"], inline=True)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="userid", description="(Staff) Busca por ID Espacial")
    @require_staff()
    async def userid(interaction: discord.Interaction, id_espacial: str):
        if await db_unavailable(interaction):
            return
        rows = await get_by_external_id(id_espacial.strip())
        if not rows:
            return await interaction.response.send_message(
                f"ℹ️ No encontré registros con la ID `{id_espacial}`."
            )

        embed = discord.Embed(title="🔎 Resultados", color=discord.Color.blurple())
        for i, r in enumerate(rows[:10], start=1):
            embed.add_field(
                name=f"Resultado {i}",
                value=(
                    f"Usuario: <@{r['user_id']}>\n"
                    f"Nick: `{r['nickname']}`\n"
                    f"Discord ID: `{r['user_id']}`"
                ),
                inline=False
            )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="editar", description="(Staff) Editar Registro")
    @require_staff()
    async def editar(interaction: discord.Interaction, usuario: discord.Member, nickname: str, external_id: str):
        if await db_unavailable(interaction):
            return
        await upsert_registro(usuario, nickname.strip(), external_id.strip())
        await interaction.response.send_message("✏️ Registro Actualizado.")

    @bot.tree.command(name="eliminar_registro", description="(Staff) Elimina registro por miembro o ID")
    @require_staff()
    @app_commands.describe(
        usuario="Selecciona al miembro si aún está en el servidor",
        id_discord="ID de Discord del usuario no esta en el servidor"
    )
    async def eliminar_registro(
        interaction: discord.Interaction,
        usuario: discord.Member | None = None,
        id_discord: str | None = None
    ):
        if await db_unavailable(interaction):
            return
        if usuario is None and id_discord is None:
            return await interaction.response.send_message(
                "⚠️ Debes indicar un **usuario** o un **ID de Discord**."
            )

        if usuario is not None and id_discord is not None:
            return await interaction.response.send_message(
                "⚠️ Usa solo una opción: **usuario** o **id_discord**, no ambas."
            )

        if usuario is not None:
            target_id = usuario.id
            target_name = str(usuario)
        else:
            id_discord = id_discord.strip()

            if not id_discord.isdigit():
                return await interaction.response.send_message(
                    "⚠️ El ID de Discord debe contener solo números."
                )

            target_id = int(id_discord)
            target_name = f"ID `{id_discord}`"

        deleted = await delete_registro(target_id)

        if not deleted:
            return await interaction.response.send_message(
                f"ℹ️ No encontré ningún registro para {target_name}."
            )

        await interaction.response.send_message(
            f"🗑️ Registro eliminado correctamente.\n"
            f"**Usuario guardado:** `{deleted['discord_tag']}`\n"
            f"**Nick:** `{deleted['nickname']}`\n"
            f"**ID espacial:** `{deleted['external_id']}`"
        )

    @bot.tree.command(name="prohibir", description="Añade un ID Espacial a la lista negra")
    @require_staff()
    @app_commands.describe(
        id_espacial="ID Espacial que será prohibida",
        nick="Nick del jugador (opcional)"
    )
    async def prohibir(interaction: discord.Interaction, id_espacial: str, nick: str | None = None):
        if await db_unavailable(interaction):
            return
        external_id = id_espacial.strip()
        nickname = nick.strip() if nick else None

        if not external_id:
            return await interaction.response.send_message(
                "⚠️ Debes indicar un **ID Espacial** válido."
            )

        already_exists = await get_blacklist_id(external_id)
        await add_blacklist_id(external_id, nickname)

        mensaje = (
            f"⛔ **ID Espacial añadido a la lista negra.**\n"
            f"**ID espacial:** `{external_id}`"
        )

        if nickname:
            mensaje += f"\n**Nick:** `{nickname}`"

        if already_exists:
            mensaje += "\nℹ️ Ya estaba en la lista negra. Se actualizó la información."

        await interaction.response.send_message(mensaje)

    @bot.tree.command(name="desprohibir", description="Quita un ID Espacial de la lista negra")
    @require_staff()
    @app_commands.describe(
        id_espacial="Remueve ID Espacial de Lista Negra"
    )
    async def desprohibir(interaction: discord.Interaction, id_espacial: str):
        if await db_unavailable(interaction):
            return
        external_id = id_espacial.strip()

        if not external_id:
            return await interaction.response.send_message(
                "⚠️ Debes indicar un **ID Espacial** válido."
            )

        deleted = await delete_blacklist_id(external_id)

        if not deleted:
            return await interaction.response.send_message(
                f"ℹ️ El ID espacial `{external_id}` no se encuentra en la lista negra."
            )

        mensaje = (
            f"✅ **ID Espacial retirado de la lista negra.**\n"
            f"**ID espacial:** `{deleted['external_id']}`"
        )

        if deleted["nickname"]:
            mensaje += f"\n**Nick:** `{deleted['nickname']}`"

        await interaction.response.send_message(mensaje)

    @bot.tree.command(name="blacklist", description="Muestra la lista negra de Jugadores vetados.")
    @require_staff()
    async def blacklist(interaction: discord.Interaction):
        if await db_unavailable(interaction):
            return
        rows = await get_all_blacklist_ids()

        if not rows:
            return await interaction.response.send_message(
                "ℹ️ La lista negra está vacía."
            )

        def esc(s: str) -> str:
            return discord.utils.escape_markdown(str(s))

        lines = []
        for i, r in enumerate(rows, start=1):
            external_id = esc(r["external_id"])
            nickname = r["nickname"]

            if nickname:
                lines.append(f"**{i}.** ID=`{external_id}` | Nick=`{esc(nickname)}`")
            else:
                lines.append(f"**{i}.** ID=`{external_id}`")

        embed = discord.Embed(
            title="⛔ Lista Negra",
            description="\n".join(lines),
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Total vetados: {len(rows)}")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="userbase", description="(Staff) Ver registrados")
    @require_staff()
    async def userbase(interaction: discord.Interaction):
        if await db_unavailable(interaction):
            return
        regs = await get_all_registros()
        if not regs:
            return await interaction.response.send_message("No hay registros.", ephemeral=True)
        view = UserbaseView(regs)
        await interaction.response.send_message(embed=await view.build_embed(), view=view)
