"""War-guild army-type roles, government roles, and MU category maintenance.

Manages:
  1. Paramilitary / State Army Discord roles (+ Commander variants) and their
     channels, based on each Dutch MU's live ``type`` field from the game API
     (added in v0.28.3, see the muSponsorship.* endpoints) — replaces the old
     hand-curated 5-division system (see "Legacy division cleanup" below).
  2. Government roles (President, Vice-President, ministers, advisors)
     auto-created and synced from the live government API.
  3. MU category channel permission repair (Commander/Member access).
  4. Hourly sync task to keep all of the above up-to-date.

Legacy division cleanup: the war guild used to mirror the game's old 5
hand-curated "divisions" with @Divisie 1-5 / @DN Leiderschap roles, [DN]
nickname prefixes, and [DN]-prefixed MU categories. That system has been
retired — this cog now actively removes those artifacts on every sync
(``_cleanup_legacy_division_artifacts``) instead of maintaining them.
``DIVISION_MU_IDS``/``DIVISION_MUS`` below are kept only because several
other cogs (mudmg.py, paraatheid.py, mu.py, mu_subscriptions.py,
sync_tasks.py, damage.py, citizens.py) still import them for display
purposes (autocomplete, table grouping) — this file no longer reads them
for anything itself.

Only active when ``config["war_guild"]`` is present.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# ── Division configuration (display-only, see module docstring) ─────────────
#
# MU IDs, not names, are the source of truth: a MU's Discord roles,
# categories, and nickname prefix used to all be keyed off DIVISION_MU_IDS
# below, back when the division system was live. Confirmed as a real
# incident: "De Munterij" was renamed in-game to "De Pomperij", and every
# permission that had been matched against the old hardcoded name silently
# broke. IDs never change on a rename, so that can't happen again. The
# trailing comment after each ID is only for a human reading this file
# (never parsed) — it can go stale after a future rename without breaking
# anything.
DIVISION_MU_IDS: dict[int, list[str]] = {
    1: [
        "695c10139cddbde0503e0d36",  # De Heeren XVII
        "697de1e370af1f7f8aa169ea",  # De Nederlandse Bank
        "69906c5a8b9151ad2ba50435",  # Oranje Huurlingen
        "69a420a143600f560ccb9570",  # Pannekoekenoffensief
        "69af031d6157435c4f5c4ed0",  # AIVD
        "6a1001fc6d0ebebbfd1b197b",  # Henk
    ],
    2: [
        "695c338ec617ea3c92421bc3",  # Korps Mariniers
        "698f253efffb3268e9276ac1",  # Van Speijk Eskader
        "69a41cd5cedfd3ae877c2def",  # Het Noordzee-verbond
        "69a588024833b3070edb7324",  # De Krooncompagnie
        "6970dd1ebea7a2698be2e84e",  # The Flying Dutchman
        "69a896d1214e4eeda01ba4c5",  # Korps Comeback (was: Kikkers Defence)
    ],
    3: [
        "69843f617cf831a523014e15",  # Reg. Genietroepen
        "69eae9ed09ef4e68d6ec6e8f",  # Gild der Gulden
        "69c08ac1638466f7dba620e0",  # De Reddingsbrigade
        "69e4c185652519adc94fa247",  # Lokale helden
        "69a42247cedfd3ae87863a6c",  # Baronie Wachteenheid
        "699bf0f5b232a1efde5b9aed",  # MIVD
    ],
    4: [
        "6980b01819decffdc7848ef9",  # Regiment Huzaren
        "69bc0bf57e5e55af7b3d9bda",  # De Nachtwacht
        "69848b1d7922e416bbcca4e9",  # Regiment van Oranje
        "699f7d14c629e138fa86ec9c",  # De Pomperij (was: De Munterij)
        "69a41d7b75b50126c381cca5",  # De Mobiele Eenheid
        "69fdc07335bf031c8ccf35b1",  # Lowland Lions
        "6a16e80dd817c223c0c2c63f",  # De Zwarte Duivels
        "6a6de39aeb87621547a9ebaa",  # LowLand Goats
    ],
    5: [
        "69b8ffbf0e5eb9b6e5241c9a",  # TSCo
        "6a00a9503bc42670f1a3afb1",  # Kwaakende Kikkers (in-game spelling: "Kwakende Kikkers")
        "69c577f0df7ae3167ff544ce",  # Dutch Bounty Hunters
        "69a5d8546719fc1a7c787cb8",  # OTCo
        "69fc860f04c619b74d144b08",  # OTCo II
        "69fb7a1c0963d7a11bd5afa6",  # De Belastingdienst
    ],
}

# Display-only mirror of DIVISION_MU_IDS, kept as names for the handful of
# other cogs (mudmg.py, paraatheid.py, mu.py, mu_subscriptions.py,
# sync_tasks.py, damage.py, citizens.py) that only need a MU's name for
# autocomplete suggestions or table display.
DIVISION_MUS: dict[int, list[str]] = {
    1: [
        "De Heeren XVII",
        "De Nederlandse Bank",
        "Oranje Huurlingen",
        "Pannekoekenoffensief",
        "AIVD",
        "Henk",
    ],
    2: [
        "Korps Mariniers",
        "Van Speijk Eskader",
        "Het Noordzee-verbond",
        "De Krooncompagnie",
        "The Flying Dutchman",
        "Korps Comeback",
    ],
    3: [
        "Reg. Genietroepen",
        "Gild der Gulden",
        "De Reddingsbrigade",
        "Lokale helden",
        "Baronie Wachteenheid",
        "MIVD",
    ],
    4: [
        "Regiment Huzaren",
        "De Nachtwacht",
        "Regiment van Oranje",
        "De Pomperij",
        "De Mobiele Eenheid",
        "Lowland Lions",
        "De Zwarte Duivels",
        "LowLand Goats",
    ],
    5: [
        "TSCo",
        "Kwaakende Kikkers",
        "Dutch Bounty Hunters",
        "OTCo",
        "OTCo II",
        "De Belastingdienst",
    ],
}

# ── Army-type configuration ──────────────────────────────────────────────────
#
# WarEra MU "type" field values (added in game v0.28.3 — see
# docs/api-changes-2026-11-17.md). A MU with no `type` field at all is the
# default case, which the game itself treats as Paramilitary: confirmed live
# from the frontend bundle, the MU_SPONSORSHIP_ENDED notification text reads
# "{muName} is Paramilitary again" once a State Army sponsorship ends.
_MU_TYPE_PARAMILITARY = "paramilitary"
_MU_TYPE_STATE_ARMY = "stateArmy"
_MU_TYPE_PRIVATE_MERCENARY = "privateMercenary"  # not relevant here — skipped

_TYPE_TO_ROLE_KEY: dict[str, str] = {
    _MU_TYPE_PARAMILITARY: "paramilitary",
    _MU_TYPE_STATE_ARMY: "state_army",
}
_COMMANDER_KEY_FOR_TYPE: dict[str, str] = {
    "paramilitary": "paramilitary_commander",
    "state_army": "state_army_commander",
}

# role key -> (Discord role name, colour)
_ARMY_ROLES: dict[str, tuple[str, discord.Colour]] = {
    "paramilitary":           ("Paramilitary",           discord.Colour(0x95A5A6)),
    "paramilitary_commander": ("Paramilitary Commander", discord.Colour(0x7F8C8D)),
    "state_army":             ("State Army",             discord.Colour(0x2ECC71)),
    "state_army_commander":   ("State Army Commander",   discord.Colour(0x27AE60)),
}

_ARMY_CATEGORY_NAME = "Army"
# role key -> Discord channel name (one channel per role, in _ARMY_CATEGORY_NAME)
_ARMY_CHANNEL_NAMES: dict[str, str] = {
    "paramilitary":           "paramilitary",
    "paramilitary_commander": "paramilitary-commander",
    "state_army":             "state-army",
    "state_army_commander":   "state-army-commander",
}

# poll_state keys used to persist the Army category/channel IDs — looked up
# by ID rather than name so a manual rename doesn't cause a duplicate to be
# created on the next sync (see _ensure_army_channels).
_ARMY_CATEGORY_POLL_KEY = "war_army_category_id"
_ARMY_CHANNEL_POLL_KEY_PREFIX = "war_army_channel_"

# Legacy Discord role names to delete now that the division system is retired
_LEGACY_DIVISION_ROLE_NAMES: set[str] = {f"Divisie {n}" for n in range(1, 6)} | {
    f"D{n} Leiderschap" for n in range(1, 6)
}

# MU category permissions — "commander is admin of their own category".
# Discord has no per-channel Administrator flag, so this is the broadest set
# of channel-scoped permissions Discord actually offers, excluding anything
# that's guild-wide (kick/ban, manage_guild, server-level manage_roles, real
# Administrator) since those can't be scoped to one category anyway.
_COMMANDER_CATEGORY_PERMS: dict[str, bool] = {
    "view_channel": True,
    "manage_channels": True,
    "manage_permissions": True,
    "manage_webhooks": True,
    "create_instant_invite": True,
    "manage_messages": True,
    "manage_threads": True,
    "mute_members": True,
    "deafen_members": True,
    "move_members": True,
    "mention_everyone": True,
}
_MEMBER_CATEGORY_PERMS: dict[str, bool] = {"view_channel": True}

# War-guild government role names
GOV_ROLE_NAMES: dict[str, str] = {
    "president":                   "President",
    "vice_president":              "Vice-President",
    "minister_of_defense":         "Min. Defensie",
    "minister_of_economy":         "Min. Economie",
    "minister_of_foreign_affairs": "Min. Buitenlandse Zaken",
    "minister_of_interior":        "Min. Binnenlandse Zaken",
    "minister_of_communication":   "Min. Communicatie",
    "chief_of_staff":              "Stafchef",
}

# API minister-slot "title" -> GOV_ROLE_NAMES key. "advisor" is deliberately
# absent here: unlike the other six, it isn't a single fixed slot (a country
# can have zero or several advisors at once), so it's handled separately via
# dynamically-named per-advisor roles (see _ensure_advisor_roles) rather than
# one shared role.
_MINISTER_TITLE_TO_GOV_KEY: dict[str, str] = {
    "minOfDefense": "minister_of_defense",
    "minOfEconomy": "minister_of_economy",
    "minOfForeignAffairs": "minister_of_foreign_affairs",
    "minOfInterior": "minister_of_interior",
    "minOfCommunication": "minister_of_communication",
    "chiefOfStaff": "chief_of_staff",
}

_DEFAULT_ADVISOR_ROLE_NAME = "Adviseur"
_ADVISOR_ROLE_MAP_POLL_KEY = "war_advisor_role_map"

# Regex to match and strip a legacy [DN] division nickname/category prefix
_NICK_PREFIX_RE = re.compile(r"^\[D\d\] ")


def _norm(name: str) -> str:
    """Normalise a MU name for fuzzy comparison (strips spaces, dots, dashes).

    Only used for name-based matching that has no better option (a Discord
    category's display name) — never for deciding game state.
    """
    return re.sub(r"[\s.\-_]+", "", name).lower()


def _unwrap_trpc(resp: dict) -> object:
    """Unwrap ``{"result": {"data": ...}}`` tRPC response envelope."""
    try:
        return resp["result"]["data"]
    except (KeyError, TypeError):
        return resp


class WarGuildDivisionsCog(TaskCogBase):
    """Manages army-type roles, government roles, and MU categories in the war guild."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._war_cfg: dict = {}
        # army-type role key (see _ARMY_ROLES) → Discord role ID
        self._army_role_ids: dict[str, int] = {}
        # gov key (e.g. "president") → Discord role ID
        self._gov_role_ids: dict[str, int] = {}

    async def cog_load(self) -> None:
        if not self.bot.config.get("war_guild"):
            return
        self._war_cfg = self.bot.config["war_guild"]
        self.daily_sync.start()

    async def cog_unload(self) -> None:
        self.daily_sync.cancel()

    @property
    def _war_guild(self) -> discord.Guild | None:
        gid = self._war_cfg.get("guild_id")
        return self.bot.get_guild(int(gid)) if gid else None

    @property
    def _admin_role_ids(self) -> set[int]:
        """War-guild admin role IDs allowed to run /syncarmy.

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

    def _is_war_admin(self, member: discord.Member) -> bool:
        return any(r.id in self._admin_role_ids for r in member.roles)

    # ── Role management ───────────────────────────────────────────────────────

    async def _ensure_army_roles(self, guild: discord.Guild) -> None:
        """Create/find the 4 army-type Discord roles if needed."""
        existing = {r.name: r for r in guild.roles}
        for key, (name, colour) in _ARMY_ROLES.items():
            role = existing.get(name)
            if role is None:
                role = await guild.create_role(
                    name=name,
                    colour=colour,
                    mentionable=True,
                    reason="war_guild_divisions: auto-create army-type role",
                )
                logger.info(
                    "war_guild_divisions: created army-type role %r (id=%d)", name, role.id
                )
            if role.colour != colour:
                await role.edit(colour=colour, reason="war_guild_divisions: colour sync")
            self._army_role_ids[key] = role.id

    async def _ensure_gov_roles(self, guild: discord.Guild) -> None:
        """Create government roles in the war guild if they do not yet exist."""
        existing = {r.name: r for r in guild.roles}
        for key, name in GOV_ROLE_NAMES.items():
            role = existing.get(name)
            if role is None:
                role = await guild.create_role(
                    name=name,
                    mentionable=True,
                    reason="war_guild_divisions: auto-create government role",
                )
                logger.info(
                    "war_guild_divisions: created gov role %r (id=%d)", name, role.id
                )
            self._gov_role_ids[key] = role.id

    # ── Legacy division cleanup ──────────────────────────────────────────────

    async def _cleanup_legacy_division_artifacts(self, guild: discord.Guild) -> dict:
        """Remove the retired division system's remnants: deletes the old
        @Divisie N / @DN Leiderschap roles, and strips any leftover [DN]
        prefix from member nicknames and category names.

        Safe to run every cycle — a no-op once everything has already been
        cleaned up once.
        """
        counts = {"roles_deleted": 0, "nicks_stripped": 0, "cats_stripped": 0}

        for role in list(guild.roles):
            if role.name in _LEGACY_DIVISION_ROLE_NAMES:
                try:
                    await role.delete(reason="war_guild_divisions: division system retired")
                    counts["roles_deleted"] += 1
                    logger.info("war_guild_divisions: deleted legacy role %r", role.name)
                except discord.Forbidden:
                    logger.warning(
                        "war_guild_divisions: no permission to delete legacy role %r",
                        role.name,
                    )

        for member in guild.members:
            if member.bot or not member.nick:
                continue
            if _NICK_PREFIX_RE.match(member.nick):
                stripped = _NICK_PREFIX_RE.sub("", member.nick)[:32] or None
                try:
                    await member.edit(
                        nick=stripped,
                        reason="war_guild_divisions: division system retired",
                    )
                    counts["nicks_stripped"] += 1
                except discord.Forbidden:
                    logger.warning(
                        "war_guild_divisions: cannot strip nick prefix for %s "
                        "(role hierarchy)",
                        member.name,
                    )

        for cat in guild.categories:
            if _NICK_PREFIX_RE.match(cat.name):
                stripped = _NICK_PREFIX_RE.sub("", cat.name)
                try:
                    await cat.edit(
                        name=stripped,
                        reason="war_guild_divisions: division system retired",
                    )
                    counts["cats_stripped"] += 1
                except discord.Forbidden:
                    logger.warning(
                        "war_guild_divisions: no permission to rename category %r",
                        cat.name,
                    )

        if any(counts.values()):
            logger.info("war_guild_divisions: legacy cleanup — %s", counts)
        return counts

    # ── Army-type sync ────────────────────────────────────────────────────────

    async def _build_mu_type_map(self, mu_ids: set[str]) -> dict[str, str]:
        """Fetch each MU's current ``type`` via the API.

        Returns mu_id -> role key ("paramilitary"/"state_army") for MUs that
        belong to NL and aren't a private mercenary unit. A MU with no
        `type` field defaults to paramilitary (see module docstring).
        """
        result: dict[str, str] = {}
        if not mu_ids or not self._client:
            return result

        mu_id_list = sorted(mu_ids)
        nl_country_id = self.bot.config.get("nl_country_id", "")
        try:
            details = await self._client.batch_get(
                "/mu.getById", [{"muId": mid} for mid in mu_id_list], batch_size=50
            )
        except Exception as exc:
            logger.warning("war_guild_divisions: batch mu.getById failed: %s", exc)
            return result

        for mid, data in zip(mu_id_list, details):
            if not isinstance(data, dict):
                continue
            if nl_country_id and data.get("country") != nl_country_id:
                continue
            mu_type = data.get("type") or _MU_TYPE_PARAMILITARY
            if mu_type == _MU_TYPE_PRIVATE_MERCENARY:
                continue
            role_key = _TYPE_TO_ROLE_KEY.get(mu_type)
            if role_key:
                result[mid] = role_key
        return result

    async def _sync_army_types(self, guild: discord.Guild) -> dict:
        """Sync Paramilitary/State Army (+ Commander) roles for all guild
        members, based on each member's current MU's live ``type`` field."""
        counts = {
            "roles_added": 0, "roles_removed": 0,
            "no_link": 0, "no_mu": 0,
        }
        if not self._army_role_ids or not self._db:
            return counts

        try:
            all_memberships = await self._db.get_all_mu_memberships()
        except Exception as exc:
            logger.warning("war_guild_divisions: failed to load MU memberships: %s", exc)
            return counts

        if not all_memberships:
            logger.warning(
                "war_guild_divisions: citizen_mu_membership table is empty — "
                "skipping army-type sync to avoid removing all roles. "
                "Run /syncwar first to populate it."
            )
            return counts

        mu_type_map = await self._build_mu_type_map({m[1] for m in all_memberships})

        discord_to_ingame: dict[int, str] = {}
        try:
            for link in await self._db.get_all_identity_links():
                discord_to_ingame[int(link["discord_user_id"])] = link["in_game_user_id"]
        except Exception as exc:
            logger.warning(
                "war_guild_divisions: failed to load identity links: %s", exc
            )

        # uid -> role_key for the first mapped MU found; uid -> set of role
        # keys for which this uid is an owner/commander of that type's MU.
        ingame_to_role_key: dict[str, str] = {}
        ingame_to_commander_keys: dict[str, set[str]] = {}
        for uid, mu_id, _mu_name, role_type in all_memberships:
            role_key = mu_type_map.get(mu_id)
            if role_key is None:
                continue
            ingame_to_role_key.setdefault(uid, role_key)
            if role_type in ("owner", "commander"):
                ingame_to_commander_keys.setdefault(uid, set()).add(role_key)

        all_army_role_ids = set(self._army_role_ids.values())

        for member in guild.members:
            if member.bot:
                continue

            in_game_id = discord_to_ingame.get(member.id)
            if in_game_id is None:
                counts["no_link"] += 1

            desired_keys: set[str] = set()
            if in_game_id:
                role_key = ingame_to_role_key.get(in_game_id)
                if role_key is None:
                    counts["no_mu"] += 1
                else:
                    desired_keys.add(role_key)
                    if role_key in ingame_to_commander_keys.get(in_game_id, set()):
                        desired_keys.add(_COMMANDER_KEY_FOR_TYPE[role_key])

            desired_role_ids = {
                self._army_role_ids[k] for k in desired_keys if k in self._army_role_ids
            }
            current_role_ids = {r.id for r in member.roles if r.id in all_army_role_ids}

            for rid in desired_role_ids - current_role_ids:
                role = guild.get_role(rid)
                if role:
                    try:
                        await member.add_roles(
                            role, reason="war_guild_divisions: army-type sync"
                        )
                        counts["roles_added"] += 1
                    except discord.Forbidden:
                        logger.warning(
                            "war_guild_divisions: no permission to add role to %s",
                            member.name,
                        )

            for rid in current_role_ids - desired_role_ids:
                role = guild.get_role(rid)
                if role:
                    try:
                        await member.remove_roles(
                            role, reason="war_guild_divisions: army-type sync"
                        )
                        counts["roles_removed"] += 1
                    except discord.Forbidden:
                        pass

        logger.info("war_guild_divisions: army-type sync done — %s", counts)
        return counts

    # ── Per-member army-type sync (called by WarSyncCog on join / verify) ────

    async def sync_member_army_type(
        self, member: discord.Member, in_game_id: str
    ) -> None:
        """Assign the correct army-type (+ commander) role for one member.

        Called from WarSyncCog after a member verifies or joins, once their
        identity link has been stored in the DB.
        """
        if not self._army_role_ids or not self._db:
            return

        guild = member.guild
        try:
            memberships = await self._db.get_mu_memberships_for_citizen(in_game_id)
        except Exception as exc:
            logger.warning(
                "war_guild_divisions: sync_member_army_type membership lookup "
                "failed for %s: %s",
                in_game_id, exc,
            )
            return
        if not memberships:
            return

        mu_type_map = await self._build_mu_type_map({mu_id for mu_id, _n, _r in memberships})

        desired_keys: set[str] = set()
        for mu_id, _mu_name, role_type in memberships:
            role_key = mu_type_map.get(mu_id)
            if role_key is None:
                continue
            desired_keys.add(role_key)
            if role_type in ("owner", "commander"):
                desired_keys.add(_COMMANDER_KEY_FOR_TYPE[role_key])

        desired_role_ids = {
            self._army_role_ids[k] for k in desired_keys if k in self._army_role_ids
        }
        all_army_role_ids = set(self._army_role_ids.values())
        current_role_ids = {r.id for r in member.roles if r.id in all_army_role_ids}

        for rid in desired_role_ids - current_role_ids:
            role = guild.get_role(rid)
            if role:
                try:
                    await member.add_roles(
                        role, reason="war_guild_divisions: join army-type sync"
                    )
                except discord.Forbidden:
                    logger.warning(
                        "war_guild_divisions: cannot add army-type role to %s",
                        member.name,
                    )

        for rid in current_role_ids - desired_role_ids:
            role = guild.get_role(rid)
            if role:
                try:
                    await member.remove_roles(
                        role, reason="war_guild_divisions: join army-type sync"
                    )
                except discord.Forbidden:
                    pass

        logger.info(
            "war_guild_divisions: sync_member_army_type — %s → %s",
            member.name, desired_keys or "none",
        )

    # ── Army-type channels ────────────────────────────────────────────────────

    async def _ensure_army_channels(self, guild: discord.Guild) -> None:
        """Create the Army category + one channel per army-type role, if missing.

        Looked up by a persisted ID (poll_state), not by name — someone
        renaming #paramilitary (e.g. adding an emoji prefix) must not make
        this think the channel is missing and create a duplicate. Confirmed
        as a real incident: renaming the 4 auto-created channels resulted
        in 4 fresh duplicates with the original plain names on the next sync.
        """
        if not self._db:
            return

        category_id = None
        try:
            stored = await self._db.get_poll_state(_ARMY_CATEGORY_POLL_KEY)
            category_id = int(stored) if stored else None
        except Exception:
            category_id = None
        category = guild.get_channel(category_id) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            # Bootstrap: a category with the default name might already exist
            # (first run, or the ID was never persisted) — use it once, then
            # persist its ID so a later rename doesn't orphan the link.
            category = discord.utils.get(guild.categories, name=_ARMY_CATEGORY_NAME)
        if category is None:
            try:
                category = await guild.create_category(
                    _ARMY_CATEGORY_NAME,
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(view_channel=False)
                    },
                    reason="war_guild_divisions: army-type channels",
                )
                logger.info(
                    "war_guild_divisions: created category %r", _ARMY_CATEGORY_NAME
                )
            except discord.Forbidden:
                logger.warning(
                    "war_guild_divisions: no permission to create category %r",
                    _ARMY_CATEGORY_NAME,
                )
                return
        try:
            await self._db.set_poll_state(_ARMY_CATEGORY_POLL_KEY, str(category.id))
        except Exception as exc:
            logger.warning("war_guild_divisions: failed to persist army category id: %s", exc)

        for key, channel_name in _ARMY_CHANNEL_NAMES.items():
            poll_key = _ARMY_CHANNEL_POLL_KEY_PREFIX + key
            channel_id = None
            try:
                stored = await self._db.get_poll_state(poll_key)
                channel_id = int(stored) if stored else None
            except Exception:
                channel_id = None
            if channel_id and guild.get_channel(channel_id):
                continue  # already has a linked, still-existing channel

            # Bootstrap: a channel with the default name might already exist
            # in this category but not yet be linked — use it once.
            existing = discord.utils.get(category.text_channels, name=channel_name)
            if existing is not None:
                try:
                    await self._db.set_poll_state(poll_key, str(existing.id))
                except Exception as exc:
                    logger.warning(
                        "war_guild_divisions: failed to persist army channel id for %s: %s",
                        key, exc,
                    )
                continue

            role_id = self._army_role_ids.get(key)
            role = guild.get_role(role_id) if role_id else None
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False)
            }
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
            try:
                new_channel = await guild.create_text_channel(
                    channel_name,
                    category=category,
                    overwrites=overwrites,
                    reason="war_guild_divisions: army-type channel",
                )
                await self._db.set_poll_state(poll_key, str(new_channel.id))
                logger.info("war_guild_divisions: created channel #%s", channel_name)
            except discord.Forbidden:
                logger.warning(
                    "war_guild_divisions: no permission to create channel #%s",
                    channel_name,
                )

    # ── Category permission repair ────────────────────────────────────────────

    def _overwrite_matches(
        self, overwrite: discord.PermissionOverwrite, desired: dict[str, bool]
    ) -> bool:
        return all(getattr(overwrite, perm) is value for perm, value in desired.items())

    async def _sync_mu_categories(self, guild: discord.Guild) -> dict:
        """Repair each linked MU category's permissions for that MU's
        Commander/Member roles, making Commander the de-facto admin of their
        own category.

        The permission repair matters because Discord automatically strips
        every channel/category permission overwrite for a role the instant
        that role is deleted. Recreating a role with the same name afterwards
        does NOT restore those overwrites — Discord treats the new role as a
        completely unrelated object. This re-applies the desired overwrites
        every hour, via set_permissions per-role (never a full overwrites=
        replace) so any other, unrelated role access manually granted on the
        category is left alone.

        Discord has no per-channel "Administrator" flag, so "commander is
        admin of their category" is approximated with the broadest set of
        channel-scoped permissions Discord actually offers
        (_COMMANDER_CATEGORY_PERMS below).

        Categories are matched to their MU via a persisted category_id,
        stored in war_mu_roles under a pseudo role_type of "category" — this
        is an ID, so it survives any future rename of either the category or
        the MU. A category with no persisted link yet is bootstrapped by
        matching its *current* bare name against war_mu_roles' current
        tracked MU names, and its link is persisted immediately so this
        fallback is only ever needed once per category.
        """
        counts = {"perms_repaired": 0}

        # {mu_id: {"commander": role_id, "member": role_id}}
        role_ids_by_mu: dict[str, dict[str, int]] = {}
        # {normalised_mu_name: mu_id} — bootstrap fallback only
        mu_id_by_name: dict[str, str] = {}
        # {mu_id: persisted category channel id}
        category_id_by_mu: dict[str, int] = {}

        if self._db:
            try:
                rows = await self._db.get_all_war_mu_roles(str(guild.id))
                for row in rows:
                    mu_id = row["mu_id"]
                    if row["role_type"] == "category":
                        category_id_by_mu[mu_id] = row["discord_role_id"]
                        continue
                    mu_name = row["mu_name"] or ""
                    if mu_name:
                        mu_id_by_name[_norm(mu_name)] = mu_id
                    if row["role_type"] in ("commander", "member"):
                        role_ids_by_mu.setdefault(mu_id, {})[row["role_type"]] = row["discord_role_id"]
            except Exception as exc:
                logger.warning(
                    "war_guild_divisions: role lookup for permission repair failed: %s", exc
                )

        # Resolve (mu_id, category) pairs: persisted ID link first, then a
        # name-matching bootstrap for anything not yet linked.
        pairs: list[tuple[str, discord.CategoryChannel]] = []
        linked_cat_ids: set[int] = set()
        for mu_id, cat_id in category_id_by_mu.items():
            cat = guild.get_channel(cat_id)
            if isinstance(cat, discord.CategoryChannel):
                pairs.append((mu_id, cat))
                linked_cat_ids.add(cat.id)

        for cat in guild.categories:
            if cat.id in linked_cat_ids:
                continue
            mu_id = mu_id_by_name.get(_norm(cat.name))
            if mu_id is None or mu_id in category_id_by_mu:
                continue  # unrelated category, or its real category is elsewhere
            pairs.append((mu_id, cat))
            if self._db:
                try:
                    await self._db.upsert_war_mu_role(
                        mu_id, "category", str(guild.id), cat.id, cat.name
                    )
                    logger.info(
                        "war_guild_divisions: linked category %r to mu_id %s by name "
                        "(one-time bootstrap)",
                        cat.name, mu_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "war_guild_divisions: failed to persist category link for "
                        "mu_id %s: %s",
                        mu_id, exc,
                    )

        for mu_id, cat in pairs:
            to_fix: list[tuple[discord.Role, dict[str, bool]]] = []
            if cat.overwrites_for(guild.default_role).view_channel is not False:
                to_fix.append((guild.default_role, {"view_channel": False}))

            mu_role_ids = role_ids_by_mu.get(mu_id, {})
            commander_role = guild.get_role(mu_role_ids["commander"]) if "commander" in mu_role_ids else None
            member_role = guild.get_role(mu_role_ids["member"]) if "member" in mu_role_ids else None

            if commander_role and not self._overwrite_matches(
                cat.overwrites_for(commander_role), _COMMANDER_CATEGORY_PERMS
            ):
                to_fix.append((commander_role, _COMMANDER_CATEGORY_PERMS))
            if member_role and not self._overwrite_matches(
                cat.overwrites_for(member_role), _MEMBER_CATEGORY_PERMS
            ):
                to_fix.append((member_role, _MEMBER_CATEGORY_PERMS))

            for target, perms in to_fix:
                try:
                    await cat.set_permissions(
                        target,
                        reason="war_guild_divisions: MU-categorie permissie herstel",
                        **perms,
                    )
                    counts["perms_repaired"] += 1
                    logger.info(
                        "war_guild_divisions: repaired permissions for %r on category %r: %s",
                        getattr(target, "name", target), cat.name, perms,
                    )
                except discord.Forbidden:
                    logger.warning(
                        "war_guild_divisions: no permission to fix permissions on category %r",
                        cat.name,
                    )

        logger.info("war_guild_divisions: category permission repair done — %s", counts)
        return counts

    # ── Government sync ───────────────────────────────────────────────────────

    async def _ensure_advisor_roles(
        self, guild: discord.Guild, advisors: list[tuple[str, str | None]]
    ) -> tuple[dict[str, int], set[int]]:
        """Assign a dynamically-named Discord role to each current advisor.

        *advisors* is a list of (in_game_user_id, custom_title_or_None) for
        every current "advisor" minister slot — unlike the other six titles
        this isn't a single fixed seat, a country can have zero or several
        at once, so there's no static GOV_ROLE_NAMES entry for it.

        Returns (uid -> role_id for *advisors* right now, every advisor role
        ID still tracked including orphaned ones) — the caller needs the
        second set too, so a former advisor still loses their old role even
        in the same cycle its role gets reused for someone else.

        Advisor custom titles are the president's free-text choice and
        change over time, so an existing role is renamed in place rather
        than recreated, and a role freed up by someone leaving the advisor
        slot is reused for the next new advisor instead of letting roles
        pile up unused.
        """
        if not self._db:
            return {}, set()

        try:
            raw = await self._db.get_poll_state(_ADVISOR_ROLE_MAP_POLL_KEY)
            prev_map: dict[str, int] = {
                k: int(v) for k, v in (json.loads(raw) if raw else {}).items()
            }
        except Exception:
            prev_map = {}

        current_ids = {uid for uid, _ in advisors}
        free_role_ids: list[int] = [
            rid for uid, rid in prev_map.items()
            if uid not in current_ids and guild.get_role(rid) is not None
        ]

        result: dict[str, int] = {}
        for uid, custom_title in advisors:
            desired_name = custom_title or _DEFAULT_ADVISOR_ROLE_NAME
            role = guild.get_role(prev_map[uid]) if uid in prev_map else None

            if role is None and free_role_ids:
                role = guild.get_role(free_role_ids.pop())

            if role is None:
                try:
                    role = await guild.create_role(
                        name=desired_name,
                        mentionable=True,
                        reason="war_guild_divisions: auto-create advisor role",
                    )
                    logger.info(
                        "war_guild_divisions: created advisor role %r (id=%d) for %s",
                        desired_name, role.id, uid,
                    )
                except discord.Forbidden:
                    logger.warning(
                        "war_guild_divisions: no permission to create advisor role for %s", uid
                    )
                    continue
            elif role.name != desired_name:
                try:
                    await role.edit(
                        name=desired_name, reason="war_guild_divisions: advisor title sync"
                    )
                except discord.Forbidden:
                    logger.warning(
                        "war_guild_divisions: no permission to rename advisor role %r", role.name
                    )

            result[uid] = role.id

        # Persist current advisors + still-orphaned roles not repurposed this
        # cycle, so the latter remain available for reuse next time.
        used_role_ids = set(result.values())
        next_map = dict(result)
        for uid, rid in prev_map.items():
            if (
                uid not in current_ids
                and rid not in used_role_ids
                and guild.get_role(rid) is not None
            ):
                next_map[uid] = rid

        try:
            await self._db.set_poll_state(_ADVISOR_ROLE_MAP_POLL_KEY, json.dumps(next_map))
        except Exception as exc:
            logger.warning("war_guild_divisions: failed to persist advisor role map: %s", exc)

        return result, set(next_map.values())

    async def _sync_government(self, guild: discord.Guild) -> dict:
        """Fetch the current government from the API and sync war-guild roles."""
        counts = {"gov_added": 0, "gov_removed": 0}

        if not self._gov_role_ids:
            return counts
        if not self._client:
            logger.warning("war_guild_divisions: API client unavailable, skipping gov sync")
            return counts

        nl_country_id = self.bot.config.get("nl_country_id", "")
        try:
            raw = await self._client.get(
                "/government.getByCountryId",
                params={"input": json.dumps({"countryId": nl_country_id})},
            )
            data = _unwrap_trpc(raw)
        except Exception as exc:
            logger.warning("war_guild_divisions: government API failed: %s", exc)
            return counts

        if not isinstance(data, dict):
            logger.warning(
                "war_guild_divisions: unexpected government response type: %r", type(data)
            )
            return counts

        # President/VP are still flat string-ID fields:
        #   {"president": "abc123", "vicePresident": "def456", "ministers": [...]}
        # Everything else moved into "ministers" — a list of
        # {"user": "...", "title": "minOfDefense", "customTitle": "...",
        # "rights": [...]}. "title" is one of 7 fixed slot keys (see
        # _MINISTER_TITLE_TO_GOV_KEY); "advisor" is a flexible N-seat slot
        # handled separately via _ensure_advisor_roles. "customTitle" is a
        # cosmetic display name the president can set on ANY slot, not a
        # distinct permission — it's only used for the advisor role name.
        def _str_id(val: object) -> str | None:
            """Return val if it is a non-empty string, else None."""
            return val if isinstance(val, str) and val else None

        # Build in_game_user_id → set of gov-role keys
        ingame_to_gov_keys: dict[str, set[str]] = {}

        def _add_gov(uid: object, key: str) -> None:
            s = _str_id(uid)
            if s:
                ingame_to_gov_keys.setdefault(s, set()).add(key)

        _add_gov(data.get("president"), "president")
        _add_gov(data.get("vicePresident"), "vice_president")

        ministers = data.get("ministers")
        advisor_entries: list[tuple[str, str | None]] = []
        n_fixed_ministers = 0
        if isinstance(ministers, list):
            for entry in ministers:
                if not isinstance(entry, dict):
                    continue
                uid = _str_id(entry.get("user"))
                title = entry.get("title")
                if not uid:
                    continue
                if title == "advisor":
                    advisor_entries.append((uid, entry.get("customTitle")))
                elif title in _MINISTER_TITLE_TO_GOV_KEY:
                    _add_gov(uid, _MINISTER_TITLE_TO_GOV_KEY[title])
                    n_fixed_ministers += 1

        advisor_assignment, all_advisor_role_ids = await self._ensure_advisor_roles(
            guild, advisor_entries
        )

        logger.info(
            "war_guild_divisions: government response — president=%s, vp=%s, "
            "%d fixed-slot ministers, %d advisors",
            data.get("president"), data.get("vicePresident"),
            n_fixed_ministers, len(advisor_entries),
        )

        # Build discord_id → in_game_id lookup
        discord_to_ingame: dict[int, str] = {}
        if self._db:
            try:
                for link in await self._db.get_all_identity_links():
                    discord_to_ingame[int(link["discord_user_id"])] = link[
                        "in_game_user_id"
                    ]
            except Exception as exc:
                logger.warning(
                    "war_guild_divisions: failed to load identity links for gov: %s", exc
                )

        logger.info(
            "war_guild_divisions: %d government positions found, %d members have identity links",
            sum(len(v) for v in ingame_to_gov_keys.values()) + len(advisor_assignment),
            len(discord_to_ingame),
        )

        all_gov_role_ids = set(self._gov_role_ids.values()) | all_advisor_role_ids

        for member in guild.members:
            if member.bot:
                continue

            in_game_id = discord_to_ingame.get(member.id)
            desired_gov_rids: set[int] = set()
            if in_game_id:
                for key in ingame_to_gov_keys.get(in_game_id, set()):
                    rid = self._gov_role_ids.get(key)
                    if rid:
                        desired_gov_rids.add(rid)
                advisor_rid = advisor_assignment.get(in_game_id)
                if advisor_rid:
                    desired_gov_rids.add(advisor_rid)

            current_gov_rids = {r.id for r in member.roles if r.id in all_gov_role_ids}
            to_add = desired_gov_rids - current_gov_rids
            to_remove = current_gov_rids - desired_gov_rids

            for rid in to_add:
                role = guild.get_role(rid)
                if role:
                    try:
                        await member.add_roles(
                            role, reason="war_guild_divisions: government sync"
                        )
                        counts["gov_added"] += 1
                    except discord.Forbidden:
                        pass

            for rid in to_remove:
                role = guild.get_role(rid)
                if role:
                    try:
                        await member.remove_roles(
                            role, reason="war_guild_divisions: government sync"
                        )
                        counts["gov_removed"] += 1
                    except discord.Forbidden:
                        pass

        logger.info("war_guild_divisions: government sync done — %s", counts)
        return counts

    # ── Daily task ────────────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def daily_sync(self) -> None:
        """Hourly sync: army-type roles/channels, government roles, category
        permissions, and legacy division-system cleanup."""
        guild = self._war_guild
        if not guild:
            logger.warning(
                "war_guild_divisions: war guild not found, skipping sync"
            )
            return

        try:
            await self._ensure_army_roles(guild)
            await self._ensure_gov_roles(guild)
        except Exception as exc:
            logger.error("war_guild_divisions: role setup failed: %s", exc)
            return

        try:
            await self._cleanup_legacy_division_artifacts(guild)
        except Exception as exc:
            logger.error("war_guild_divisions: legacy cleanup failed: %s", exc)

        # citizen_mu_membership is kept fresh by war_sync's own hourly/daily tasks;
        # no need to trigger a full API scan here on every run.

        try:
            await self._sync_army_types(guild)
        except Exception as exc:
            logger.error("war_guild_divisions: army-type sync failed: %s", exc)

        try:
            await self._sync_government(guild)
        except Exception as exc:
            logger.error("war_guild_divisions: government sync failed: %s", exc)

        try:
            await self._sync_mu_categories(guild)
        except Exception as exc:
            logger.error("war_guild_divisions: category sync failed: %s", exc)

        try:
            await self._ensure_army_channels(guild)
        except Exception as exc:
            logger.error("war_guild_divisions: army channel setup failed: %s", exc)

    @daily_sync.before_loop
    async def _before_daily_sync(self) -> None:
        await self._wait_for_services()
        # Initialise roles immediately at startup so they are available right away
        guild = self._war_guild
        if guild:
            try:
                await self._ensure_army_roles(guild)
                await self._ensure_gov_roles(guild)
            except Exception as exc:
                logger.warning(
                    "war_guild_divisions: startup role setup failed: %s", exc
                )

    # ── Slash command ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="syncarmy",
        description="Synchroniseer Paramilitary/State Army rollen, kanalen en regering-rollen.",
    )
    async def syncarmy(self, interaction: discord.Interaction) -> None:
        """Manually trigger a full army-type + government sync."""
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ Alleen uitvoerbaar op de server.", ephemeral=True
            )
            return

        is_admin = self._is_war_admin(interaction.user)
        is_owner = await self.bot.is_owner(interaction.user)
        if not is_admin and not is_owner:
            await interaction.response.send_message("❌ Geen toegang.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = self._war_guild
        if not guild:
            await interaction.followup.send("❌ War-guild niet gevonden.", ephemeral=True)
            return

        # Trigger a fresh MU scan so citizen_mu_membership is up-to-date
        war_sync_cog = self.bot.cogs.get("war_sync")
        if war_sync_cog and hasattr(war_sync_cog, "scan_dutch_mus"):
            try:
                n_mus = await war_sync_cog.scan_dutch_mus()
                logger.info("war_guild_divisions: triggered MU scan via WarSyncCog — %d MUs", n_mus)
            except Exception as exc:
                logger.warning("war_guild_divisions: MU scan failed: %s", exc)

        try:
            await self._ensure_army_roles(guild)
            await self._ensure_gov_roles(guild)
            cleanup_counts = await self._cleanup_legacy_division_artifacts(guild)
            army_counts = await self._sync_army_types(guild)
            gov_counts = await self._sync_government(guild)
            cat_counts = await self._sync_mu_categories(guild)
            await self._ensure_army_channels(guild)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Synchronisatie mislukt: {exc}", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Synchronisatie voltooid.\n"
            f"• Army-rollen toegevoegd: {army_counts.get('roles_added', 0)}, "
            f"verwijderd: {army_counts.get('roles_removed', 0)}\n"
            f"• Regering-rollen toegevoegd: {gov_counts.get('gov_added', 0)}, "
            f"verwijderd: {gov_counts.get('gov_removed', 0)}\n"
            f"• Categorie-permissies hersteld: {cat_counts.get('perms_repaired', 0)}\n"
            f"• Legacy opruiming — rollen verwijderd: {cleanup_counts.get('roles_deleted', 0)}, "
            f"nicks ontdaan van prefix: {cleanup_counts.get('nicks_stripped', 0)}, "
            f"categorieën ontdaan van prefix: {cleanup_counts.get('cats_stripped', 0)}\n"
            f"• Leden zonder gekoppeld in-game account: {army_counts.get('no_link', 0)}\n"
            f"• Leden met account maar geen (relevante) MU: {army_counts.get('no_mu', 0)}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    if not bot.config.get("war_guild"):
        return
    await bot.add_cog(WarGuildDivisionsCog(bot))
