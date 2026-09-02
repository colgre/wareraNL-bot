"""War-guild role sync cog.

This cog is ONLY loaded when ``config["war_guild"]`` is present.  The
production bot's config.json does not contain this key, so this file is
silently skipped there — making it safe to commit to the shared repo.

Expected config shape (under ``config["war_guild"]``):
    {
        "guild_id":                    <int>,  # war guild Discord ID
        "source_guild_id":             <int>,  # production guild Discord ID
        "source_nederlander_role_id":  <int>,  # Nederlander role ID in prod guild
        "nederlander_role_id":         <int>,  # Nederlander role ID in war guild
        "verify_channel_id":           <int>,  # channel for the Verifieren button
        "admin_role_id":               <int>,  # war admin role that can trigger /syncwar (legacy, single role)
        "admin_role_ids":              [<int>, ...]  # optional: additional war admin roles
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# ── Colour mapping for game colorScheme strings ──────────────────────────────

_COLOUR_MAP: dict[str, int] = {
    "blue":    0x3498DB,
    "red":     0xE74C3C,
    "green":   0x2ECC71,
    "yellow":  0xF1C40F,
    "purple":  0x9B59B6,
    "orange":  0xE67E22,
    "pink":    0xFF69B4,
    "teal":    0x1ABC9C,
    "cyan":    0x00BCD4,
    "brown":   0x795548,
    "gold":    0xFFD700,
    "silver":  0xBDC3C7,
    "white":   0xF8F8F8,
    "black":   0x2C2F33,
    "default": 0x99AAB5,
}


def _colour(scheme: str | None) -> discord.Colour:
    raw = (scheme or "default").strip().lower()
    return discord.Colour(_COLOUR_MAP.get(raw, _COLOUR_MAP["default"]))


# ── Persistent verify button ──────────────────────────────────────────────────

class VerifyView(discord.ui.View):
    """Persistent view for the Verifieren button.

    ``timeout=None`` keeps it alive across bot restarts.
    The cog instance is looked up via the button interaction's bot attribute
    so the view itself stays stateless.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verifieren",
        style=discord.ButtonStyle.primary,
        emoji="🇳🇱",
        custom_id="war_verify_v1",
    )
    async def verify_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        cog: Optional[WarSyncCog] = interaction.client.cogs.get("war_sync")  # type: ignore[assignment]
        if cog is None:
            await interaction.response.send_message(
                "❌ Verificatiesysteem niet beschikbaar.", ephemeral=True
            )
            return
        await cog.handle_verify(interaction)


# ── Main cog ─────────────────────────────────────────────────────────────────

class WarSyncCog(TaskCogBase, name="war_sync"):
    """Manages roles on the war guild based on production-server membership."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._war_cfg: dict = bot.config["war_guild"]

        # Populated by daily MU scan; used by hourly sync.
        # {in_game_user_id: {mu_id: 'owner' | 'commander' | 'member'}}
        self._user_mu_roles: dict[str, dict[str, str]] = {}
        # {mu_id: {'owner': discord_role_id, 'commander': ..., 'member': ...}}
        self._mu_discord_role_ids: dict[str, dict[str, int]] = {}
        # Prevents concurrent scan_dutch_mus calls (e.g. before_hourly_sync +
        # daily_mu_scan_task both firing at startup).
        self._scan_lock = asyncio.Lock()

        # Resolved at startup — may differ from config when the bot created the role.
        self._resolved_ned_role_id: Optional[int] = None

        self._http: Optional[aiohttp.ClientSession] = None
        self._setup_task: Optional[asyncio.Task] = None

    # ── Config helpers ────────────────────────────────────────────────────────

    @property
    def _war_guild(self) -> Optional[discord.Guild]:
        return self.bot.get_guild(int(self._war_cfg["guild_id"]))

    @property
    def _verifier_url(self) -> str:
        return str(self._war_cfg.get("verifier_url", "http://127.0.0.1:8765")).rstrip("/")

    @property
    def _verifier_secret(self) -> str:
        return os.environ.get("VERIFIER_SECRET", "")

    @property
    def _war_nederlander_role_id(self) -> int:
        return self._resolved_ned_role_id or int(self._war_cfg.get("nederlander_role_id", 0))

    @property
    def _verify_channel_id(self) -> int:
        return int(self._war_cfg["verify_channel_id"])

    @property
    def _admin_role_ids(self) -> set[int]:
        """War-guild admin role IDs allowed to run /syncwar.

        Supports both the legacy single ``admin_role_id`` key and a
        ``admin_role_ids`` list, so multiple roles can be granted access.
        """
        ids: set[int] = set()
        single = self._war_cfg.get("admin_role_id")
        if single:
            ids.add(int(single))
        for rid in self._war_cfg.get("admin_role_ids") or []:
            ids.add(int(rid))
        return ids

    @property
    def _nl_country_id(self) -> str:
        return str(self.bot.config.get("nl_country_id", ""))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        # Register the persistent view immediately (before any DB/API is ready)
        # so button clicks on existing messages work right away.
        self.bot.add_view(VerifyView())
        self._http = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._verifier_secret}"}
        )
        self._setup_task = asyncio.create_task(self._deferred_startup())
        self.hourly_sync_task.start()
        self.daily_mu_scan_task.start()

    async def cog_unload(self) -> None:
        if self._setup_task:
            self._setup_task.cancel()
        self.hourly_sync_task.cancel()
        self.daily_mu_scan_task.cancel()
        if self._http:
            await self._http.close()
            self._http = None

    async def _deferred_startup(self) -> None:
        """Ensure all required Discord roles exist, then post the verify message."""
        await self._wait_for_services()
        war_guild = self._war_guild
        if war_guild:
            await self._ensure_nederlander_role(war_guild)
        await self._ensure_verify_message()

    async def _ensure_nederlander_role(self, war_guild: discord.Guild) -> Optional[discord.Role]:
        """Return the war-guild Nederlander role, creating it if it doesn't exist.

        Resolution order:
        1. Config ``nederlander_role_id`` (if non-zero and role found)
        2. ``poll_state`` key ``war_ned_role_id`` (cached from a previous auto-create)
        3. Role named "Nederlander" already in the guild
        4. Create a new "Nederlander" role (Dutch-orange) and cache its ID
        """
        # 1. Config-specified ID
        cfg_id = int(self._war_cfg.get("nederlander_role_id", 0))
        if cfg_id:
            role = war_guild.get_role(cfg_id)
            if role:
                self._resolved_ned_role_id = cfg_id
                return role

        # 2. Cached ID from a previous run
        if self._db:
            cached = await self._db.get_poll_state("war_ned_role_id")
            if cached:
                role = war_guild.get_role(int(cached))
                if role:
                    self._resolved_ned_role_id = int(cached)
                    return role

        # 3. Existing role by name
        existing = discord.utils.get(war_guild.roles, name="Nederlander")
        if existing:
            self._resolved_ned_role_id = existing.id
            if self._db:
                await self._db.set_poll_state("war_ned_role_id", str(existing.id))
            return existing

        # 4. Create it
        try:
            role = await war_guild.create_role(
                name="Nederlander",
                colour=discord.Colour(0xFF6600),  # Dutch orange
                reason="war_sync: auto-aanmaken Nederlander-rol",
            )
            self._resolved_ned_role_id = role.id
            if self._db:
                await self._db.set_poll_state("war_ned_role_id", str(role.id))
            logger.info("war_sync: created Nederlander role (id=%d)", role.id)
            return role
        except discord.Forbidden:
            logger.error("war_sync: no permission to create Nederlander role")
            return None

    # ── Verify message management ─────────────────────────────────────────────

    async def _ensure_verify_message(self) -> None:
        """Post a Verifieren button in the verify channel if not already there."""
        war_guild = self._war_guild
        if not war_guild:
            logger.warning("war_sync: war guild %s not found", self._war_cfg.get("guild_id"))
            return

        channel = war_guild.get_channel(self._verify_channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning("war_sync: verify channel %d not found", self._verify_channel_id)
            return

        # Check if we already posted a button message (stored in poll_state)
        if self._db:
            stored_id = await self._db.get_poll_state("war_verify_msg_id")
            if stored_id:
                try:
                    await channel.fetch_message(int(stored_id))
                    logger.info("war_sync: verify message %s still exists, skipping post", stored_id)
                    return
                except discord.NotFound:
                    pass  # Message was deleted; fall through to re-post

        embed = discord.Embed(
            title="🇳🇱 Verificatie",
            description=(
                "Klik op de knop hieronder om te verifiëren.\n\n"
                "Je Discord-account wordt gekoppeld aan je **Nederlander**-status "
                "op de officiële server."
            ),
            colour=self._embed_colour("primary"),
        )
        msg = await channel.send(embed=embed, view=VerifyView())
        if self._db:
            await self._db.set_poll_state("war_verify_msg_id", str(msg.id))
        logger.info("war_sync: posted verify message %d", msg.id)

    # ── Button handler ────────────────────────────────────────────────────────

    async def handle_verify(self, interaction: discord.Interaction) -> None:
        """Called by VerifyView when a user clicks Verifieren."""
        await interaction.response.defer(ephemeral=True)

        war_guild = self._war_guild
        if not war_guild:
            await interaction.followup.send(
                "❌ Configuratiefout. Neem contact op met een admin.", ephemeral=True
            )
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = war_guild.get_member(interaction.user.id)
        if not member:
            await interaction.followup.send("❌ Kon jouw lidmaatschap niet ophalen.", ephemeral=True)
            return

        # Check if they already have the Nederlander role
        war_ned_role = war_guild.get_role(self._war_nederlander_role_id)
        if war_ned_role and war_ned_role in member.roles:
            await interaction.followup.send(
                "✅ Je bent al geverifieerd als Nederlander.", ephemeral=True
            )
            return

        # Ask the verifier bot whether this user has Nederlander on the production server
        # and retrieve their server nickname (= warera username) in the same call.
        has_ned, nickname = await self._verifier_check_member(member.id)
        if has_ned is None:
            await interaction.followup.send(
                "❌ Verificatieservice tijdelijk niet beschikbaar. Probeer het later opnieuw.",
                ephemeral=True,
            )
            return
        if not has_ned:
            await interaction.followup.send(
                "❌ Je hebt de **Nederlander**-rol niet op de officiële server. "
                "Verifieer daar eerst.",
                ephemeral=True,
            )
            return

        # Grant the Nederlander role
        if war_ned_role:
            try:
                await member.add_roles(war_ned_role, reason="war_sync: verificatie via knop")
            except discord.Forbidden:
                await interaction.followup.send(
                    "❌ Ik heb geen toestemming om jou een rol te geven. Neem contact op met een admin.",
                    ephemeral=True,
                )
                return

        # Link Discord identity → in-game user so MU roles can be synced
        in_game_id: Optional[str] = None
        if nickname and self._db:
            existing = await self._db.get_identity_link_by_discord(str(member.id))
            if existing:
                in_game_id = existing["in_game_user_id"]
            else:
                in_game_id = await self._lookup_in_game_id(nickname)
                if in_game_id:
                    try:
                        await self._db.upsert_identity_link(
                            discord_user_id=str(member.id),
                            guild_id=str(war_guild.id),
                            in_game_user_id=in_game_id,
                            nationality="nederlander",
                            request_type="war_verify",
                            approved_by_discord_id=str(self.bot.user.id) if self.bot.user else "0",
                            approved_at=datetime.now(timezone.utc).isoformat(),
                        )
                        logger.info(
                            "war_sync: linked %s (%d) → in_game_id %s (nickname=%r)",
                            member.name, member.id, in_game_id, nickname,
                        )
                    except Exception as exc:
                        logger.warning("war_sync: failed to store identity link: %s", exc)
                else:
                    logger.warning(
                        "war_sync: could not find in-game user for nickname %r (member %s)",
                        nickname, member.name,
                    )

        # Set in-game nickname (preserve any [DN] division prefix already applied)
        if nickname:
            try:
                if member.nick and (m := re.match(r"^\[D\d\] ", member.nick)):
                    target_nick = (m.group(0) + nickname)[:32]
                else:
                    target_nick = nickname
                await member.edit(nick=target_nick, reason="war_sync: verificatie")
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.debug("war_sync: cannot set nickname for %s: %s", member.name, exc)

        # Assign division role + [DN] prefix
        if in_game_id:
            divisions_cog = self.bot.cogs.get("WarGuildDivisionsCog")
            if divisions_cog:
                try:
                    await divisions_cog.sync_member_division(member, in_game_id)
                except Exception as exc:
                    logger.warning("war_sync: division sync failed for %s: %s", member.name, exc)

        # Assign MU roles — use DB path so it works regardless of in-memory cache state
        if in_game_id:
            await self._assign_mu_roles_from_db(member, in_game_id)
        else:
            await self._assign_mu_roles_for_member(member)

        await interaction.followup.send(
            "✅ Je bent geverifieerd als **Nederlander**! Welkom op de server.",
            ephemeral=True,
        )
        logger.info("war_sync: verified %s (%d) via button", member.name, member.id)

    # ── Member join ───────────────────────────────────────────────────────────

    @discord.ext.commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Auto-verify members who already have Nederlander on the production server."""
        war_guild = self._war_guild
        if not war_guild or member.guild.id != war_guild.id:
            return

        has_ned, nickname = await self._verifier_check_member(member.id)
        if not has_ned:
            return

        war_ned_role = war_guild.get_role(self._war_nederlander_role_id)
        if war_ned_role:
            try:
                await member.add_roles(war_ned_role, reason="war_sync: auto-verificatie bij joinen")
                logger.info("war_sync: auto-verified %s (%d) on join", member.name, member.id)
            except discord.Forbidden:
                logger.warning("war_sync: cannot add Nederlander to %s — missing perms", member.name)

        # Set in-game nickname (new join: no existing [DN] prefix yet)
        if nickname:
            try:
                await member.edit(nick=nickname, reason="war_sync: auto-verificatie bij joinen")
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.debug("war_sync: cannot set nickname for %s on join: %s", member.name, exc)

        # Link identity and assign MU roles
        in_game_id: Optional[str] = None
        if nickname and self._db:
            existing = await self._db.get_identity_link_by_discord(str(member.id))
            if existing:
                in_game_id = existing["in_game_user_id"]
            else:
                in_game_id = await self._lookup_in_game_id(nickname)
                if in_game_id:
                    try:
                        await self._db.upsert_identity_link(
                            discord_user_id=str(member.id),
                            guild_id=str(war_guild.id),
                            in_game_user_id=in_game_id,
                            nationality="nederlander",
                            request_type="war_verify",
                            approved_by_discord_id=str(self.bot.user.id) if self.bot.user else "0",
                            approved_at=datetime.now(timezone.utc).isoformat(),
                        )
                        logger.info(
                            "war_sync: linked %s (%d) → in_game_id %s on join (nickname=%r)",
                            member.name, member.id, in_game_id, nickname,
                        )
                    except Exception as exc:
                        logger.warning("war_sync: failed to store identity link on join: %s", exc)
                else:
                    logger.warning(
                        "war_sync: could not find in-game user for nickname %r on join (member %s)",
                        nickname, member.name,
                    )

        if in_game_id:
            # Assign division role + [DN] prefix
            divisions_cog = self.bot.cogs.get("WarGuildDivisionsCog")
            if divisions_cog:
                try:
                    await divisions_cog.sync_member_division(member, in_game_id)
                except Exception as exc:
                    logger.warning(
                        "war_sync: division sync on join failed for %s: %s",
                        member.name, exc,
                    )
            await self._assign_mu_roles_from_db(member, in_game_id)
        else:
            await self._assign_mu_roles_for_member(member)

    # ── MU role helpers ───────────────────────────────────────────────────────

    async def _assign_mu_roles_from_db(
        self, member: discord.Member, in_game_id: str
    ) -> None:
        """Assign MU Discord roles using DB data — works even when the in-memory
        cache hasn't been populated yet (e.g. right after a bot restart).

        Looks up the citizen's MU membership from citizen_mu_membership, then
        resolves Discord role IDs via the war_mu_roles table or by role name.
        """
        if not self._db:
            return
        war_guild = self._war_guild
        if not war_guild:
            return
        try:
            memberships = await self._db.get_mu_memberships_for_citizen(in_game_id)
        except Exception as exc:
            logger.warning("war_sync: failed to get MU memberships for %s: %s", in_game_id, exc)
            return

        # Fallback: citizen_mu_membership only contains users seen in the latest
        # scan_dutch_mus run.  For users in Dutch MUs that the scan missed (e.g.
        # the MU owner is foreign but all members are Dutch), fall back to
        # citizen_levels.mu_id / mu_name which the data-fetcher keeps up-to-date.
        if not memberships:
            try:
                mu_row = await self._db.get_citizen_mu_from_levels(in_game_id)
                if mu_row:
                    memberships = [(mu_row[0], mu_row[1], "member")]
                    logger.debug(
                        "war_sync: MU fallback via citizen_levels for %s → %s",
                        in_game_id, mu_row[1],
                    )
            except Exception as exc:
                logger.warning(
                    "war_sync: citizen_levels MU fallback failed for %s: %s", in_game_id, exc
                )

        for mu_id, mu_name, role_type in memberships:
            # Owners have the same in-game rights as commanders and no longer
            # get a dedicated Discord role — grant Commander (+ Member) instead.
            discord_role_type = "commander" if role_type == "owner" else role_type
            role_types_to_grant = [discord_role_type]
            if discord_role_type == "commander":
                role_types_to_grant.append("member")
            for rt in role_types_to_grant:
                # Try in-memory map first (fast path)
                role_id = self._mu_discord_role_ids.get(mu_id, {}).get(rt)
                if role_id:
                    role = war_guild.get_role(role_id)
                else:
                    # Fall back: look up in war_mu_roles DB table, then by name
                    stored = await self._get_war_mu_role(mu_id, rt, str(war_guild.id))
                    role = war_guild.get_role(stored) if stored else None
                    if not role:
                        role = discord.utils.get(war_guild.roles, name=f"{mu_name} {rt.capitalize()}")
                if role and role not in member.roles:
                    try:
                        await member.add_roles(role, reason=f"war_sync: MU-rol {rt}")
                    except discord.Forbidden:
                        logger.warning(
                            "war_sync: cannot add role %s to %s", role.name, member.name
                        )

    async def _assign_mu_roles_for_member(self, member: discord.Member) -> None:
        """Assign MU Discord roles to a single war guild member based on cached data."""
        if not self._user_mu_roles or not self._mu_discord_role_ids:
            return
        if not self._db:
            return

        identity = await self._db.get_identity_link_by_discord(str(member.id))
        if not identity:
            return

        in_game_id = identity["in_game_user_id"]
        mu_map = self._user_mu_roles.get(in_game_id, {})
        if not mu_map:
            return

        war_guild = self._war_guild
        if not war_guild:
            return

        for mu_id, role_type in mu_map.items():
            # Owners have the same in-game rights as commanders and no longer
            # get a dedicated Discord role — grant Commander (+ Member) instead.
            discord_role_type = "commander" if role_type == "owner" else role_type
            role_ids = self._mu_discord_role_ids.get(mu_id, {})
            role_id_list = [role_ids.get(discord_role_type)]
            if discord_role_type == "commander":
                role_id_list.append(role_ids.get("member"))
            for role_id in role_id_list:
                if not role_id:
                    continue
                role = war_guild.get_role(role_id)
                if role and role not in member.roles:
                    try:
                        await member.add_roles(role, reason=f"war_sync: MU-rol {role_type}")
                    except discord.Forbidden:
                        logger.warning("war_sync: cannot add role %s to %s", role.name, member.name)

    # ── Full sync ─────────────────────────────────────────────────────────────

    async def sync_war_guild(self) -> dict[str, int]:
        """Sync Nederlander + MU roles for every member in the war guild.

        Returns a dict with counts: nederlander_added, nederlander_removed,
        mu_roles_added, mu_roles_removed.
        """
        war_guild = self._war_guild
        if not war_guild:
            logger.warning("war_sync: war guild unavailable for sync")
            return {}

        war_ned_role = war_guild.get_role(self._war_nederlander_role_id)

        counts = {"nederlander_added": 0, "nederlander_removed": 0,
                  "mu_roles_added": 0, "mu_roles_removed": 0,
                  "nicknames_set": 0}

        # Build set of all MU Discord role IDs we manage (to identify stale ones)
        all_managed_role_ids: set[int] = set()
        for role_map in self._mu_discord_role_ids.values():
            all_managed_role_ids.update(role_map.values())

        # Fetch the full set of Nederlander holders from the verifier bot (one call)
        nederlander_ids = await self._verifier_get_nederlanders()

        # Build Discord-user → in_game_id lookup from identity_links
        discord_to_ingame: dict[int, str] = {}
        if self._db:
            try:
                all_links = await self._db.get_all_identity_links()
                for link in all_links:
                    discord_to_ingame[int(link["discord_user_id"])] = link["in_game_user_id"]
            except Exception as exc:
                logger.warning("war_sync: failed to load identity links: %s", exc)

        # Batch-fetch citizen display names for all war guild members with identity links
        discord_to_nick: dict[int, str] = {}
        if self._db:
            try:
                all_member_ids = [str(m.id) for m in war_guild.members if not m.bot]
                raw_names = await self._db.get_citizen_names_by_discord_ids(all_member_ids)
                discord_to_nick = {int(k): v for k, v in raw_names.items()}
            except Exception as exc:
                logger.warning("war_sync: failed to fetch citizen names: %s", exc)

        logger.info(
            "war_sync: sync start — managed_roles=%d, links=%d, user_mu_map=%d, nick_map=%d",
            len(all_managed_role_ids), len(discord_to_ingame), len(self._user_mu_roles), len(discord_to_nick),
        )

        for member in war_guild.members:
            if member.bot:
                continue

            # ── Nederlander sync ──────────────────────────────────────────
            should_have_ned = member.id in nederlander_ids
            has_ned = war_ned_role in member.roles if war_ned_role else False

            try:
                if should_have_ned and not has_ned and war_ned_role:
                    await member.add_roles(war_ned_role, reason="war_sync: hourly sync")
                    counts["nederlander_added"] += 1
                elif not should_have_ned and has_ned and war_ned_role:
                    await member.remove_roles(war_ned_role, reason="war_sync: hourly sync")
                    counts["nederlander_removed"] += 1
            except discord.Forbidden:
                logger.warning("war_sync: no permission to update Nederlander for %s", member.name)

            # ── Auto-link Nederlanders without identity link ───────────────
            if should_have_ned and member.id not in discord_to_ingame:
                # Use the production guild display name from the verifier, because
                # war-guild nicks may not match the in-game username.
                _, prod_nick = await self._verifier_check_member(member.id)
                nickname = prod_nick or member.nick or member.name
                in_game_id = await self._lookup_in_game_id(nickname)
                if in_game_id and self._db:
                    try:
                        await self._db.upsert_identity_link(
                            discord_user_id=str(member.id),
                            guild_id=str(war_guild.id),
                            in_game_user_id=in_game_id,
                            nationality="nederlander",
                            request_type="war_verify",
                            approved_by_discord_id=str(self.bot.user.id) if self.bot.user else "0",
                            approved_at=datetime.now(timezone.utc).isoformat(),
                        )
                        discord_to_ingame[member.id] = in_game_id
                        if prod_nick:
                            discord_to_nick[member.id] = prod_nick
                        logger.info(
                            "war_sync: auto-linked %s (%d) → %s (nick=%r)",
                            member.name, member.id, in_game_id, nickname,
                        )
                    except Exception as exc:
                        logger.warning("war_sync: auto-link failed for %s: %s", member.name, exc)

            # ── Nickname sync ─────────────────────────────────────────────
            if should_have_ned:
                target_nick = discord_to_nick.get(member.id)
                if target_nick:
                    # Preserve any existing [DN] division prefix
                    if member.nick and (m := re.match(r"^\[D\d\] ", member.nick)):
                        target_nick = (m.group(0) + target_nick)[:32]
                    else:
                        target_nick = target_nick[:32]
                    current = member.nick or member.name
                    logger.debug(
                        "war_sync: nick check %s (%d): current=%r target=%r",
                        member.name, member.id, current, target_nick,
                    )
                    if current != target_nick:
                        try:
                            await member.edit(nick=target_nick, reason="war_sync: nickname sync")
                            counts["nicknames_set"] += 1
                            logger.info("war_sync: set nick for %s → %r", member.name, target_nick)
                        except discord.Forbidden:
                            logger.warning("war_sync: no permission to set nick for %s", member.name)

            # ── MU role sync ──────────────────────────────────────────────
            if not all_managed_role_ids:
                continue

            in_game_id = discord_to_ingame.get(member.id)
            desired_role_ids: set[int] = set()
            if in_game_id and self._user_mu_roles:
                for mu_id, role_type in self._user_mu_roles.get(in_game_id, {}).items():
                    # Owners have the same in-game rights as commanders and no
                    # longer get a dedicated Discord role — grant Commander
                    # (+ Member) instead.
                    discord_role_type = "commander" if role_type == "owner" else role_type
                    role_ids = self._mu_discord_role_ids.get(mu_id, {})
                    rid = role_ids.get(discord_role_type)
                    if rid:
                        desired_role_ids.add(rid)
                    if discord_role_type == "commander":
                        member_rid = role_ids.get("member")
                        if member_rid:
                            desired_role_ids.add(member_rid)

            current_managed = {r.id for r in member.roles if r.id in all_managed_role_ids}
            to_add = desired_role_ids - current_managed
            to_remove = current_managed - desired_role_ids

            for rid in to_add:
                role = war_guild.get_role(rid)
                if role:
                    try:
                        await member.add_roles(role, reason="war_sync: MU sync")
                        counts["mu_roles_added"] += 1
                    except discord.Forbidden:
                        pass

            for rid in to_remove:
                role = war_guild.get_role(rid)
                if role:
                    try:
                        await member.remove_roles(role, reason="war_sync: MU sync")
                        counts["mu_roles_removed"] += 1
                    except discord.Forbidden:
                        pass

        logger.info("war_sync: sync complete — %s", counts)
        return counts

    # ── Dutch MU scan ─────────────────────────────────────────────────────────

    async def scan_dutch_mus(self) -> int:
        """Paginate all MUs, find Dutch-owned ones, and create/update Discord roles.

        Returns the number of Dutch MUs processed.
        """
        if self._scan_lock.locked():
            logger.debug("war_sync: scan_dutch_mus already running, skipping concurrent call")
            async with self._scan_lock:  # wait for the running scan to finish
                pass
            return len(self._user_mu_roles)
        await self._scan_lock.acquire()
        try:
            return await self._scan_dutch_mus_body()
        finally:
            self._scan_lock.release()

    async def _scan_dutch_mus_body(self) -> int:
        """Inner implementation of scan_dutch_mus, called under the scan lock."""
        if not self._client:
            logger.warning("war_sync: API client not available for MU scan")
            return 0

        war_guild = self._war_guild
        if not war_guild:
            logger.warning("war_sync: war guild not available for MU scan")
            return 0

        # Step 1: paginate all MU IDs/names
        try:
            all_mus = await self._paginate_all_mus()
        except Exception as exc:
            logger.warning(
                "war_sync: _paginate_all_mus failed (API likely down) — "
                "aborting scan without touching any roles: %s", exc,
            )
            return 0
        logger.info("war_sync: found %d MUs in total", len(all_mus))
        if not all_mus:
            logger.warning(
                "war_sync: getManyPaginated returned zero MUs — treating as a "
                "failed fetch, not \"the game has no MUs\", and aborting the "
                "scan without touching any roles"
            )
            return 0

        # Step 2: batch-fetch detailed MU info
        mu_ids = [m["mu_id"] for m in all_mus]
        inputs = [{"muId": mid} for mid in mu_ids]
        try:
            details = await self._client.batch_get("/mu.getById", inputs, batch_size=50)
        except Exception as exc:
            logger.warning("war_sync: batch_get mu.getById failed: %s", exc)
            return 0

        # Step 3: identify Dutch-owned MUs
        dutch_mus: list[dict[str, Any]] = []
        fallback_count = 0
        for mu_info, detail in zip(all_mus, details):
            if not isinstance(detail, dict):
                continue
            mu_name = detail.get("name") or mu_info["mu_name"]
            owner_id = (
                (detail.get("roles") or {}).get("managers", [None])[0]
                or detail.get("user")
                or ""
            )
            if not owner_id:
                continue
            # Primary check: owner is in Dutch citizen_levels
            is_dutch = await self._is_dutch_citizen(owner_id)
            # Fallback 1: if citizen_levels is unreliable/empty, treat any MU whose
            # Discord roles already exist in the war guild as Dutch (they were
            # classified Dutch in a previous scan when the DB was populated).
            if not is_dutch and war_guild:
                if discord.utils.get(war_guild.roles, name=f"{mu_name} Member") is not None:
                    is_dutch = True
                    fallback_count += 1
            # Fallback 2: check if any member/commander listed in the API response
            # is a Dutch citizen in citizen_levels.  Catches MUs whose owner is
            # foreign but whose membership is predominantly Dutch.
            if not is_dutch and self._db and self._nl_country_id:
                all_candidate_ids: list[str] = (
                    ([owner_id] if owner_id else [])
                    + list((detail.get("roles") or {}).get("commanders", []))
                    + (detail.get("members") or [])
                )
                if all_candidate_ids:
                    try:
                        if await self._db.any_citizen_in_country(
                            all_candidate_ids, self._nl_country_id
                        ):
                            is_dutch = True
                            fallback_count += 1
                    except Exception as exc:
                        logger.warning(
                            "war_sync: any_citizen_in_country check failed for %s: %s",
                            mu_name, exc,
                        )
            if is_dutch:
                dutch_mus.append({
                    "mu_id": mu_info["mu_id"],
                    "mu_name": mu_name,
                    "owner_id": owner_id,
                    "commanders": list({
                        *((detail.get("roles") or {}).get("commanders", [])),
                    }),
                    "members": detail.get("members", []),
                })

        logger.info(
            "war_sync: %d Dutch-owned MUs found (%d via citizen_levels owner, %d via fallbacks)",
            len(dutch_mus), len(dutch_mus) - fallback_count, fallback_count,
        )

        # Step 4: for each Dutch MU, get owner colour and ensure Discord roles exist
        new_user_mu_roles: dict[str, dict[str, str]] = {}
        new_mu_discord_role_ids: dict[str, dict[str, int]] = {}

        # Keep track of Dutch MU IDs to handle cleanup later
        dutch_mu_ids = {m["mu_id"] for m in dutch_mus}
        await self._cleanup_removed_mus(war_guild, dutch_mu_ids)

        mu_colour = discord.Colour(0xe67e22)
        for mu in dutch_mus:
            role_ids = await self._ensure_mu_roles(war_guild, mu["mu_id"], mu["mu_name"], mu_colour)
            if role_ids:
                new_mu_discord_role_ids[mu["mu_id"]] = role_ids

            # Map every member to their role type for this MU
            owner_set = {mu["owner_id"]}
            commander_set = set(mu["commanders"]) - owner_set
            member_set = set(mu["members"]) - owner_set - commander_set

            for uid in owner_set:
                new_user_mu_roles.setdefault(uid, {})[mu["mu_id"]] = "owner"
            for uid in commander_set:
                new_user_mu_roles.setdefault(uid, {})[mu["mu_id"]] = "commander"
            for uid in member_set:
                new_user_mu_roles.setdefault(uid, {})[mu["mu_id"]] = "member"

        # Deduplicate: in eRepublik a player can only be in ONE MU.
        # If the API returns the same user as owner/manager of multiple MUs,
        # keep the highest-priority role.  For same-priority ties (e.g. "owner"
        # in both old and new MU because the old MU list is stale), prefer the
        # MU that citizen_levels.mu_id points to — that value comes from the
        # per-citizen profile or refresh_mu_memberships and is fresher than a
        # stale MU member list.  Fall back to alphabetical mu_id order only when
        # citizen_levels has no data for this user.
        _ROLE_PRIO = {"owner": 0, "commander": 1, "member": 2}
        deduped: dict[str, dict[str, str]] = {}
        for uid, mu_map in new_user_mu_roles.items():
            if len(mu_map) <= 1:
                deduped[uid] = mu_map
                continue
            # Try citizen_levels.mu_id as tiebreaker first
            cl_mu_id: str | None = None
            if self._db:
                try:
                    cl_row = await self._db.get_citizen_mu_from_levels(uid)
                    if cl_row:
                        cl_mu_id = cl_row[0]
                except Exception as exc:
                    logger.debug(
                        "war_sync: citizen_levels tiebreak lookup failed for %s: %s", uid, exc
                    )
            if cl_mu_id and cl_mu_id in mu_map:
                best_mu_id = cl_mu_id
                best_role = mu_map[cl_mu_id]
            else:
                best_mu_id, best_role = min(
                    mu_map.items(), key=lambda kv: (_ROLE_PRIO.get(kv[1], 99), kv[0])
                )
            deduped[uid] = {best_mu_id: best_role}
            dropped = {mid: r for mid, r in mu_map.items() if mid != best_mu_id}
            logger.warning(
                "war_sync: user %s appears in multiple Dutch MUs — keeping %s (%s), "
                "dropping %s — likely stale API data",
                uid, best_mu_id, best_role,
                ", ".join(f"{mid}={r}" for mid, r in dropped.items()),
            )
        new_user_mu_roles = deduped

        # Safety net: don't let a scan that found zero Dutch MUs (e.g. every
        # per-MU detail fetch failed, even though all_mus itself wasn't
        # empty) wipe out good cached/persisted data from the last
        # successful scan — same reasoning as the _cleanup_removed_mus guard.
        if not dutch_mus and (self._user_mu_roles or self._mu_discord_role_ids):
            logger.warning(
                "war_sync: scan found 0 Dutch MUs but the previous scan had %d — "
                "keeping the old cache/DB data instead of overwriting it",
                len(self._user_mu_roles),
            )
            return 0

        # Persist user→MU membership so other features (e.g. war status) can
        # look up a player's MU even when citizen_levels.mu_name is null.
        if self._db:
            mu_name_map = {m["mu_id"]: m["mu_name"] for m in dutch_mus}
            try:
                await self._db.replace_citizen_mu_memberships(new_user_mu_roles, mu_name_map)
            except Exception as exc:
                logger.warning("war_sync: failed to persist citizen_mu_membership: %s", exc)

        # Atomically update cached data
        self._user_mu_roles = new_user_mu_roles
        self._mu_discord_role_ids = new_mu_discord_role_ids

        try:
            n_owner_removed = await self._cleanup_owner_roles(war_guild)
            if n_owner_removed:
                logger.info("war_sync: owner-role cleanup removed %d role(s)", n_owner_removed)
        except Exception as exc:
            logger.warning("war_sync: owner-role cleanup failed: %s", exc)

        logger.info("war_sync: MU scan complete, %d Dutch MUs with Discord roles", len(dutch_mus))
        return len(dutch_mus)

    async def _cleanup_owner_roles(self, war_guild: discord.Guild) -> int:
        """Delete all per-MU 'Owner' Discord roles — no longer used.

        The war guild hit Discord's 250-role hard cap, blocking role creation
        for new/renamed MUs. Owner roles were dropped as a category (owners
        still get the Member role via the existing owner→also-member grant in
        _assign_mu_roles_from_db/sync_war_guild), freeing up headroom. Catches
        both DB-tracked owner roles and orphaned ones left over from past
        in-game MU renames (matched by name against the known_mus registry).
        """
        if not self._db:
            return 0
        removed = 0

        try:
            tracked = await self._db.get_all_war_mu_roles(str(war_guild.id))
        except Exception as exc:
            logger.warning("war_sync: owner-role cleanup: DB read failed: %s", exc)
            tracked = []
        owner_role_ids = {row["discord_role_id"] for row in tracked if row["role_type"] == "owner"}
        owner_mu_ids = {row["mu_id"] for row in tracked if row["role_type"] == "owner"}

        try:
            known_names = {name.lower() for _mid, name, _cid in await self._db.get_all_known_mu_ids()}
        except Exception:
            known_names = set()

        for role in list(war_guild.roles):
            if not role.name.endswith(" Owner"):
                continue
            base = role.name[: -len(" Owner")]
            if role.id not in owner_role_ids and base.lower() not in known_names:
                continue  # not a recognised MU-owner role — leave unrelated roles alone
            try:
                await role.delete(reason="war_sync: MU Owner-rollen niet langer gebruikt (250-rol limiet)")
                removed += 1
                logger.info("war_sync: deleted owner role '%s' (id=%d)", role.name, role.id)
            except discord.Forbidden:
                logger.warning("war_sync: no permission to delete owner role '%s'", role.name)
            except discord.NotFound:
                pass

        for mu_id in owner_mu_ids:
            try:
                await self._db.delete_war_mu_role(mu_id, "owner", str(war_guild.id))
            except Exception as exc:
                logger.warning("war_sync: failed to clear owner DB row for %s: %s", mu_id, exc)

        return removed

    async def _paginate_all_mus(self) -> list[dict[str, str]]:
        """Return [{mu_id, mu_name}] for all MUs in the game.

        Raises on an API failure instead of swallowing it into a partial/empty
        result — the caller (_scan_dutch_mus_body) treats an empty MU list as
        "no Dutch MUs exist right now" and cleans up every tracked role
        accordingly. Confirmed as the cause of a real incident: a WarEra API
        outage made the very first page fail here, which used to be caught
        and turned into an empty list, which then made the scan delete every
        MU role in the war guild. Letting the exception propagate makes the
        scan abort before touching any roles (see _scan_dutch_mus_body).
        """
        mus: list[dict[str, str]] = []
        cursor: str | None = None

        while True:
            if not self._client:
                break
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await self._client.get(
                "/mu.getManyPaginated",
                params={"input": json.dumps(params)},
            )

            data_obj: Any = resp
            if isinstance(resp, dict):
                for key in ("result", "data"):
                    v = resp.get(key)
                    if isinstance(v, dict):
                        data_obj = v.get("data", v)
                        break

            items: list[Any] = []
            next_cursor: str | None = None
            if isinstance(data_obj, list):
                items = data_obj
            elif isinstance(data_obj, dict):
                for key in ("items", "mus", "data"):
                    v = data_obj.get(key)
                    if isinstance(v, list):
                        items = v
                        break
                next_cursor = data_obj.get("nextCursor") or data_obj.get("cursor")

            for item in items:
                if not isinstance(item, dict):
                    continue
                mu_id = str(item.get("_id") or item.get("id") or "").strip()
                mu_name = str(item.get("name") or item.get("title") or "").strip()
                if mu_id and mu_name:
                    mus.append({"mu_id": mu_id, "mu_name": mu_name})

            if not next_cursor or not items:
                break
            cursor = next_cursor

        return mus

    async def _is_dutch_citizen(self, in_game_user_id: str) -> bool:
        """Return True if the in-game user is in the citizen_levels table (= Dutch)."""
        if not self._db or not self._nl_country_id:
            return False
        try:
            return await self._db.citizen_exists_in_country(in_game_user_id, self._nl_country_id)
        except Exception as exc:
            logger.warning("war_sync: _is_dutch_citizen query failed: %s", exc)
            return False

    async def _get_owner_colour(self, owner_id: str) -> discord.Colour:
        """Fetch the owner's colorScheme and return the matching Discord Colour."""
        if not self._client:
            return _colour(None)
        try:
            resp = await self._client.get(
                "/user.getUserById",
                params={"input": json.dumps({"userId": owner_id})},
            )
            data: Any = resp
            if isinstance(resp, dict):
                for key in ("result", "data"):
                    v = resp.get(key)
                    if isinstance(v, dict):
                        data = v.get("data", v)
                        break
            scheme = None
            if isinstance(data, dict):
                scheme = (data.get("infos") or {}).get("colorScheme")
            return _colour(scheme)
        except Exception as exc:
            logger.debug("war_sync: _get_owner_colour failed for %s: %s", owner_id, exc)
            return _colour(None)

    async def _ensure_mu_roles(
        self,
        war_guild: discord.Guild,
        mu_id: str,
        mu_name: str,
        colour: discord.Colour,
    ) -> dict[str, int]:
        """Create or fetch the three Discord roles for a Dutch MU.

        Returns {owner: role_id, commander: role_id, member: role_id}.
        Persists IDs in the war_mu_roles table so they survive restarts.
        """
        # No dedicated "Owner" role anymore — the guild hit Discord's 250-role
        # hard cap, and owners already get the Member role via the
        # owner→also-member grant in _assign_mu_roles_from_db/sync_war_guild.
        # See _cleanup_owner_roles for removal of previously-created ones.
        role_defs = [
            ("commander",  f"{mu_name} Commander"),
            ("member",     f"{mu_name} Member"),
        ]
        result: dict[str, int] = {}

        for role_type, role_name in role_defs:
            # Check DB cache first
            stored_id = await self._get_war_mu_role(mu_id, role_type, str(war_guild.id))
            if stored_id:
                # Verify the role still exists in Discord
                existing = war_guild.get_role(stored_id)
                if existing:
                    # Update colour and mentionable if they changed
                    needs_edit = existing.colour != colour or not existing.mentionable
                    if needs_edit:
                        try:
                            await existing.edit(colour=colour, mentionable=True, reason="war_sync: kleur/mentionable update")
                            logger.info("war_sync: updated colour/mentionable for '%s'", existing.name)
                        except discord.Forbidden:
                            logger.warning("war_sync: no permission to update colour for '%s'", existing.name)
                    result[role_type] = stored_id
                    continue
                # Role was deleted externally; fall through to re-create

            # Try to find by name — also cleans up any duplicates created by
            # concurrent scans (keep the stored ID if known, else keep the first).
            all_by_name = [r for r in war_guild.roles if r.name == role_name]
            if all_by_name:
                canonical = next(
                    (r for r in all_by_name if stored_id and r.id == stored_id),
                    all_by_name[0],
                )
                for dup in all_by_name:
                    if dup.id != canonical.id:
                        try:
                            await dup.delete(reason="war_sync: duplicate role cleanup")
                            logger.info("war_sync: deleted duplicate role '%s' (id=%d)", dup.name, dup.id)
                        except discord.Forbidden:
                            logger.warning("war_sync: no permission to delete duplicate '%s'", dup.name)
                existing = canonical
                needs_edit = existing.colour != colour or not existing.mentionable
                if needs_edit:
                    try:
                        await existing.edit(colour=colour, mentionable=True, reason="war_sync: kleur/mentionable update")
                        logger.info("war_sync: updated colour/mentionable for '%s'", existing.name)
                    except discord.Forbidden:
                        logger.warning("war_sync: no permission to update colour for '%s'", existing.name)
                result[role_type] = existing.id
                await self._upsert_war_mu_role(mu_id, role_type, str(war_guild.id), existing.id, mu_name)
                continue

            # Fallback: this MU may have been renamed in-game since its roles
            # were created (its Discord role names still reflect the OLD
            # name, so the by-name search above found nothing). Look up any
            # previously-tracked role for this mu_id regardless of guild_id
            # and rename it in place instead of creating a brand new role —
            # the war guild has a hard 250-role cap, and leaving old-named
            # roles behind on every rename burns through it permanently.
            stale_id = await self._db.get_war_mu_role_any_guild(mu_id, role_type) if self._db else None
            if stale_id and stale_id != stored_id and stale_id != war_guild.id:
                stale_role = war_guild.get_role(stale_id)
                # Never touch @everyone — its role ID always equals the guild
                # ID, and corrupted legacy war_mu_roles rows can contain the
                # guild ID in the discord_role_id column, which would
                # otherwise make this fallback edit @everyone's name/colour.
                if stale_role and stale_role != war_guild.default_role:
                    try:
                        await stale_role.edit(
                            name=role_name,
                            colour=colour,
                            mentionable=True,
                            reason=f"war_sync: MU hernoemd → '{mu_name}'",
                        )
                        result[role_type] = stale_role.id
                        await self._upsert_war_mu_role(mu_id, role_type, str(war_guild.id), stale_role.id, mu_name)
                        logger.info(
                            "war_sync: renamed stale role → '%s' (id=%d, MU hernoemd)",
                            role_name, stale_role.id,
                        )
                        continue
                    except discord.Forbidden:
                        logger.warning("war_sync: no permission to rename stale role for '%s'", mu_name)

            # Create the role
            try:
                new_role = await war_guild.create_role(
                    name=role_name,
                    colour=colour,
                    mentionable=True,
                    reason=f"war_sync: Dutch MU '{mu_name}'",
                )
                result[role_type] = new_role.id
                await self._upsert_war_mu_role(mu_id, role_type, str(war_guild.id), new_role.id, mu_name)
                logger.info("war_sync: created role '%s'", role_name)
            except discord.Forbidden:
                logger.warning("war_sync: no permission to create role '%s'", role_name)
            except Exception as exc:
                logger.warning("war_sync: failed to create role '%s': %s", role_name, exc)

        return result

    async def _cleanup_removed_mus(
        self, war_guild: discord.Guild, active_mu_ids: set[str]
    ) -> None:
        """Delete Discord roles for MUs that are no longer Dutch-owned."""
        if not self._db:
            return
        try:
            all_rows = await self._db.get_all_war_mu_roles(str(war_guild.id))
        except Exception as exc:
            logger.warning("war_sync: _cleanup_removed_mus query failed: %s", exc)
            return

        # Safety net: active_mu_ids empty while roles are already tracked
        # almost never means every single Dutch MU genuinely disappeared in
        # one scan — it means the scan failed to find any (API outage, a
        # partial per-MU detail-fetch failure, etc). _scan_dutch_mus_body
        # already aborts before this point when the MU list itself is
        # empty; this catches the case where MUs were found but every
        # "is it Dutch" detail lookup failed, so treating that as "clean up
        # everything" doesn't repeat the mass role-deletion incident this
        # guarded against.
        if not active_mu_ids and all_rows:
            logger.warning(
                "war_sync: 0 Dutch MUs identified this scan but %d MU role(s) "
                "are tracked — skipping cleanup instead of deleting them all",
                len(all_rows),
            )
            return

        removed_mu_ids: set[str] = set()
        for row in all_rows:
            mu_id_raw = row["mu_id"]
            if mu_id_raw in active_mu_ids:
                continue
            role = war_guild.get_role(row["discord_role_id"])
            if role:
                try:
                    await role.delete(reason="war_sync: MU niet langer Nederlands eigendom")
                    logger.info("war_sync: deleted stale role '%s'", role.name)
                except discord.Forbidden:
                    pass
            removed_mu_ids.add(mu_id_raw)

        for mu_id_raw in removed_mu_ids:
            try:
                await self._db.delete_war_mu_roles(mu_id_raw, str(war_guild.id))
            except Exception as exc:
                logger.warning("war_sync: failed to remove DB entry for mu %s: %s", mu_id_raw, exc)

    # ── Verifier HTTP helpers ─────────────────────────────────────────────────

    async def _verifier_check(self, discord_id: int) -> Optional[bool]:
        """Return True/False if the verifier says the user has Nederlander, None on error."""
        if not self._http:
            return None
        try:
            async with self._http.get(
                f"{self._verifier_url}/check/{discord_id}", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 503:
                    logger.warning("war_sync: verifier not ready")
                    return None
                if resp.status != 200:
                    logger.warning("war_sync: verifier returned HTTP %d", resp.status)
                    return None
                data = await resp.json()
                return bool(data.get("has_role", False))
        except Exception as exc:
            logger.warning("war_sync: _verifier_check failed: %s", exc)
            return None

    async def _verifier_check_member(
        self, discord_id: int
    ) -> tuple[Optional[bool], Optional[str]]:
        """Return (has_role, nickname) from the verifier.  Both None on error.

        Uses the /member/<id> endpoint which combines the role check and the
        member's server-display-name (= their warera username) in one call.
        """
        if not self._http:
            return None, None
        try:
            async with self._http.get(
                f"{self._verifier_url}/member/{discord_id}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 503:
                    logger.warning("war_sync: verifier not ready")
                    return None, None
                if resp.status != 200:
                    logger.warning("war_sync: verifier /member returned HTTP %d", resp.status)
                    return None, None
                data = await resp.json()
                return bool(data.get("has_role", False)), data.get("nickname")
        except Exception as exc:
            logger.warning("war_sync: _verifier_check_member failed: %s", exc)
            return None, None

    async def _lookup_in_game_id(self, username: str) -> Optional[str]:
        """Resolve a warera username to an in-game user ID.

        Tries an exact case-insensitive match in the local citizen DB first,
        then falls back to the game's search API.
        """
        # 1. Exact match in local citizen_levels
        if self._db:
            try:
                matches = await self._db.search_citizen_names(username, limit=10)
                exact = next(
                    (uid for name, uid in matches if name.lower() == username.lower()),
                    None,
                )
                if exact:
                    return exact
            except Exception as exc:
                logger.debug("war_sync: DB citizen lookup failed for %r: %s", username, exc)

        # 2. Game search API fallback
        if self._client:
            try:
                raw = await self._client.get(
                    "/search.searchAnything",
                    params={"input": json.dumps({"searchText": username})},
                )
                data: Any = raw
                if isinstance(raw, dict):
                    for key in ("result", "data"):
                        v = raw.get(key)
                        if isinstance(v, dict):
                            data = v.get("data", v)
                            break
                user_ids: list = data.get("userIds", []) if isinstance(data, dict) else []
                if user_ids:
                    return str(user_ids[0])
            except Exception as exc:
                logger.debug("war_sync: API citizen lookup failed for %r: %s", username, exc)

        return None

    async def _verifier_get_nederlanders(self) -> set[int]:
        """Return the set of Discord user IDs with Nederlander on the production server."""
        if not self._http:
            return set()
        try:
            async with self._http.get(
                f"{self._verifier_url}/nederlanders", timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.warning("war_sync: verifier /nederlanders returned HTTP %d", resp.status)
                    return set()
                data = await resp.json()
                return {int(i) for i in data.get("ids", [])}
        except Exception as exc:
            logger.warning("war_sync: _verifier_get_nederlanders failed: %s", exc)
            return set()

    # ── DB helpers for war_mu_roles ───────────────────────────────────────────

    async def _get_war_mu_role(
        self, mu_id: str, role_type: str, guild_id: str
    ) -> Optional[int]:
        if not self._db:
            return None
        try:
            return await self._db.get_war_mu_role(mu_id, role_type, guild_id)
        except Exception:
            return None

    async def _upsert_war_mu_role(
        self,
        mu_id: str,
        role_type: str,
        guild_id: str,
        discord_role_id: int,
        mu_name: str,
    ) -> None:
        if not self._db:
            return
        try:
            await self._db.upsert_war_mu_role(mu_id, role_type, guild_id, discord_role_id, mu_name)
        except Exception as exc:
            logger.warning("war_sync: _upsert_war_mu_role failed: %s", exc)

    # ── Scheduled tasks ───────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def hourly_sync_task(self) -> None:
        try:
            await self.sync_war_guild()
        except Exception:
            logger.exception("war_sync: hourly_sync_task failed")

    @hourly_sync_task.before_loop
    async def before_hourly_sync(self) -> None:
        await self._wait_for_services()
        # Ensure the MU scan has run at least once before the first sync so
        # _user_mu_roles and _mu_discord_role_ids are populated.
        if not self._user_mu_roles:
            try:
                await self.scan_dutch_mus()
            except Exception:
                logger.exception("war_sync: startup MU scan failed")

    @tasks.loop(hours=8)
    async def daily_mu_scan_task(self) -> None:
        try:
            await self.scan_dutch_mus()
        except Exception:
            logger.exception("war_sync: daily_mu_scan_task failed")

    @daily_mu_scan_task.before_loop
    async def before_daily_mu_scan(self) -> None:
        await self._wait_for_services()

    # ── Admin command ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="syncwar",
        description="Synchroniseer rollen op de war-server (MU-scan + leden-sync).",
    )
    async def syncwar(self, interaction: discord.Interaction) -> None:
        # Only allow war admins
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Alleen uitvoerbaar op de server.", ephemeral=True)
            return

        is_admin = any(r.id in self._admin_role_ids for r in interaction.user.roles)
        is_owner = await self.bot.is_owner(interaction.user)
        if not is_admin and not is_owner:
            await interaction.response.send_message("❌ Geen toegang.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            n_mus = await self.scan_dutch_mus()
            counts = await self.sync_war_guild()
        except Exception as exc:
            await interaction.followup.send(f"❌ Synchronisatie mislukt: {exc}", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ Synchronisatie voltooid.\n"
            f"• **{n_mus}** Nederlandse MUs verwerkt\n"
            f"• Nederlander toegevoegd: {counts.get('nederlander_added', 0)}, "
            f"verwijderd: {counts.get('nederlander_removed', 0)}\n"
            f"• MU-rollen toegevoegd: {counts.get('mu_roles_added', 0)}, "
            f"verwijderd: {counts.get('mu_roles_removed', 0)}",
            ephemeral=True,
        )


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot) -> None:
    """Only register this cog when the war_guild config block is present.

    This guard ensures the production bot ignores this file even if it is
    committed to the shared repository.
    """
    if not bot.config.get("war_guild"):
        logger.debug("war_sync: no war_guild config — cog not loaded")
        return
    await bot.add_cog(WarSyncCog(bot))
    logger.info("war_sync: cog loaded")
