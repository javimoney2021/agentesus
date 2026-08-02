import asyncio
import logging

import discord
from discord.ext import commands, tasks

from api.verification_api import VerificationAPIServer
from core.config import GUILD_ID, TOKEN, get_missing_verification_settings, intents
from core.database import init_db
from modules import posts, registro_eventos, registros, verificacion


logger = logging.getLogger(__name__)


class MyBot(commands.Bot):
    verification_api = None

    async def setup_hook(self):
        await init_db()
        await posts.load_cache()

        missing_verification_settings = get_missing_verification_settings()
        if missing_verification_settings:
            print(
                "⚠️ Verificacion SA pendiente de configurar: "
                + ", ".join(missing_verification_settings)
            )
        else:
            print("✅ Configuracion base de Verificacion SA cargada.")
            self.verification_api = VerificationAPIServer()
            try:
                await self.verification_api.start()
            except Exception:
                logger.exception(
                    "No se pudo iniciar la API de Verificacion SA; "
                    "el bot continuara conectado."
                )

        registros.setup(self)
        posts.setup(self)
        registro_eventos.setup(self)
        verificacion.setup(self)

        self.check_scheduled_posts_task.start()

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

    async def close(self):
        if self.verification_api is not None:
            await self.verification_api.stop()
        await super().close()

    @tasks.loop(minutes=1)
    async def check_scheduled_posts_task(self):
        await posts.publish_due_posts(self)

    @check_scheduled_posts_task.before_loop
    async def before_check(self):
        await self.wait_until_ready()
        await asyncio.sleep(5)


bot = MyBot(command_prefix="_", intents=intents)


@bot.event
async def on_ready():
    print(f"🤖 Bot conectado como {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    if await posts.handle_message(message):
        return

    await bot.process_commands(message)


bot.run(TOKEN)
