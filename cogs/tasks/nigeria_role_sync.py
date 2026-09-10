"""Background task: sync country-specific roles to NL players temporarily abroad.

Every 6 hours, iterates all members of the production guild who have the
Netherlands role and adds or removes country roles based on in-game country.

Supported countries:
  - Nigeria           -> role 1530164551163842611
  - Luxembourg        -> role 1531298705892835498
  - Equatorial Guinea -> role 1547651313397928006

Lookup strategy (in order):
  1. Refresh the target countries live from the WarEra API into citizen_levels
  2. identity_links (all guilds) -> in-game user ID -> citizen_levels by user ID
  3. Fallback: citizen_levels by Discord display name (for members without a link)

Role assignment:
  - country_id matches AND logged in within 3 days -> keep/add role
  - country_id doesn't match, inactive, or not found in citizen_levels -> remove role

Safety:
  - If a target country refresh fails, skip syncing that country instead of
    removing roles based on stale cache data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_INACTIVE_THRESHOLD = timedelta(days=3)

# country_id -> Discord role ID
_COUNTRY_ROLES: dict[str, int] = {
    "683ddd2c24b5a2e114af15fa": 1530164551163842611,  # Nigeria
    "6813b6d446e731854c7ac7fb": 1531298705892835498,  # Luxembourg
    "6873d0ea1758b40e712b5f31": 1547651313397928006,  # Equatorial Guinea
}

_COUNTRY_NAMES: dict[str, str] = {
    "683ddd2c24b5a2e114af15fa": "Nigeria",
    "6813b6d446e731854c7ac7fb": "Luxemburg",
    "6873d0ea1758b40e712b5f31": "Equatoriaal-Guinea",
}


def _is_active_in_country(
    entry: tuple[str, str | None] | None,
    country_id: str,
    now: datetime,
) -> bool:
    """Return True iff the citizen_levels entry shows *country_id* + recent login."""
    if entry is None:
        return False
    entry_country, last_login_at = entry
    if entry_country != country_id:
        return False
    if not last_login_at:
        return False
    try:
        login_dt = datetime.fromisoformat(last_login_at.replace("Z", "+00:00"))
        return (now - login_dt) < _INACTIVE_THRESHOLD
    except Exception:
        return False


class NigeriaRoleSyncCog(TaskCogBase, name="nigeria_role_sync"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.nigeria_role_sync.start()

    def cog_unload(self) -> None:
        self.nigeria_role_sync.cancel()

    @tasks.loop(hours=6)
    async def nigeria_role_sync(self) -> None:
        try:
            await self._run_sync()
        except Exception:
            logger.exception("nigeria_role_sync: unexpected error")

    @nigeria_role_sync.before_loop
    async def _before(self) -> None:
        await self._wait_for_services()

    async def _refresh_target_countries(self, country_ids: list[str]) -> set[str]:
        """Refresh target countries live before making any sync decisions."""
        if not self._citizen_cache:
            logger.warning(
                "nigeria_role_sync: citizen cache unavailable; skipping sync to avoid stale removals"
            )
            return set()

        refreshed: set[str] = set()
        lock = self._heavy_api_lock

        async def _refresh_one(country_id: str) -> None:
            country_name = _COUNTRY_NAMES.get(country_id, country_id)
            await self._citizen_cache.refresh_country(country_id, country_name)
            refreshed.add(country_id)
            logger.info(
                "nigeria_role_sync: refreshed %s before role sync",
                country_name,
            )

        if lock:
            async with lock:
                for country_id in country_ids:
                    try:
                        await _refresh_one(country_id)
                    except Exception:
                        logger.exception(
                            "nigeria_role_sync: failed to refresh %s before sync; skipping that country",
                            _COUNTRY_NAMES.get(country_id, country_id),
                        )
        else:
            for country_id in country_ids:
                try:
                    await _refresh_one(country_id)
                except Exception:
                    logger.exception(
                        "nigeria_role_sync: failed to refresh %s before sync; skipping that country",
                        _COUNTRY_NAMES.get(country_id, country_id),
                    )

        return refreshed

    async def _run_sync(self) -> dict[str, tuple[int, int, int, int]]:
        """Sync country roles for all NL members.

        Returns {country_id: (added, removed, no_data_removed, already_correct)}.
        """
        guild_id = int(self.config.get("guild_id") or 0)
        nl_role_id = int((self.config.get("roles") or {}).get("nederlander") or 0)
        if not guild_id or not nl_role_id:
            logger.warning(
                "nigeria_role_sync: guild_id or roles.nederlander not configured"
            )
            return {}

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning(
                "nigeria_role_sync: production guild %d not in cache", guild_id
            )
            return {}

        nl_role = guild.get_role(nl_role_id)
        if nl_role is None:
            logger.warning("nigeria_role_sync: Netherlands role not found in guild")
            return {}

        # Resolve Discord role objects; skip any that don't exist in this guild.
        country_discord_roles: dict[str, discord.Role] = {}
        for country_id, role_id in _COUNTRY_ROLES.items():
            role = guild.get_role(role_id)
            if role is None:
                logger.warning(
                    "nigeria_role_sync: role %d for country %s not found in guild",
                    role_id,
                    country_id,
                )
            else:
                country_discord_roles[country_id] = role

        if not country_discord_roles:
            return {}

        refreshed_country_ids = await self._refresh_target_countries(
            list(country_discord_roles.keys())
        )
        if not refreshed_country_ids:
            logger.warning(
                "nigeria_role_sync: no target countries refreshed successfully; aborting sync"
            )
            return {}

        skipped_country_ids = set(country_discord_roles) - refreshed_country_ids
        for country_id in skipped_country_ids:
            logger.warning(
                "nigeria_role_sync: skipping %s because the live refresh failed",
                _COUNTRY_NAMES.get(country_id, country_id),
            )

        nl_members = [m for m in nl_role.members if not m.bot]
        if not nl_members or not self._db:
            return {}

        # Build lookup data from the freshly refreshed citizen cache.
        discord_to_ingame: dict[str, str] = await self._db.get_discord_to_ingame_map()

        mapped_ids: list[str] = []
        unmapped_nicks: list[str] = []
        for member in nl_members:
            ingame_id = discord_to_ingame.get(str(member.id))
            if ingame_id:
                mapped_ids.append(ingame_id)
            else:
                unmapped_nicks.append((member.nick or member.name).lower())

        id_country_map = await self._db.get_citizen_countries_by_ids(mapped_ids)
        name_country_map = await self._db.get_citizen_countries_by_names(unmapped_nicks)

        now = datetime.now(timezone.utc)
        results: dict[str, list[int]] = {
            cid: [0, 0, 0, 0] for cid in refreshed_country_ids
        }

        for member in nl_members:
            ingame_id = discord_to_ingame.get(str(member.id))
            if ingame_id:
                entry = id_country_map.get(ingame_id)
            else:
                nick = (member.nick or member.name).lower()
                entry = name_country_map.get(nick)

            for country_id, role in country_discord_roles.items():
                if country_id not in refreshed_country_ids:
                    continue

                should_have = _is_active_in_country(entry, country_id, now)
                has_role = role in member.roles

                if should_have == has_role:
                    results[country_id][3] += 1
                    continue

                if should_have:
                    try:
                        await member.add_roles(
                            role,
                            reason=f"country_role_sync: NL speler actief in {country_id}",
                        )
                        results[country_id][0] += 1
                        logger.info(
                            "country_role_sync: added role %s to %s (%d)",
                            role.name,
                            member,
                            member.id,
                        )
                    except discord.Forbidden:
                        logger.warning(
                            "country_role_sync: no permission to add %s to %s",
                            role.name,
                            member,
                        )
                else:
                    reason = (
                        f"country_role_sync: NL speler niet meer actief in {country_id}"
                        if entry is not None
                        else "country_role_sync: geen in-game data gevonden, rol verwijderd"
                    )
                    try:
                        await member.remove_roles(role, reason=reason)
                        if entry is None:
                            results[country_id][2] += 1
                        else:
                            results[country_id][1] += 1
                        logger.info(
                            "country_role_sync: removed role %s from %s (%d) - %s",
                            role.name,
                            member,
                            member.id,
                            "no data" if entry is None else f"country={entry[0]}",
                        )
                    except discord.Forbidden:
                        logger.warning(
                            "country_role_sync: no permission to remove %s from %s",
                            role.name,
                            member,
                        )

        for country_id, (added, removed, no_data, correct) in {
            cid: tuple(v) for cid, v in results.items()
        }.items():
            role = country_discord_roles[country_id]
            logger.info(
                "country_role_sync [%s/%s]: done - %d added, %d removed, %d removed (no data), %d already correct",
                role.name,
                country_id,
                added,
                removed,
                no_data,
                correct,
            )

        return {cid: tuple(v) for cid, v in results.items()}

    @commands.command(name="sync_nigeria_roles", hidden=True)
    @commands.is_owner()
    async def sync_nigeria_roles(self, ctx: commands.Context) -> None:
        """Immediately run the country role sync."""
        results = await self._run_sync()
        lines = []
        for country_id, (added, removed, no_data, correct) in results.items():
            name = _COUNTRY_NAMES.get(country_id, country_id)
            lines.append(
                f"**{name}**: {added} toegevoegd, {removed} verwijderd (land/inactief), "
                f"{no_data} verwijderd (geen data), {correct} al correct"
            )
        await ctx.send(
            "✅ Country role sync klaar:\n" + "\n".join(lines)
            if lines
            else "✅ Geen rollen gesynchroniseerd."
        )


async def setup(bot) -> None:
    await bot.add_cog(NigeriaRoleSyncCog(bot))
