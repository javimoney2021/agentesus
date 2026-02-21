import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # obligatorio para limpiar el guild

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="_", intents=intents)

async def show_commands():
    global_cmds = await bot.tree.fetch_commands(guild=None)
    print("\n🌍 Comandos GLOBALES en Discord:")
    if not global_cmds:
        print("  (ninguno)")
    for c in global_cmds:
        print(f"  - {c.name}  (id={c.id})")

    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        guild_cmds = await bot.tree.fetch_commands(guild=guild)
        print("\n🏠 Comandos del GUILD en Discord:")
        if not guild_cmds:
            print("  (ninguno)")
        for c in guild_cmds:
            print(f"  - {c.name}  (id={c.id})")
    else:
        print("\n⚠️ No hay GUILD_ID, no puedo mostrar comandos del guild.")

@bot.event
async def on_ready():
    print(f"Conectado como {bot.user}")

    # 1) Mostrar estado actual
    await show_commands()

    # 2) BORRAR TODO GLOBAL
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync(guild=None)
    print("\n✅ Borré comandos GLOBALES.")

    # 3) BORRAR TODO DEL GUILD
    if not GUILD_ID:
        print("❌ Falta GUILD_ID. No puedo borrar comandos del guild.")
    else:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
        print("✅ Borré comandos del GUILD.")

    # 4) Confirmar que quedó limpio
    await show_commands()

    await bot.close()

bot.run(TOKEN)
