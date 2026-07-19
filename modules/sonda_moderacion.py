"""Sonda temporal y pasiva para diagnosticar borrados y acciones de AutoMod."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands


logger = logging.getLogger(__name__)

UPPERCASE_SEQUENCE = re.compile(r"[A-Z]{8,}")
TRACKING_TTL = timedelta(minutes=10)
MAX_TRACKED_MESSAGES = 2_000
AUDIT_LOG_DELAY_SECONDS = 1.5
AUDIT_LOG_WINDOW_SECONDS = 20


@dataclass(frozen=True)
class TrackedMessage:
    id: int
    guild_id: int
    author_id: int
    author_name: str
    channel_id: int
    channel_name: str
    content: str
    created_at: datetime


class ModerationProbe(commands.Cog):
    """Observa mensajes sin alterar el flujo normal ni aplicar moderacion."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tracked_messages: OrderedDict[int, TrackedMessage] = OrderedDict()
        self.audit_tasks: set[asyncio.Task[None]] = set()

    def cog_unload(self):
        for task in self.audit_tasks:
            task.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        self.prune_expired_messages()
        self.tracked_messages[message.id] = TrackedMessage(
            id=message.id,
            guild_id=message.guild.id,
            author_id=message.author.id,
            author_name=str(message.author),
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", "canal-desconocido"),
            content=message.content,
            created_at=datetime.now(timezone.utc),
        )
        self.tracked_messages.move_to_end(message.id)

        while len(self.tracked_messages) > MAX_TRACKED_MESSAGES:
            self.tracked_messages.popitem(last=False)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        tracked = self.tracked_messages.pop(payload.message_id, None)
        if tracked is None or not UPPERCASE_SEQUENCE.search(tracked.content):
            return

        guild = self.bot.get_guild(tracked.guild_id)
        if guild is None:
            logger.warning(
                "SONDA | borrado detectado sin servidor disponible | autor=%s (%s) | "
                "canal=%s (%s) | mensaje=%s | contenido=%r | ejecutor=desconocido | auditoria=no",
                tracked.author_name,
                tracked.author_id,
                tracked.channel_name,
                tracked.channel_id,
                tracked.id,
                tracked.content,
            )
            return

        task = asyncio.create_task(self.log_deletion_after_audit(guild, tracked))
        self.audit_tasks.add(task)
        task.add_done_callback(self.audit_tasks.discard)

    @commands.Cog.listener()
    async def on_automod_action(self, execution: discord.AutoModAction):
        rule_name = f"regla ID {execution.rule_id}"
        try:
            rule = await execution.fetch_rule()
            rule_name = rule.name
        except discord.Forbidden:
            logger.warning("SONDA AutoMod | sin permiso para consultar la regla %s.", execution.rule_id)
        except discord.HTTPException:
            logger.exception("SONDA AutoMod | no se pudo consultar la regla %s.", execution.rule_id)

        member = execution.member
        author_name = str(member) if member else "usuario no disponible"
        channel = execution.channel
        channel_name = getattr(channel, "name", "canal no disponible")
        content = execution.content or "<contenido no disponible>"

        logger.warning(
            "SONDA AutoMod | regla=%s (%s) | accion=%s | usuario=%s (%s) | "
            "canal=%s (%s) | contenido=%r",
            rule_name,
            execution.rule_id,
            execution.action.type.name,
            author_name,
            execution.user_id,
            channel_name,
            execution.channel_id,
            content,
        )

    def prune_expired_messages(self):
        cutoff = datetime.now(timezone.utc) - TRACKING_TTL
        while self.tracked_messages:
            oldest = next(iter(self.tracked_messages.values()))
            if oldest.created_at >= cutoff:
                break
            self.tracked_messages.popitem(last=False)

    async def log_deletion_after_audit(self, guild: discord.Guild, tracked: TrackedMessage):
        await asyncio.sleep(AUDIT_LOG_DELAY_SECONDS)
        executor, found_in_audit = await self.find_delete_executor(guild, tracked)

        logger.warning(
            "SONDA | borrado de mayusculas detectado | autor=%s (%s) | "
            "canal=%s (%s) | mensaje=%s | contenido=%r | posible_ejecutor=%s | auditoria=%s",
            tracked.author_name,
            tracked.author_id,
            tracked.channel_name,
            tracked.channel_id,
            tracked.id,
            tracked.content,
            executor,
            "encontrada" if found_in_audit else "no encontrada (posible autoeliminacion)",
        )

    async def find_delete_executor(
        self, guild: discord.Guild, tracked: TrackedMessage
    ) -> tuple[str, bool]:
        try:
            async for entry in guild.audit_logs(
                limit=10, action=discord.AuditLogAction.message_delete
            ):
                entry_channel = getattr(entry.extra, "channel", None)
                entry_channel_id = getattr(entry_channel, "id", None)
                target_id = getattr(entry.target, "id", None)
                age_seconds = (datetime.now(timezone.utc) - entry.created_at).total_seconds()

                if (
                    target_id == tracked.author_id
                    and entry_channel_id == tracked.channel_id
                    and 0 <= age_seconds <= AUDIT_LOG_WINDOW_SECONDS
                ):
                    user = entry.user
                    if user is None:
                        return "desconocido", True
                    actor_type = "bot" if user.bot else "moderador"
                    return f"{user} ({user.id}, {actor_type})", True
        except discord.Forbidden:
            logger.warning("SONDA | sin View Audit Log en el servidor %s.", guild.id)
        except discord.HTTPException:
            logger.exception("SONDA | error al consultar Audit Logs en el servidor %s.", guild.id)

        return "no encontrado; posible autoeliminacion del autor", False


async def setup(bot: commands.Bot):
    """Registra la sonda temporal sin tocar comandos ni eventos existentes."""
    await bot.add_cog(ModerationProbe(bot))
