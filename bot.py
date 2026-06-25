import asyncio

import discord
from discord.ext import commands, tasks

from core.config import GUILD_ID, TOKEN, intents
from core.database import init_db
from modules import adivinar, posts, registros


class MyBot(commands.Bot):
    async def setup_hook(self):
        await init_db()
        await posts.load_cache()

        registros.setup(self)
        posts.setup(self)
        adivinar.setup(self)

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

    if await adivinar.handle_setup_message(bot, message):
        return

    if await posts.handle_message(message):
        return

    if await adivinar.handle_guess_message(message):
        return

    await bot.process_commands(message)


bot.run(TOKEN)
