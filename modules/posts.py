import io
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import aiohttp
import discord
from discord import app_commands, ui

from core.config import TZ_BRASILIA, db_unavailable, require_staff
from core.database import (
    add_scheduled_post,
    delete_scheduled_post,
    load_scheduled_posts,
)
from core.r2_storage import delete_from_r2, upload_to_r2


PENDING_POSTS = {}
SCHEDULED_POSTS_CACHE = []


async def load_cache(guild_id=None):
    global SCHEDULED_POSTS_CACHE
    SCHEDULED_POSTS_CACHE = await load_scheduled_posts(guild_id)


class ThreadChoiceView(ui.View):
    """Vista con botones SI / NO para decidir si se crea un hilo al publicar."""
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id

    @ui.button(label="SI", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("⛔ Esta acción no es tuya.", ephemeral=True)
        if self.user_id in PENDING_POSTS:
            PENDING_POSTS[self.user_id]["step"] = "awaiting_thread_name"
        await interaction.response.edit_message(
            content="📝 Escribe el **nombre del hilo** que se creará al publicar:",
            embed=None, view=None
        )
        self.stop()

    @ui.button(label="NO", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("⛔ Esta acción no es tuya.", ephemeral=True)
        data = PENDING_POSTS.pop(self.user_id, None)
        if data is None:
            await interaction.response.edit_message(content="❌ No se encontró el agendamiento pendiente.", embed=None, view=None)
            self.stop()
            return
        data["thread_name"] = None
        fecha_str = data["scheduled_at"].strftime("%d/%m/%Y a las %H:%M")
        embed = discord.Embed(
            title="Confirmar Agendamiento ?",
            description=(
                f"**Título:** {data['title']}\n"
                f"**Canal:** <#{data['channel_id']}>\n"
                f"**Fecha:** {fecha_str}\n"
                f"**Hilo:** No\n"
                f"**Adjuntos:** {len(data['attachments'])} archivo(s) guardado(s) ✅\n\n"
                "¿Deseas programar esta publicación?"
            ),
            color=discord.Color.orange()
        )
        await interaction.response.edit_message(embed=embed, view=PostConfirmView(data))
        self.stop()


class PostConfirmView(ui.View):
    def __init__(self, data):
        super().__init__(timeout=60)
        self.data = data

    @ui.button(label="Aceptar", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
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

        fecha = self.data["scheduled_at"].strftime("%d/%m/%y")
        hora = self.data["scheduled_at"].strftime("%H:%M")
        await interaction.response.edit_message(
            content=f"✅ Tu publicación ha sido agendada con éxito el día **{fecha}** a las **{hora}**",
            embed=None, view=None
        )
        self.stop()

    @ui.button(label="Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if self.data.get("attachments"):
            for url in self.data["attachments"]:
                await delete_from_r2(url)
        await interaction.response.edit_message(content="❌ Agendamiento cancelado...", embed=None, view=None)
        self.stop()


async def handle_message(message: discord.Message) -> bool:
    if message.author.id not in PENDING_POSTS:
        return False

    data = PENDING_POSTS[message.author.id]

    if data.get("step") == "awaiting_content":
        data["content"] = message.content
        data["step"] = "awaiting_thread_choice"

        r2_urls = []
        if message.attachments:
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

        data["attachments"] = r2_urls

        embed = discord.Embed(
            title="¿Deseas crear un Hilo?",
            description="El hilo se creará automáticamente cuando se publique el post.",
            color=discord.Color.blurple()
        )
        await message.reply(embed=embed, view=ThreadChoiceView(message.author.id))
        return True

    if data.get("step") == "awaiting_thread_name":
        thread_name = message.content.strip()
        if not thread_name:
            await message.reply("⚠️ El nombre del hilo no puede estar vacío. Escríbelo de nuevo.")
            return True

        data["thread_name"] = thread_name
        PENDING_POSTS.pop(message.author.id)

        fecha_str = data["scheduled_at"].strftime("%d/%m/%Y a las %H:%M")
        embed = discord.Embed(
            title="Confirmar Agendamiento ?",
            description=(
                f"**Título:** {data['title']}\n"
                f"**Canal:** <#{data['channel_id']}>\n"
                f"**Fecha:** {fecha_str}\n"
                f"**Hilo:** `{thread_name}`\n"
                f"**Adjuntos:** {len(data['attachments'])} archivo(s) guardado(s) ✅\n\n"
                "¿Deseas programar esta publicación?"
            ),
            color=discord.Color.orange()
        )
        await message.reply(embed=embed, view=PostConfirmView(data))
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
        guild = bot.get_guild(post["guild_id"])
        if guild:
            channel = guild.get_channel(post["channel_id"])
            if channel:
                try:
                    content = post["content"] or ""
                    files = []

                    if post["attachment_urls"]:
                        async with aiohttp.ClientSession() as session:
                            for url in post["attachment_urls"]:
                                try:
                                    async with session.get(url) as resp:
                                        if resp.status == 200:
                                            data = await resp.read()
                                            parsed = urlparse(url)
                                            filename = unquote(parsed.path.split("/")[-1])
                                            files.append(
                                                discord.File(
                                                    io.BytesIO(data),
                                                    filename=filename
                                                )
                                            )
                                        else:
                                            print(f"⚠️ No se pudo descargar adjunto: {url} | Status: {resp.status}")
                                except Exception as e:
                                    print(f"⚠️ Error descargando adjunto {url}: {e}")

                    if files:
                        sent_message = await channel.send(content=content, files=files)
                    else:
                        sent_message = await channel.send(content=content)

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

                if post.get("attachment_urls"):
                    for url in post["attachment_urls"]:
                        await delete_from_r2(url)

        await delete_scheduled_post(post["id"])
        if post in SCHEDULED_POSTS_CACHE:
            SCHEDULED_POSTS_CACHE.remove(post)


def setup(bot):
    @bot.tree.command(name="post", description="(Staff) Agendar Publicación")
    @require_staff()
    @app_commands.describe(canal="Canal a Publicar", data="Fecha (DDMMAAAA)", hora="Hora (HH:MM)", nombre="Nombre del post")
    async def post(interaction: discord.Interaction, canal: discord.TextChannel, data: str, hora: str, nombre: str):
        if await db_unavailable(interaction):
            return
        try:
            dt_obj = datetime.strptime(f"{data} {hora}", "%d%m%Y %H:%M")
            scheduled_at = dt_obj.replace(tzinfo=TZ_BRASILIA)

            if scheduled_at <= datetime.now(TZ_BRASILIA):
                return await interaction.response.send_message("❌ La fecha y hora deben ser futuras.", ephemeral=False)

            PENDING_POSTS[interaction.user.id] = {
                "channel_id": canal.id,
                "scheduled_at": scheduled_at,
                "title": nombre,
                "step": "awaiting_content"
            }

            await interaction.response.send_message("📝 Ahora prepara publicación para agendar y envía...", ephemeral=False)

        except ValueError:
            await interaction.response.send_message("❌ Formato inválido. Usa: Data `DDMMAAAA` (ej: 22042025) y Hora `HH:MM` (ej: 15:30).", ephemeral=False)

    @bot.tree.command(name="agendamientos", description="(Staff) Ver agendamientos activos")
    @require_staff()
    async def agendamientos(interaction: discord.Interaction):
        rows = [p for p in SCHEDULED_POSTS_CACHE if p["guild_id"] == interaction.guild_id]
        rows = sorted(rows, key=lambda x: x["scheduled_at"])

        if not rows:
            return await interaction.response.send_message("📅 No hay publicaciones agendadas.", ephemeral=False)

        embed = discord.Embed(title="**Agendamientos Activos**", color=discord.Color.blue())
        lines = []
        for i, r in enumerate(rows, 1):
            scheduled = r["scheduled_at"]
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=timezone.utc)
            fecha = scheduled.astimezone(TZ_BRASILIA).strftime("%d/%m/%y %H:%M")
            lines.append(f"{i}. **{r['title']}** en <#{r['channel_id']}> a las `{fecha}`")

        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @bot.tree.command(name="cancelar_post", description="(Staff) Elimina un agendamiento")
    @require_staff()
    @app_commands.describe(numero="Número del post en la lista de /agendamientos")
    async def cancelar_post(interaction: discord.Interaction, numero: int):
        if await db_unavailable(interaction):
            return
        rows = [p for p in SCHEDULED_POSTS_CACHE if p["guild_id"] == interaction.guild_id]
        rows = sorted(rows, key=lambda x: x["scheduled_at"])

        if not rows or numero < 1 or numero > len(rows):
            return await interaction.response.send_message("❌ Número de agendamiento inválido.", ephemeral=False)

        target = rows[numero - 1]

        if target.get("attachment_urls"):
            for url in target["attachment_urls"]:
                await delete_from_r2(url)

        await delete_scheduled_post(target["id"])

        for p in SCHEDULED_POSTS_CACHE:
            if p["id"] == target["id"]:
                SCHEDULED_POSTS_CACHE.remove(p)
                break

        await interaction.response.send_message(f"🗑️ Agendamiento **{target['title']}** eliminado completamente.", ephemeral=False)
