import discord
from discord import app_commands

from core.config import normalize_text, require_staff


ACTIVE_GUESSES = {}
PENDING_SETUPS = {}


async def handle_setup_message(bot, message: discord.Message) -> bool:
    if message.author.id in PENDING_SETUPS:
        setup = PENDING_SETUPS[message.author.id]
        if setup.get("origin_channel_id") != message.channel.id:
            return False

        if message.content.startswith(bot.command_prefix):
            return True

        if setup.get("step") == 1:
            setup["winner_msg"] = message.content
            setup["step"] = 2
            await message.reply(
                "✅ **Mensaje de Ganador guardado.**\n\n"
                "👉 **Paso 3:** Escribe ahora el mensaje que se enviará al canal para **ANUNCIAR EL INICIO** de la dinámica.\n"
                "*(Este mensaje se enviará al canal destino inmediatamente)*"
            )
            return True

        if setup.get("step") == 2:
            start_msg = message.content
            setup_data = PENDING_SETUPS.pop(message.author.id)

            canal_id = setup_data["channel_id"]
            canal_obj = message.guild.get_channel(canal_id)

            ACTIVE_GUESSES[canal_id] = {
                "word": setup_data["word"],
                "announcement": setup_data["winner_msg"],
                "original_word": setup_data["original_word"]
            }

            if canal_obj:
                await canal_obj.send(start_msg)

            await message.reply(f"🚀 **Dinámica Activada.** El anuncio de inicio ha sido enviado a {canal_obj.mention if canal_obj else 'el canal'}.")
            return True

    return False


async def handle_guess_message(message: discord.Message) -> bool:
    if message.channel.id in ACTIVE_GUESSES:
        guess_data = ACTIVE_GUESSES[message.channel.id]

        if normalize_text(message.content) == guess_data["word"]:
            announcement = guess_data["announcement"]
            del ACTIVE_GUESSES[message.channel.id]
            final_msg = announcement.replace("{winner}", message.author.mention).replace("{word}", guess_data["original_word"])
            await message.channel.send(final_msg)
            return True

    return False


async def handle_message(bot, message: discord.Message) -> bool:
    if await handle_setup_message(bot, message):
        return True
    return await handle_guess_message(message)


def setup(bot):
    @bot.tree.command(name="adivinar", description="(Staff) Configura una palabra y su anuncio de inicio/fin")
    @require_staff()
    @app_commands.describe(palabra="La palabra que deben escribir", canal="Canal donde se activará la dinámica")
    async def adivinar(interaction: discord.Interaction, palabra: str, canal: discord.TextChannel):
        PENDING_SETUPS[interaction.user.id] = {
            "channel_id": canal.id,
            "origin_channel_id": interaction.channel_id,
            "word": normalize_text(palabra),
            "original_word": palabra,
            "step": 1
        }
        await interaction.response.send_message(
            f"🎯 **Dinámica Iniciada (Paso 1/3)**\n"
            f"Palabra: `{palabra}` | Canal: {canal.mention}\n\n"
            f"👉 **Paso 2:** Escribe el anuncio que el bot enviará cuando alguien **GANE**.\n"
            f"*(Puedes usar `{{winner}}` y `{{word}}`)*",
            ephemeral=True
        )
