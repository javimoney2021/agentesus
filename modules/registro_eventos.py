import asyncio
import logging
import re

import discord
from discord import app_commands

from core import database
from core.config import (
    EVENT_ADMIN_ROLE_ID,
    EVENT_ALLOWED_CHANNEL_IDS,
    EVENT_CATALOG_MAX_ITEMS,
    EVENT_PANEL_ROLE_NAME,
    EVENT_PARTICIPANT_LIMITS,
    EVENT_PARTICIPANT_ROLE_ID,
    EVENT_VERIFICATION_CHANNEL_ID,
    EVENT_VERIFIED_ROLE_ID,
    require_staff,
)


logger = logging.getLogger(__name__)
SPATIAL_ID_PATTERN = re.compile(r"^\d{7,10}$")
USERS_PER_PAGE = 10
PARTICIPANTS_PER_EMBED = 15
EDIT_USERS_PER_PAGE = 20
STAFF_PANEL_THUMBNAIL_URL = (
    "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/"
    "Purple%20jack%20Harcode/Kill.png"
)
ADMIN_PANEL_THUMBNAIL_URL = (
    "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/"
    "Purple%20jack%20Harcode/Inge%20thumb.png"
)


def has_role_id(member: discord.Member, role_id: int) -> bool:
    return any(role.id == role_id for role in member.roles)


def is_event_team(member: discord.Member) -> bool:
    return any(role.name == EVENT_PANEL_ROLE_NAME for role in member.roles)


def require_event_team():
    async def predicate(interaction: discord.Interaction):
        if isinstance(interaction.user, discord.Member) and is_event_team(interaction.user):
            return True
        await send_ephemeral(
            interaction,
            f"Solo el rol **{EVENT_PANEL_ROLE_NAME}** puede usar este comando.",
        )
        return False

    return app_commands.check(predicate)


def normalize_event_name(value: str) -> str:
    return " ".join(value.split())


def limit_label(value: int) -> str:
    return "Sin limite" if value == 0 else str(value)


async def send_ephemeral(interaction: discord.Interaction, content=None, **kwargs):
    kwargs["ephemeral"] = True
    if interaction.response.is_done():
        return await interaction.followup.send(content, **kwargs)
    return await interaction.response.send_message(content, **kwargs)


def opening_embed(description: str, confirmation: bool = False) -> discord.Embed:
    return discord.Embed(
        title=(
            "APERTURA DE EVENTO CONFIRMACIÓN"
            if confirmation
            else "APERTURA DE EVENTO"
        ),
        description=description,
        color=discord.Color.orange(),
    )


def registration_open_embed(event_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{event_name} - Registro Abierto",
        description=(
            "<:ygoldstar:1004555717610590258> El Evento comenzara dentro de poco...\n\n"
            "Presiona el botón **Registrarse** y aparta tu lugar."
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Cada usuario puede registrarse una sola vez.")
    return embed


def registration_closed_embed(event_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{event_name} - Registro Cerrado",
        description="**Registro Cerrado, El evento esta por iniciar...!**",
        color=discord.Color.dark_gray(),
    )
    embed.set_footer(text="Cada usuario puede registrarse una sola vez.")
    return embed


def staff_panel_embed(active_event) -> discord.Embed:
    if active_event:
        status = "Abiertas" if active_event["status"] == "open" else "Cerradas"
        description = (
            f"Eventos Activos: **{active_event['event_name']}**\n"
            f"Inscripciones: **{status}**\n"
            f"Limite de participantes: **{limit_label(active_event['participant_limit'])}**"
        )
    else:
        description = "Eventos Activos: *No hay eventos activos*"

    embed = discord.Embed(
        title="Panel de Gerenciamiento de Eventos ES",
        description=description,
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url=STAFF_PANEL_THUMBNAIL_URL)
    embed.set_footer(text="Escoja una opción abajo...")
    return embed


class OwnerView(discord.ui.View):
    def __init__(self, owner_id: int, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await send_ephemeral(interaction, "Este panel pertenece a otro usuario.")
        return False


class AdminOwnerView(OwnerView):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if (
            isinstance(interaction.user, discord.Member)
            and has_role_id(interaction.user, EVENT_ADMIN_ROLE_ID)
        ):
            return True
        await send_ephemeral(interaction, "Ya no posees el rol autorizado para administrar eventos.")
        return False


class EventSelect(discord.ui.Select):
    def __init__(self, manager: "RegistroEventos", owner_id: int, events):
        self.manager = manager
        self.owner_id = owner_id
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in events
        ]
        super().__init__(
            placeholder="Selecciona un evento",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        event_id = int(self.values[0])
        event_name = next(
            option.label for option in self.options if option.value == self.values[0]
        )
        await interaction.response.edit_message(
            embed=opening_embed("Seleccione el limite de participantes"),
            view=LimitSelectionView(
                self.manager, self.owner_id, event_id, event_name
            ),
        )


class EventSelectionView(OwnerView):
    def __init__(self, manager: "RegistroEventos", owner_id: int, events):
        super().__init__(owner_id)
        self.add_item(EventSelect(manager, owner_id, events))


class LimitSelect(discord.ui.Select):
    def __init__(
        self,
        manager: "RegistroEventos",
        owner_id: int,
        event_id: int,
        event_name: str,
    ):
        self.manager = manager
        self.owner_id = owner_id
        self.event_id = event_id
        self.event_name = event_name
        options = [
            discord.SelectOption(label=limit_label(value), value=str(value))
            for value in EVENT_PARTICIPANT_LIMITS
        ]
        super().__init__(
            placeholder="Selecciona el limite",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        participant_limit = int(self.values[0])
        description = (
            f"Evento: **{self.event_name}**\n"
            f"Limite Participantes: **{limit_label(participant_limit)}**"
        )
        await interaction.response.edit_message(
            embed=opening_embed(description, confirmation=True),
            view=OpeningConfirmationView(
                self.manager,
                self.owner_id,
                self.event_id,
                self.event_name,
                participant_limit,
            ),
        )


class LimitSelectionView(OwnerView):
    def __init__(
        self,
        manager: "RegistroEventos",
        owner_id: int,
        event_id: int,
        event_name: str,
    ):
        super().__init__(owner_id)
        self.add_item(
            LimitSelect(manager, owner_id, event_id, event_name)
        )


class OpeningConfirmationView(OwnerView):
    def __init__(
        self,
        manager: "RegistroEventos",
        owner_id: int,
        event_id: int,
        event_name: str,
        participant_limit: int,
    ):
        super().__init__(owner_id)
        self.manager = manager
        self.event_id = event_id
        self.event_name = event_name
        self.participant_limit = participant_limit

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.confirm_opening(
            interaction,
            self.event_id,
            self.event_name,
            self.participant_limit,
        )

    @discord.ui.button(label="Empezar de nuevo", style=discord.ButtonStyle.primary)
    async def restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.restart_opening(interaction, self.owner_id)


class RegistrationView(discord.ui.View):
    def __init__(self, manager: "RegistroEventos", *, disabled: bool = False):
        super().__init__(timeout=None)
        self.manager = manager
        self.register.disabled = disabled
        if disabled:
            self.register.style = discord.ButtonStyle.secondary

    @discord.ui.button(
        label="Registrarse",
        style=discord.ButtonStyle.primary,
        custom_id="event_registration:register:v2",
    )
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.begin_registration(interaction)


class ExistingProfileView(OwnerView):
    def __init__(
        self,
        manager: "RegistroEventos",
        owner_id: int,
        profile,
        overflow_warning: bool,
    ):
        super().__init__(owner_id)
        self.manager = manager
        self.profile = profile
        self.overflow_warning = overflow_warning

    @discord.ui.button(label="Continuar", style=discord.ButtonStyle.success)
    async def continue_registration(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.manager.complete_registration(interaction, self.profile)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Inscripción cancelada.", embed=None, view=None)


class OverflowNewProfileView(OwnerView):
    def __init__(self, manager: "RegistroEventos", owner_id: int):
        super().__init__(owner_id)
        self.manager = manager

    @discord.ui.button(label="Continuar", style=discord.ButtonStyle.success)
    async def continue_registration(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(RegistrationModal(self.manager))

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Inscripción cancelada.", view=None)


class RegistrationModal(discord.ui.Modal, title="Inscripción al Evento"):
    nickname = discord.ui.TextInput(
        label="Nickname",
        min_length=2,
        max_length=32,
        required=True,
    )
    spatial_id = discord.ui.TextInput(
        label="ID Espacial",
        placeholder="Entre 7 y 10 numeros",
        min_length=7,
        max_length=10,
        required=True,
    )
    country = discord.ui.TextInput(
        label="Pais",
        min_length=2,
        max_length=40,
        required=True,
    )

    def __init__(self, manager: "RegistroEventos"):
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction):
        spatial_id = self.spatial_id.value.strip()
        nickname = self.nickname.value.strip()
        country = self.country.value.strip()
        if not SPATIAL_ID_PATTERN.fullmatch(spatial_id):
            await send_ephemeral(
                interaction,
                "La ID Espacial debe contener solamente entre 7 y 10 numeros.",
            )
            return
        if len(nickname) < 2 or len(country) < 2:
            await send_ephemeral(
                interaction,
                "Nickname y Pais deben contener al menos 2 caracteres visibles.",
            )
            return

        profile = {
            "nickname": nickname,
            "external_id": spatial_id,
            "country": country,
        }
        await self.manager.complete_registration(interaction, profile)


class StaffPanelView(discord.ui.View):
    def __init__(self, manager: "RegistroEventos"):
        super().__init__(timeout=None)
        self.manager = manager

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild
            and isinstance(interaction.user, discord.Member)
            and is_event_team(interaction.user)
        ):
            return True
        await send_ephemeral(
            interaction,
            f"Solo el rol **{EVENT_PANEL_ROLE_NAME}** puede usar este panel.",
        )
        return False

    @discord.ui.button(
        label="👨‍👦‍👦 Ver Participantes",
        style=discord.ButtonStyle.primary,
        custom_id="event_staff:participants:v2",
    )
    async def participants(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.show_participants(interaction)

    @discord.ui.button(
        label="🔒 Cerrar Inscripciones",
        style=discord.ButtonStyle.secondary,
        custom_id="event_staff:close:v2",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.close_registration(interaction)

    @discord.ui.button(
        label="📍 Finalizar Evento",
        style=discord.ButtonStyle.danger,
        custom_id="event_staff:finish:v2",
    )
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.request_finish_confirmation(interaction)

    @discord.ui.button(
        label="⚙️ ADM",
        style=discord.ButtonStyle.success,
        custom_id="event_staff:admin:v2",
    )
    async def admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.open_admin(interaction)


class FinishConfirmationView(OwnerView):
    def __init__(
        self,
        manager: "RegistroEventos",
        owner_id: int,
        event_id: int,
        event_name: str,
        panel_message: discord.Message,
    ):
        super().__init__(owner_id)
        self.manager = manager
        self.event_id = event_id
        self.event_name = event_name
        self.panel_message = panel_message

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.finish_event(
            interaction,
            self.event_id,
            self.panel_message,
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Finalización cancelada.", embed=None, view=None
        )


class AdminMenuView(AdminOwnerView):
    def __init__(self, manager: "RegistroEventos", owner_id: int):
        super().__init__(owner_id)
        self.manager = manager

    @discord.ui.button(label="Agregar Evento", style=discord.ButtonStyle.success)
    async def add_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddEventModal(self.manager))

    @discord.ui.button(label="Remover Evento", style=discord.ButtonStyle.danger)
    async def remove_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.open_remove_event(interaction, self.owner_id)

    @discord.ui.button(label="Registros DB", style=discord.ButtonStyle.primary)
    async def database_records(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.manager.open_database_records(interaction, self.owner_id)

    @discord.ui.button(label="Editar registro", style=discord.ButtonStyle.secondary)
    async def edit_record(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.open_edit_records(interaction, self.owner_id)


class AddEventModal(discord.ui.Modal, title="Agregar Evento"):
    event_name = discord.ui.TextInput(
        label="Nombre del Evento",
        min_length=2,
        max_length=80,
        required=True,
    )

    def __init__(self, manager: "RegistroEventos"):
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction):
        await self.manager.add_catalog_event(interaction, self.event_name.value)


class RemoveEventSelect(discord.ui.Select):
    def __init__(self, manager: "RegistroEventos", owner_id: int, events):
        self.manager = manager
        self.owner_id = owner_id
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in events
        ]
        super().__init__(
            placeholder="Selecciona el evento a remover",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        event_id = int(self.values[0])
        event_name = next(
            option.label for option in self.options if option.value == self.values[0]
        )
        embed = discord.Embed(
            title="Confirmar eliminación",
            description=f"¿Deseas remover **{event_name}** del catálogo?",
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=RemoveEventConfirmationView(
                self.manager, self.owner_id, event_id, event_name
            ),
        )


class RemoveEventSelectionView(AdminOwnerView):
    def __init__(self, manager: "RegistroEventos", owner_id: int, events):
        super().__init__(owner_id)
        self.add_item(RemoveEventSelect(manager, owner_id, events))


class RemoveEventConfirmationView(AdminOwnerView):
    def __init__(
        self,
        manager: "RegistroEventos",
        owner_id: int,
        event_id: int,
        event_name: str,
    ):
        super().__init__(owner_id)
        self.manager = manager
        self.event_id = event_id
        self.event_name = event_name

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.remove_catalog_event(
            interaction, self.event_id, self.event_name
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Eliminación cancelada.", embed=None, view=None
        )


class EditEventUserModal(discord.ui.Modal):
    def __init__(self, manager: "RegistroEventos", profile):
        super().__init__(title="Editar Registro DB")
        self.manager = manager
        self.user_id = profile["user_id"]
        self.nickname = discord.ui.TextInput(
            label="Nickname",
            default=profile["nickname"],
            min_length=2,
            max_length=32,
            required=True,
        )
        self.spatial_id = discord.ui.TextInput(
            label="ID Espacial",
            default=profile["external_id"],
            min_length=7,
            max_length=10,
            required=True,
        )
        self.country = discord.ui.TextInput(
            label="Pais",
            default=profile["country"],
            min_length=2,
            max_length=40,
            required=True,
        )
        self.add_item(self.nickname)
        self.add_item(self.spatial_id)
        self.add_item(self.country)

    async def on_submit(self, interaction: discord.Interaction):
        await self.manager.edit_event_user_profile(
            interaction,
            self.user_id,
            self.nickname.value,
            self.spatial_id.value,
            self.country.value,
        )


class EditEventUserSelect(discord.ui.Select):
    def __init__(
        self,
        manager: "RegistroEventos",
        owner_id: int,
        page: int,
        options: list[discord.SelectOption],
    ):
        self.manager = manager
        self.owner_id = owner_id
        self.page = page
        super().__init__(
            placeholder="Selecciona el registro a editar",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value.startswith("page:"):
            await self.manager.show_edit_records_page(
                interaction,
                self.owner_id,
                int(value.split(":", 1)[1]),
            )
            return
        await self.manager.open_edit_record_modal(interaction, int(value))


class EditEventUserSelectionView(AdminOwnerView):
    def __init__(
        self,
        manager: "RegistroEventos",
        owner_id: int,
        page: int,
        options: list[discord.SelectOption],
    ):
        super().__init__(owner_id)
        self.add_item(EditEventUserSelect(manager, owner_id, page, options))


class EventUsersPaginator(AdminOwnerView):
    def __init__(
        self,
        manager: "RegistroEventos",
        owner_id: int,
        page: int,
        total: int,
    ):
        super().__init__(owner_id)
        self.manager = manager
        self.page = page
        self.total = total
        self.previous.disabled = page <= 0
        self.next.disabled = (page + 1) * USERS_PER_PAGE >= total

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.change_database_page(interaction, self.owner_id, self.page - 1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.change_database_page(interaction, self.owner_id, self.page + 1)


class RegistroEventos:
    def __init__(self, bot):
        self.bot = bot
        self.registration_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.finishing_guilds: set[int] = set()

    def database_ready(self) -> bool:
        return database.bot_pool is not None

    async def require_database(self, interaction: discord.Interaction) -> bool:
        if self.database_ready():
            return True
        await send_ephemeral(interaction, "La base de datos no esta disponible.")
        return False

    async def require_event_admin(self, interaction: discord.Interaction) -> bool:
        if (
            isinstance(interaction.user, discord.Member)
            and has_role_id(interaction.user, EVENT_ADMIN_ROLE_ID)
        ):
            return True
        await send_ephemeral(
            interaction,
            "No posees el rol autorizado para entrar en la administración de eventos.",
        )
        return False

    async def open_registration(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.channel_id not in EVENT_ALLOWED_CHANNEL_IDS:
            channels = " ".join(f"<#{channel_id}>" for channel_id in EVENT_ALLOWED_CHANNEL_IDS)
            await send_ephemeral(
                interaction,
                f"Este comando solo puede usarse en: {channels}",
            )
            return
        if not await self.require_database(interaction):
            return

        active = await database.get_active_event(interaction.guild.id)
        if active:
            await send_ephemeral(
                interaction,
                f"Ya existe un evento activo: **{active['event_name']}**. "
                "Debes finalizarlo antes de abrir otro.",
            )
            return

        events = await database.list_event_catalog(interaction.guild.id)
        if not events:
            await send_ephemeral(
                interaction,
                "No hay eventos configurados. Agrega uno desde **Adm** en /panel_eventos.",
            )
            return

        await interaction.response.send_message(
            embed=opening_embed("Selecciona un Evento de la Lista"),
            view=EventSelectionView(self, interaction.user.id, events),
            ephemeral=True,
        )

    async def restart_opening(self, interaction: discord.Interaction, owner_id: int):
        if not interaction.guild or not await self.require_database(interaction):
            return
        events = await database.list_event_catalog(interaction.guild.id)
        if not events:
            await interaction.response.edit_message(
                content="Ya no hay eventos configurados.", embed=None, view=None
            )
            return
        await interaction.response.edit_message(
            embed=opening_embed("Selecciona un Evento de la Lista"),
            view=EventSelectionView(self, owner_id, events),
        )

    async def confirm_opening(
        self,
        interaction: discord.Interaction,
        event_id: int,
        event_name: str,
        participant_limit: int,
    ):
        if not interaction.guild or interaction.channel_id not in EVENT_ALLOWED_CHANNEL_IDS:
            await send_ephemeral(interaction, "El canal de apertura ya no es valido.")
            return
        if not await self.require_database(interaction):
            return
        if not isinstance(interaction.channel, discord.abc.Messageable):
            await send_ephemeral(interaction, "No se pudo identificar el canal del evento.")
            return

        await interaction.response.defer(ephemeral=True)
        status, event = await database.create_active_event(
            interaction.guild.id,
            event_id,
            participant_limit,
            interaction.channel_id,
            interaction.user.id,
        )
        if status == "active":
            await interaction.edit_original_response(
                content=f"Ya existe un evento activo: **{event['event_name']}**.",
                embed=None,
                view=None,
            )
            return
        if status != "created":
            await interaction.edit_original_response(
                content="El evento seleccionado ya no existe.", embed=None, view=None
            )
            return

        message = None
        try:
            message = await interaction.channel.send(
                embed=registration_open_embed(event_name),
                view=RegistrationView(self),
            )
            await database.set_event_message(event["id"], message.id)
        except Exception:
            logger.exception("No se pudo publicar o persistir el panel del evento")
            if message is not None:
                try:
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    logger.exception("No se pudo retirar el panel huérfano del evento")
            try:
                await database.delete_event(event["id"])
            except Exception:
                logger.exception("No se pudo revertir la apertura del evento %s", event["id"])
            await interaction.edit_original_response(
                content="No se pudo publicar el panel. La apertura fue revertida.",
                embed=None,
                view=None,
            )
            return

        await interaction.edit_original_response(
            content=f"Panel de registro de **{event_name}** publicado.",
            embed=None,
            view=None,
        )

    async def begin_registration(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await send_ephemeral(interaction, "Este registro solo funciona dentro del servidor.")
            return
        if not await self.require_database(interaction):
            return

        verified_role = interaction.guild.get_role(EVENT_VERIFIED_ROLE_ID)
        if verified_role is None or verified_role not in interaction.user.roles:
            await send_ephemeral(
                interaction,
                "Es necesario estar verificado en el servidor para participar en "
                f"Eventos de la Comunidad: <#{EVENT_VERIFICATION_CHANNEL_ID}>",
            )
            return

        event = await database.get_active_event(interaction.guild.id)
        if not event or event["status"] != "open":
            await send_ephemeral(interaction, "Las inscripciones de este evento estan cerradas.")
            return
        if (
            interaction.channel_id != event["registration_channel_id"]
            or interaction.message is None
            or interaction.message.id != event["registration_message_id"]
        ):
            await send_ephemeral(
                interaction,
                "Este panel ya no corresponde al evento activo.",
            )
            return
        if await database.get_event_registration(event["id"], interaction.user.id):
            await send_ephemeral(interaction, "Ya estas registrado en este evento.")
            return

        profile = await database.get_event_user(interaction.guild.id, interaction.user.id)
        count = await database.get_event_participant_count(event["id"])
        overflow = event["participant_limit"] > 0 and count >= event["participant_limit"]

        if profile:
            description = (
                "Ya posees un registro con los siguientes datos:\n\n"
                f"**Nickname:** {profile['nickname']}\n"
                f"**ID Espacial:** {profile['external_id']}\n"
                f"**Pais:** {profile['country']}\n\n"
                "¿Deseas continuar con la inscripción?"
            )
            if overflow:
                description += "\n\n" + self.overflow_text()
            embed = discord.Embed(
                title="Confirmar Inscripción",
                description=description,
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(
                embed=embed,
                view=ExistingProfileView(
                    self, interaction.user.id, profile, overflow
                ),
                ephemeral=True,
            )
            return

        if overflow:
            await interaction.response.send_message(
                self.overflow_text(),
                view=OverflowNewProfileView(self, interaction.user.id),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(RegistrationModal(self))

    @staticmethod
    def overflow_text() -> str:
        return (
            "La lista principal de participantes esta llena. ¿Deseas registrarte de "
            "todas formas? Seras considerado en caso falte algun jugador de la lista inicial..."
        )

    async def complete_registration(self, interaction: discord.Interaction, profile):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await send_ephemeral(interaction, "No se pudo identificar al miembro.")
            return
        if not await self.require_database(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        key = (interaction.guild.id, interaction.user.id)
        lock = self.registration_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                await self._complete_registration(interaction, profile)
        finally:
            if not lock.locked():
                self.registration_locks.pop(key, None)

    async def _complete_registration(self, interaction: discord.Interaction, profile):
        guild = interaction.guild
        member = interaction.user
        verified_role = guild.get_role(EVENT_VERIFIED_ROLE_ID)
        participant_role = guild.get_role(EVENT_PARTICIPANT_ROLE_ID)

        if verified_role is None or verified_role not in member.roles:
            await interaction.followup.send(
                "Ya no posees el rol de verificación requerido.", ephemeral=True
            )
            return
        if participant_role is None:
            await interaction.followup.send(
                "El rol de participante no esta disponible. La inscripción no fue guardada.",
                ephemeral=True,
            )
            return

        event = await database.get_active_event(guild.id)
        if not event or event["status"] != "open":
            await interaction.followup.send(
                "Las inscripciones de este evento estan cerradas.", ephemeral=True
            )
            return

        existing_profile = await database.get_event_user(guild.id, member.id)
        if existing_profile:
            nickname = external_id = country = None
        else:
            nickname = profile["nickname"]
            external_id = profile["external_id"]
            country = profile["country"]

        had_role = participant_role in member.roles
        if not had_role:
            try:
                await member.add_roles(
                    participant_role,
                    reason=f"Inscripción al evento: {event['event_name']}",
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("No se pudo asignar el rol de evento a %s", member.id)
                await interaction.followup.send(
                    "No se pudo otorgar el rol de participante. La inscripción no fue guardada.",
                    ephemeral=True,
                )
                return

        try:
            status, result = await database.register_event_participant(
                guild.id,
                member.id,
                str(member),
                nickname,
                external_id,
                country,
            )
        except Exception:
            logger.exception("Fallo al guardar una inscripción de evento")
            if not had_role:
                await self.remove_role_safely(member, participant_role)
            await interaction.followup.send(
                "No se pudo guardar la inscripción. El cambio de rol fue revertido.",
                ephemeral=True,
            )
            return

        if status == "registered":
            await interaction.followup.send(
                f"Inscripción al Evento **{result['event']['event_name']}** Exitosa, "
                "Asegúrate de llegar Puntual. 🎉",
                ephemeral=True,
            )
            return

        if status == "duplicate":
            await interaction.followup.send("Ya estas registrado en este evento.", ephemeral=True)
            return

        if not had_role:
            await self.remove_role_safely(member, participant_role)
        messages = {
            "external_id_duplicate": "Esa ID Espacial ya pertenece a otro usuario.",
            "closed": "Las inscripciones de este evento estan cerradas.",
            "no_event": "El evento ya no esta activo.",
        }
        await interaction.followup.send(
            messages.get(status, "No se pudo completar la inscripción."), ephemeral=True
        )

    @staticmethod
    async def remove_role_safely(member: discord.Member, role: discord.Role) -> bool:
        try:
            await member.remove_roles(role, reason="Reversión de inscripción de evento")
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("No se pudo retirar el rol de evento a %s", member.id)
            return False

    async def show_staff_panel(self, interaction: discord.Interaction):
        if not interaction.guild or not await self.require_database(interaction):
            return
        active = await database.get_active_event(interaction.guild.id)
        await interaction.response.send_message(
            embed=staff_panel_embed(active),
            view=StaffPanelView(self),
        )

    async def show_participants(self, interaction: discord.Interaction):
        if not interaction.guild or not await self.require_database(interaction):
            return
        event = await database.get_active_event(interaction.guild.id)
        if not event:
            await send_ephemeral(interaction, "No hay eventos activos.")
            return
        participants = await database.get_event_participants(event["id"])
        if not participants:
            await send_ephemeral(interaction, "Este evento aun no tiene participantes.")
            return

        await interaction.response.defer()
        for start in range(0, len(participants), PARTICIPANTS_PER_EMBED):
            chunk = participants[start:start + PARTICIPANTS_PER_EMBED]
            lines = [
                f"**{row['position']}.** {row['nickname']} | ID: {row['external_id']}"
                + (" ❎" if row["is_overflow"] else "")
                for row in chunk
            ]
            embed = discord.Embed(
                title=f"Participantes - {event['event_name']}",
                description="\n".join(lines),
                color=discord.Color.blue(),
            )
            embed.set_footer(text=f"Total de inscritos: {len(participants)}")
            await interaction.followup.send(embed=embed)

    async def close_registration(self, interaction: discord.Interaction):
        if not interaction.guild or not await self.require_database(interaction):
            return
        event = await database.get_active_event(interaction.guild.id)
        if not event:
            await send_ephemeral(interaction, "No hay eventos activos.")
            return
        if event["status"] == "closed":
            await send_ephemeral(interaction, "Las inscripciones ya estan cerradas.")
            return

        await interaction.response.defer(ephemeral=True)
        closed_event = await database.close_active_event(interaction.guild.id)
        panel_updated = await self.update_registration_message(closed_event, closed=True)
        try:
            await interaction.message.edit(
                embed=staff_panel_embed(closed_event), view=StaffPanelView(self)
            )
        except (discord.NotFound, discord.HTTPException):
            pass

        suffix = "" if panel_updated else " No se pudo actualizar el mensaje público."
        await interaction.followup.send(
            f"Inscripciones de **{closed_event['event_name']}** cerradas.{suffix}",
            ephemeral=True,
        )

    async def update_registration_message(self, event, *, closed: bool) -> bool:
        channel_id = event["registration_channel_id"]
        message_id = event["registration_message_id"]
        if channel_id not in EVENT_ALLOWED_CHANNEL_IDS or not message_id:
            return False
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            await message.edit(
                embed=(
                    registration_closed_embed(event["event_name"])
                    if closed
                    else registration_open_embed(event["event_name"])
                ),
                view=RegistrationView(self, disabled=closed),
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            logger.exception("No se pudo actualizar el panel público del evento %s", event["id"])
            return False

    async def request_finish_confirmation(self, interaction: discord.Interaction):
        if not interaction.guild or not await self.require_database(interaction):
            return
        event = await database.get_active_event(interaction.guild.id)
        if not event:
            await send_ephemeral(interaction, "No hay eventos activos.")
            return
        if interaction.message is None:
            await send_ephemeral(interaction, "No se pudo identificar el panel de eventos.")
            return

        embed = discord.Embed(
            title="Confirmar Finalización",
            description=(
                f"¿Deseas finalizar **{event['event_name']}**?\n\n"
                "Se cerrarán las inscripciones, se publicará la lista final y se retirarán "
                "los cargos después de 5 segundos."
            ),
            color=discord.Color.red(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=FinishConfirmationView(
                self,
                interaction.user.id,
                event["id"],
                event["event_name"],
                interaction.message,
            ),
            ephemeral=True,
        )

    async def finish_event(
        self,
        interaction: discord.Interaction,
        expected_event_id: int,
        panel_message: discord.Message,
    ):
        if (
            not interaction.guild
            or not isinstance(interaction.user, discord.Member)
            or not is_event_team(interaction.user)
        ):
            await send_ephemeral(
                interaction,
                f"Solo el rol **{EVENT_PANEL_ROLE_NAME}** puede finalizar eventos.",
            )
            return
        if not await self.require_database(interaction):
            return

        event = await database.get_active_event(interaction.guild.id)
        if not event or event["id"] != expected_event_id:
            await interaction.response.edit_message(
                content="Este evento ya no está activo.", embed=None, view=None
            )
            return
        if interaction.guild.id in self.finishing_guilds:
            await send_ephemeral(interaction, "Este evento ya se esta finalizando.")
            return

        self.finishing_guilds.add(interaction.guild.id)
        try:
            await self._finish_event(interaction, event, panel_message)
        finally:
            self.finishing_guilds.discard(interaction.guild.id)

    async def _finish_event(
        self,
        interaction: discord.Interaction,
        event,
        panel_message: discord.Message,
    ):
        await interaction.response.defer(ephemeral=True)

        if event["status"] == "open":
            event = await database.close_active_event(interaction.guild.id)
            if event is None:
                event = await database.get_active_event(interaction.guild.id)
        if not event:
            await interaction.edit_original_response(
                content="El evento ya no está activo.", embed=None, view=None
            )
            return

        await self.update_registration_message(event, closed=True)
        participants = await database.get_event_participants(event["id"])

        try:
            if participants:
                for start in range(0, len(participants), PARTICIPANTS_PER_EMBED):
                    chunk = participants[start:start + PARTICIPANTS_PER_EMBED]
                    lines = [
                        f"**{row['position']}.** <@{row['user_id']}> | "
                        f"{row['nickname']} | ID: {row['external_id']}"
                        for row in chunk
                    ]
                    embed = discord.Embed(
                        title=f"Registro Final - {event['event_name']}",
                        description="\n".join(lines),
                        color=discord.Color.gold(),
                    )
                    embed.set_footer(text=f"Total de participantes: {len(participants)}")
                    await interaction.channel.send(embed=embed)
            else:
                await interaction.channel.send(
                    f"**{event['event_name']}** finalizó sin participantes registrados."
                )
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            logger.exception("No se pudo publicar la lista final del evento %s", event["id"])
            await interaction.edit_original_response(
                content=(
                    "No se pudo publicar la lista final. El evento quedó cerrado, pero no "
                    "se retiraron cargos ni se limpiaron inscripciones."
                ),
                embed=None,
                view=None,
            )
            return

        await asyncio.sleep(5)
        role = interaction.guild.get_role(EVENT_PARTICIPANT_ROLE_ID)
        role_failures = 0
        if role:
            for row in participants:
                member = interaction.guild.get_member(row["user_id"])
                if member is None:
                    try:
                        member = await interaction.guild.fetch_member(row["user_id"])
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        member = None
                if member is None:
                    role_failures += 1
                elif role in member.roles:
                    if not await self.remove_role_safely(member, role):
                        role_failures += 1
        elif participants:
            role_failures = len(participants)

        try:
            await database.delete_event(event["id"])
        except Exception:
            logger.exception("No se pudo limpiar el evento finalizado %s", event["id"])
            await interaction.edit_original_response(
                content=(
                    "Los cargos fueron procesados, pero no se pudo limpiar el evento en la DB. "
                    "Puedes intentar **Finalizar Evento** nuevamente."
                ),
                embed=None,
                view=None,
            )
            return

        for key, lock in list(self.registration_locks.items()):
            if key[0] == interaction.guild.id and not lock.locked():
                self.registration_locks.pop(key, None)
        try:
            await panel_message.edit(
                embed=staff_panel_embed(None), view=StaffPanelView(self)
            )
        except (discord.NotFound, discord.HTTPException):
            pass

        summary = f"Evento **{event['event_name']}** finalizado y lista activa limpiada."
        if role_failures:
            summary += f" No se pudo retirar el rol a {role_failures} participante(s)."
        await interaction.edit_original_response(content=summary, embed=None, view=None)

    async def open_admin(self, interaction: discord.Interaction):
        if not await self.require_event_admin(interaction):
            return
        embed = discord.Embed(
            title="Administración de Eventos",
            description="Selecciona una opción de gestión.",
            color=discord.Color.orange(),
        )
        embed.set_thumbnail(url=ADMIN_PANEL_THUMBNAIL_URL)
        await interaction.response.send_message(
            embed=embed,
            view=AdminMenuView(self, interaction.user.id),
            ephemeral=True,
        )

    async def add_catalog_event(self, interaction: discord.Interaction, raw_name: str):
        if (
            not interaction.guild
            or not await self.require_event_admin(interaction)
            or not await self.require_database(interaction)
        ):
            return
        name = normalize_event_name(raw_name)
        if len(name) < 2:
            await send_ephemeral(interaction, "El nombre del evento es demasiado corto.")
            return
        status, row = await database.add_event_catalog(
            interaction.guild.id,
            name,
            name.casefold(),
            interaction.user.id,
            EVENT_CATALOG_MAX_ITEMS,
        )
        messages = {
            "created": f"Evento **{name}** agregado al catálogo.",
            "duplicate": "Ya existe un evento con ese nombre.",
            "full": f"El catálogo alcanzó su limite de {EVENT_CATALOG_MAX_ITEMS} eventos.",
        }
        await send_ephemeral(interaction, messages.get(status, "No se pudo agregar el evento."))

    async def open_edit_records(self, interaction: discord.Interaction, owner_id: int):
        await self.show_edit_records_page(interaction, owner_id, 0)

    async def show_edit_records_page(
        self,
        interaction: discord.Interaction,
        owner_id: int,
        page: int,
    ):
        if (
            not interaction.guild
            or not await self.require_event_admin(interaction)
            or not await self.require_database(interaction)
        ):
            return

        total = await database.get_event_user_count(interaction.guild.id)
        if total == 0:
            await interaction.response.edit_message(
                content="No hay registros para editar.", embed=None, view=None
            )
            return

        max_page = max(0, (total - 1) // EDIT_USERS_PER_PAGE)
        page = min(max(page, 0), max_page)
        rows = await database.get_event_users_page(
            interaction.guild.id,
            EDIT_USERS_PER_PAGE,
            page * EDIT_USERS_PER_PAGE,
        )
        options = []
        for row in rows:
            member = interaction.guild.get_member(row["user_id"])
            if member is not None:
                discord_nick = member.nick or member.name
            else:
                discord_nick = row["discord_tag"]
            options.append(
                discord.SelectOption(
                    label=str(discord_nick)[:100],
                    value=str(row["user_id"]),
                    description=(
                        f"{row['nickname']} | ID {row['external_id']}"
                    )[:100],
                )
            )

        if page > 0:
            options.append(
                discord.SelectOption(
                    label="Volver a los 20 registros anteriores",
                    value=f"page:{page - 1}",
                )
            )
        if (page + 1) * EDIT_USERS_PER_PAGE < total:
            options.append(
                discord.SelectOption(
                    label="Ver los siguientes 20 registros",
                    value=f"page:{page + 1}",
                )
            )

        embed = discord.Embed(
            title="Editar registro",
            description=(
                "Selecciona un usuario por su nickname de Discord.\n"
                f"Página **{page + 1}/{max_page + 1}**"
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=EditEventUserSelectionView(
                self,
                owner_id,
                page,
                options,
            ),
        )

    async def open_edit_record_modal(
        self,
        interaction: discord.Interaction,
        user_id: int,
    ):
        if (
            not interaction.guild
            or not await self.require_event_admin(interaction)
            or not await self.require_database(interaction)
        ):
            return
        profile = await database.get_event_user(interaction.guild.id, user_id)
        if not profile:
            await send_ephemeral(interaction, "Ese registro ya no existe.")
            return
        await interaction.response.send_modal(EditEventUserModal(self, profile))

    async def edit_event_user_profile(
        self,
        interaction: discord.Interaction,
        user_id: int,
        raw_nickname: str,
        raw_spatial_id: str,
        raw_country: str,
    ):
        if (
            not interaction.guild
            or not await self.require_event_admin(interaction)
            or not await self.require_database(interaction)
        ):
            return

        nickname = raw_nickname.strip()
        spatial_id = raw_spatial_id.strip()
        country = raw_country.strip()
        if len(nickname) < 2 or len(country) < 2:
            await send_ephemeral(
                interaction,
                "Nickname y Pais deben contener al menos 2 caracteres visibles.",
            )
            return
        if not SPATIAL_ID_PATTERN.fullmatch(spatial_id):
            await send_ephemeral(
                interaction,
                "La ID Espacial debe contener solamente entre 7 y 10 numeros.",
            )
            return

        status, profile = await database.update_event_user_profile(
            interaction.guild.id,
            user_id,
            nickname,
            spatial_id,
            country,
        )
        messages = {
            "updated": f"Registro de <@{user_id}> actualizado correctamente.",
            "external_id_duplicate": "Esa ID Espacial ya pertenece a otro usuario.",
            "missing": "El registro ya no existe.",
        }
        await send_ephemeral(
            interaction,
            messages.get(status, "No se pudo actualizar el registro."),
        )

    async def open_remove_event(self, interaction: discord.Interaction, owner_id: int):
        if not interaction.guild or not await self.require_database(interaction):
            return
        events = await database.list_event_catalog(interaction.guild.id)
        if not events:
            await interaction.response.edit_message(
                content="No hay eventos guardados para remover.", embed=None, view=None
            )
            return
        embed = discord.Embed(
            title="Remover Evento",
            description="Selecciona un evento del catálogo.",
            color=discord.Color.orange(),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=RemoveEventSelectionView(self, owner_id, events),
        )

    async def remove_catalog_event(
        self, interaction: discord.Interaction, event_id: int, event_name: str
    ):
        if not interaction.guild or not await self.require_database(interaction):
            return
        status, row = await database.remove_event_catalog(interaction.guild.id, event_id)
        messages = {
            "deleted": f"Evento **{event_name}** removido del catálogo.",
            "active": "No puedes remover el evento mientras esté activo.",
            "missing": "El evento ya no existe en el catálogo.",
        }
        await interaction.response.edit_message(
            content=messages.get(status, "No se pudo remover el evento."),
            embed=None,
            view=None,
        )

    async def open_database_records(self, interaction: discord.Interaction, owner_id: int):
        if not interaction.guild or not await self.require_database(interaction):
            return
        total = await database.get_event_user_count(interaction.guild.id)
        embed = await self.database_records_embed(interaction.guild.id, 0, total)
        await interaction.response.edit_message(
            embed=embed,
            view=EventUsersPaginator(self, owner_id, 0, total),
        )

    async def change_database_page(
        self, interaction: discord.Interaction, owner_id: int, page: int
    ):
        if not interaction.guild or not await self.require_database(interaction):
            return
        total = await database.get_event_user_count(interaction.guild.id)
        max_page = max(0, (total - 1) // USERS_PER_PAGE)
        page = min(max(page, 0), max_page)
        embed = await self.database_records_embed(interaction.guild.id, page, total)
        await interaction.response.edit_message(
            embed=embed,
            view=EventUsersPaginator(self, owner_id, page, total),
        )

    async def database_records_embed(self, guild_id: int, page: int, total: int):
        rows = await database.get_event_users_page(
            guild_id, USERS_PER_PAGE, page * USERS_PER_PAGE
        )
        total_pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
        embed = discord.Embed(
            title="Registros DB",
            color=discord.Color.blue(),
        )
        if not rows:
            embed.description = "No hay perfiles registrados."
        else:
            for row in rows:
                embed.add_field(
                    name=f"{row['nickname']} | {row['external_id']}",
                    value=(
                        f"Usuario: <@{row['user_id']}> (`{row['user_id']}`)\n"
                        f"Pais: {row['country']}"
                    ),
                    inline=False,
                )
        embed.set_footer(text=f"Página {page + 1}/{total_pages} | {total} registros")
        return embed


def setup(bot):
    registro_eventos = RegistroEventos(bot)
    bot.add_view(RegistrationView(registro_eventos))
    bot.add_view(StaffPanelView(registro_eventos))

    @bot.tree.command(
        name="abrir_registro",
        description="(Staff) Configura y publica el registro de un evento.",
    )
    @require_staff()
    async def abrir_registro(interaction: discord.Interaction):
        await registro_eventos.open_registration(interaction)

    @bot.tree.command(
        name="panel_eventos",
        description="(Equipo de Eventos) Abre el panel de gerenciamiento.",
    )
    @require_event_team()
    async def panel_eventos(interaction: discord.Interaction):
        await registro_eventos.show_staff_panel(interaction)
