import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # opcional

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="_", intents=intents)

@bot.event
async def on_ready():
    print(f"Conectado como {bot.user}")

    # 1) BORRAR COMANDOS GLOBALES (esto elimina los "duplicados" globales)
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    print("✅ Comandos Globales Eliminados, VUELVE A CAMBIAR A BOT.PY")

    await bot.close()

bot.run(TOKEN)
