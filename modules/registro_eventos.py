import logging
from dataclasses import dataclass, field

import discord
from discord import app_commands

from core.config import (
    EVENT_REGISTRATION_CHANNEL_ID,
    EVENT_REGISTRATION_ROLE_ID,
    require_staff,
)


logger = logging.getLogger(__name__)


@dataclass
class ActivePanel:
    message: discord.Message
    event_name: str
    registered_user_ids: set[int] = field(default_factory=set)


class RegistrationModal(discord.ui.Modal, title="Registro de Evento"):
    nickname = discord.ui.TextInput(
        label="Nickname",
        placeholder="Tu nickname en el juego...",
        min_length=2,
        max_length=32,
        required=True,
    )
    spatial_id = discord.ui.TextInput(
        label="ID Espacial",
        placeholder="Tu ID Espacial...",
        min_length=2,
        max_length=30,
        required=True,
    )

    def __init__(self, cog: "RegistroEventos", channel_id: int, event_name: str):
        super().__init__()
        self.cog = cog
        self.channel_id = channel_id
        self.event_name = event_name

    async def on_submit(self, interaction: discord.Interaction):
        panel = self.cog.active_panels.get(self.channel_id)
        if panel is None:
            await interaction.response.send_message(
                "El registro de este evento ya esta cerrado.", ephemeral=True
            )
            return

        if interaction.user.id in panel.registered_user_ids:
            await interaction.response.send_message(
                "Ya estas registrado en este evento.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self.assign_registration_role(interaction)
        await self.forward_registration(interaction)

        panel.registered_user_ids.add(interaction.user.id)
        await interaction.followup.send(
            "Registro exitoso. Nos vemos a la hora del evento.", ephemeral=True
        )

    async def assign_registration_role(self, interaction: discord.Interaction):
        if not interaction.guild or not EVENT_REGISTRATION_ROLE_ID:
            return

        role = interaction.guild.get_role(EVENT_REGISTRATION_ROLE_ID)
        if role is None or not isinstance(interaction.user, discord.Member):
            return

        try:
            await interaction.user.add_roles(role, reason=f"Registro: {self.event_name}")
        except discord.Forbidden:
            logger.warning("No se pudo asignar el rol de registro a %s.", interaction.user.id)
        except discord.HTTPException:
            logger.exception("Error al asignar el rol de registro a %s.", interaction.user.id)

    async def forward_registration(self, interaction: discord.Interaction):
        if not EVENT_REGISTRATION_CHANNEL_ID:
            return

        channel = self.cog.bot.get_channel(EVENT_REGISTRATION_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.cog.bot.fetch_channel(EVENT_REGISTRATION_CHANNEL_ID)
            except discord.HTTPException:
                logger.warning("No se encontro el canal de inscripciones configurado.")
                return

        if not isinstance(channel, discord.abc.Messageable):
            logger.warning("El canal de inscripciones configurado no acepta mensajes.")
            return

        embed = discord.Embed(
            title=f"Nuevo Registro - {self.event_name}",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Discord User", value=interaction.user.mention, inline=False)
        embed.add_field(name="Nickname", value=self.nickname.value, inline=False)
        embed.add_field(name="ID Espacial", value=self.spatial_id.value, inline=False)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            logger.exception("No se pudo reenviar una inscripcion de evento.")


class RegistrationView(discord.ui.View):
    def __init__(self, cog: "RegistroEventos", channel_id: int, event_name: str, open: bool):
        super().__init__(timeout=None)
        self.cog = cog
        self.channel_id = channel_id
        self.event_name = event_name
        self.register.disabled = not open
        self.register.style = discord.ButtonStyle.primary if open else discord.ButtonStyle.secondary

    @discord.ui.button(
        label="Registrarse",
        style=discord.ButtonStyle.primary,
        custom_id="event_registration:register",
    )
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            RegistrationModal(self.cog, self.channel_id, self.event_name)
        )


def open_embed(event_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{event_name} - Registro Abierto",
        description=(
            "El evento esta por comenzar.\n\n"
            "Presiona el boton **Registrarse** para apartar tu lugar.\n"
            "Se te pedira tu **Nickname** y tu **ID Espacial**."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Cada usuario puede registrarse una sola vez.")
    return embed


def closed_embed(event_name: str) -> discord.Embed:
    return discord.Embed(
        title=f"{event_name} - Registro Cerrado",
        description="**Registro Cerrado, El evento esta por iniciar...!**",
        color=discord.Color.dark_gray(),
    )


class RegistroEventos:
    def __init__(self, bot):
        self.bot = bot
        self.active_panels: dict[int, ActivePanel] = {}

    async def open_registration(self, interaction: discord.Interaction, event_name: str):
        if interaction.channel_id in self.active_panels:
            await interaction.response.send_message(
                "Ya hay un panel activo en este canal. Usa /cerrar_registro primero.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "No se pudo identificar un canal donde publicar el panel.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        view = RegistrationView(self, interaction.channel_id, event_name, open=True)
        message = await interaction.channel.send(embed=open_embed(event_name), view=view)
        self.active_panels[interaction.channel_id] = ActivePanel(message, event_name)

        await interaction.followup.send(
            f"Panel de registro **{event_name}** publicado.", ephemeral=True
        )

    async def close_registration(self, interaction: discord.Interaction):
        panel = self.active_panels.get(interaction.channel_id)
        if panel is None:
            await interaction.response.send_message(
                "No hay un panel activo en este canal.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        view = RegistrationView(self, interaction.channel_id, panel.event_name, open=False)
        try:
            await panel.message.edit(embed=closed_embed(panel.event_name), view=view)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logger.exception("No se pudo actualizar el panel de registro cerrado.")

        self.active_panels.pop(interaction.channel_id, None)
        await interaction.followup.send(
            f"Registro de **{panel.event_name}** cerrado.", ephemeral=True
        )


def setup(bot):
    registro_eventos = RegistroEventos(bot)

    @bot.tree.command(
        name="abrir_registro",
        description="(Staff) Publica el panel de registro de un evento.",
    )
    @require_staff()
    @app_commands.describe(nombre_evento="Nombre del evento que aparecera en el panel")
    async def abrir_registro(interaction: discord.Interaction, nombre_evento: str):
        await registro_eventos.open_registration(interaction, nombre_evento)

    @bot.tree.command(
        name="cerrar_registro",
        description="(Staff) Cierra el registro activo de este canal.",
    )
    @require_staff()
    async def cerrar_registro(interaction: discord.Interaction):
        await registro_eventos.close_registration(interaction)
