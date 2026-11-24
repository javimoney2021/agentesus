from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path

import discord
from discord import ui

from core.activity import operation_stage, record
from core.config import (
    POST_MAX_ATTACHMENTS,
    POST_MAX_FILE_BYTES,
    POST_MAX_TOTAL_BYTES,
    require_staff,
)


POST_REACTION_EMOJIS = (
    discord.PartialEmoji(name="E05emoji", id=1284825952903364702),
    discord.PartialEmoji(name="D04", id=998026873118400673),
    discord.PartialEmoji(name="20260319172839", id=1484288004213444738),
)


class AttachmentLimitError(ValueError):
    """El adjunto o la suma de adjuntos supera el límite configurado."""


def validate_attachments(attachments):
    attachments = list(attachments or [])
    if len(attachments) > POST_MAX_ATTACHMENTS:
        raise AttachmentLimitError(
            f"Puedes adjuntar como máximo {POST_MAX_ATTACHMENTS} archivos."
        )

    total_bytes = 0
    for attachment in attachments:
        size = getattr(attachment, "size", 0) or 0
        filename = getattr(attachment, "filename", "archivo")
        if size > POST_MAX_FILE_BYTES:
            limit_mb = POST_MAX_FILE_BYTES // (1024 * 1024)
            raise AttachmentLimitError(
                f"El archivo `{filename}` supera el límite de {limit_mb} MB."
            )
        total_bytes += size

    if total_bytes > POST_MAX_TOTAL_BYTES:
        limit_mb = POST_MAX_TOTAL_BYTES // (1024 * 1024)
        raise AttachmentLimitError(
            f"La suma de los adjuntos supera el límite de {limit_mb} MB."
        )
    return total_bytes


async def send_post_to_channel(channel, content, attachments):
    attachments = list(attachments or [])
    validate_attachments(attachments)

    files = []
    try:
        # Descarga en streaming a disco para no mantener todos los adjuntos
        # en memoria a la vez.
        with tempfile.TemporaryDirectory(prefix="asus_post_") as temp_dir:
            used_names = set()
            for index, attachment in enumerate(attachments, start=1):
                original_name = (
                    getattr(attachment, "filename", "") or f"adjunto_{index}"
                )
                safe_name = Path(original_name).name or f"adjunto_{index}"
                if safe_name in used_names:
                    safe_name = f"{index}_{safe_name}"
                used_names.add(safe_name)
                path = Path(temp_dir) / safe_name
                with open(path, "wb") as destination:
                    await attachment.save(destination)
                files.append(discord.File(path, filename=original_name))

            try:
                record('discord.post_send')
                with operation_stage('discord.post_send'):
                    if files:
                        return await channel.send(content=content or "", files=files)
                    return await channel.send(content=content or "")
            finally:
                # Cerrar antes de que TemporaryDirectory intente borrar
                # los archivos (Windows no permite borrar archivos abiertos).
                for file in files:
                    file.close()
                files.clear()
    finally:
        for file in files:
            file.close()


def is_editable_post_message(message: discord.Message, bot_user_id: int) -> bool:
    return (
        message.author is not None
        and message.author.id == bot_user_id
        and message.type is discord.MessageType.default
    )


async def add_post_reactions(message: discord.Message, post_label):
    for emoji in POST_REACTION_EMOJIS:
        record('discord.reaction')
        try:
            with operation_stage('discord.reaction'):
                await message.add_reaction(emoji)
        except Exception as error:
            print(
                f"⚠️ No se pudo reaccionar con {emoji.name} "
                f"en post {post_label}: {error}"
            )


def pending_is_expired(data):
    return datetime.now(timezone.utc) > data.get("expires_at", datetime.now(timezone.utc))


class AuthorView(ui.View):
    def __init__(self, author_id: int, timeout=300):
        super().__init__(timeout=timeout)
        self.author_id = author_id

    async def guard(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("⛔ Esta acción no es tuya.", ephemeral=True)
            return False
        return True


class ChannelSelect(ui.ChannelSelect):
    def __init__(self, mode: str):
        self.mode = mode
        super().__init__(
            placeholder="Busca o selecciona un canal",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        )

    async def callback(self, interaction: discord.Interaction):
        view: ChannelSelectView = self.view
        if not await view.guard(interaction):
            return

        channel_id = self.values[0].id
        if self.mode == "post_edit":
            if channel_id != interaction.channel_id:
                return await interaction.response.send_message(
                    "⛔ Solo puedes editar mensajes del mismo canal donde ejecutaste `/post_edit`.",
                    ephemeral=True,
                )
            return await interaction.response.send_modal(
                PostEditMessageModal(view.author_id, channel_id)
            )

        return await interaction.response.send_modal(InstantPostModal(
            view.author_id,
            {
                "mode": "instant",
                "target_channel_id": channel_id,
                "author_id": interaction.user.id,
            },
        ))


class ChannelSelectView(AuthorView):
    def __init__(self, author_id: int, mode: str):
        super().__init__(author_id)
        self.add_item(ChannelSelect(mode))


class InstantPostModal(ui.Modal, title="Crear Publicación"):
    def __init__(self, author_id: int, data):
        super().__init__()
        self.author_id = author_id
        self.data = data
        self.content_input = ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="Contenido de la publicación",
            required=False,
            max_length=2000,
        )
        self.file_upload = ui.FileUpload(required=False, min_values=0, max_values=10)
        self.thread_name = ui.TextInput(
            placeholder="Déjalo vacío si no deseas crear un hilo",
            required=False,
            max_length=100,
        )
        self.add_item(ui.Label(text="Contenido", component=self.content_input))
        self.add_item(ui.Label(text="Adjuntos opcionales", component=self.file_upload))
        self.add_item(ui.Label(text="Nombre del hilo (opcional)", component=self.thread_name))

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "⛔ Esta acción no es tuya.",
                ephemeral=True,
            )

        content = self.content_input.value.strip()
        selected_attachments = list(self.file_upload.values)
        if not content and not selected_attachments:
            return await interaction.response.send_message(
                "❌ Debes proporcionar contenido, adjuntos o ambos.",
                ephemeral=True,
            )

        try:
            validate_attachments(selected_attachments)
        except AttachmentLimitError as error:
            return await interaction.response.send_message(
                f"❌ {error}",
                ephemeral=True,
            )

        self.data.update({
            "content": content,
            "attachments": selected_attachments,
            "thread_name": self.thread_name.value.strip() or None,
        })
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.edit_original_response(
            content=build_instant_confirm_text(self.data),
            embed=None,
            view=InstantConfirmView(self.author_id, self.data),
        )


class InstantConfirmView(AuthorView):
    def __init__(self, author_id: int, data):
        super().__init__(author_id, timeout=60)
        self.data = data
        self.processing = False

    async def begin_processing(self, interaction: discord.Interaction) -> bool:
        if self.processing:
            await interaction.response.send_message(
                "⏳ Esta acción ya se está procesando.",
                ephemeral=True,
            )
            return False
        self.processing = True
        for item in self.children:
            item.disabled = True
        try:
            with operation_stage('discord.instant_ack'):
                await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            self.processing = False
            for item in self.children:
                item.disabled = False
            raise
        return True

    @ui.button(label="Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return

        channel = interaction.guild.get_channel(self.data["target_channel_id"])
        if not channel:
            return await interaction.response.edit_message(
                content="❌ No se encontró el canal.",
                embed=None,
                view=None,
            )
        if not await self.begin_processing(interaction):
            return

        try:
            sent_message = await send_post_to_channel(
                channel,
                self.data["content"],
                self.data["attachments"],
            )
            await add_post_reactions(
                sent_message,
                f"inmediato de {interaction.user.id}",
            )
        except AttachmentLimitError as error:
            await interaction.edit_original_response(
                content=f"❌ {error}",
                embed=None,
                view=None,
            )
            self.stop()
            return
        except Exception:
            await interaction.edit_original_response(
                content=(
                    "❌ No se pudo completar el envío. Consulta los registros "
                    "antes de repetir para evitar duplicados."
                ),
                embed=None,
                view=None,
            )
            self.stop()
            return

        thread_error = None
        thread_name = self.data.get("thread_name")
        if thread_name:
            try:
                with operation_stage('discord.thread_create'):
                    await sent_message.create_thread(name=thread_name)
            except Exception as error:
                thread_error = error
                print(
                    "⚠️ La publicación inmediata se envió, pero no se pudo "
                    f"crear el hilo `{thread_name}`: {error}"
                )

        result_message = "✅ Publicación enviada correctamente."
        if thread_error is not None:
            result_message += " ⚠️ No se pudo crear el hilo solicitado."
        await interaction.edit_original_response(
            content=result_message,
            embed=None,
            view=None,
        )
        self.stop()

    @ui.button(label="Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        if not await self.begin_processing(interaction):
            return
        await interaction.edit_original_response(
            content="❌ Publicación cancelada.",
            embed=None,
            view=None,
        )
        self.stop()


def build_instant_confirm_text(data):
    content = data["content"] or "*Sin contenido textual.*"
    thread = f"`{data['thread_name']}`" if data.get("thread_name") else "No"
    details = (
        f"\n\n**Canal:** <#{data['target_channel_id']}>\n"
        f"**Adjuntos:** {len(data['attachments'])}\n"
        f"**Hilo:** {thread}\n\n"
        "¿Publicar ahora?"
    )
    header = "**Vista previa**\n\n"
    max_content = max(0, 2000 - len(header) - len(details))
    return f"{header}{content[:max_content]}{details}"


class PostEditStartView(AuthorView):
    def __init__(self, author_id: int, data):
        super().__init__(author_id)
        self.data = data

    @ui.button(label="Redactar edición", style=discord.ButtonStyle.primary)
    async def edit_content(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(PostEditContentModal(self.author_id, self.data))


class PostEditContentModal(ui.Modal, title="Nuevo contenido"):
    def __init__(self, author_id: int, data):
        super().__init__()
        self.author_id = author_id
        self.data = data
        self.content_input = ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="Escribe el contenido que reemplazará al actual",
            max_length=2000,
        )
        self.add_item(ui.Label(text="Contenido", component=self.content_input))

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "⛔ Esta acción no es tuya.",
                ephemeral=True,
            )
        self.data["new_content"] = self.content_input.value
        self.data["expires_at"] = datetime.now(timezone.utc) + timedelta(minutes=5)
        await interaction.response.send_message(
            "¿Deseas aplicar este nuevo contenido a la publicación?",
            view=PostEditConfirmView(self.author_id, self.data),
            ephemeral=True,
        )


class PostEditMessageModal(ui.Modal, title="Editar Publicación"):
    message_id = ui.TextInput(
        label="ID del mensaje",
        placeholder="Pega aquí el ID de la publicación",
        min_length=17,
        max_length=20,
    )

    def __init__(self, author_id: int, channel_id: int):
        super().__init__()
        self.author_id = author_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "⛔ Esta acción no es tuya.",
                ephemeral=True,
            )

        if interaction.channel_id != self.channel_id:
            return await interaction.response.send_message(
                "⛔ El mensaje debe pertenecer al canal donde ejecutaste `/post_edit`.",
                ephemeral=True,
            )

        message_id_text = self.message_id.value.strip()
        if not message_id_text.isdigit():
            return await interaction.response.send_message(
                "❌ El ID del mensaje solo puede contener números.",
                ephemeral=True,
            )

        channel = interaction.guild.get_channel(self.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "❌ No se encontró el canal de texto seleccionado.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            source_message = await channel.fetch_message(int(message_id_text))
        except discord.NotFound:
            return await interaction.followup.send(
                "❌ No se encontró un mensaje con ese ID en este canal.",
                ephemeral=True,
            )
        except discord.HTTPException as error:
            return await interaction.followup.send(
                f"❌ No se pudo consultar el mensaje: {error}",
                ephemeral=True,
            )

        bot_user = interaction.client.user
        if bot_user is None or not is_editable_post_message(source_message, bot_user.id):
            return await interaction.followup.send(
                "⛔ Solo puedes editar publicaciones enviadas por este bot mediante `/post`.",
                ephemeral=True,
            )

        files = []
        try:
            unavailable_urls = []
            for attachment in source_message.attachments:
                try:
                    files.append(await attachment.to_file(use_cached=True))
                except (discord.HTTPException, OSError):
                    unavailable_urls.append(attachment.url)
            preview_content = source_message.content or "*Publicación sin contenido textual.*"
            if unavailable_urls:
                available = 2000 - len(preview_content) - 2
                if available > 0:
                    preview_content += "\n\n" + "\n".join(unavailable_urls)[:available]
            await interaction.followup.send(
                preview_content,
                files=files,
                ephemeral=True,
            )
        except discord.HTTPException as error:
            return await interaction.followup.send(
                f"❌ No se pudo mostrar la publicación a editar: {error}",
                ephemeral=True,
            )
        finally:
            for file in files:
                file.close()

        data = {
            "mode": "post_edit",
            "target_channel_id": channel.id,
            "message_id": source_message.id,
            "author_id": interaction.user.id,
        }
        await interaction.followup.send(
            "✅ Publicación encontrada. Pulsa el botón para redactar el contenido nuevo.",
            view=PostEditStartView(self.author_id, data),
            ephemeral=True,
        )


class PostEditConfirmView(AuthorView):
    def __init__(self, author_id: int, data):
        super().__init__(author_id, timeout=300)
        self.data = data
        self.processing = False

    async def begin_processing(self, interaction: discord.Interaction) -> bool:
        if self.processing:
            await interaction.response.send_message(
                "⏳ Esta acción ya se está procesando.",
                ephemeral=True,
            )
            return False

        self.processing = True
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            self.processing = False
            for item in self.children:
                item.disabled = False
            raise
        return True

    @ui.button(label="Aceptar", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        if pending_is_expired(self.data):
            self.stop()
            return await interaction.response.edit_message(
                content="⌛ El tiempo para editar la publicación expiró.",
                view=None,
            )
        if not await self.begin_processing(interaction):
            return

        channel = interaction.guild.get_channel(self.data["target_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            self.stop()
            return await interaction.edit_original_response(
                content="❌ No se encontró el canal de la publicación.",
                view=None,
            )

        try:
            source_message = await channel.fetch_message(self.data["message_id"])
        except discord.NotFound:
            self.stop()
            return await interaction.edit_original_response(
                content="❌ La publicación original ya no existe.",
                view=None,
            )
        except discord.HTTPException as error:
            self.processing = False
            for item in self.children:
                item.disabled = False
            return await interaction.edit_original_response(
                content=f"❌ No se pudo consultar la publicación: {error}",
                view=self,
            )

        bot_user = interaction.client.user
        if bot_user is None or not is_editable_post_message(source_message, bot_user.id):
            self.stop()
            return await interaction.edit_original_response(
                content="⛔ La publicación ya no pertenece a este bot.",
                view=None,
            )

        try:
            await source_message.edit(content=self.data["new_content"])
        except discord.HTTPException as error:
            self.processing = False
            for item in self.children:
                item.disabled = False
            return await interaction.edit_original_response(
                content=f"❌ No se pudo editar la publicación: {error}",
                view=self,
            )

        self.stop()
        await interaction.edit_original_response(
            content="✅ Publicación editada correctamente.",
            view=None,
        )

    @ui.button(label="Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        self.stop()
        await interaction.response.edit_message(
            content="❌ Edición cancelada.",
            view=None,
        )


def setup(bot):
    @bot.tree.command(name="post", description="(Staff) Publica un mensaje inmediatamente")
    @require_staff()
    async def post(interaction: discord.Interaction):
        if not interaction.guild or not interaction.guild.text_channels:
            return await interaction.response.send_message(
                "❌ No hay canales de texto disponibles.",
                ephemeral=True,
            )
        await interaction.response.send_message(
            "Selecciona el canal para publicar ahora.",
            view=ChannelSelectView(interaction.user.id, "instant"),
            ephemeral=True,
        )

    @bot.tree.command(
        name="post_edit",
        description="(Staff) Editar una publicación enviada por /post",
    )
    @require_staff()
    async def post_edit(interaction: discord.Interaction):
        if not interaction.guild or not isinstance(
            interaction.channel,
            discord.TextChannel,
        ):
            return await interaction.response.send_message(
                "❌ Este comando solo puede utilizarse en un canal de texto del servidor.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Selecciona el canal donde se encuentra la publicación a editar.",
            view=ChannelSelectView(interaction.user.id, "post_edit"),
            ephemeral=True,
        )
