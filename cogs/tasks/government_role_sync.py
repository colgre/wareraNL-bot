"""Production-guild government role sync.

Mirrors cogs/tasks/war_guild_divisions.py's _sync_government for the main
production guild (1401530718223335496) instead of the war guild. Kept as
a separate cog rather than generalizing war_guild_divisions.py itself,
since that file is otherwise entirely war-guild-specific (division roles,
@d1-d5, MU leadership roles) and is gated behind config["war_guild"],
which the production config doesn't have.

Reads /government.getByCountryId for NL and keeps Discord roles for
President, Vice-President, and each minister title in sync with whoever
actually holds that in-game cabinet seat. President/Vice-President/
Minister of Foreign Affairs reuse the role IDs already tracked in
config["roles"] (so this doesn't create duplicate roles for positions
admins already set up); every other fixed ministry is auto-created by
name if missing. "advisor" is a flexible 0-N seat with a president-chosen
customTitle (freeform, changes over time), so it gets a
dynamically-named/reused role per appointee instead of one shared role —
same approach as war_guild_divisions.py.
"""

from __future__ import annotations

import json
import logging

import discord
from discord.ext import commands, tasks

from ._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_GUILD_ID = 1401530718223335496

# config["roles"] key -> government-sync key, for the 3 slots whose Discord
# role already exists and is tracked in config.json (reused by ID, not
# recreated by name).
_CONFIG_ROLE_KEYS: dict[str, str] = {
    "president": "president",
    "vice_president": "vice_president",
    "minister_foreign_affairs": "minister_of_foreign_affairs",
}

# Fixed ministry slots with no existing config-tracked role — auto-created
# by name if missing, same naming convention as the war guild's roles.
_AUTO_CREATE_ROLE_NAMES: dict[str, str] = {
    "minister_of_defense": "Min. Defensie",
    "minister_of_economy": "Min. Economie",
    "minister_of_interior": "Min. Binnenlandse Zaken",
    "minister_of_communication": "Min. Communicatie",
    "chief_of_staff": "Stafchef",
}

_MINISTER_TITLE_TO_GOV_KEY: dict[str, str] = {
    "minOfDefense": "minister_of_defense",
    "minOfEconomy": "minister_of_economy",
    "minOfForeignAffairs": "minister_of_foreign_affairs",
    "minOfInterior": "minister_of_interior",
    "minOfCommunication": "minister_of_communication",
    "chiefOfStaff": "chief_of_staff",
}

_DEFAULT_ADVISOR_ROLE_NAME = "Adviseur"
_ADVISOR_ROLE_MAP_POLL_KEY = "gov_advisor_role_map"


def _unwrap_trpc(resp: dict) -> object:
    """Unwrap ``{"result": {"data": ...}}`` tRPC response envelope."""
    try:
        return resp["result"]["data"]
    except (KeyError, TypeError):
        return resp


class GovernmentRoleSyncCog(TaskCogBase, name="government_role_sync"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._gov_role_ids: dict[str, int] = {}

    def cog_load(self) -> None:
        self.hourly_sync.start()

    def cog_unload(self) -> None:
        self.hourly_sync.cancel()

    @property
    def _guild(self) -> discord.Guild | None:
        return self.bot.get_guild(_GUILD_ID)

    async def _ensure_gov_roles(self, guild: discord.Guild) -> None:
        """Resolve the 3 config-tracked roles and create the rest if missing."""
        configured_roles = self.bot.config.get("roles") or {}
        for config_key, gov_key in _CONFIG_ROLE_KEYS.items():
            role_id = configured_roles.get(config_key)
            role = guild.get_role(int(role_id)) if role_id else None
            if role is None:
                logger.warning(
                    "government_role_sync: configured role %r (id=%s) not found in guild",
                    config_key, role_id,
                )
                continue
            self._gov_role_ids[gov_key] = role.id

        existing_by_name = {r.name: r for r in guild.roles}
        for gov_key, name in _AUTO_CREATE_ROLE_NAMES.items():
            role = existing_by_name.get(name)
            if role is None:
                try:
                    role = await guild.create_role(
                        name=name,
                        mentionable=True,
                        reason="government_role_sync: auto-create government role",
                    )
                    logger.info(
                        "government_role_sync: created gov role %r (id=%d)", name, role.id
                    )
                except discord.Forbidden:
                    logger.warning(
                        "government_role_sync: no permission to create role %r", name
                    )
                    continue
            self._gov_role_ids[gov_key] = role.id

    async def _ensure_advisor_roles(
        self, guild: discord.Guild, advisors: list[tuple[str, str | None]]
    ) -> tuple[dict[str, int], set[int]]:
        """Assign a dynamically-named Discord role to each current advisor.

        Same reuse-by-renaming strategy as war_guild_divisions.py's
        _ensure_advisor_roles: advisor custom titles are the president's
        free-text choice and change over time, so an existing role is
        renamed in place rather than recreated, and a role freed up by
        someone leaving the advisor slot is reused for the next advisor.
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
                        reason="government_role_sync: auto-create advisor role",
                    )
                    logger.info(
                        "government_role_sync: created advisor role %r (id=%d) for %s",
                        desired_name, role.id, uid,
                    )
                except discord.Forbidden:
                    logger.warning(
                        "government_role_sync: no permission to create advisor role for %s", uid
                    )
                    continue
            elif role.name != desired_name:
                try:
                    await role.edit(
                        name=desired_name, reason="government_role_sync: advisor title sync"
                    )
                except discord.Forbidden:
                    logger.warning(
                        "government_role_sync: no permission to rename advisor role %r", role.name
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
            logger.warning(
                "government_role_sync: failed to persist advisor role map: %s", exc
            )

        return result, set(next_map.values())

    async def _sync_government(self, guild: discord.Guild) -> dict:
        """Fetch the current government from the API and sync guild roles."""
        counts = {"gov_added": 0, "gov_removed": 0}

        if not self._gov_role_ids:
            return counts
        if not self._client:
            logger.warning("government_role_sync: API client unavailable, skipping sync")
            return counts

        nl_country_id = self.bot.config.get("nl_country_id", "")
        try:
            raw = await self._client.get(
                "/government.getByCountryId",
                params={"input": json.dumps({"countryId": nl_country_id})},
            )
            data = _unwrap_trpc(raw)
        except Exception as exc:
            logger.warning("government_role_sync: government API failed: %s", exc)
            return counts

        if not isinstance(data, dict):
            logger.warning(
                "government_role_sync: unexpected government response type: %r", type(data)
            )
            return counts

        def _str_id(val: object) -> str | None:
            return val if isinstance(val, str) and val else None

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
            "government_role_sync: government response — president=%s, vp=%s, "
            "%d fixed-slot ministers, %d advisors",
            data.get("president"), data.get("vicePresident"),
            n_fixed_ministers, len(advisor_entries),
        )

        discord_to_ingame: dict[int, str] = {}
        if self._db:
            try:
                for link in await self._db.get_all_identity_links():
                    discord_to_ingame[int(link["discord_user_id"])] = link["in_game_user_id"]
            except Exception as exc:
                logger.warning(
                    "government_role_sync: failed to load identity links: %s", exc
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
                        await member.add_roles(role, reason="government_role_sync: hourly sync")
                        counts["gov_added"] += 1
                    except discord.Forbidden:
                        logger.warning(
                            "government_role_sync: no permission to add %s to %s",
                            role.name, member.name,
                        )

            for rid in to_remove:
                role = guild.get_role(rid)
                if role:
                    try:
                        await member.remove_roles(role, reason="government_role_sync: hourly sync")
                        counts["gov_removed"] += 1
                    except discord.Forbidden:
                        logger.warning(
                            "government_role_sync: no permission to remove %s from %s",
                            role.name, member.name,
                        )

        logger.info("government_role_sync: sync done — %s", counts)
        return counts

    @tasks.loop(hours=1)
    async def hourly_sync(self) -> None:
        guild = self._guild
        if not guild:
            logger.warning("government_role_sync: production guild %d not found", _GUILD_ID)
            return
        try:
            await self._ensure_gov_roles(guild)
        except Exception:
            logger.exception("government_role_sync: role setup failed")
            return
        try:
            await self._sync_government(guild)
        except Exception:
            logger.exception("government_role_sync: sync failed")

    @hourly_sync.before_loop
    async def _before_hourly_sync(self) -> None:
        await self._wait_for_services()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GovernmentRoleSyncCog(bot))
