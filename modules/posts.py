import asyncio
import io
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

import aiohttp
import discord
from discord import ui

from core.config import TZ_BRASILIA, db_unavailable, require_staff
from core.database import (
    add_scheduled_post,
    delete_scheduled_post,
    load_scheduled_posts,
    update_scheduled_post,
)
from core.r2_storage import delete_from_r2, upload_to_r2


SCHEDULED_POSTS_CACHE = []
MAX_PUBLISH_ATTEMPTS = 3
R2_CLEANUP_DELAY_SECONDS = 30
POST_REACTION_EMOJIS = (
    discord.PartialEmoji(name="E05emoji", id=1284825952903364702),
    discord.PartialEmoji(name="D04", id=998026873118400673),
    discord.PartialEmoji(name="20260319172839", id=1484288004213444738),
)

async def load_cache(guild_id=None):
    global SCHEDULED_POSTS_CACHE
    SCHEDULED_POSTS_CACHE = await load_scheduled_posts(guild_id)


def guild_posts(guild_id):
    rows = [p for p in SCHEDULED_POSTS_CACHE if p["guild_id"] == guild_id]
    return sorted(rows, key=lambda x: x["scheduled_at"])


def format_scheduled_at(value, fmt="%d/%m/%Y a las %H:%M"):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(TZ_BRASILIA).strftime(fmt)


def post_title_from_content(content: str):
    first_line = (content or "").strip().splitlines()
    if first_line and first_line[0].strip():
        return first_line[0].strip()[:80]
    return "Publicación agendada"


def build_panel_embed(guild_id):
    rows = guild_posts(guild_id)
    embed = discord.Embed(
        title="Sistema de Publicaciones SUS",
        color=discord.Color.blurple()
    )
    if not rows:
        embed.description = "*No hay agendamientos activos.*"
        return embed

    lines = []
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['title']}")
    embed.description = "\n".join(lines)
    return embed


def scheduled_options(guild_id):
    options = []
    for row in guild_posts(guild_id)[:25]:
        fecha = format_scheduled_at(row["scheduled_at"], "%d/%m/%Y %H:%M")
        options.append(
            discord.SelectOption(
                label=str(row["title"])[:100],
                value=str(row["id"]),
                description=f"{fecha} | Canal {row['channel_id']}"[:100],
            )
        )
    return options


def find_cached_post(post_id: int):
    for post in SCHEDULED_POSTS_CACHE:
        if post["id"] == post_id:
            return post
    return None


async def cleanup_r2_after_delay(urls):
    if not urls:
        return

    await asyncio.sleep(R2_CLEANUP_DELAY_SECONDS)
    for url in urls:
        await delete_from_r2(url)


async def cleanup_r2_now(urls):
    if not urls:
        return
    for url in urls:
        await delete_from_r2(url)


async def upload_attachments(attachments: list[discord.Attachment]):
    r2_urls = []
    if not attachments:
        return r2_urls

    try:
        for attachment in attachments:
            file_bytes = await attachment.read()
            r2_url = await upload_to_r2(file_bytes, attachment.filename)
            r2_urls.append(r2_url)
    except Exception:
        await cleanup_r2_now(r2_urls)
        raise
    return r2_urls


async def files_from_r2_urls(urls):
    files = []
    if not urls:
        return files

    async with aiohttp.ClientSession() as session:
        for url in urls:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        parsed = urlparse(url)
                        filename = unquote(parsed.path.split("/")[-1])
                        files.append(discord.File(io.BytesIO(data), filename=filename))
                    else:
                        print(f"⚠️ No se pudo descargar adjunto: {url} | Status: {resp.status}")
            except Exception as e:
                print(f"⚠️ Error descargando adjunto {url}: {e}")
    return files


async def send_post_to_channel(channel, content, attachment_urls):
    files = await files_from_r2_urls(attachment_urls)
    if files:
        return await channel.send(content=content or "", files=files)
    return await channel.send(content=content or "")


def is_editable_post_message(message: discord.Message, bot_user_id: int) -> bool:
    return (
        message.author is not None
        and message.author.id == bot_user_id
        and message.type is discord.MessageType.default
    )


async def add_post_reactions(message: discord.Message, post_label):
    for emoji in POST_REACTION_EMOJIS:
        try:
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


class PostPanelView(AuthorView):
    def __init__(self, author_id: int, guild_id: int):
        super().__init__(author_id)
        self.guild_id = guild_id

    @ui.button(label="📢 Post", style=discord.ButtonStyle.primary)
    async def instant_post(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        if not interaction.guild.text_channels:
            return await interaction.response.edit_message(
                content="❌ No hay canales de texto disponibles.",
                embed=None,
                view=PostPanelView(self.author_id, interaction.guild_id)
            )
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(title="Selecciona el canal para publicar ahora", color=discord.Color.blurple()),
            view=ChannelSelectView(self.author_id, "instant")
        )

    @ui.button(label="💾 Agendar", style=discord.ButtonStyle.success)
    async def schedule_post(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        if not interaction.guild.text_channels:
            return await interaction.response.edit_message(
                content="❌ No hay canales de texto disponibles.",
                embed=None,
                view=PostPanelView(self.author_id, interaction.guild_id)
            )
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(title="Selecciona el canal para agendar", color=discord.Color.green()),
            view=ChannelSelectView(self.author_id, "schedule")
        )

    @ui.button(label="📝 Editar", style=discord.ButtonStyle.secondary)
    async def edit_post(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        if not guild_posts(interaction.guild_id):
            return await interaction.response.edit_message(
                content=None,
                embed=build_panel_embed(interaction.guild_id),
                view=self
            )
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(title="Selecciona el agendamiento a editar", color=discord.Color.orange()),
            view=ScheduledSelectView(self.author_id, "edit", interaction.guild_id)
        )

    @ui.button(label="❌ Eliminar", style=discord.ButtonStyle.danger)
    async def delete_post(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        if not guild_posts(interaction.guild_id):
            return await interaction.response.edit_message(
                content=None,
                embed=build_panel_embed(interaction.guild_id),
                view=self
            )
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(title="Selecciona el agendamiento a eliminar", color=discord.Color.red()),
            view=ScheduledSelectView(self.author_id, "delete", interaction.guild_id)
        )


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

        if self.mode == "instant":
            return await interaction.response.send_modal(InstantPostModal(
                view.author_id,
                {
                "mode": "instant",
                "target_channel_id": channel_id,
                "author_id": interaction.user.id,
                },
            ))

        await interaction.response.send_modal(ScheduleModal(view.author_id, channel_id))


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

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            attachments = await upload_attachments(selected_attachments)
        except Exception as error:
            print(f"❌ Error procesando adjuntos del modal: {error}")
            return await interaction.edit_original_response(
                content="❌ No se pudieron procesar los adjuntos.",
            )

        self.data.update({
            "content": content,
            "attachments": attachments,
            "thread_name": self.thread_name.value.strip() or None,
            "title": post_title_from_content(content),
        })
        await interaction.edit_original_response(
            content=None,
            embed=build_instant_confirm_embed(self.data),
            view=InstantConfirmView(self.author_id, self.data),
        )


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

        try:
            files = []
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


class ScheduleModal(ui.Modal, title="Agendar Publicación"):
    def __init__(self, author_id: int, channel_id: int):
        super().__init__()
        self.author_id = author_id
        self.channel_id = channel_id
        self.nombre = ui.TextInput(placeholder="Nombre visible", max_length=100)
        self.fecha_hora = ui.TextInput(placeholder="DD/MM/AAAA HH:MM", max_length=16)
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
        self.add_item(ui.Label(text="Nombre del post", component=self.nombre))
        self.add_item(ui.Label(text="Fecha y hora", component=self.fecha_hora))
        self.add_item(ui.Label(text="Contenido", component=self.content_input))
        self.add_item(ui.Label(text="Adjuntos opcionales", component=self.file_upload))
        self.add_item(ui.Label(text="Nombre del hilo (opcional)", component=self.thread_name))

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("⛔ Esta acción no es tuya.", ephemeral=True)

        try:
            dt_obj = datetime.strptime(self.fecha_hora.value.strip(), "%d/%m/%Y %H:%M")
            scheduled_at = dt_obj.replace(tzinfo=TZ_BRASILIA)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Formato inválido. Usa Fecha `DD/MM/AAAA` y Hora `HH:MM`.",
                ephemeral=True
            )

        if scheduled_at <= datetime.now(TZ_BRASILIA):
            return await interaction.response.send_message("❌ La fecha y hora deben ser futuras.", ephemeral=True)

        title = self.nombre.value.strip()
        if not title:
            return await interaction.response.send_message("❌ El nombre del post no puede estar vacío.", ephemeral=True)

        content = self.content_input.value.strip()
        selected_attachments = list(self.file_upload.values)
        if not content and not selected_attachments:
            return await interaction.response.send_message(
                "❌ Debes proporcionar contenido, adjuntos o ambos.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            attachments = await upload_attachments(selected_attachments)
        except Exception as error:
            print(f"❌ Error procesando adjuntos del modal: {error}")
            return await interaction.edit_original_response(
                content="❌ No se pudieron procesar los adjuntos.",
            )

        data = {
            "mode": "schedule",
            "channel_id": self.channel_id,
            "scheduled_at": scheduled_at,
            "title": title,
            "content": content,
            "attachments": attachments,
            "thread_name": self.thread_name.value.strip() or None,
            "author_id": interaction.user.id,
        }
        await interaction.edit_original_response(
            content=None,
            embed=build_schedule_confirm_embed(data),
            view=ScheduleConfirmView(self.author_id, data),
        )


class ScheduledSelect(ui.Select):
    def __init__(self, mode: str, guild_id: int):
        self.mode = mode
        super().__init__(
            placeholder="Selecciona una publicación agendada",
            min_values=1,
            max_values=1,
            options=scheduled_options(guild_id)
        )

    async def callback(self, interaction: discord.Interaction):
        view: ScheduledSelectView = self.view
        if not await view.guard(interaction):
            return

        post = find_cached_post(int(self.values[0]))
        if not post:
            return await interaction.response.edit_message(
                content="❌ No se encontró el agendamiento seleccionado.",
                embed=None,
                view=PostPanelView(view.author_id, interaction.guild_id)
            )

        if self.mode == "delete":
            embed = discord.Embed(
                title="¿Está seguro?",
                description=f"Eliminar **{post['title']}** de <#{post['channel_id']}>.",
                color=discord.Color.red()
            )
            return await interaction.response.edit_message(
                content=None,
                embed=embed,
                view=DeleteConfirmView(view.author_id, post["id"])
            )

        data = {
            "mode": "edit",
            "post_id": post["id"],
            "author_id": interaction.user.id,
        }
        await interaction.response.send_modal(
            ScheduledContentModal(view.author_id, data, post)
        )


class ScheduledSelectView(AuthorView):
    def __init__(self, author_id: int, mode: str, guild_id: int):
        super().__init__(author_id)
        self.add_item(ScheduledSelect(mode, guild_id))


class ScheduledContentModal(ui.Modal, title="Editar Agendamiento"):
    def __init__(self, author_id: int, data, post):
        super().__init__()
        self.author_id = author_id
        self.data = data
        self.post = post
        self.content_input = ui.TextInput(
            style=discord.TextStyle.paragraph,
            default=post["content"] or "",
            placeholder="Contenido de la publicación",
            required=False,
            max_length=2000,
        )
        self.file_upload = ui.FileUpload(required=False, min_values=0, max_values=10)
        self.add_item(ui.Label(text="Contenido", component=self.content_input))
        self.add_item(ui.Label(
            text="Nuevos adjuntos (opcional)",
            description="Si no adjuntas archivos, la publicación quedará sin adjuntos.",
            component=self.file_upload,
        ))

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

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            attachments = await upload_attachments(selected_attachments)
        except Exception as error:
            print(f"❌ Error procesando adjuntos del modal: {error}")
            return await interaction.edit_original_response(
                content="❌ No se pudieron procesar los adjuntos.",
            )

        self.data.update({
            "content": content,
            "attachments": attachments,
            "title": self.post["title"],
            "channel_id": self.post["channel_id"],
        })
        await interaction.edit_original_response(
            content=(
                f"El agendamiento **{self.post['title']}** será publicado en "
                f"<#{self.post['channel_id']}> el "
                f"**{format_scheduled_at(self.post['scheduled_at'])}**.\n\n"
                "¿Deseas conservar esos datos?"
            ),
            embed=None,
            view=EditDateChoiceView(self.author_id, self.data),
        )


def build_schedule_confirm_embed(data):
    fecha_str = data["scheduled_at"].strftime("%d/%m/%Y a las %H:%M")
    return discord.Embed(
        title="Confirmar Agendamiento ?",
        description=(
            f"**Título:** {data['title']}\n"
            f"**Canal:** <#{data['channel_id']}>\n"
            f"**Fecha:** {fecha_str}\n"
            f"**Hilo:** {f'`{data.get('thread_name')}`' if data.get('thread_name') else 'No'}\n"
            f"**Adjuntos:** {len(data['attachments'])} archivo(s) guardado(s) ✅\n\n"
            "¿Deseas programar esta publicación?"
        ),
        color=discord.Color.orange()
    )


class ScheduleConfirmView(AuthorView):
    def __init__(self, author_id: int, data):
        super().__init__(author_id, timeout=60)
        self.data = data
        self.processing = False

    async def on_timeout(self):
        await cleanup_r2_now(self.data.get("attachments"))

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
        if await db_unavailable(interaction):
            return
        if not await self.begin_processing(interaction):
            return

        try:
            post_id = await add_scheduled_post(
                interaction.guild_id,
                self.data["channel_id"],
                self.data["title"],
                self.data["content"],
                self.data["attachments"],
                self.data["scheduled_at"],
                interaction.user.id,
                self.data.get("thread_name")
            )
        except Exception as error:
            print(f"❌ Error guardando agendamiento: {error}")
            await cleanup_r2_now(self.data.get("attachments"))
            await interaction.edit_original_response(
                content="❌ No se pudo guardar el agendamiento.",
                embed=None,
                view=None,
            )
            self.stop()
            return

        if post_id:
            SCHEDULED_POSTS_CACHE.append({
                "id": post_id,
                "guild_id": interaction.guild_id,
                "channel_id": self.data["channel_id"],
                "title": self.data["title"],
                "content": self.data["content"],
                "attachment_urls": self.data["attachments"],
                "scheduled_at": self.data["scheduled_at"],
                "author_id": interaction.user.id,
                "thread_name": self.data.get("thread_name")
            })
        else:
            await cleanup_r2_now(self.data.get("attachments"))
            await interaction.edit_original_response(
                content="❌ No se pudo guardar el agendamiento.",
                embed=None,
                view=None
            )
            self.stop()
            return

        fecha = self.data["scheduled_at"].strftime("%d/%m/%y")
        hora = self.data["scheduled_at"].strftime("%H:%M")
        try:
            await interaction.edit_original_response(
                content=f"✅ Tu publicación ha sido agendada con éxito el día **{fecha}** a las **{hora}**",
                embed=None,
                view=None,
            )
        except discord.HTTPException as error:
            print(
                "⚠️ El agendamiento se guardó, pero no se pudo actualizar "
                f"la confirmación en Discord: {error}"
            )
        self.stop()

    @ui.button(label="Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        if not await self.begin_processing(interaction):
            return
        await cleanup_r2_now(self.data.get("attachments"))
        await interaction.edit_original_response(
            content="❌ Agendamiento cancelado...",
            embed=None,
            view=None,
        )
        self.stop()


class InstantConfirmView(AuthorView):
    def __init__(self, author_id: int, data):
        super().__init__(author_id, timeout=60)
        self.data = data
        self.processing = False

    async def on_timeout(self):
        await cleanup_r2_now(self.data.get("attachments"))

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

    @ui.button(label="Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return

        channel = interaction.guild.get_channel(self.data["target_channel_id"])
        if not channel:
            return await interaction.response.edit_message(content="❌ No se encontró el canal.", embed=None, view=None)
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
        except Exception as e:
            await cleanup_r2_now(self.data.get("attachments"))
            await interaction.edit_original_response(
                content=f"❌ Error al publicar: {e}",
                embed=None,
                view=None,
            )
            self.stop()
            return

        thread_error = None
        thread_name = self.data.get("thread_name")
        if thread_name:
            try:
                await sent_message.create_thread(name=thread_name)
            except Exception as error:
                thread_error = error
                print(
                    "⚠️ La publicación inmediata se envió, pero no se pudo "
                    f"crear el hilo `{thread_name}`: {error}"
                )

        interaction.client.loop.create_task(cleanup_r2_after_delay(self.data.get("attachments")))
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
        await cleanup_r2_now(self.data.get("attachments"))
        await interaction.edit_original_response(
            content="❌ Publicación cancelada.",
            embed=None,
            view=None,
        )
        self.stop()


def build_instant_confirm_embed(data):
    embed = discord.Embed(
        title="Vista previa",
        description=data["content"] or "*Sin contenido textual.*",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Canal",
        value=f"<#{data['target_channel_id']}>",
        inline=True,
    )
    embed.add_field(
        name="Adjuntos",
        value=str(len(data["attachments"])),
        inline=True,
    )
    embed.add_field(
        name="Hilo",
        value=f"`{data['thread_name']}`" if data.get("thread_name") else "No",
        inline=False,
    )
    return embed


def build_post_confirmation(author_id: int, data):
    if data["mode"] == "instant":
        return build_instant_confirm_embed(data), InstantConfirmView(author_id, data)
    return build_schedule_confirm_embed(data), ScheduleConfirmView(author_id, data)


class EditDateChoiceView(AuthorView):
    def __init__(self, author_id: int, data):
        super().__init__(author_id)
        self.data = data

    async def on_timeout(self):
        await cleanup_r2_now(self.data.get("attachments"))

    @ui.button(label="Conservar", style=discord.ButtonStyle.success)
    async def keep(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        post = find_cached_post(self.data["post_id"])
        if not post:
            return await interaction.response.edit_message(content="❌ No se encontró el agendamiento.", embed=None, view=None)
        self.data["scheduled_at"] = post["scheduled_at"]
        await interaction.response.edit_message(
            content=None,
            embed=build_edit_confirm_embed(self.data),
            view=EditConfirmView(self.author_id, self.data)
        )
        self.stop()

    @ui.button(label="Editar", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(
            EditScheduleModal(self.author_id, self.data, self)
        )


class EditScheduleModal(ui.Modal, title="Editar Fecha"):
    fecha = ui.TextInput(label="Fecha", placeholder="DD/MM/AAAA", max_length=10)
    hora = ui.TextInput(label="Hora", placeholder="HH:MM", max_length=5)

    def __init__(self, author_id: int, data, parent_view: EditDateChoiceView):
        super().__init__()
        self.author_id = author_id
        self.data = data
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("⛔ Esta acción no es tuya.", ephemeral=True)
        try:
            dt_obj = datetime.strptime(f"{self.fecha.value} {self.hora.value}", "%d/%m/%Y %H:%M")
            scheduled_at = dt_obj.replace(tzinfo=TZ_BRASILIA)
        except ValueError:
            return await interaction.response.send_message("❌ Formato inválido.", ephemeral=True)
        if scheduled_at <= datetime.now(TZ_BRASILIA):
            return await interaction.response.send_message("❌ La fecha y hora deben ser futuras.", ephemeral=True)
        self.data["scheduled_at"] = scheduled_at
        self.parent_view.stop()
        await interaction.response.edit_message(
            content=None,
            embed=build_edit_confirm_embed(self.data),
            view=EditConfirmView(self.author_id, self.data)
        )


def build_edit_confirm_embed(data):
    post = find_cached_post(data["post_id"])
    channel_id = post["channel_id"] if post else data.get("channel_id")
    return discord.Embed(
        title="Confirmar Edición",
        description=(
            f"El agendamiento **{data['title']}** será publicado en <#{channel_id}> "
            f"el **{format_scheduled_at(data['scheduled_at'])}**.\n\n"
            "¿Deseas confirmar los cambios?"
        ),
        color=discord.Color.orange()
    )


class EditConfirmView(AuthorView):
    def __init__(self, author_id: int, data):
        super().__init__(author_id, timeout=60)
        self.data = data

    async def on_timeout(self):
        await cleanup_r2_now(self.data.get("attachments"))

    @ui.button(label="Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        if await db_unavailable(interaction):
            return

        post = find_cached_post(self.data["post_id"])
        if not post:
            return await interaction.response.edit_message(content="❌ No se encontró el agendamiento.", embed=None, view=None)

        old_attachments = post.get("attachment_urls")
        row = await update_scheduled_post(
            post["id"],
            self.data["title"],
            self.data["content"],
            self.data["attachments"],
            self.data["scheduled_at"],
            post.get("thread_name")
        )
        if not row:
            await cleanup_r2_now(self.data.get("attachments"))
            return await interaction.response.edit_message(content="❌ No se pudo actualizar el agendamiento.", embed=None, view=None)

        post.update(dict(row))
        interaction.client.loop.create_task(cleanup_r2_after_delay(old_attachments))
        await interaction.response.edit_message(content="✅ Agendamiento actualizado correctamente.", embed=None, view=None)
        self.stop()

    @ui.button(label="Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        await cleanup_r2_now(self.data.get("attachments"))
        await interaction.response.edit_message(content="❌ Edición cancelada.", embed=None, view=None)
        self.stop()


class DeleteConfirmView(AuthorView):
    def __init__(self, author_id: int, post_id: int):
        super().__init__(author_id, timeout=60)
        self.post_id = post_id

    @ui.button(label="Eliminar", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return

        post = find_cached_post(self.post_id)
        if not post:
            return await interaction.response.edit_message(content="❌ No se encontró el agendamiento.", embed=None, view=None)

        await delete_scheduled_post(post["id"])
        if post in SCHEDULED_POSTS_CACHE:
            SCHEDULED_POSTS_CACHE.remove(post)
        await cleanup_r2_now(post.get("attachment_urls"))
        await interaction.response.edit_message(content="🗑️ Agendamiento eliminado completamente.", embed=None, view=None)
        self.stop()

    @ui.button(label="Conservar", style=discord.ButtonStyle.success)
    async def keep(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        await interaction.response.edit_message(
            content=None,
            embed=build_panel_embed(interaction.guild_id),
            view=PostPanelView(self.author_id, interaction.guild_id)
        )
        self.stop()


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
            self.clear_pending()
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


async def publish_due_posts(bot):
    now = datetime.now(TZ_BRASILIA)
    if not (0 <= now.hour < 24):
        return

    from core import database
    if database.bot_pool is None:
        return

    now_utc = datetime.now(timezone.utc)
    posts_to_publish = [
        p for p in SCHEDULED_POSTS_CACHE
        if p["scheduled_at"] <= now_utc
    ]

    for post in posts_to_publish:
        published = False
        guild = bot.get_guild(post["guild_id"])
        if guild:
            channel = guild.get_channel(post["channel_id"])
            if channel:
                try:
                    sent_message = await send_post_to_channel(channel, post["content"] or "", post.get("attachment_urls"))
                    published = True

                    await add_post_reactions(sent_message, post["id"])

                    thread_name = post.get("thread_name")
                    if thread_name:
                        try:
                            await sent_message.create_thread(name=thread_name)
                        except Exception as e:
                            print(f"⚠️ No se pudo crear hilo para post {post['id']}: {e}")

                except Exception as e:
                    print(f"❌ Error al publicar post agendado {post['id']}: {e}")
        else:
            print(f"⚠️ No se encontró guild para post agendado {post['id']}: {post['guild_id']}")

        if guild and not published and not guild.get_channel(post["channel_id"]):
            print(f"⚠️ No se encontró canal para post agendado {post['id']}: {post['channel_id']}")

        if published:
            bot.loop.create_task(cleanup_r2_after_delay(post.get("attachment_urls")))
            await delete_scheduled_post(post["id"])
            if post in SCHEDULED_POSTS_CACHE:
                SCHEDULED_POSTS_CACHE.remove(post)
            continue

        post["publish_attempts"] = post.get("publish_attempts", 0) + 1
        if post["publish_attempts"] < MAX_PUBLISH_ATTEMPTS:
            print(
                f"⏳ Reintentando post agendado {post['id']} en el próximo ciclo "
                f"({post['publish_attempts']}/{MAX_PUBLISH_ATTEMPTS})."
            )
            continue

        print(f"🗑️ Post agendado {post['id']} falló {MAX_PUBLISH_ATTEMPTS} veces. Se eliminará.")
        bot.loop.create_task(cleanup_r2_after_delay(post.get("attachment_urls")))
        await delete_scheduled_post(post["id"])
        if post in SCHEDULED_POSTS_CACHE:
            SCHEDULED_POSTS_CACHE.remove(post)


def setup(bot):
    @bot.tree.command(name="post", description="(Staff) Panel de publicaciones")
    @require_staff()
    async def post(interaction: discord.Interaction):
        if await db_unavailable(interaction):
            return
        await interaction.response.send_message(
            embed=build_panel_embed(interaction.guild_id),
            view=PostPanelView(interaction.user.id, interaction.guild_id),
            ephemeral=True
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
