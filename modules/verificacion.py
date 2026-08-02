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
                label="Verificarme Ahora",
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


class ClearVerificationRecordsView(discord.ui.View):
    def __init__(
        self,
        manager: "VerificationManager",
        requested_by: int,
        target: discord.Member,
    ):
        super().__init__(timeout=60)
        self.manager = manager
        self.requested_by = requested_by
        self.target = target
        self._operation_lock = asyncio.Lock()
        self._completed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requested_by:
            return True
        await interaction.response.send_message(
            "Solo quien ejecutó el comando puede confirmar esta limpieza.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        async with self._operation_lock:
            if self._completed:
                await interaction.response.send_message(
                    "Esta solicitud ya fue procesada.",
                    ephemeral=True,
                )
                return

            self._completed = True
            await interaction.response.defer()
            try:
                attempts, tokens = await database.clear_verification_records(
                    self.target.guild.id,
                    self.target.id,
                )
            except Exception:
                self._completed = False
                logger.exception(
                    "No se pudo limpiar la verificacion del usuario %s.",
                    self.target.id,
                )
                await interaction.edit_original_response(
                    content=(
                        "No fue posible limpiar los registros. "
                        "Inténtalo nuevamente más tarde."
                    ),
                    embed=None,
                    view=None,
                )
                return

            self.manager.clear_user_runtime_state(self.target.id)
            logger.warning(
                (
                    "Registros de verificacion eliminados | ejecutor=%s | "
                    "usuario=%s | intentos=%s | tokens=%s"
                ),
                interaction.user.id,
                self.target.id,
                attempts,
                tokens,
            )
            await interaction.edit_original_response(
                content=(
                    f"Limpieza completada para {self.target.mention}.\n"
                    f"Intentos eliminados: **{attempts}**\n"
                    f"Tokens eliminados: **{tokens}**\n\n"
                    "El rol de Discord no fue modificado."
                ),
                embed=None,
                view=None,
            )
            self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ):
        if self._completed:
            await interaction.response.send_message(
                "Esta solicitud ya fue procesada.",
                ephemeral=True,
            )
            return
        self._completed = True
        await interaction.response.edit_message(
            content="Limpieza cancelada.",
            embed=None,
            view=None,
        )
        self.stop()


class VerificationManager:
    def __init__(self, bot):
        self.bot = bot
        self._issue_locks = {}
        self._last_issued_at = {}

    def clear_user_runtime_state(self, user_id: int) -> None:
        self._last_issued_at.pop(user_id, None)
        lock = self._issue_locks.get(user_id)
        if lock is None or not lock.locked():
            self._issue_locks.pop(user_id, None)

    @staticmethod
    def panel_embed() -> discord.Embed:
        embed = discord.Embed(
            title="Verificación Super Sus SA Oficial",
            description="Conviértete en un usuario Verificado de la Comunidad...",
            color=discord.Color.blue(),
        )
        embed.set_thumbnail(
            url=(
                "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/"
                "verify.png"
            )
        )
        embed.set_footer(text="Desarrollado por Agente SUS")
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
                title="Proceso de Verificación.",
                description=(
                    "Presiona **Verificarme Ahora** para recibir un enlace personal "
                    "y temporal visible únicamente para ti. Te recomendamos leer "
                    "y aceptar el aviso de privacidad."
                ),
                color=discord.Color.green(),
            )
            embed.set_footer(text="El enlace solo puede utilizarse una vez.")
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
    @discord.app_commands.describe(
        canal="Canal donde se publicará el panel permanente de verificación.",
    )
    async def verificacion_sa(
        interaction: discord.Interaction,
        canal: discord.TextChannel,
    ):
        bot_member = canal.guild.me
        if bot_member is None:
            await interaction.response.send_message(
                "No fue posible localizar al bot dentro del servidor.",
                ephemeral=True,
            )
            return

        permissions = canal.permissions_for(bot_member)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
        ):
            await interaction.response.send_message(
                (
                    "El bot necesita **Ver canal**, **Enviar mensajes** e "
                    "**Insertar enlaces** en el canal seleccionado."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await canal.send(
                embed=verification.panel_embed(),
                view=VerificationPanelView(verification),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Discord rechazó la publicación por falta de permisos en ese canal.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger.exception(
                "No se pudo publicar el panel de verificacion en el canal %s.",
                canal.id,
            )
            await interaction.followup.send(
                "No fue posible publicar el panel. Inténtalo nuevamente más tarde.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Panel permanente de verificación publicado en {canal.mention}.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="limpiar_registro",
        description="(Admin) Elimina el historial web de verificación de un usuario.",
    )
    @discord.app_commands.describe(
        usuario="Usuario cuyos intentos y tokens serán eliminados.",
    )
    async def limpiar_registro(
        interaction: discord.Interaction,
        usuario: discord.Member,
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Este comando está limitado a administradores.",
                ephemeral=True,
            )
            return

        if database.bot_pool is None:
            await interaction.response.send_message(
                DB_NO_DISPONIBLE,
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Confirmar limpieza de verificación",
            description=(
                f"Se eliminarán de PostgreSQL todos los intentos y tokens de "
                f"{usuario.mention} (`{usuario.id}`).\n\n"
                "Esta acción es irreversible y no retirará su rol de Discord."
            ),
            color=discord.Color.red(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=ClearVerificationRecordsView(
                verification,
                interaction.user.id,
                usuario,
            ),
            ephemeral=True,
        )
