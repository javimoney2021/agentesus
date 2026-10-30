import asyncio
import logging
import time

import discord
from discord.ext import commands, tasks

from core.config import DB_HEARTBEAT_HOURS, GUILD_ID, TOKEN, intents
from core.database import (
    cleanup_expired_data_deletion_cooldowns,
    heartbeat_db,
    init_db,
)
from modules import posts, registro_eventos, registros
from core.activity import DiscordErrorSummary, record, report

logger = logging.getLogger(__name__)


class MyBot(commands.Bot):
    async def setup_hook(self):
        await init_db()
        self.cache_loaded = False
        self.db_ready = False
        self.next_db_check = 0
        self.db_retry_seconds = 60

        registros.setup(self)
        posts.setup(self)
        registro_eventos.setup(self)

        self.check_scheduled_posts_task.start()
        self.database_heartbeat_task.start()
        self.database_cleanup_task.start()
        self.activity_task.start()

        if not GUILD_ID:
            print("❌ Falta GUILD_ID en las variables de entorno. No sincronizo comandos.")
            return

        guild = discord.Object(id=int(GUILD_ID))
        self.tree.copy_global_to(guild=guild)

        synced = await self.tree.sync(guild=guild)
        print(f"✅ Comandos sincronizados en tu servidor (guild): {len(synced)}")

        if len(synced) == 0:
            cmds = [c.name for c in self.tree.get_commands()]
            print("🧾 Comandos cargados en el árbol:", cmds)
            print("👉 Si esta lista sale vacía, tus comandos no están registrándose antes del sync.")

    @tasks.loop(minutes=1)
    async def check_scheduled_posts_task(self):
        if not self.db_ready or not self.cache_loaded:
            return
        try:
            await posts.publish_due_posts(self)
        except Exception as error:
            record('posts.cycle_failed')
            logger.warning('Publicador interrumpido: %s', type(error).__name__)
            self.db_ready = False
            self.next_db_check = time.monotonic() + 60

    @check_scheduled_posts_task.before_loop
    async def before_check(self):
        await self.wait_until_ready()
        await asyncio.sleep(5)

    @tasks.loop(minutes=1)
    async def database_heartbeat_task(self):
        if time.monotonic() < self.next_db_check:
            return
        try:
            self.db_ready = await heartbeat_db()
            if self.db_ready and not self.cache_loaded:
                await posts.load_cache()
                self.cache_loaded = True
        except Exception as error:
            self.db_ready = False
            logger.warning('Recuperacion DB: %s', type(error).__name__)
        if self.db_ready:
            self.db_retry_seconds = 60
            self.next_db_check = time.monotonic() + DB_HEARTBEAT_HOURS * 3600
            logger.info('PostgreSQL disponible; siguiente check en %sh', DB_HEARTBEAT_HOURS)
        else:
            self.next_db_check = time.monotonic() + self.db_retry_seconds
            self.db_retry_seconds = min(1800, self.db_retry_seconds * 2)

    @tasks.loop(hours=24)
    async def database_cleanup_task(self):
        if not self.db_ready:
            return
        try:
            await asyncio.wait_for(cleanup_expired_data_deletion_cooldowns(), 30)
            record('db.cleanup')
        except Exception as error:
            logger.warning('Limpieza DB: %s', type(error).__name__)

    @tasks.loop(minutes=15)
    async def activity_task(self):
        report()
        logger.info('HEALTH pending_posts=%s db_ready=%s publisher_running=%s',
                    len(posts.SCHEDULED_POSTS_CACHE), self.db_ready,
                    self.check_scheduled_posts_task.is_running())

    @database_cleanup_task.before_loop
    async def before_cleanup(self):
        await self.wait_until_ready()
        await asyncio.sleep(30)

    async def close(self):
        for task in (self.check_scheduled_posts_task, self.database_heartbeat_task,
                     self.database_cleanup_task, self.activity_task):
            task.cancel()
        await super().close()

    @database_heartbeat_task.before_loop
    async def before_database_heartbeat(self):
        await self.wait_until_ready()
        await asyncio.sleep(10)


bot = MyBot(
    command_prefix=commands.when_mentioned,
    help_command=None,
    intents=intents,
)


@bot.event
async def on_ready():
    print(f"🤖 Bot conectado como {bot.user}")


@bot.event
async def on_resumed():
    record('gateway.resume')


@bot.event
async def on_disconnect():
    record('gateway.disconnect')


@bot.listen('on_interaction')
async def count_interaction(interaction):
    record('discord.interaction_received')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('discord').addHandler(DiscordErrorSummary())
    bot.run(TOKEN, log_handler=None)
