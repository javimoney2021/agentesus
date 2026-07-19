"""Dinamica /adivinar reutilizable para discord.py 2.x.

Integracion minima en otro bot:

    from adivinar import AdivinarCog

    await bot.add_cog(AdivinarCog(bot, staff_role_ids={123456789012345678}))

El Cog gestiona sus propios mensajes, por lo que no necesitas modificar
``on_message``. Recuerda sincronizar el arbol de comandos de tu bot.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands


def normalize_text(text: str) -> str:
    """Compara palabras sin distinguir mayusculas, tildes ni espacios extremos."""
    normalized = unicodedata.normalize("NFD", text.lower().strip())
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


@dataclass
class PendingSetup:
    destination_channel_id: int
    origin_channel_id: int
    word: str
    original_word: str
    step: int = 1
    start_announcement: str | None = None


@dataclass
class ActiveGuess:
    word: str
    original_word: str
    winner_announcement: str


class AdivinarCog(commands.Cog):
    """Configura y ejecuta dinamicas de adivinar una palabra por servidor."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        staff_role_ids: set[int] | None = None,
        staff_role_names: set[str] | None = None,
    ) -> None:
        self.bot = bot
        self.staff_role_ids = staff_role_ids or set()
        self.staff_role_names = staff_role_names or set()
        self.pending_setups: dict[tuple[int, int], PendingSetup] = {}
        self.active_guesses: dict[tuple[int, int], ActiveGuess] = {}

    def is_staff(self, member: discord.Member) -> bool:
        """Autoriza administradores, moderadores y los roles configurados."""
        permissions = member.guild_permissions
        if permissions.administrator or permissions.manage_guild or permissions.moderate_members:
            return True

        return any(
            role.id in self.staff_role_ids or role.name in self.staff_role_names
            for role in member.roles
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and self.is_staff(interaction.user):
            return True

        if not interaction.response.is_done():
            await interaction.response.send_message("No tienes permisos.", ephemeral=True)
        return False

    @app_commands.command(
        name="adivinar",
        description="(Staff) Configura una palabra y sus anuncios de inicio y ganadores",
    )
    @app_commands.describe(
        palabra="La palabra que deben escribir",
        canal="Canal donde se publicara y se adivinara la palabra",
    )
    async def adivinar(
        self,
        interaction: discord.Interaction,
        palabra: str,
        canal: discord.TextChannel,
    ) -> None:
        if not interaction.guild_id or not interaction.channel_id:
            await interaction.response.send_message(
                "Este comando solo se puede usar dentro de un servidor.",
                ephemeral=True,
            )
            return

        key = (interaction.guild_id, interaction.user.id)
        self.pending_setups[key] = PendingSetup(
            destination_channel_id=canal.id,
            origin_channel_id=interaction.channel_id,
            word=normalize_text(palabra),
            original_word=palabra,
        )

        await interaction.response.send_message(
            "Dinamica iniciada (Paso 1/2)\n"
            f"Palabra: `{palabra}` | Canal: {canal.mention}\n\n"
            "Escribe Anuncio de **Inicio** de la Dinamica...",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        if await self.handle_setup_message(message):
            return

        await self.handle_guess_message(message)

    async def handle_setup_message(self, message: discord.Message) -> bool:
        key = (message.guild.id, message.author.id)
        setup = self.pending_setups.get(key)
        if setup is None or setup.origin_channel_id != message.channel.id:
            return False

        command_prefix = self.bot.command_prefix
        if isinstance(command_prefix, str) and message.content.startswith(command_prefix):
            return True

        if setup.step == 1:
            setup.start_announcement = message.content
            setup.step = 2
            await message.reply(
                "Anuncio de Inicio guardado.\n\n"
                "Anuncio de Ganadores! **> usar {word} - {winner} <**"
            )
            return True

        self.pending_setups.pop(key, None)
        channel = message.guild.get_channel(setup.destination_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await message.reply("No pude encontrar el canal seleccionado. Vuelve a iniciar la dinamica.")
            return True

        active_key = (message.guild.id, channel.id)
        self.active_guesses[active_key] = ActiveGuess(
            word=setup.word,
            original_word=setup.original_word,
            winner_announcement=message.content,
        )
        await channel.send(setup.start_announcement or "")
        await message.reply(f"Dinamica activada. El anuncio de inicio fue enviado a {channel.mention}.")
        return True

    async def handle_guess_message(self, message: discord.Message) -> bool:
        active_key = (message.guild.id, message.channel.id)
        guess = self.active_guesses.get(active_key)
        if guess is None or normalize_text(message.content) != guess.word:
            return False

        self.active_guesses.pop(active_key, None)
        announcement = (
            guess.winner_announcement
            .replace("{winner}", message.author.mention)
            .replace("{word}", guess.original_word)
        )
        await message.channel.send(announcement)
        return True


def role_ids_from_env(variable: str = "ADIVINAR_STAFF_ROLE_IDS") -> set[int]:
    """Lee IDs de roles separados por comas desde una variable de entorno."""
    raw_value = os.getenv(variable, "")
    return {int(value.strip()) for value in raw_value.split(",") if value.strip().isdigit()}


async def setup(bot: commands.Bot) -> None:
    """Permite cargar el archivo como extension con ``await bot.load_extension``."""
    await bot.add_cog(AdivinarCog(bot, staff_role_ids=role_ids_from_env()))
