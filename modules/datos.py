import discord

from core.config import (
    DATA_DELETION_COOLDOWN_DAYS,
    EVENT_PARTICIPANT_ROLE_ID,
    SUPPORT_CHANNEL_ID,
    db_unavailable,
)
from core.database import delete_user_data_and_start_cooldown


def setup(bot):
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
                "Se eliminarán permanentemente tu perfil del videojuego y cualquier "
                "inscripción activa almacenada por Agente SUS.\n\n"
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
                "✅ Tus datos de perfil e inscripciones fueron eliminados. "
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
