"""Nachtdivisie role self-assign setup.

Provisions (once, idempotently cached via poll_state) in the war guild:
  - Role "nachtdivisie" (created if it doesn't exist yet)
  - Channel "nachtdivisie-aanmelden" in category 1505972671224090744 with a
    button that toggles the role on/off
  - Private channel "nachtdivisie" in category 1559972902466625566, visible
    only to members with that role

Runs once at startup and is safe to re-run on every restart: each step
checks poll_state for a cached ID first and only creates something new when
nothing is cached (or the cached object was deleted).
"""

from __future__ import annotations

import asyncio
import logging

import discord

from cogs.role_selection.roles import RoleToggleView
from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_GUILD_ID = 1505216404285030410
_SIGNUP_CATEGORY_ID = 1505972671224090744
_PRIVATE_CATEGORY_ID = 1559972902466625566
_SIGNUP_CHANNEL_NAME = "nachtdivisie-aanmelden"
_PRIVATE_CHANNEL_NAME = "nachtdivisie"
_ROLE_NAME = "nachtdivisie"


def _toggle_view(role_id: int) -> RoleToggleView:
    """Build the persistent Aanmelden/Afmelden button for *role_id*.

    Reuses RoleToggleButton's existing add-if-missing/remove-if-present
    logic, so clicking it again removes the role.
    """
    return RoleToggleView(
        [{"label": "Aanmelden", "role_id": role_id, "style": "success", "emoji": "🌙"}],
        exclusive=False,
    )


class NachtdivisieSetupCog(TaskCogBase, name="nachtdivisie_setup"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._setup_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        # Re-register the persistent view immediately with any cached role
        # id, so existing button clicks keep working right after a restart
        # even before _deferred_setup (which waits for DB/API) finishes.
        cached_role_id = await self._cached_role_id()
        if cached_role_id:
            self.bot.add_view(_toggle_view(cached_role_id))
        self._setup_task = asyncio.create_task(self._deferred_setup())

    async def cog_unload(self) -> None:
        if self._setup_task:
            self._setup_task.cancel()

    async def _cached_role_id(self) -> int | None:
        if not self._db:
            return None
        cached = await self._db.get_poll_state("nachtdivisie_role_id")
        return int(cached) if cached else None

    async def _deferred_setup(self) -> None:
        await self._wait_for_services()
        guild = self.bot.get_guild(_GUILD_ID)
        if guild is None:
            logger.warning("nachtdivisie_setup: guild %d not found", _GUILD_ID)
            return
        try:
            role = await self._ensure_role(guild)
            if role is None:
                return
            # Covers the very first run, when cog_load had no cached id yet.
            self.bot.add_view(_toggle_view(role.id))
            await self._ensure_signup_channel(guild, role)
            await self._ensure_private_channel(guild, role)
        except Exception:
            logger.exception("nachtdivisie_setup: provisioning failed")

    async def _ensure_role(self, guild: discord.Guild) -> discord.Role | None:
        cached_id = await self._cached_role_id()
        if cached_id:
            role = guild.get_role(cached_id)
            if role:
                return role

        existing = discord.utils.get(guild.roles, name=_ROLE_NAME)
        if existing:
            if self._db:
                await self._db.set_poll_state("nachtdivisie_role_id", str(existing.id))
            return existing

        try:
            role = await guild.create_role(
                name=_ROLE_NAME,
                mentionable=True,
                reason="nachtdivisie_setup: automatisch aangemaakt",
            )
        except discord.Forbidden:
            logger.error("nachtdivisie_setup: no permission to create role")
            return None

        if self._db:
            await self._db.set_poll_state("nachtdivisie_role_id", str(role.id))
        logger.info("nachtdivisie_setup: created role %s (%d)", role.name, role.id)
        return role

    async def _ensure_channel_in_category(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel,
        name: str,
        cache_key: str,
        overwrites: dict,
    ) -> discord.TextChannel | None:
        cached_id = await self._db.get_poll_state(cache_key) if self._db else None
        if cached_id:
            channel = guild.get_channel(int(cached_id))
            if isinstance(channel, discord.TextChannel):
                return channel

        existing = discord.utils.get(category.text_channels, name=name)
        if existing:
            if self._db:
                await self._db.set_poll_state(cache_key, str(existing.id))
            return existing

        try:
            channel = await guild.create_text_channel(
                name,
                category=category,
                overwrites=overwrites,
                reason="nachtdivisie_setup: automatisch aangemaakt",
            )
        except discord.Forbidden:
            logger.error("nachtdivisie_setup: no permission to create channel %s", name)
            return None

        logger.info("nachtdivisie_setup: created channel #%s (%d)", channel.name, channel.id)
        if self._db:
            await self._db.set_poll_state(cache_key, str(channel.id))
        return channel

    async def _ensure_signup_channel(self, guild: discord.Guild, role: discord.Role) -> None:
        category = guild.get_channel(_SIGNUP_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            logger.warning(
                "nachtdivisie_setup: signup category %d not found", _SIGNUP_CATEGORY_ID
            )
            return

        channel = await self._ensure_channel_in_category(
            guild,
            category,
            _SIGNUP_CHANNEL_NAME,
            "nachtdivisie_signup_channel_id",
            overwrites={
                guild.default_role: discord.PermissionOverwrite(
                    send_messages=False, view_channel=True
                ),
            },
        )
        if channel is None:
            return

        msg_id = await self._db.get_poll_state("nachtdivisie_signup_msg_id") if self._db else None
        if msg_id:
            try:
                await channel.fetch_message(int(msg_id))
                return  # Button message already posted and still exists.
            except (discord.NotFound, discord.HTTPException):
                pass

        embed = discord.Embed(
            title="🌙 Nachtdivisie",
            description=(
                "Klik op de knop om je aan te melden voor de nachtdivisie.\n"
                "Klik nogmaals om je weer af te melden."
            ),
            color=self._embed_colour(),
        )
        msg = await channel.send(embed=embed, view=_toggle_view(role.id))
        if self._db:
            await self._db.set_poll_state("nachtdivisie_signup_msg_id", str(msg.id))

    async def _ensure_private_channel(self, guild: discord.Guild, role: discord.Role) -> None:
        category = guild.get_channel(_PRIVATE_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            logger.warning(
                "nachtdivisie_setup: private category %d not found", _PRIVATE_CATEGORY_ID
            )
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        channel = await self._ensure_channel_in_category(
            guild,
            category,
            _PRIVATE_CHANNEL_NAME,
            "nachtdivisie_private_channel_id",
            overwrites=overwrites,
        )
        if channel is not None:
            try:
                await channel.edit(overwrites=overwrites)
            except discord.Forbidden:
                logger.warning(
                    "nachtdivisie_setup: no permission to sync private channel overwrites"
                )


async def setup(bot) -> None:
    await bot.add_cog(NachtdivisieSetupCog(bot))
