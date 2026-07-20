import discord
from discord import app_commands

from core.config import db_unavailable, require_staff
from core.database import get_all_registros, get_by_external_id, get_registro


class BaulRegistrosView(discord.ui.View):
    def __init__(self, registros, page=0):
        super().__init__(timeout=120)
        self.registros = registros
        self.page = page

    async def build_embed(self):
        start = self.page * 10
        end = start + 10
        page_regs = self.registros[start:end]

        embed = discord.Embed(
            title="📋 Baúl de Registros",
            color=discord.Color.green(),
        )

        def escape(value: str) -> str:
            return discord.utils.escape_markdown(str(value))

        lines = []
        for index, registro in enumerate(page_regs, start=start + 1):
            discord_tag = escape(registro.get("discord_tag", "N/A"))
            nickname = escape(registro.get("nickname", "N/A"))
            external_id = escape(registro.get("external_id", "N/A"))
            lines.append(
                f"**{index}.** {discord_tag} | Nick=`{nickname}` | ID=`{external_id}`"
            )

        embed.description = "\n".join(lines) if lines else "Fin de la lista."
        return embed

    @discord.ui.button(label="❮", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="❯", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        if (self.page + 1) * 10 < len(self.registros):
            self.page += 1
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)


def setup(bot):
    @bot.tree.command(name="consultar_baul", description="(Staff) Consulta un registro histórico")
    @require_staff()
    async def consultar_baul(interaction: discord.Interaction, usuario: discord.Member):
        if await db_unavailable(interaction):
            return

        registro = await get_registro(usuario.id)
        if not registro:
            await interaction.response.send_message("No registrado.", ephemeral=True)
            return

        embed = discord.Embed(title="📄 Registro Histórico", color=discord.Color.blue())
        embed.set_thumbnail(url=usuario.display_avatar.url)
        embed.add_field(name="Usuario", value=f"<@{usuario.id}>", inline=False)
        embed.add_field(name="Nick", value=registro["nickname"], inline=True)
        embed.add_field(name="ID", value=registro["external_id"], inline=True)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="user_id_baul", description="(Staff) Busca un registro histórico por ID Espacial")
    @require_staff()
    @app_commands.describe(id_espacial="ID Espacial a consultar en el baúl")
    async def user_id_baul(interaction: discord.Interaction, id_espacial: str):
        if await db_unavailable(interaction):
            return

        registros = await get_by_external_id(id_espacial.strip())
        if not registros:
            await interaction.response.send_message(
                f"ℹ️ No encontré registros con la ID `{id_espacial}`."
            )
            return

        embed = discord.Embed(title="🔎 Resultados del Baúl", color=discord.Color.blurple())
        for index, registro in enumerate(registros[:10], start=1):
            embed.add_field(
                name=f"Resultado {index}",
                value=(
                    f"Usuario: <@{registro['user_id']}>\n"
                    f"Nick: `{registro['nickname']}`\n"
                    f"Discord ID: `{registro['user_id']}`"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="baul_registros", description="(Staff) Lista los registros históricos")
    @require_staff()
    async def baul_registros(interaction: discord.Interaction):
        if await db_unavailable(interaction):
            return

        registros = await get_all_registros()
        if not registros:
            await interaction.response.send_message("No hay registros históricos.", ephemeral=True)
            return

        view = BaulRegistrosView(registros)
        await interaction.response.send_message(embed=await view.build_embed(), view=view)
