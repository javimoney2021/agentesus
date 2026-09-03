import discord
from discord import app_commands

from core.config import (
    DATA_DELETION_COOLDOWN_DAYS,
    EVENT_PARTICIPANT_ROLE_ID,
    SUPPORT_CHANNEL_ID,
    db_unavailable,
    require_staff,
)
from core.database import (
    delete_user_data_and_start_cooldown,
    get_all_registros,
    get_by_external_id,
    get_registro,
)


class BaulRegistrosView(discord.ui.View):
    def __init__(self, registros, owner_id: int, page=0):
        super().__init__(timeout=120)
        self.registros = registros
        self.owner_id = owner_id
        self.page = page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "⛔ Esta consulta pertenece a otro miembro del staff.",
            ephemeral=True,
        )
        return False

    async def build_embed(self):
        start = self.page * 10
        end = start + 10
        page_regs = self.registros[start:end]

        embed = discord.Embed(
            title="📋 Baúl de Registros",
            color=discord.Color.green(),
        )

        def escape(value: str) -> str:
            return discord.utils.escape_markdown(str(value))

        lines = []
        for index, registro in enumerate(page_regs, start=start + 1):
            discord_tag = escape(registro.get("discord_tag", "N/A"))
            nickname = escape(registro.get("nickname", "N/A"))
            external_id = escape(registro.get("external_id", "N/A"))
            lines.append(
                f"**{index}.** {discord_tag} | Nick=`{nickname}` | ID=`{external_id}`"
            )

        embed.description = "\n".join(lines) if lines else "Fin de la lista."
        return embed

    @discord.ui.button(label="❮", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="❯", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        if (self.page + 1) * 10 < len(self.registros):
            self.page += 1
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)


def setup(bot):
    @bot.tree.command(name="consultar_baul", description="(Staff) Consulta un registro histórico")
    @require_staff()
    async def consultar_baul(interaction: discord.Interaction, usuario: discord.Member):
        if await db_unavailable(interaction):
            return

        registro = await get_registro(usuario.id)
        if not registro:
            await interaction.response.send_message("No registrado.", ephemeral=True)
            return

        embed = discord.Embed(title="📄 Registro Histórico", color=discord.Color.blue())
        embed.set_thumbnail(url=usuario.display_avatar.url)
        embed.add_field(name="Usuario", value=f"<@{usuario.id}>", inline=False)
        embed.add_field(name="Nick", value=registro["nickname"], inline=True)
        embed.add_field(name="ID", value=registro["external_id"], inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="user_id_baul", description="(Staff) Busca un registro histórico por ID Espacial")
    @require_staff()
    @app_commands.describe(id_espacial="ID Espacial a consultar en el baúl")
    async def user_id_baul(interaction: discord.Interaction, id_espacial: str):
        if await db_unavailable(interaction):
            return

        registros = await get_by_external_id(id_espacial.strip())
        if not registros:
            await interaction.response.send_message(
                f"ℹ️ No encontré registros con la ID `{id_espacial}`.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="🔎 Resultados del Baúl", color=discord.Color.blurple())
        for index, registro in enumerate(registros[:10], start=1):
            embed.add_field(
                name=f"Resultado {index}",
                value=(
                    f"Usuario: <@{registro['user_id']}>\n"
                    f"Nick: `{registro['nickname']}`\n"
                    f"Discord ID: `{registro['user_id']}`"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="baul_registros", description="(Staff) Lista los registros históricos")
    @require_staff()
    async def baul_registros(interaction: discord.Interaction):
        if await db_unavailable(interaction):
            return

        registros = await get_all_registros()
        if not registros:
            await interaction.response.send_message("No hay registros históricos.", ephemeral=True)
            return

        view = BaulRegistrosView(registros, interaction.user.id)
        await interaction.response.send_message(
            embed=await view.build_embed(),
            view=view,
            ephemeral=True,
        )

    @bot.tree.command(
        name="eliminar_datos",
        description="Elimina tus datos de perfil e inscripciones de eventos.",
    )
    async def eliminar_datos(interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ Este comando solo puede utilizarse dentro del servidor.",
                ephemeral=True,
            )
        if await db_unavailable(interaction):
            return

        channel_name = getattr(interaction.channel, "name", "").casefold()
        in_support = (
            (SUPPORT_CHANNEL_ID and interaction.channel_id == SUPPORT_CHANNEL_ID)
            or (not SUPPORT_CHANNEL_ID and channel_name == "soporte")
        )
        if not in_support:
            destination = (
                f"<#{SUPPORT_CHANNEL_ID}>"
                if SUPPORT_CHANNEL_ID
                else "el canal **#soporte**"
            )
            return await interaction.response.send_message(
                f"🔒 Para proteger tu privacidad, utiliza este comando en {destination}.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title="Confirmar eliminación de datos",
            description=(
                "Se eliminarán permanentemente tu registro del baúl, tu perfil "
                "del videojuego y cualquier inscripción activa almacenada por Agente SUS.\n\n"
                "Los controles administrativos o entradas de blacklist justificadas no "
                "se eliminan mediante este comando. Después de la baja no podrás registrar "
                f"otro perfil durante **{DATA_DELETION_COOLDOWN_DAYS} días**."
            ),
            color=discord.Color.red(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=DataDeletionConfirmView(interaction.user.id),
            ephemeral=True,
        )


class DataDeletionConfirmView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.processing = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "⛔ Esta solicitud pertenece a otro usuario.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Eliminar mis datos", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processing or not interaction.guild:
            return
        self.processing = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        try:
            result = await delete_user_data_and_start_cooldown(
                interaction.guild.id,
                interaction.user.id,
                DATA_DELETION_COOLDOWN_DAYS,
            )
        except Exception:
            self.processing = False
            await interaction.edit_original_response(
                content="❌ No se pudo completar la eliminación. Inténtalo más tarde.",
                embed=None,
                view=None,
            )
            return

        if not result["deleted_count"]:
            message = "ℹ️ No encontramos datos de perfil o inscripciones asociados a tu cuenta."
        else:
            cooldown = result["cooldown"]
            available_at = int(cooldown["can_register_at"].timestamp())
            role = interaction.guild.get_role(EVENT_PARTICIPANT_ROLE_ID)
            role_warning = ""
            if role and isinstance(interaction.user, discord.Member) and role in interaction.user.roles:
                try:
                    await interaction.user.remove_roles(
                        role,
                        reason="Eliminación de datos solicitada por el usuario",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    role_warning = " No fue posible retirar automáticamente el rol temporal."
            message = (
                "✅ Tus datos de perfil, baúl e inscripciones fueron eliminados. "
                f"Podrás registrar un perfil nuevamente <t:{available_at}:R>, "
                f"el <t:{available_at}:F>.{role_warning}"
            )

        self.stop()
        await interaction.edit_original_response(content=message, embed=None, view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            content="Solicitud cancelada. No se eliminó ningún dato.",
            embed=None,
            view=None,
        )
