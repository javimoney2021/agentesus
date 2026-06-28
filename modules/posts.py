import asyncio
import io
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

import aiohttp
import discord
from discord import app_commands, ui

from core.config import TZ_BRASILIA, db_unavailable, require_staff
from core.database import (
    add_scheduled_post,
    delete_scheduled_post,
    load_scheduled_posts,
    update_scheduled_post,
)
from core.r2_storage import delete_from_r2, upload_to_r2


PENDING_POSTS = {}
SCHEDULED_POSTS_CACHE = []
MAX_PUBLISH_ATTEMPTS = 3
R2_CLEANUP_DELAY_SECONDS = 30
PENDING_TIMEOUT_SECONDS = 300


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


def channel_options(guild):
    options = []
    for channel in guild.text_channels[:25]:
        options.append(
            discord.SelectOption(
                label=channel.name[:100],
                value=str(channel.id),
                description=f"#{channel.name}"[:100],
            )
        )
    return options


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


async def upload_message_attachments(message: discord.Message):
    r2_urls = []
    if not message.attachments:
        return r2_urls

    async with aiohttp.ClientSession() as session:
        for attachment in message.attachments:
            try:
                async with session.get(attachment.url) as resp:
                    if resp.status == 200:
                        file_bytes = await resp.read()
                        r2_url = await upload_to_r2(file_bytes, attachment.filename)
                        r2_urls.append(r2_url)
                        print(f"✅ Adjunto subido a R2: {r2_url}")
                    else:
                        print(f"⚠️ No se pudo descargar adjunto de Discord: {attachment.url}")
            except Exception as e:
                print(f"⚠️ Error subiendo adjunto a R2: {e}")
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


def pending_is_expired(data):
    return datetime.now(timezone.utc) > data.get("expires_at", datetime.now(timezone.utc))


def set_pending(user_id, data):
    data["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=PENDING_TIMEOUT_SECONDS)
    PENDING_POSTS[user_id] = data


async def edit_pending_panel(data, *, content=None, embed=None, view=None):
    panel_message = data.get("panel_message")
    if panel_message is not None:
        try:
            await panel_message.edit(content=content, embed=embed, view=view)
            return
        except Exception as e:
            print(f"⚠️ No se pudo editar mensaje ephemeral de posts: {e}")

    interaction = data.get("panel_interaction")
    if interaction is None:
        return
    try:
        await interaction.edit_original_response(content=content, embed=embed, view=view)
    except Exception as e:
        print(f"⚠️ No se pudo editar panel ephemeral de posts: {e}")


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
        if not channel_options(interaction.guild):
            return await interaction.response.edit_message(
                content="❌ No hay canales de texto disponibles.",
                embed=None,
                view=PostPanelView(self.author_id, interaction.guild_id)
            )
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(title="Selecciona el canal para publicar ahora", color=discord.Color.blurple()),
            view=ChannelSelectView(self.author_id, "instant", interaction.guild)
        )

    @ui.button(label="💾 Agendar", style=discord.ButtonStyle.success)
    async def schedule_post(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        if not channel_options(interaction.guild):
            return await interaction.response.edit_message(
                content="❌ No hay canales de texto disponibles.",
                embed=None,
                view=PostPanelView(self.author_id, interaction.guild_id)
            )
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(title="Selecciona el canal para agendar", color=discord.Color.green()),
            view=ChannelSelectView(self.author_id, "schedule", interaction.guild)
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


class ChannelSelect(ui.Select):
    def __init__(self, mode: str, options):
        self.mode = mode
        super().__init__(placeholder="Selecciona un canal", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: ChannelSelectView = self.view
        if not await view.guard(interaction):
            return

        channel_id = int(self.values[0])
        if self.mode == "instant":
            set_pending(interaction.user.id, {
                "mode": "instant",
                "step": "awaiting_content",
                "target_channel_id": channel_id,
                "origin_channel_id": interaction.channel_id,
                "author_id": interaction.user.id,
                "panel_interaction": interaction,
                "panel_message": interaction.message,
            })
            return await interaction.response.edit_message(
                content="✅ Ahora posees 5 minutos para realizar tu publicación.",
                embed=None,
                view=None
            )

        await interaction.response.send_modal(ScheduleModal(view.author_id, channel_id, interaction))


class ChannelSelectView(AuthorView):
    def __init__(self, author_id: int, mode: str, guild):
        super().__init__(author_id)
        select = ChannelSelect(mode, channel_options(guild))
        self.add_item(select)


class ScheduleModal(ui.Modal, title="Agendar Publicación"):
    nombre = ui.TextInput(label="Nombre del Post", placeholder="Nombre visible del agendamiento", max_length=100)
    fecha = ui.TextInput(label="Fecha", placeholder="DD/MM/AAAA", max_length=10)
    hora = ui.TextInput(label="Hora", placeholder="HH:MM", max_length=5)

    def __init__(self, author_id: int, channel_id: int, panel_interaction: discord.Interaction):
        super().__init__()
        self.author_id = author_id
        self.channel_id = channel_id
        self.panel_interaction = panel_interaction
        self.panel_message = panel_interaction.message

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("⛔ Esta acción no es tuya.", ephemeral=True)

        try:
            dt_obj = datetime.strptime(f"{self.fecha.value} {self.hora.value}", "%d/%m/%Y %H:%M")
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

        set_pending(interaction.user.id, {
            "mode": "schedule",
            "step": "awaiting_content",
            "channel_id": self.channel_id,
            "origin_channel_id": self.panel_interaction.channel_id,
            "scheduled_at": scheduled_at,
            "title": title,
            "author_id": interaction.user.id,
            "panel_interaction": interaction,
            "panel_message": getattr(interaction, "message", None) or self.panel_message,
        })
        await interaction.response.edit_message(
            content="✅ Ahora posees 5 minutos para agendar tu publicación.",
            embed=None,
            view=None
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

        set_pending(interaction.user.id, {
            "mode": "edit",
            "step": "awaiting_edit_content",
            "post_id": post["id"],
            "origin_channel_id": interaction.channel_id,
            "author_id": interaction.user.id,
            "panel_interaction": interaction,
            "panel_message": interaction.message,
        })
        embed = discord.Embed(
            title=f"Editando: {post['title']}",
            description=post["content"] or "*Sin contenido textual.*",
            color=discord.Color.orange()
        )
        embed.add_field(name="Canal", value=f"<#{post['channel_id']}>", inline=True)
        embed.add_field(name="Fecha", value=format_scheduled_at(post["scheduled_at"]), inline=True)
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        await asyncio.sleep(1)
        await interaction.edit_original_response(content="📝 Redacte la edición de su post.", embed=None, view=None)


class ScheduledSelectView(AuthorView):
    def __init__(self, author_id: int, mode: str, guild_id: int):
        super().__init__(author_id)
        self.add_item(ScheduledSelect(mode, guild_id))


class ThreadChoiceView(AuthorView):
    """Vista con botones SI / NO para decidir si se crea un hilo al publicar."""
    @ui.button(label="SI", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        if self.author_id in PENDING_POSTS:
            PENDING_POSTS[self.author_id]["step"] = "awaiting_thread_name"
            PENDING_POSTS[self.author_id]["panel_interaction"] = interaction
            PENDING_POSTS[self.author_id]["panel_message"] = interaction.message
        await interaction.response.edit_message(
            content="📝 Escribe el **nombre del hilo** que se creará al publicar:",
            embed=None, view=None
        )
        self.stop()

    @ui.button(label="NO", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        data = PENDING_POSTS.pop(self.author_id, None)
        if data is None:
            await interaction.response.edit_message(content="❌ No se encontró el agendamiento pendiente.", embed=None, view=None)
            self.stop()
            return
        data["thread_name"] = None
        data["panel_interaction"] = interaction
        data["panel_message"] = interaction.message
        await interaction.response.edit_message(
            content=None,
            embed=build_schedule_confirm_embed(data),
            view=ScheduleConfirmView(self.author_id, data)
        )
        self.stop()


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

    @ui.button(label="Aceptar", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        if await db_unavailable(interaction):
            return

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
            return await interaction.response.edit_message(
                content="❌ No se pudo guardar el agendamiento.",
                embed=None,
                view=None
            )

        fecha = self.data["scheduled_at"].strftime("%d/%m/%y")
        hora = self.data["scheduled_at"].strftime("%H:%M")
        await interaction.response.edit_message(
            content=f"✅ Tu publicación ha sido agendada con éxito el día **{fecha}** a las **{hora}**",
            embed=None, view=None
        )
        self.stop()

    @ui.button(label="Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        await cleanup_r2_now(self.data.get("attachments"))
        await interaction.response.edit_message(content="❌ Agendamiento cancelado...", embed=None, view=None)
        self.stop()


class InstantConfirmView(AuthorView):
    def __init__(self, author_id: int, data):
        super().__init__(author_id, timeout=60)
        self.data = data

    @ui.button(label="Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return

        channel = interaction.guild.get_channel(self.data["target_channel_id"])
        if not channel:
            return await interaction.response.edit_message(content="❌ No se encontró el canal.", embed=None, view=None)

        try:
            await send_post_to_channel(channel, self.data["content"], self.data["attachments"])
        except Exception as e:
            await cleanup_r2_now(self.data.get("attachments"))
            PENDING_POSTS.pop(interaction.user.id, None)
            return await interaction.response.edit_message(content=f"❌ Error al publicar: {e}", embed=None, view=None)

        interaction.client.loop.create_task(cleanup_r2_after_delay(self.data.get("attachments")))
        PENDING_POSTS.pop(interaction.user.id, None)
        await interaction.response.edit_message(content="✅ Publicación enviada correctamente.", embed=None, view=None)
        self.stop()

    @ui.button(label="Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        await cleanup_r2_now(self.data.get("attachments"))
        PENDING_POSTS.pop(interaction.user.id, None)
        await interaction.response.edit_message(content="❌ Publicación cancelada.", embed=None, view=None)
        self.stop()


class EditDateChoiceView(AuthorView):
    def __init__(self, author_id: int, data):
        super().__init__(author_id)
        self.data = data

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

    @ui.button(label="Editar", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(EditScheduleModal(self.author_id, self.data, interaction))


class EditScheduleModal(ui.Modal, title="Editar Fecha"):
    fecha = ui.TextInput(label="Fecha", placeholder="DD/MM/AAAA", max_length=10)
    hora = ui.TextInput(label="Hora", placeholder="HH:MM", max_length=5)

    def __init__(self, author_id: int, data, panel_interaction: discord.Interaction):
        super().__init__()
        self.author_id = author_id
        self.data = data
        self.panel_interaction = panel_interaction
        self.panel_message = panel_interaction.message

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
        PENDING_POSTS.pop(interaction.user.id, None)
        await interaction.response.edit_message(content="✅ Agendamiento actualizado correctamente.", embed=None, view=None)
        self.stop()

    @ui.button(label="Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if not await self.guard(interaction):
            return
        await cleanup_r2_now(self.data.get("attachments"))
        PENDING_POSTS.pop(interaction.user.id, None)
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


async def handle_message(message: discord.Message) -> bool:
    if message.author.id not in PENDING_POSTS:
        return False

    data = PENDING_POSTS[message.author.id]
    if data.get("origin_channel_id") != message.channel.id:
        return False

    if pending_is_expired(data):
        await cleanup_r2_now(data.get("attachments"))
        PENDING_POSTS.pop(message.author.id, None)
        await edit_pending_panel(data, content="⌛ El tiempo para completar la publicación expiró.", embed=None, view=None)
        return False

    if data.get("step") == "awaiting_content":
        if not message.content and not message.attachments:
            await edit_pending_panel(data, content="⚠️ Envía texto, adjuntos o ambos para continuar.", embed=None, view=None)
            return True

        data["content"] = message.content
        data["attachments"] = await upload_message_attachments(message)

        if data["mode"] == "instant":
            data["title"] = post_title_from_content(message.content)
            embed = discord.Embed(
                title="Vista previa",
                description=message.content or "*Sin contenido textual.*",
                color=discord.Color.blurple()
            )
            embed.add_field(name="Canal", value=f"<#{data['target_channel_id']}>", inline=True)
            embed.add_field(name="Adjuntos", value=str(len(data["attachments"])), inline=True)
            await edit_pending_panel(
                data,
                content=None,
                embed=embed,
                view=InstantConfirmView(message.author.id, data)
            )
            return True

        data["step"] = "awaiting_thread_choice"
        await edit_pending_panel(
            data,
            content=None,
            embed=discord.Embed(
                title="¿Deseas crear un Hilo?",
                description="El hilo se creará automáticamente cuando se publique el post.",
                color=discord.Color.blurple()
            ),
            view=ThreadChoiceView(message.author.id)
        )
        return True

    if data.get("step") == "awaiting_thread_name":
        thread_name = message.content.strip()
        if not thread_name:
            await edit_pending_panel(data, content="⚠️ El nombre del hilo no puede estar vacío. Escríbelo de nuevo.", embed=None, view=None)
            return True

        data["thread_name"] = thread_name
        PENDING_POSTS.pop(message.author.id)
        await edit_pending_panel(
            data,
            content=None,
            embed=build_schedule_confirm_embed(data),
            view=ScheduleConfirmView(message.author.id, data)
        )
        return True

    if data.get("step") == "awaiting_edit_content":
        if not message.content and not message.attachments:
            await edit_pending_panel(data, content="⚠️ Envía texto, adjuntos o ambos para continuar.", embed=None, view=None)
            return True

        post = find_cached_post(data["post_id"])
        if not post:
            PENDING_POSTS.pop(message.author.id, None)
            await edit_pending_panel(data, content="❌ No se encontró el agendamiento.", embed=None, view=None)
            return True

        data["content"] = message.content
        data["attachments"] = await upload_message_attachments(message)
        data["title"] = post["title"]
        data["channel_id"] = post["channel_id"]

        await asyncio.sleep(1)
        await edit_pending_panel(
            data,
            content=(
                f"El agendamiento **{post['title']}** será publicado en <#{post['channel_id']}> "
                f"el **{format_scheduled_at(post['scheduled_at'])}**.\n\n"
                "¿Desea conservar esos datos?"
            ),
            embed=None,
            view=EditDateChoiceView(message.author.id, data)
        )
        return True

    return False


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

                    reaction_emojis = [
                        discord.PartialEmoji(name="E05emoji", id=1284825952903364702),
                        discord.PartialEmoji(name="D04", id=998026873118400673),
                        discord.PartialEmoji(name="20260319172839", id=1484288004213444738),
                    ]
                    for emoji in reaction_emojis:
                        try:
                            await sent_message.add_reaction(emoji)
                        except Exception as e:
                            print(f"⚠️ No se pudo reaccionar con {emoji.name} en post {post['id']}: {e}")

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
