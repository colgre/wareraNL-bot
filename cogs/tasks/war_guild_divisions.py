"""War-guild division roles and government roles sync.

Manages:
  1. @d1–@d5 Discord roles (auto-created with distinct colours)
  2. Nickname prefix ``[DN] `` for war-guild members based on their MU
  3. Government roles (President, Vice-President, ministers) auto-created
  4. Daily sync task to keep roles and nicknames up-to-date

Only active when ``config["war_guild"]`` is present.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ._base import TaskCogBase

if TYPE_CHECKING:
    pass

logger = logging.getLogger("discord_bot")

# ── Division configuration ────────────────────────────────────────────────────
#
# MU IDs, not names, are the source of truth: a MU's Discord roles,
# categories, and nickname prefix are all keyed off DIVISION_MU_IDS below.
# Confirmed as a real incident: "De Munterij" was renamed in-game to
# "De Pomperij", and every permission that had been matched against the old
# hardcoded name silently broke — the sync logic simply stopped recognising
# that MU as belonging to any division at all. IDs never change on a rename,
# so this can't happen again. The trailing comment after each ID is only for
# a human reading this file (never parsed) — it can go stale after a future
# rename without breaking anything; update it opportunistically.
#
# Not resolved: "V.O.C." (division 5) — not found under that name in
# known_mus, war_mu_roles, or a live game search. It may have renamed to
# something not matching that search, or disbanded. Dropped from this list
# pending confirmation of its current name/ID — re-add it once known.
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
        "69a896d1214e4eeda01ba4c5",  # Kikkers Defence
    ],
    3: [
        "695acce63ae9eac7fb6e59e1",  # Korps Commando's
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
# sync_tasks.py, war_guild_status.py, damage.py, citizens.py) that only need
# a MU's name for autocomplete suggestions or table display — never used for
# permission/role-granting decisions inside this file, so a name here going
# stale after a rename is a cosmetic issue (a name shown somewhere is
# outdated), not a security one. Kept in sync by hand alongside the comments
# above; _apply_division_override updates both together at runtime.
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
        "Kikkers Defence",
    ],
    3: [
        "Korps Commando's",
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

# Discord user IDs of each division's general (hardcoded)
DIVISION_GENERALS: dict[int, int] = {
    1: 434948719373189120,
    2: 135824772331339776,
    3: 538004132288659487,
    4: 516326282867245067,
    # D5 general not yet assigned
}

# Discord colour for each division role
DIVISION_COLOURS: dict[int, discord.Colour] = {
    1: discord.Colour(0xFFD700),  # Gold
    2: discord.Colour(0x4C72B0),  # Blue
    3: discord.Colour(0x55A868),  # Green
    4: discord.Colour(0xE74C3C),  # Red
    5: discord.Colour(0x9B59B6),  # Purple
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
}

# Regex to match and strip an existing [DN] prefix from a nickname
_NICK_PREFIX_RE = re.compile(r"^\[D\d\] ")


def _norm(name: str) -> str:
    """Normalise a MU name for fuzzy comparison (strips spaces, dots, dashes).

    Only used for name-based matching that has no better option (a Discord
    category's display name, an admin-typed /mudivisie search) — never for
    deciding which division a MU belongs to; that's _MU_ID_TO_DIV.
    """
    return re.sub(r"[\s.\-_]+", "", name).lower()


def _build_mu_id_to_div() -> dict[str, int]:
    result: dict[str, int] = {}
    for div, mu_ids in DIVISION_MU_IDS.items():
        for mu_id in mu_ids:
            result[mu_id] = div
    return result


_MU_ID_TO_DIV: dict[str, int] = _build_mu_id_to_div()


def _apply_division_override(mu_id: str, mu_name: str, division: int) -> None:
    """Move/add/remove a MU (identified by *mu_id*) to *division*.

    ``division == 0`` removes the MU from every division. Mutates
    ``DIVISION_MU_IDS`` (the real, ID-keyed source of truth) and
    ``_MU_ID_TO_DIV`` in place, plus the display-only ``DIVISION_MUS`` name
    mirror, so every ``from cogs.tasks.war_guild_divisions import
    DIVISION_MUS`` import elsewhere in the bot (mu.py, mudmg.py, damage.py,
    citizens.py, war_guild_status.py, paraatheid.py, mu_subscriptions.py,
    sync_tasks.py) observes the change immediately, without a restart.
    Keyed by mu_id so a later in-game rename of this MU doesn't silently
    drop the override the way name-based matching used to.
    """
    for mu_ids in DIVISION_MU_IDS.values():
        if mu_id in mu_ids:
            mu_ids.remove(mu_id)
    norm_target = _norm(mu_name)
    for names in DIVISION_MUS.values():
        for existing in list(names):
            if _norm(existing) == norm_target:
                names.remove(existing)
    if division:
        DIVISION_MU_IDS.setdefault(division, [])
        if mu_id not in DIVISION_MU_IDS[division]:
            DIVISION_MU_IDS[division].append(mu_id)
        DIVISION_MUS.setdefault(division, [])
        if mu_name not in DIVISION_MUS[division]:
            DIVISION_MUS[division].append(mu_name)
    _MU_ID_TO_DIV.clear()
    _MU_ID_TO_DIV.update(_build_mu_id_to_div())


async def _mu_naam_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest MU names: current DIVISION_MUS entries + live known_mus DB."""
    names: set[str] = set()
    for mus in DIVISION_MUS.values():
        names.update(mus)
    db = getattr(interaction.client, "_ext_db", None)
    if db:
        try:
            names.update(await db.get_known_mu_names(current))
        except Exception:
            pass
    q = current.strip().lower()
    filtered = sorted(n for n in names if q in n.lower())[:25]
    return [app_commands.Choice(name=n, value=n) for n in filtered]


def _unwrap_trpc(resp: dict) -> object:
    """Unwrap ``{"result": {"data": ...}}`` tRPC response envelope."""
    try:
        return resp["result"]["data"]
    except (KeyError, TypeError):
        return resp


class WarGuildDivisionsCog(TaskCogBase):
    """Manages @d1–@d5 division roles and government roles in the war guild."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._war_cfg: dict = {}
        # division number (1-5) → Discord role ID
        self._division_role_ids: dict[int, int] = {}
        # gov key (e.g. "president") → Discord role ID
        self._gov_role_ids: dict[str, int] = {}
        # division number (1-5) → Discord role ID for "Dx Leiderschap" roles
        self._leadership_role_ids: dict[int, int] = {}

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
        """War-guild admin role IDs allowed to run /syncdivisions and /mudivisie.

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

    async def _load_division_overrides(self) -> None:
        """Apply persisted /mudivisie edits on top of the hardcoded DIVISION_MU_IDS."""
        if not self._db:
            return
        try:
            overrides = await self._db.get_all_division_mu_overrides()
        except Exception as exc:
            logger.warning(
                "war_guild_divisions: failed to load division overrides: %s", exc
            )
            return
        applied = 0
        for mu_id, mu_name, division in overrides:
            if not mu_id:
                # Legacy row from before mu_id was tracked on this table —
                # resolve it once now and backfill so future loads don't
                # need this fallback.
                try:
                    found_id, found_name = await self._db.get_known_mu_by_name(mu_name)
                except Exception:
                    found_id = None
                if not found_id:
                    logger.warning(
                        "war_guild_divisions: could not resolve mu_id for legacy "
                        "override %r — skipping until it's re-added via /mudivisie",
                        mu_name,
                    )
                    continue
                mu_id, mu_name = found_id, found_name or mu_name
                try:
                    await self._db.upsert_division_mu_override(mu_id, mu_name, division)
                except Exception as exc:
                    logger.warning(
                        "war_guild_divisions: failed to backfill mu_id for %r: %s",
                        mu_name, exc,
                    )
            _apply_division_override(mu_id, mu_name, division)
            applied += 1
        if applied:
            logger.info(
                "war_guild_divisions: applied %d persisted division override(s)",
                applied,
            )

    # ── Role management ───────────────────────────────────────────────────────

    async def _ensure_division_roles(self, guild: discord.Guild) -> None:
        """Create/rename @Divisie 1–@Divisie 5 roles in the war guild if needed."""
        existing = {r.name: r for r in guild.roles}
        for div_n in range(1, 6):
            name = f"Divisie {div_n}"
            old_name = f"d{div_n}"
            role = existing.get(name)
            if role is None:
                # Migrate old short name → new name if it exists
                role = existing.get(old_name)
                if role is not None:
                    await role.edit(
                        name=name,
                        reason="war_guild_divisions: rename d{n} → Divisie {n}",
                    )
                    logger.info(
                        "war_guild_divisions: renamed role %r → %r (id=%d)",
                        old_name, name, role.id,
                    )
                else:
                    role = await guild.create_role(
                        name=name,
                        colour=DIVISION_COLOURS[div_n],
                        mentionable=True,
                        reason="war_guild_divisions: auto-create division role",
                    )
                    logger.info(
                        "war_guild_divisions: created role %r (id=%d)", name, role.id
                    )
            if role.colour != DIVISION_COLOURS[div_n]:
                await role.edit(
                    colour=DIVISION_COLOURS[div_n],
                    reason="war_guild_divisions: colour sync",
                )
            self._division_role_ids[div_n] = role.id

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

    async def _ensure_leadership_roles(self, guild: discord.Guild) -> None:
        """Create 'D1 Leiderschap'–'D5 Leiderschap' roles if they don't yet exist."""
        existing = {r.name: r for r in guild.roles}
        for div_n in range(1, 6):
            name = f"D{div_n} Leiderschap"
            role = existing.get(name)
            if role is None:
                role = await guild.create_role(
                    name=name,
                    colour=DIVISION_COLOURS[div_n],
                    mentionable=True,
                    reason="war_guild_divisions: auto-create leiderschap role",
                )
                logger.info(
                    "war_guild_divisions: created leiderschap role %r (id=%d)", name, role.id
                )
            self._leadership_role_ids[div_n] = role.id

    # ── Division sync ─────────────────────────────────────────────────────────

    def _build_mu_id_div_map(
        self, all_memberships: list
    ) -> tuple[dict[str, int], set[str]]:
        """Build mu_id → division map from all rows in citizen_mu_membership.

        Returns (mu_id_to_div, unmatched_mu_names).
        """
        mu_id_to_div: dict[str, int] = {}
        unmatched: set[str] = set()
        for _uid, mu_id, mu_name, _role in all_memberships:
            if mu_id in mu_id_to_div:
                continue
            div = _MU_ID_TO_DIV.get(mu_id)
            if div is not None:
                mu_id_to_div[mu_id] = div
            else:
                unmatched.add(mu_name)
        if unmatched:
            logger.info(
                "war_guild_divisions: MU names not matched to any division (%d): %s",
                len(unmatched),
                ", ".join(sorted(unmatched)),
            )
        return mu_id_to_div, unmatched

    async def _sync_divisions(self, guild: discord.Guild) -> dict:
        """Sync @dN roles and [DN] nickname prefixes for all guild members."""
        counts = {
            "roles_added": 0,
            "roles_removed": 0,
            "nicks_updated": 0,
            "no_link": 0,
            "no_division": 0,
            "unmatched_mus": set(),
            "no_link_members": [],
            "no_division_members": [],
        }

        if not self._division_role_ids:
            logger.warning("war_guild_divisions: division role IDs not loaded, skipping")
            return counts

        # ── Load citizen_mu_membership (needed for role sync + fallback) ─────────
        all_memberships: list = []
        if self._db:
            try:
                all_memberships = await self._db.get_all_mu_memberships()
            except Exception as exc:
                logger.warning(
                    "war_guild_divisions: failed to load MU memberships: %s", exc
                )

        if not all_memberships:
            logger.warning(
                "war_guild_divisions: citizen_mu_membership table is empty — "
                "skipping member sync to avoid removing all division roles. "
                "Run /syncwar first to populate it."
            )
            return counts

        mu_id_to_div, unmatched_mus = self._build_mu_id_div_map(all_memberships)
        counts["unmatched_mus"] = unmatched_mus

        # ── Build discord_id → in_game_id from identity_links ─────────────────
        discord_to_ingame: dict[int, str] = {}
        if self._db:
            try:
                for link in await self._db.get_all_identity_links():
                    discord_to_ingame[int(link["discord_user_id"])] = link[
                        "in_game_user_id"
                    ]
            except Exception as exc:
                logger.warning(
                    "war_guild_divisions: failed to load identity links: %s", exc
                )

        # ── Source 1 (primary): citizen_levels.mu_id ──────────────────────────
        # Per-citizen profiles are refreshed hourly and update immediately when a
        # player switches MU.  MU member-list API responses can lag hours after a
        # switch, so citizen_levels is the more accurate source for division mapping.
        # Keyed by mu_id, not mu_name — a MU's name can change in-game while its
        # ID never does (confirmed: "De Munterij" → "De Pomperij" broke every
        # permission tied to matching on the old name).
        ingame_to_div: dict[str, int] = {}
        all_ingame_ids = list(discord_to_ingame.values())
        if all_ingame_ids and self._db:
            try:
                cl_mu_ids = await self._db.get_citizen_mu_ids_for_users(all_ingame_ids)
                for uid, mu_id in cl_mu_ids.items():
                    div = _MU_ID_TO_DIV.get(mu_id)
                    if div is not None:
                        ingame_to_div[uid] = div
                if cl_mu_ids:
                    logger.info(
                        "war_guild_divisions: citizen_levels resolved %d/%d users to a division",
                        len(ingame_to_div), len(all_ingame_ids),
                    )
            except Exception as exc:
                logger.warning(
                    "war_guild_divisions: citizen_levels primary lookup failed: %s", exc
                )

        # ── Source 2 (fallback): citizen_mu_membership ────────────────────────
        # Fills gaps where citizen_levels has no MU recorded for a user.
        fallback_hits = 0
        for uid, mu_id, _mu_name, _role in all_memberships:
            if uid in ingame_to_div:
                continue  # already resolved by citizen_levels
            div = mu_id_to_div.get(mu_id)
            if div is not None:
                ingame_to_div[uid] = div
                fallback_hits += 1
        if fallback_hits:
            logger.info(
                "war_guild_divisions: citizen_mu_membership fallback added %d members",
                fallback_hits,
            )

        # Safety: if *no* member mapped to any division skip to avoid stripping all roles.
        if not ingame_to_div:
            logger.warning(
                "war_guild_divisions: ingame_to_div is empty after processing %d rows "
                "— skipping member sync (check DIVISION_MUS names against API names; "
                "unmatched: %s)",
                len(all_memberships),
                ", ".join(sorted(unmatched_mus)) if unmatched_mus else "none",
            )
            return counts

        logger.info(
            "war_guild_divisions: syncing %d guild members, %d have identity links, "
            "%d in a mapped division",
            sum(1 for m in guild.members if not m.bot),
            len(discord_to_ingame),
            len(ingame_to_div),
        )

        all_div_role_ids = set(self._division_role_ids.values())

        for member in guild.members:
            if member.bot:
                continue

            in_game_id = discord_to_ingame.get(member.id)
            if in_game_id is None:
                counts["no_link"] += 1
                counts["no_link_members"].append(f"{member.display_name} ({member.id})")

            desired_div: int | None = (
                ingame_to_div.get(in_game_id) if in_game_id else None
            )
            if in_game_id is not None and desired_div is None:
                counts["no_division"] += 1
                counts["no_division_members"].append(f"{member.display_name} ({member.id})")

            # Role sync: grant the correct @dN role, remove all others
            desired_rid: int | None = (
                self._division_role_ids.get(desired_div) if desired_div else None
            )
            current_div_roles = {r.id for r in member.roles if r.id in all_div_role_ids}
            to_add = {desired_rid} - current_div_roles if desired_rid else set()
            to_remove = current_div_roles - ({desired_rid} if desired_rid else set())

            for rid in to_add:
                role = guild.get_role(rid)
                if role:
                    try:
                        await member.add_roles(
                            role, reason="war_guild_divisions: division sync"
                        )
                        counts["roles_added"] += 1
                    except discord.Forbidden:
                        logger.warning(
                            "war_guild_divisions: no permission to add role to %s",
                            member.name,
                        )

            for rid in to_remove:
                role = guild.get_role(rid)
                if role:
                    try:
                        await member.remove_roles(
                            role, reason="war_guild_divisions: division sync"
                        )
                        counts["roles_removed"] += 1
                    except discord.Forbidden:
                        pass

            # Nickname prefix sync
            await self._update_nick_prefix(member, desired_div, counts)

        logger.info("war_guild_divisions: division sync done — %s", counts)
        return counts

    async def _update_nick_prefix(
        self,
        member: discord.Member,
        desired_div: int | None,
        counts: dict,
    ) -> None:
        """Add or remove the ``(dN) `` prefix from a member's nickname."""
        # Use current displayed name (nick or username) as the base
        current_nick = member.nick if member.nick is not None else member.name
        # Strip any pre-existing (dN) prefix to get the bare name
        base_nick = _NICK_PREFIX_RE.sub("", current_nick)

        if desired_div is None:
            # We have no division data for this member — leave their nick untouched
            # so a stale DB after restart never strips valid prefixes.
            return

        target_nick = f"[D{desired_div}] {base_nick}"

        # Truncate to Discord's 32-character limit
        target_nick = target_nick[:32]

        # No-op if already correct
        effective_current = member.nick if member.nick is not None else member.name
        if effective_current == target_nick:
            return

        try:
            await member.edit(
                nick=target_nick,
                reason="war_guild_divisions: division prefix sync",
            )
            counts["nicks_updated"] += 1
        except discord.Forbidden:
            # Bot cannot edit nicks of members whose highest role is above the
            # bot's own highest role, or the server owner.  Skip silently but
            # track it so the /syncdivisions reply can report the count.
            counts.setdefault("nicks_forbidden", 0)
            counts["nicks_forbidden"] += 1
            logger.warning(
                "war_guild_divisions: cannot set nick for %s — "
                "member's role is above the bot's role in the hierarchy (Forbidden)",
                member.name,
            )

    # ── Per-member division sync (called by WarSyncCog on join / verify) ──────

    async def sync_member_division(
        self, member: discord.Member, in_game_id: str
    ) -> None:
        """Assign the correct @dN role and [DN] nick prefix for one member.

        Called from WarSyncCog after a member verifies or joins, once their
        identity link has been stored in the DB.
        """
        if not self._division_role_ids:
            return

        guild = member.guild
        desired_div: int | None = None

        if self._db:
            # Primary: citizen_mu_membership
            try:
                memberships = await self._db.get_mu_memberships_for_citizen(in_game_id)
                for mu_id, _mu_name, _role_type in memberships:
                    div = _MU_ID_TO_DIV.get(mu_id)
                    if div is not None and (desired_div is None or div < desired_div):
                        desired_div = div
            except Exception as exc:
                logger.warning(
                    "war_guild_divisions: sync_member_division membership lookup "
                    "failed for %s: %s",
                    in_game_id,
                    exc,
                )

            # Fallback: citizen_levels.mu_id
            if desired_div is None:
                try:
                    fallback = await self._db.get_citizen_mu_ids_for_users([in_game_id])
                    mu_id = fallback.get(in_game_id)
                    if mu_id:
                        desired_div = _MU_ID_TO_DIV.get(mu_id)
                except Exception as exc:
                    logger.warning(
                        "war_guild_divisions: sync_member_division fallback failed "
                        "for %s: %s",
                        in_game_id,
                        exc,
                    )

        if desired_div is None:
            logger.debug(
                "war_guild_divisions: sync_member_division — no division found for %s",
                in_game_id,
            )
            return

        # Role sync
        all_div_role_ids = set(self._division_role_ids.values())
        desired_rid = self._division_role_ids.get(desired_div)
        current_div_rids = {r.id for r in member.roles if r.id in all_div_role_ids}
        to_add = {desired_rid} - current_div_rids if desired_rid else set()
        to_remove = current_div_rids - ({desired_rid} if desired_rid else set())

        for rid in to_add:
            role = guild.get_role(rid)
            if role:
                try:
                    await member.add_roles(
                        role, reason="war_guild_divisions: join division sync"
                    )
                except discord.Forbidden:
                    logger.warning(
                        "war_guild_divisions: cannot add division role to %s",
                        member.name,
                    )

        for rid in to_remove:
            role = guild.get_role(rid)
            if role:
                try:
                    await member.remove_roles(
                        role, reason="war_guild_divisions: join division sync"
                    )
                except discord.Forbidden:
                    pass

        # Nick prefix
        counts: dict = {"nicks_updated": 0}
        await self._update_nick_prefix(member, desired_div, counts)
        logger.info(
            "war_guild_divisions: sync_member_division — %s → D%d (nick_updated=%s)",
            member.name,
            desired_div,
            bool(counts["nicks_updated"]),
        )

    # ── Leiderschap sync ─────────────────────────────────────────────────────

    async def _sync_leadership_roles(self, guild: discord.Guild) -> dict:
        """Sync 'Dx Leiderschap' roles.

        Grants the 'Dx Leiderschap' role for division X to:
        - The hardcoded general for that division (by Discord user ID)
        - MU owners and commanders whose MU belongs to division X
        - The minister of defence (receives all 5 Dx Leiderschap roles)
        """
        counts = {"leadership_added": 0, "leadership_removed": 0}
        if not self._leadership_role_ids:
            return counts

        # Build: in_game_id → set of divisions where this citizen is owner/commander
        ingame_to_leadership_divs: dict[str, set[int]] = {}
        if self._db:
            try:
                all_memberships = await self._db.get_all_mu_memberships()
                for uid, mu_id, _mu_name, role_type in all_memberships:
                    if role_type not in ("owner", "commander"):
                        continue
                    div = _MU_ID_TO_DIV.get(mu_id)
                    if div is not None:
                        ingame_to_leadership_divs.setdefault(uid, set()).add(div)
            except Exception as exc:
                logger.warning(
                    "war_guild_divisions: leadership memberships lookup failed: %s", exc
                )

        # Fetch minister of defence in-game ID from government API
        minister_of_defense_id: str | None = None
        if self._client:
            nl_country_id = self.bot.config.get("nl_country_id", "")
            try:
                raw = await self._client.get(
                    "/government.getByCountryId",
                    params={"input": json.dumps({"countryId": nl_country_id})},
                )
                data = _unwrap_trpc(raw)
                if isinstance(data, dict):
                    val = data.get("minOfDefense") or data.get("ministerOfDefense")
                    if isinstance(val, str) and val:
                        minister_of_defense_id = val
            except Exception as exc:
                logger.warning(
                    "war_guild_divisions: gov API for leiderschap sync failed: %s", exc
                )

        # Load identity links: Discord ID → in-game ID
        discord_to_ingame: dict[int, str] = {}
        if self._db:
            try:
                for link in await self._db.get_all_identity_links():
                    discord_to_ingame[int(link["discord_user_id"])] = link[
                        "in_game_user_id"
                    ]
            except Exception as exc:
                logger.warning(
                    "war_guild_divisions: leadership identity links failed: %s", exc
                )

        # Reverse map: general's Discord ID → their division
        general_to_div: dict[int, int] = {v: k for k, v in DIVISION_GENERALS.items()}
        all_leadership_role_ids = set(self._leadership_role_ids.values())

        for member in guild.members:
            if member.bot:
                continue

            desired_divs: set[int] = set()

            # Hardcoded general for a division
            if member.id in general_to_div:
                desired_divs.add(general_to_div[member.id])

            in_game_id = discord_to_ingame.get(member.id)
            if in_game_id:
                # MU owner/commander
                desired_divs |= ingame_to_leadership_divs.get(in_game_id, set())
                # Minister of defence → all 5 divisions
                if minister_of_defense_id and in_game_id == minister_of_defense_id:
                    desired_divs |= set(range(1, 6))

            desired_rids = {
                self._leadership_role_ids[d]
                for d in desired_divs
                if d in self._leadership_role_ids
            }
            current_leadership_rids = {
                r.id for r in member.roles if r.id in all_leadership_role_ids
            }

            for rid in desired_rids - current_leadership_rids:
                role = guild.get_role(rid)
                if role:
                    try:
                        await member.add_roles(
                            role, reason="war_guild_divisions: leiderschap sync"
                        )
                        counts["leadership_added"] += 1
                    except discord.Forbidden:
                        pass

            for rid in current_leadership_rids - desired_rids:
                role = guild.get_role(rid)
                if role:
                    try:
                        await member.remove_roles(
                            role, reason="war_guild_divisions: leiderschap sync"
                        )
                        counts["leadership_removed"] += 1
                    except discord.Forbidden:
                        pass

        logger.info("war_guild_divisions: leiderschap sync done — %s", counts)
        return counts

    # ── Category prefix sync ──────────────────────────────────────────────────

    def _overwrite_matches(
        self, overwrite: discord.PermissionOverwrite, desired: dict[str, bool]
    ) -> bool:
        return all(getattr(overwrite, perm) is value for perm, value in desired.items())

    async def _sync_mu_categories(self, guild: discord.Guild) -> dict:
        """Prefix MU category channels with [DN] based on their division;
        repair each category's permissions for that MU's Commander/Member
        roles; and make Commander the de-facto admin of their own category.

        The permission repair matters because Discord automatically strips
        every channel/category permission overwrite for a role the instant
        that role is deleted. Recreating a role with the same name afterwards
        (e.g. war_sync.py re-creating MU roles after they were wiped by the
        API-outage incident) does NOT restore those overwrites — Discord
        treats the new role as a completely unrelated object. _ensure_mu_category
        only sets these overwrites when a category is first CREATED, so an
        existing category whose MU role got recreated was otherwise never
        re-permissioned. This re-applies the desired overwrites every hour,
        via set_permissions per-role (never a full overwrites= replace) so any
        other, unrelated role access manually granted on the category is left
        alone.

        Discord has no per-channel "Administrator" flag, so "commander is
        admin of their category" is approximated with the broadest set of
        channel-scoped permissions Discord actually offers (_COMMANDER_CATEGORY_PERMS
        below) — channel/permission/webhook management, message and thread
        moderation, voice moderation, and @everyone mentions. It deliberately
        excludes guild-wide powers (kick/ban, manage_guild, manage_roles at
        the server level, real Administrator) since those can't be scoped to
        one category anyway.

        Deliberately does NOT sync individual channels to their category
        (channel.edit(sync_permissions=True)) on an ongoing basis: that was
        tried and reverted — running it every hour meant any per-channel
        permission an admin set on purpose (e.g. a stricter sub-channel)
        got silently wiped back to match the category on the very next run.
        A one-off channel resync (for channels left with zero permissions
        right after the incident) should be done as a manual, one-time
        action instead of baked into this recurring task.

        Categories are matched to their MU via a persisted category_id,
        stored in war_mu_roles under a pseudo role_type of "category" (see
        _ensure_mu_category) — this is an ID, so it survives ANY future
        rename of either the category or the MU. A category with no
        persisted link yet (the first run after this existed, or one
        created by hand) is bootstrapped by matching its *current* bare
        name against war_mu_roles' current tracked MU names — the same way
        _ensure_mu_category finds an existing category — and its link is
        persisted immediately so this fallback is only ever needed once per
        category.

        Confirmed as a real gap this closes: after "De Munterij" renamed to
        "De Pomperij" in-game, its Discord category kept its stale
        "De Munterij" name (nothing ever renamed it), which meant
        NAME-based matching could never find it again either — a category
        whose own name has gone stale can't be found by matching on that
        same stale name. Once a category is linked once by ID, that
        deadlock can't recur: a rename never touches the stored ID.
        A category that both has a stale name AND was never linked (e.g.
        this incident, discovered before this fix existed) still needs one
        manual rename to bootstrap the very first link — after that, it's
        permanent.
        """
        counts = {"cats_updated": 0, "perms_repaired": 0}

        # {mu_id: {"commander": role_id, "member": role_id}}
        role_ids_by_mu: dict[str, dict[str, int]] = {}
        # {normalised_mu_name: mu_id} — bootstrap fallback only
        mu_id_by_name: dict[str, str] = {}
        # {mu_id: current mu_name} — for renaming to the CURRENT name, not
        # whatever the category itself still happens to be called
        mu_name_by_id: dict[str, str] = {}
        # {mu_id: persisted category channel id}
        category_id_by_mu: dict[str, int] = {}

        if self._db:
            try:
                rows = await self._db.get_all_war_mu_roles(str(guild.id))
                for row in rows:
                    mu_id = row["mu_id"]
                    if row["role_type"] == "category":
                        # This row's own mu_name is just a snapshot from
                        # whenever the link was made — war_sync.py doesn't
                        # keep it fresh, only the commander/member rows
                        # below. Never let it feed mu_name_by_id/mu_id_by_name
                        # or it can clobber the current name with a stale one.
                        category_id_by_mu[mu_id] = row["discord_role_id"]
                        continue
                    mu_name = row["mu_name"] or ""
                    if mu_name:
                        mu_id_by_name[_norm(mu_name)] = mu_id
                        mu_name_by_id[mu_id] = mu_name
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
            bare = _NICK_PREFIX_RE.sub("", cat.name)
            mu_id = mu_id_by_name.get(_norm(bare))
            if mu_id is None or mu_id in category_id_by_mu:
                continue  # unrelated category, or its real category is elsewhere
            pairs.append((mu_id, cat))
            if self._db:
                try:
                    await self._db.upsert_war_mu_role(
                        mu_id, "category", str(guild.id), cat.id, mu_name_by_id.get(mu_id, bare)
                    )
                    logger.info(
                        "war_guild_divisions: linked category %r to mu_id %s by name (one-time bootstrap)",
                        cat.name, mu_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "war_guild_divisions: failed to persist category link for mu_id %s: %s",
                        mu_id, exc,
                    )

        for mu_id, cat in pairs:
            div = _MU_ID_TO_DIV.get(mu_id)
            if div is None:
                continue

            current_name = mu_name_by_id.get(mu_id) or _NICK_PREFIX_RE.sub("", cat.name)
            expected = f"[D{div}] {current_name}"
            if cat.name != expected:
                try:
                    await cat.edit(
                        name=expected,
                        reason="war_guild_divisions: division category prefix sync",
                    )
                    counts["cats_updated"] += 1
                    logger.info(
                        "war_guild_divisions: renamed category %r → %r", cat.name, expected
                    )
                except discord.Forbidden as exc:
                    logger.warning(
                        "war_guild_divisions: no permission to rename category %r "
                        "(HTTP %s, code %s: %s)",
                        cat.name,
                        getattr(exc, "status", "?"),
                        getattr(exc, "code", "?"),
                        getattr(exc, "text", str(exc)),
                    )

            # ── Permission repair ───────────────────────────────────────────
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

        logger.info("war_guild_divisions: category sync done — %s", counts)
        return counts

    async def _ensure_mu_category(
        self,
        guild: discord.Guild,
        mu_name: str,
        division: int,
        mu_id: str | None,
    ) -> str:
        """Create the MU's Discord category if it doesn't exist yet, prefixed [Dn].

        Grants view access to the MU's commander/member roles (looked up
        from war_mu_roles via *mu_id*, if known) and hides it from @everyone.
        Persists the category's own Discord ID to war_mu_roles under a
        pseudo role_type of "category" so _sync_mu_categories can find this
        exact category by ID forever after, regardless of what either the
        MU or the category get renamed to later — see that method's
        docstring for why matching on name alone isn't enough.
        Returns a short human-readable status string for the /mudivisie reply.
        """
        norm_target = _norm(mu_name)
        expected_name = f"[D{division}] {mu_name}"

        existing = None
        for cat in guild.categories:
            bare = _NICK_PREFIX_RE.sub("", cat.name)
            if _norm(bare) == norm_target:
                existing = cat
                break

        if existing is not None:
            if mu_id and self._db:
                try:
                    await self._db.upsert_war_mu_role(
                        mu_id, "category", str(guild.id), existing.id, mu_name
                    )
                except Exception as exc:
                    logger.warning(
                        "war_guild_divisions: failed to track category id for %s: %s",
                        mu_name, exc,
                    )
            if existing.name == expected_name:
                return f"bestond al ({expected_name})"
            try:
                await existing.edit(
                    name=expected_name,
                    reason="war_guild_divisions: mudivisie categorie-sync",
                )
                return f"bestond al, hernoemd → {expected_name}"
            except discord.Forbidden:
                return f"bestond al ({existing.name}), kon niet hernoemen (rechten)"

        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False)
        }
        if mu_id and self._db:
            # No dedicated "owner" role anymore — owners get Commander+Member.
            # Commander is the de-facto admin of their own category (see
            # _COMMANDER_CATEGORY_PERMS); Member just gets to see it.
            for role_type, perms in (
                ("commander", _COMMANDER_CATEGORY_PERMS),
                ("member", _MEMBER_CATEGORY_PERMS),
            ):
                try:
                    rid = await self._db.get_war_mu_role(
                        mu_id, role_type, str(guild.id)
                    )
                except Exception as exc:
                    logger.warning(
                        "war_guild_divisions: role lookup failed for %s/%s: %s",
                        mu_name, role_type, exc,
                    )
                    continue
                role = guild.get_role(rid) if rid else None
                if role:
                    overwrites[role] = discord.PermissionOverwrite(**perms)

        try:
            new_cat = await guild.create_category(
                expected_name,
                overwrites=overwrites,
                reason=f"war_guild_divisions: nieuwe MU-categorie voor '{mu_name}'",
            )
            if mu_id and self._db:
                try:
                    await self._db.upsert_war_mu_role(
                        mu_id, "category", str(guild.id), new_cat.id, mu_name
                    )
                except Exception as exc:
                    logger.warning(
                        "war_guild_divisions: failed to track new category id for %s: %s",
                        mu_name, exc,
                    )
            logger.info(
                "war_guild_divisions: created category %r for MU %r",
                expected_name, mu_name,
            )
            return f"aangemaakt → {expected_name}"
        except discord.Forbidden:
            return "geen rechten om categorie aan te maken"
        except Exception as exc:
            logger.warning(
                "war_guild_divisions: category creation failed for %s: %s",
                mu_name, exc,
            )
            return f"mislukt ({exc})"

    # ── Government sync ───────────────────────────────────────────────────────

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

        # The API returns direct string user-IDs, e.g.:
        #   {"president": "abc123", "vicePresident": "def456",
        #    "minOfDefense": "...", "minOfEconomy": "...", "minOfForeignAffairs": "..."}
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
        _add_gov(
            data.get("vicePresident") or data.get("vice_president"),
            "vice_president",
        )
        _add_gov(
            data.get("minOfDefense") or data.get("ministerOfDefense"),
            "minister_of_defense",
        )
        _add_gov(
            data.get("minOfEconomy") or data.get("ministerOfEconomy"),
            "minister_of_economy",
        )
        _add_gov(
            data.get("minOfForeignAffairs") or data.get("ministerOfForeignAffairs"),
            "minister_of_foreign_affairs",
        )

        logger.info(
            "war_guild_divisions: government response — president=%s, vp=%s, "
            "def=%s, eco=%s, fa=%s",
            data.get("president"), data.get("vicePresident"),
            data.get("minOfDefense"), data.get("minOfEconomy"),
            data.get("minOfForeignAffairs"),
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
            sum(len(v) for v in ingame_to_gov_keys.values()),
            len(discord_to_ingame),
        )

        all_gov_role_ids = set(self._gov_role_ids.values())

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
        """Hourly sync: division roles, nicks, government roles, category prefixes."""
        guild = self._war_guild
        if not guild:
            logger.warning(
                "war_guild_divisions: war guild not found, skipping sync"
            )
            return

        # Ensure all Discord roles exist before syncing members
        try:
            await self._ensure_division_roles(guild)
            await self._ensure_gov_roles(guild)
            await self._ensure_leadership_roles(guild)
        except Exception as exc:
            logger.error("war_guild_divisions: role setup failed: %s", exc)
            return

        # citizen_mu_membership is kept fresh by war_sync's own hourly/daily tasks;
        # no need to trigger a full API scan here on every run.

        try:
            await self._sync_divisions(guild)
        except Exception as exc:
            logger.error("war_guild_divisions: division sync failed: %s", exc)

        try:
            await self._sync_government(guild)
        except Exception as exc:
            logger.error("war_guild_divisions: government sync failed: %s", exc)

        try:
            await self._sync_leadership_roles(guild)
        except Exception as exc:
            logger.error("war_guild_divisions: leiderschap sync failed: %s", exc)

        try:
            await self._sync_mu_categories(guild)
        except Exception as exc:
            logger.error("war_guild_divisions: category sync failed: %s", exc)

    @daily_sync.before_loop
    async def _before_daily_sync(self) -> None:
        await self._wait_for_services()
        await self._load_division_overrides()
        # Initialise roles immediately at startup so they are available right away
        guild = self._war_guild
        if guild:
            try:
                await self._ensure_division_roles(guild)
                await self._ensure_gov_roles(guild)
                await self._ensure_leadership_roles(guild)
            except Exception as exc:
                logger.warning(
                    "war_guild_divisions: startup role setup failed: %s", exc
                )

    # ── Slash command ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="syncdivisions",
        description="Synchroniseer divisie-rollen en [DN]-nicks (+ regering-rollen).",
    )
    async def syncdivisions(self, interaction: discord.Interaction) -> None:
        """Manually trigger a full division + government sync."""
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
            await self._ensure_division_roles(guild)
            await self._ensure_gov_roles(guild)
            await self._ensure_leadership_roles(guild)
            div_counts = await self._sync_divisions(guild)
            gov_counts = await self._sync_government(guild)
            leadership_counts = await self._sync_leadership_roles(guild)
            cat_counts = await self._sync_mu_categories(guild)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Synchronisatie mislukt: {exc}", ephemeral=True
            )
            return

        no_link_members = div_counts.get("no_link_members", [])
        no_div_members = div_counts.get("no_division_members", [])

        await interaction.followup.send(
            f"✅ Divisie-synchronisatie voltooid.\n"
            f"• Divisie-rollen toegevoegd: {div_counts.get('roles_added', 0)}, "
            f"verwijderd: {div_counts.get('roles_removed', 0)}\n"
            f"• Nicks bijgewerkt: {div_counts.get('nicks_updated', 0)}"
            + (
                f", niet toegestaan (hogere rol dan bot): {div_counts['nicks_forbidden']}"
                if div_counts.get("nicks_forbidden")
                else ""
            )
            + f"\n• Regering-rollen toegevoegd: {gov_counts.get('gov_added', 0)}, "
            f"verwijderd: {gov_counts.get('gov_removed', 0)}\n"
            f"• Leiderschap-rollen toegevoegd: {leadership_counts.get('leadership_added', 0)}, "
            f"verwijderd: {leadership_counts.get('leadership_removed', 0)}\n"
            f"• Categorieën hernoemd: {cat_counts.get('cats_updated', 0)}, "
            f"permissies hersteld: {cat_counts.get('perms_repaired', 0)}\n"
            f"• Leden zonder gekoppeld in-game account: {div_counts.get('no_link', 0)}"
            + (
                "\n  → " + ", ".join(no_link_members)
                if no_link_members
                else ""
            )
            + f"\n• Leden met account maar geen divisie-MU: {div_counts.get('no_division', 0)}"
            + (
                "\n  → " + ", ".join(no_div_members)
                if no_div_members
                else ""
            )
            + (
                "\n⚠️ Onbekende MU-namen (staan niet in DIVISION_MUS): "
                + ", ".join(sorted(div_counts.get("unmatched_mus", set())))
                if div_counts.get("unmatched_mus")
                else ""
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="mudivisie",
        description="Voeg een MU toe aan, verplaats binnen, of verwijder uit de divisie-indeling.",
    )
    @app_commands.describe(
        actie="Wat wil je doen?",
        mu_naam="Naam van de Military Unit (exact zoals in-game)",
        divisie="Doeldivisie (1-5) — verplicht bij toevoegen/verplaatsen",
    )
    @app_commands.choices(
        actie=[
            app_commands.Choice(name="Toevoegen", value="add"),
            app_commands.Choice(name="Verplaatsen", value="move"),
            app_commands.Choice(name="Verwijderen", value="remove"),
        ]
    )
    @app_commands.autocomplete(mu_naam=_mu_naam_autocomplete)
    async def mudivisie(
        self,
        interaction: discord.Interaction,
        actie: app_commands.Choice[str],
        mu_naam: str,
        divisie: Optional[app_commands.Range[int, 1, 5]] = None,
    ) -> None:
        """Add/move/remove a MU in DIVISION_MUS, then wire up its roles + category."""
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

        if actie.value in ("add", "move") and divisie is None:
            await interaction.response.send_message(
                "❌ Geef een divisie (1-5) op bij toevoegen/verplaatsen.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild = self._war_guild
        if not guild:
            await interaction.followup.send("❌ War-guild niet gevonden.", ephemeral=True)
            return

        # Resolve mu_id + canonical name via the known_mus registry — mu_id is
        # what division membership is actually keyed on now (see
        # DIVISION_MU_IDS), so this command can't do anything without it.
        canonical_name = mu_naam
        mu_id: Optional[str] = None
        if self._db:
            try:
                found_id, found_name = await self._db.get_known_mu_by_name(mu_naam)
                if found_id:
                    mu_id, canonical_name = found_id, found_name
            except Exception as exc:
                logger.warning("mudivisie: known_mus lookup failed for %r: %s", mu_naam, exc)

        if not mu_id:
            await interaction.followup.send(
                f"❌ Kon geen MU-ID vinden voor **{mu_naam}**. Gebruik het autocomplete-menu "
                "voor een geldige naam.",
                ephemeral=True,
            )
            return

        old_div = _MU_ID_TO_DIV.get(mu_id)

        if actie.value == "remove":
            if old_div is None:
                await interaction.followup.send(
                    f"⚠️ **{canonical_name}** zit niet in een divisie.", ephemeral=True
                )
                return
            _apply_division_override(mu_id, canonical_name, 0)
            if self._db:
                try:
                    await self._db.upsert_division_mu_override(mu_id, canonical_name, 0)
                except Exception as exc:
                    logger.warning("mudivisie: failed to persist removal: %s", exc)
            await interaction.followup.send(
                f"✅ **{canonical_name}** verwijderd uit Divisie {old_div}.",
                ephemeral=True,
            )
            return

        # add / move (divisie is guaranteed non-None here, checked above)
        new_div = divisie
        _apply_division_override(mu_id, canonical_name, new_div)
        if self._db:
            try:
                await self._db.upsert_division_mu_override(mu_id, canonical_name, new_div)
            except Exception as exc:
                logger.warning("mudivisie: failed to persist override: %s", exc)

        lines = [
            f"✅ **{canonical_name}** "
            + (
                f"toegevoegd aan Divisie {new_div}."
                if old_div is None
                else f"verplaatst van Divisie {old_div} naar Divisie {new_div}."
            )
        ]

        # Ensure the MU's Discord roles (Owner/Commander/Member) exist — role
        # creation is driven independently by the live game API, not by
        # DIVISION_MUS, so re-running the MU scan picks up this MU too.
        war_sync_cog = self.bot.cogs.get("war_sync")
        if war_sync_cog and hasattr(war_sync_cog, "scan_dutch_mus"):
            try:
                await war_sync_cog.scan_dutch_mus()
                role_status = "gecontroleerd/aangemaakt via MU-scan"
            except Exception as exc:
                role_status = f"scan mislukt: {exc}"
                logger.warning("mudivisie: scan_dutch_mus failed: %s", exc)
        else:
            role_status = "niet gecontroleerd (war_sync cog niet geladen)"
        lines.append(f"• MU-rollen (Owner/Commander/Member): {role_status}")

        # Ensure a [Dn]-prefixed category exists for this MU.
        cat_status = await self._ensure_mu_category(guild, canonical_name, new_div, mu_id)
        lines.append(f"• Discord-categorie: {cat_status}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    if not bot.config.get("war_guild"):
        return
    await bot.add_cog(WarGuildDivisionsCog(bot))
