import asyncio
import logging
import math

import discord

from core import database
from core.config import (
    DB_NO_DISPONIBLE,
    GUILD_ID,
    TOKEN_EXPIRATION_MINUTES,
    VERIFIED_ROLE_ID,
    get_missing_verification_settings,
    require_staff,
)
from core.verification_security import create_signed_verification_token


logger = logging.getLogger(__name__)
ISSUE_COOLDOWN_SECONDS = 15


class PersonalVerificationLinkView(discord.ui.View):
    def __init__(self, verification_url: str):
        super().__init__(timeout=TOKEN_EXPIRATION_MINUTES * 60)
        self.add_item(
            discord.ui.Button(
                label="Continuar en la web",
                style=discord.ButtonStyle.link,
                url=verification_url,
            )
        )


class VerificationPanelView(discord.ui.View):
    def __init__(self, manager: "VerificationManager"):
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(
        label="Verificar",
        style=discord.ButtonStyle.success,
        custom_id="verification_sa:start:v1",
    )
    async def verify(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        await self.manager.issue_personal_link(interaction)


class VerificationManager:
    def __init__(self, bot):
        self.bot = bot
        self._issue_locks = {}
        self._last_issued_at = {}

    @staticmethod
    def panel_embed() -> discord.Embed:
        embed = discord.Embed(
            title="Verificación Super Sus SA",
            description=(
                "Verifica tu cuenta para acceder a la comunidad.\n\n"
                "Presiona **Verificar** para recibir un enlace personal y temporal "
                "visible únicamente para ti. Antes de continuar deberás leer y "
                "aceptar el aviso de privacidad."
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(
            text=(
                f"El enlace expira en {TOKEN_EXPIRATION_MINUTES} minutos "
                "y solo puede utilizarse una vez."
            )
        )
        return embed

    async def issue_personal_link(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "La verificación solo está disponible dentro del servidor.",
                ephemeral=True,
            )
            return

        if GUILD_ID and interaction.guild.id != int(GUILD_ID):
            await interaction.response.send_message(
                "Este panel no pertenece al servidor configurado.",
                ephemeral=True,
            )
            return

        if get_missing_verification_settings():
            await interaction.response.send_message(
                "La verificación no está disponible temporalmente.",
                ephemeral=True,
            )
            return

        if database.bot_pool is None:
            await interaction.response.send_message(
                DB_NO_DISPONIBLE,
                ephemeral=True,
            )
            return

        if any(role.id == VERIFIED_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message(
                "Tu cuenta ya está verificada en el servidor.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = interaction.user.id
        lock = self._issue_locks.setdefault(user_id, asyncio.Lock())

        async with lock:
            loop_time = asyncio.get_running_loop().time()
            last_issued_at = self._last_issued_at.get(user_id, 0.0)
            remaining = ISSUE_COOLDOWN_SECONDS - (loop_time - last_issued_at)
            if remaining > 0:
                await interaction.followup.send(
                    f"Espera {math.ceil(remaining)} segundos antes de solicitar otro enlace.",
                    ephemeral=True,
                )
                return

            try:
                issued = create_signed_verification_token(
                    interaction.guild.id,
                    user_id,
                )
                await database.create_verification_token(
                    issued.payload.token_id,
                    issued.digest,
                    issued.payload.guild_id,
                    issued.payload.user_id,
                    issued.payload.expires_at,
                )
            except Exception:
                logger.exception(
                    "No se pudo emitir un enlace de verificacion para el usuario %s",
                    user_id,
                )
                await interaction.followup.send(
                    "No fue posible generar tu enlace. Inténtalo nuevamente más tarde.",
                    ephemeral=True,
                )
                return

            self._last_issued_at[user_id] = asyncio.get_running_loop().time()
            embed = discord.Embed(
                title="Tu enlace de verificación",
                description=(
                    "El enlace es personal, no lo compartas. "
                    f"Caducará en **{TOKEN_EXPIRATION_MINUTES} minutos** "
                    "y dejará de funcionar después de utilizarse."
                ),
                color=discord.Color.green(),
            )
            await interaction.followup.send(
                embed=embed,
                view=PersonalVerificationLinkView(issued.verification_url),
                ephemeral=True,
            )


def setup(bot):
    verification = VerificationManager(bot)
    bot.add_view(VerificationPanelView(verification))

    @bot.tree.command(
        name="verificacion_sa",
        description="(Staff) Publica el panel de verificación del servidor.",
    )
    @require_staff()
    async def verificacion_sa(interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=verification.panel_embed(),
            view=VerificationPanelView(verification),
        )
