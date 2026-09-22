"""Background tasks: citizen level cache refresh (NL every hour, all countries every 6 h)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.tasks._base import TaskCogBase
from cogs.tasks.war_guild_divisions import DIVISION_MUS
from services.country_utils import country_id as cid_of
from services.country_utils import extract_country_list
from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")

# Full all-countries sweep at most once every N hours.
_ALL_COUNTRIES_INTERVAL_H = 6
_MU_ROLE_SYNC_INTERVAL_H = 24
_PILL_BUFF_CODE = "cocain"

# Channel / role IDs for level-5 notifications.
_LVL5_PROD_CHANNEL_ID  = 1500927730517147890
_LVL5_PROD_ROLE_ID     = 1500453028392861746
_LVL5_TEST_CHANNEL_ID  = 1474452856584011929
_LVL5_TEST_ROLE_ID     = 1494431123391119531


class CitizenTasks(TaskCogBase, name="citizen_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.citizen_refresh.start()

    def cog_unload(self) -> None:
        self.citizen_refresh.cancel()

    # ------------------------------------------------------------------ #
    # Merged hourly citizen refresh (NL every tick; all countries every 6 h)
    # ------------------------------------------------------------------ #

    @tasks.loop(hours=1)
    async def citizen_refresh(self):
        if not self._client or not self._db or not self._citizen_cache:
            return

        nl_country_id = self.config.get("nl_country_id")

        # ── Always refresh NL ──────────────────────────────────────────
        if nl_country_id:
            await self._do_nl_refresh(nl_country_id)

        # ── All-countries sweep (guarded by 6-hour cooldown) ──────────
        now_utc = datetime.now(timezone.utc)
        run_full_sweep = True

        # The standalone data-fetcher container owns the all-countries sweep.
        # Running it here too means two processes doing hundreds of thousands of
        # writes to citizen_levels at once, which is the main source of
        # "database is locked" and of unbounded WAL growth.  Timing-based
        # coordination is unreliable (whichever process starts first wins the
        # race), so this is an explicit switch.  Set to true only when no
        # data-fetcher is deployed.
        if not self.config.get("enable_all_countries_sweep", False):
            logger.info(
                "citizen_refresh: skipping full sweep — data-fetcher owns it "
                "(set enable_all_countries_sweep=true to run it here)"
            )
            run_full_sweep = False

        if run_full_sweep:
            try:
                last_run_str = await self._db.get_poll_state("citizen_refresh_last_run")
                if last_run_str:
                    elapsed_h = (
                        now_utc - datetime.fromisoformat(last_run_str)
                    ).total_seconds() / 3600
                    if elapsed_h < _ALL_COUNTRIES_INTERVAL_H:
                        logger.info(
                            "citizen_refresh: skipping full sweep — last run %.1fh ago (< %dh)",
                            elapsed_h,
                            _ALL_COUNTRIES_INTERVAL_H,
                        )
                        run_full_sweep = False
            except Exception:
                logger.exception("citizen_refresh: failed to read last-run state")

        if run_full_sweep:
            await self._do_all_countries_refresh(now_utc)

        try:
            await self._maybe_sync_mu_roles_daily(now_utc)
        except (
            aiosqlite.Error,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            discord.HTTPException,
            discord.Forbidden,
            discord.NotFound,
        ):
            logger.exception("citizen_refresh: daily MU role sync failed")

        try:
            await self._do_level5_notify()
        except Exception:
            logger.exception("citizen_refresh: hourly level-5 scan failed")

    @citizen_refresh.before_loop
    async def before_citizen_refresh(self):
        await self._wait_for_services()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    async def _do_nl_refresh(self, nl_country_id: str) -> None:
        """Refresh citizen level cache for NL only."""
        logger.info("citizen_refresh: refreshing NL citizens")
        # Look up the display name, but don't let a failed getAllCountries call
        # abort the entire NL refresh — fall back to "Netherlands" if needed.
        name = "Netherlands"
        try:
            all_countries = await self._client.get("/country.getAllCountries")
            country_list = extract_country_list(all_countries)
            nl_country = next(
                (c for c in country_list if cid_of(c) == nl_country_id), None
            )
            if nl_country:
                name = nl_country.get("name", name)
        except Exception:
            logger.warning("citizen_refresh: could not fetch country list, using fallback name '%s'", name)
        try:
            async with self._heavy_api_lock:
                await self._citizen_cache.refresh_country(nl_country_id, name)
            logger.info("citizen_refresh: NL done")
        except Exception:
            logger.exception("citizen_refresh: NL refresh failed")
            return

        try:
            await self._sync_discord_nicknames_for_country(nl_country_id, name)
        except Exception:
            logger.exception("citizen_refresh: NL nickname sync failed")

        # Refresh MU memberships from the authoritative MU member lists so that
        # citizens who recently joined/changed MUs are correctly tagged.
        try:
            mu_entries: list[tuple[str, str]] = []
            for mu_name in [n for names in DIVISION_MUS.values() for n in names]:
                mu_id, effective_name = await self._db.get_known_mu_by_name(mu_name)
                if mu_id:
                    mu_entries.append((mu_id, effective_name or mu_name))
            if mu_entries:
                updated = await self._citizen_cache.refresh_mu_memberships(nl_country_id, mu_entries)
                logger.info("citizen_refresh: NL MU memberships refreshed (%d updated)", updated)
            else:
                logger.warning(
                    "citizen_refresh: no division MU IDs found in known_mus, "
                    "skipping MU membership refresh"
                )
        except Exception:
            logger.exception("citizen_refresh: NL MU membership refresh failed")

        try:
            await self._do_nl_pill_scan(nl_country_id)
        except Exception:
            logger.exception("citizen_refresh: NL pill scan failed")

    async def _do_nl_pill_scan(self, nl_country_id: str) -> None:
        """Batch-fetch getUserLite for all NL citizens and persist pill buff expiry.

        Only overwrites buff_expires_at when an active buff is found; existing
        values (for debuff detection) are preserved for players with no current buff.
        """
        if not self._client or not self._db:
            return
        try:
            user_rows = await self._db.get_citizens_in_country(nl_country_id)
        except Exception:
            logger.exception("pill_scan: failed to fetch NL citizen IDs")
            return

        user_ids = [row[0] for row in user_rows]
        if not user_ids:
            return

        logger.info("pill_scan: scanning %d NL citizens", len(user_ids))
        try:
            results = await self._client.batch_get(
                "user.getUserLite",
                [{"userId": uid} for uid in user_ids],
            )
        except Exception:
            logger.exception("pill_scan: batch API call failed")
            return

        import time as _time
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        now_ts = int(_time.time())
        updated_at = datetime.now(timezone.utc).isoformat()

        tracking_rows: list[tuple] = []
        for uid, data in zip(user_ids, results):
            if not isinstance(data, dict):
                tracking_rows.append((uid, nl_country_id, None, updated_at))
                continue
            buffs = data.get("buffs") or {}
            buff_codes: list = buffs.get("buffCodes") or []
            buff_end_at: str | None = buffs.get("buffEndAt")
            expires_at: int | None = None
            if _PILL_BUFF_CODE in buff_codes and buff_end_at:
                try:
                    ts = _dt.fromisoformat(buff_end_at.replace("Z", "+00:00")).timestamp()
                    expires_at = int(ts)
                except Exception:
                    pass
            tracking_rows.append((uid, nl_country_id, expires_at, updated_at))

        try:
            await self._db.bulk_upsert_pill_tracking(tracking_rows)
        except Exception:
            logger.exception("pill_scan: failed to persist tracking data")
            return

        active = sum(1 for _, _, exp, _ in tracking_rows if exp is not None and exp > now_ts)
        logger.info("pill_scan: done — %d/%d with active buff", active, len(user_ids))

    async def _do_all_countries_refresh(self, now_utc: datetime) -> None:
        """Refresh citizen level cache for all countries."""
        logger.info("citizen_refresh: starting full country sweep")

        # Persist start time first so a mid-sweep crash doesn't cause instant retry.
        try:
            await self._db.set_poll_state(
                "citizen_refresh_last_run", now_utc.isoformat()
            )
        except Exception:
            logger.exception("citizen_refresh: failed to save last-run state")

        try:
            all_countries = await self._client.get("/country.getAllCountries")
        except Exception:
            logger.exception("citizen_refresh: failed to fetch countries")
            return

        country_list = extract_country_list(all_countries)
        total = len(country_list)
        for i, country in enumerate(country_list, 1):
            cid = cid_of(country)
            name = country.get("name", cid)
            logger.info("citizen_refresh: (%d/%d) %s", i, total, name)
            try:
                await self._citizen_cache.refresh_country(cid, name)
            except Exception:
                logger.exception("citizen_refresh: error refreshing %s", name)
                continue

            try:
                await self._sync_discord_nicknames_for_country(cid, name)
            except Exception:
                logger.exception("citizen_refresh: nickname sync error for %s", name)
        logger.info("citizen_refresh: full sweep complete (%d countries)", total)

    def _mus_template_path(self) -> Path:
        testing = bool(getattr(self.bot, "testing", False))
        return Path("templates/mus.testing.json" if testing else "templates/mus.json")

    @staticmethod
    def _mu_role_map_from_template(data: dict) -> dict[str, int]:
        mapping: dict[str, int] = {}
        for entry in data.get("embeds", []):
            if not isinstance(entry, dict):
                continue
            mu_id = str(entry.get("id") or "").strip()
            if not mu_id:
                continue
            try:
                role_id = int(entry.get("role_id") or 0)
            except (TypeError, ValueError):
                role_id = 0
            if role_id > 0:
                mapping[mu_id] = role_id
        return mapping

    async def _maybe_sync_mu_roles_daily(self, now_utc: datetime) -> None:
        """Run MU role sync at most once per day, after citizen refresh."""
        if not self._db:
            return

        try:
            last_run_str = await self._db.get_poll_state("mu_role_sync_last_run")
            if last_run_str:
                elapsed_h = (
                    now_utc - datetime.fromisoformat(last_run_str)
                ).total_seconds() / 3600
                if elapsed_h < _MU_ROLE_SYNC_INTERVAL_H:
                    logger.info(
                        "mu_role_sync: skipping daily run — last run %.1fh ago (< %dh)",
                        elapsed_h,
                        _MU_ROLE_SYNC_INTERVAL_H,
                    )
                    return
        except (aiosqlite.Error, ValueError, TypeError):
            logger.exception("mu_role_sync: failed to read last-run state")

        stats = await self._sync_mu_roles_for_all_guilds()

        try:
            await self._db.set_poll_state("mu_role_sync_last_run", now_utc.isoformat())
        except aiosqlite.Error:
            logger.exception("mu_role_sync: failed to save last-run state")

        logger.info(
            "mu_role_sync: done (guilds=%d citizens_with_mu=%d mapped=%d assigned=%d removed=%d had=%d role_missing=%d member_missing=%d errors=%d)",
            stats["guilds"],
            stats["citizens_with_mu"],
            stats["mapped_discord_users"],
            stats["assigned"],
            stats["removed"],
            stats["already_had_role"],
            stats["roles_missing_in_guild"],
            stats["member_not_found"],
            stats["errors"],
        )

    async def _sync_mu_roles_for_all_guilds(self) -> dict[str, int]:
        """Assign MU roles for mapped Discord users across all guilds."""
        stats = {
            "guilds": 0,
            "citizens_with_mu": 0,
            "mapped_discord_users": 0,
            "assigned": 0,
            "already_had_role": 0,
            "removed": 0,
            "roles_missing_in_guild": 0,
            "member_not_found": 0,
            "errors": 0,
        }

        mus_path = self._mus_template_path()
        template_raw = json.loads(mus_path.read_text(encoding="utf-8"))
        mu_to_role_id = self._mu_role_map_from_template(template_raw)
        if not mu_to_role_id:
            return stats

        # Set of all Discord role IDs that represent MU memberships.
        all_mu_role_ids: set[int] = set(mu_to_role_id.values())

        # Get ALL NL citizens (including those with no MU) so we can also
        # remove stale MU roles from members who switched or left their MU.
        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            return stats

        citizen_mu_info = await self._db.get_nl_citizens_mu_info(nl_country_id)
        # Map: in_game_id → desired role_id (None = should have no MU role)
        desired_mu_role: dict[str, int | None] = {}
        for in_game_id, mu_id in citizen_mu_info:
            if mu_id and mu_id in mu_to_role_id:
                desired_mu_role[in_game_id] = mu_to_role_id[mu_id]
            else:
                desired_mu_role[in_game_id] = None

        stats["citizens_with_mu"] = sum(1 for v in desired_mu_role.values() if v is not None)
        if not desired_mu_role:
            return stats

        in_game_ids = list(desired_mu_role.keys())
        production_guild_id = str(self.config.get("guild_id", ""))

        for guild in self.bot.guilds:
            if str(guild.id) == production_guild_id:
                continue
            guild_id = str(guild.id)
            stats["guilds"] += 1

            in_game_to_discord = await self._db.get_discord_ids_by_ingame_user_ids(
                guild_id=guild_id,
                in_game_user_ids=in_game_ids,
            )
            stats["mapped_discord_users"] += len(in_game_to_discord)
            if not in_game_to_discord:
                continue

            for in_game_id, discord_id_str in in_game_to_discord.items():
                if not discord_id_str or not discord_id_str.isdigit():
                    stats["member_not_found"] += 1
                    continue
                member = guild.get_member(int(discord_id_str))
                if member is None or member.bot:
                    stats["member_not_found"] += 1
                    continue

                target_role_id = desired_mu_role.get(in_game_id)

                # Determine which MU roles to remove (any MU role that isn't target)
                stale_roles = [
                    r for r in member.roles
                    if r.id in all_mu_role_ids and r.id != target_role_id
                ]

                # Determine if the target role needs to be added
                role_to_add: discord.Role | None = None
                if target_role_id is not None:
                    target_role = guild.get_role(target_role_id)
                    if target_role is None:
                        stats["roles_missing_in_guild"] += 1
                    elif target_role in member.roles:
                        stats["already_had_role"] += 1
                    else:
                        role_to_add = target_role

                if not stale_roles and role_to_add is None:
                    continue

                try:
                    if stale_roles:
                        await member.remove_roles(
                            *stale_roles,
                            reason="Daily MU role sync: stale MU role removed",
                        )
                        stats["removed"] += len(stale_roles)
                    if role_to_add is not None:
                        await member.add_roles(
                            role_to_add,
                            reason="Daily MU role sync from citizen MU membership",
                        )
                        stats["assigned"] += 1
                except discord.HTTPException:
                    stats["errors"] += 1

        return stats

    async def _sync_discord_nicknames_for_country(
        self,
        country_id: str,
        country_name: str,
    ) -> dict[str, int]:
        """Sync Discord nicknames to latest citizen names for mapped users.

        Returns a stats dict so callers (including the manual /syncnicknames
        command) can tell WHERE the pipeline came up empty, rather than just
        "nothing updated" — citizens/links_checked/matched narrow down
        whether the gap is in citizen_levels, identity_links, or the actual
        Discord edit.
        """
        stats = {
            "citizens": 0, "links_checked": 0, "matched": 0,
            "updated": 0, "skipped": 0, "failed": 0,
        }
        if not self._db:
            return stats

        try:
            citizens = await self._db.get_nl_citizen_ids(country_id)
        except Exception:
            logger.exception(
                "citizen_refresh: failed loading citizens for nickname sync (%s)",
                country_name,
            )
            return stats

        stats["citizens"] = len(citizens)
        if not citizens:
            return stats

        name_by_ingame = {
            str(user_id): str(citizen_name).strip()
            for user_id, citizen_name in citizens
            if str(citizen_name).strip()
        }
        if not name_by_ingame:
            return stats

        for guild in self.bot.guilds:
            try:
                links = await self._db.get_identity_links_for_guild(str(guild.id))
            except Exception:
                logger.exception(
                    "citizen_refresh: failed loading identity links for guild %s",
                    guild.id,
                )
                continue
            stats["links_checked"] += len(links)

            for link in links:
                in_game_user_id = str(link.get("in_game_user_id") or "").strip()
                target_nick = name_by_ingame.get(in_game_user_id)
                if not target_nick:
                    continue
                stats["matched"] += 1

                target_nick = target_nick[:32]
                if not target_nick:
                    continue

                discord_user_id = str(link.get("discord_user_id") or "").strip()
                if not discord_user_id.isdigit():
                    continue

                member = guild.get_member(int(discord_user_id))
                if member is None or member.bot:
                    continue

                # Preserve any [Dx] division prefix applied by war_guild_divisions
                if member.nick and (m := re.match(r"^\[D\d\] ", member.nick)):
                    target_nick = (m.group(0) + target_nick)[:32]

                current = member.nick or member.name
                if current == target_nick:
                    stats["skipped"] += 1
                    continue

                try:
                    await member.edit(
                        nick=target_nick,
                        reason=f"Sync met WarEra citizen naam ({country_name})",
                    )
                    stats["updated"] += 1
                except Exception:
                    stats["failed"] += 1

        logger.info(
            "citizen_refresh: nickname sync %s finished (citizens=%d links_checked=%d "
            "matched=%d updated=%d skipped=%d failed=%d)",
            country_name,
            stats["citizens"], stats["links_checked"], stats["matched"],
            stats["updated"], stats["skipped"], stats["failed"],
        )
        return stats

    @app_commands.command(
        name="syncnicknames",
        description="Ververs direct alle NL Discord-bijnamen naar de laatste WarEra-naam, met diagnose.",
    )
    @has_privileged_role()
    async def cmd_sync_nicknames(self, interaction: discord.Interaction) -> None:
        """Manual trigger for the NL nickname sync that citizen_refresh runs
        hourly — added because that automatic sync's only visibility was a
        log line, unreachable to anyone without server access. Reports the
        same stats the log line has, straight into Discord, so a stuck sync
        can be told apart from "nothing to sync" without SSH: e.g. citizens=0
        means the NL citizen cache itself is stale/empty; links_checked=0
        means identity_links has nothing for this guild (so nobody can be
        matched no matter how fresh the citizen cache is); matched=0 despite
        real links means in_game_user_id values aren't lining up with
        citizen_levels; high failed means Discord is rejecting the edits
        (role hierarchy, permissions).
        """
        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id or not self._db or not self._citizen_cache:
            await interaction.response.send_message(
                "❌ Services niet beschikbaar.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "🔄 WarEra-namen verversen en bijnamen synchroniseren...", ephemeral=True
        )
        try:
            await self._citizen_cache.refresh_country(nl_country_id, "Netherlands")
        except Exception as exc:
            logger.exception("syncnicknames: citizen refresh failed")
            await interaction.followup.send(
                f"❌ Kon WarEra-namen niet verversen: {exc}", ephemeral=True
            )
            return
        try:
            stats = await self._sync_discord_nicknames_for_country(
                nl_country_id, "Netherlands"
            )
        except Exception as exc:
            logger.exception("syncnicknames: nickname sync failed")
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            "✅ Bijnamen-sync klaar.\n"
            f"NL burgers in cache: **{stats['citizens']}**\n"
            f"Identity-links gecontroleerd: **{stats['links_checked']}**\n"
            f"Gematcht (link + bekende WarEra-naam): **{stats['matched']}**\n"
            f"Bijgewerkt: **{stats['updated']}**  •  "
            f"Al correct: **{stats['skipped']}**  •  "
            f"Mislukt: **{stats['failed']}**",
            ephemeral=True,
        )

    # ------------------------------------------------------------------ #
    # Level-5 hourly threshold detection                                   #
    # ------------------------------------------------------------------ #

    _LVL5_NEW_ACCOUNT_DAYS: int = 7  # accounts younger than N days are "new players"

    async def _do_level5_notify(self) -> None:
        """Detect NL citizens who crossed the ≥4 level threshold since the last scan.

        Runs every hour.  Uses ``lvl5_tracker`` to remember each citizen's last
        seen level so we fire exactly once when they cross from <4 to ≥4.

        Citizens who appear at ≥4 without a prior record are checked against their
        account creation date (``dates.createdAt`` from getUserLite).  Accounts
        younger than ``_LVL5_NEW_ACCOUNT_DAYS`` days are treated as fast-levelers
        and included in the notification.  Older accounts are treated as immigrants
        (already high-level before joining NL) and silently skipped.
        """
        testing: bool = bool(getattr(self.bot, "testing", False))

        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            logger.warning("level5_notify: nl_country_id not set in config — skipping")
            return

        now_utc = datetime.now(timezone.utc)
        updated_at = now_utc.isoformat()

        # 1. Current levels for all NL citizens.
        try:
            async with self._db._conn.execute(
                "SELECT user_id, COALESCE(citizen_name, user_id), level"
                " FROM citizen_levels WHERE country_id = ?",
                (nl_country_id,),
            ) as cur:
                nl_citizens: list[tuple[str, str, int]] = [
                    (str(r[0]), str(r[1]), int(r[2]) if r[2] is not None else 0)
                    for r in await cur.fetchall()
                ]
        except Exception:
            logger.exception("level5_notify: DB query for NL citizens failed")
            return

        if not nl_citizens:
            logger.info("level5_notify: no NL citizens found — skipping")
            return

        # 2. Load tracker snapshot.
        try:
            tracker = await self._db.get_lvl5_tracker()
        except Exception:
            logger.exception("level5_notify: failed to load lvl5_tracker")
            return

        notify_candidates: list[tuple[str, str]] = []  # (user_id, citizen_name)
        tracker_updates: list[tuple[str, int, int, str]] = []  # (uid, level, notified, updated_at)

        # Citizens not yet in the tracker who are already at ≥5 — need API age check.
        new_high_level: list[tuple[str, str, int]] = []  # (user_id, name, level)

        for uid, name, level in nl_citizens:
            row = tracker.get(uid)
            if row is not None:
                # Known player — detect threshold crossing.
                was_below = row["last_seen_level"] < 4
                already_notified = row["notified"] == 1
                if was_below and not already_notified and level >= 4:
                    notify_candidates.append((uid, name))
                    tracker_updates.append((uid, level, 1, updated_at))
                else:
                    tracker_updates.append((uid, level, row["notified"], updated_at))
            else:
                # First time we see this citizen.
                if level >= 4:
                    new_high_level.append((uid, name, level))
                else:
                    tracker_updates.append((uid, level, 0, updated_at))

        # 3. For new-to-tracker citizens at ≥5: check account creation date via API.
        if new_high_level:
            new_ids = [uid for uid, _, _ in new_high_level]
            logger.info(
                "level5_notify: %d new high-level citizens — checking account age",
                len(new_ids),
            )
            try:
                api_results = await self._client.batch_get(
                    "user.getUserLite",
                    [{"userId": uid} for uid in new_ids],
                )
            except Exception:
                logger.exception("level5_notify: batch getUserLite for account age failed")
                api_results = [None] * len(new_ids)

            cutoff = now_utc - timedelta(days=self._LVL5_NEW_ACCOUNT_DAYS)
            for (uid, name, lvl), data in zip(new_high_level, api_results):
                is_new_account = False
                if isinstance(data, dict):
                    created_str = (
                        (data.get("dates") or {}).get("createdAt")
                        or data.get("createdAt")
                    )
                    if created_str:
                        try:
                            created_dt = datetime.fromisoformat(
                                created_str.replace("Z", "+00:00")
                            )
                            is_new_account = created_dt >= cutoff
                        except (ValueError, TypeError):
                            pass
                if is_new_account:
                    notify_candidates.append((uid, name))
                # Mark notified=1 regardless: immigrant or fast-leveler, we've
                # evaluated this citizen and won't re-evaluate at ≥5 again.
                tracker_updates.append((uid, lvl, 1, updated_at))

        # 4. Persist updated tracker rows.
        try:
            await self._db.bulk_upsert_lvl5_tracker(tracker_updates)
        except Exception:
            logger.exception("level5_notify: failed to persist lvl5_tracker updates")
            return

        if not notify_candidates:
            logger.info("level5_notify: no new level-4 threshold crossers this hour")
            return

        # 5. Build Discord display names and post notification.
        guild_id_cfg = self.config.get("guild_id")
        guild = self.bot.get_guild(int(guild_id_cfg)) if guild_id_cfg else None
        discord_map: dict[str, str] = {}
        if guild:
            try:
                in_game_ids = [uid for uid, _ in notify_candidates]
                id_to_discord = await self._db.get_discord_ids_by_ingame_user_ids(
                    guild_id=str(guild.id),
                    in_game_user_ids=in_game_ids,
                )
                for ig_id, disc_id in id_to_discord.items():
                    if disc_id and disc_id.isdigit():
                        member = guild.get_member(int(disc_id))
                        if member:
                            discord_map[ig_id] = member.display_name
            except Exception:
                logger.exception("level5_notify: failed to look up Discord names")

        lines: list[str] = []
        for uid, name in notify_candidates:
            profile_url = f"https://app.warera.io/user/{uid}"
            disc_name = discord_map.get(uid)
            if disc_name:
                lines.append(f"• **[{name}]({profile_url})** (Discord: {disc_name})")
            else:
                lines.append(f"• **[{name}]({profile_url})**")

        channel_id = (
            self.config.get("channels", {}).get("lvl5_notificaties")
            or (_LVL5_TEST_CHANNEL_ID if testing else _LVL5_PROD_CHANNEL_ID)
        )
        role_id = _LVL5_TEST_ROLE_ID if testing else _LVL5_PROD_ROLE_ID
        today_str = now_utc.strftime("%Y-%m-%d")

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            logger.warning(
                "level5_notify: channel %d not found — cannot post", channel_id
            )
            return

        embed = discord.Embed(
            title="🎉 Nieuwe level-4 spelers!",
            description="\n".join(lines),
            colour=self._embed_colour("success"),
        )
        embed.set_footer(text=f"Gedetecteerd op {today_str}")
        try:
            await channel.send(
                content=f"<@&{role_id}>",
                embed=embed,
            )
            logger.info(
                "level5_notify: posted %d new level-4 citizens", len(notify_candidates)
            )
        except discord.HTTPException:
            logger.exception("level5_notify: failed to send message")

    # ------------------------------------------------------------------ #
    # Manual trigger command                                               #
    # ------------------------------------------------------------------ #

    @commands.command(name="lvl5-scan")
    @commands.is_owner()
    async def lvl5_scan(self, ctx: commands.Context) -> None:
        """Trigger the level-5 notification scan immediately (owner only)."""
        if not self._db:
            await ctx.send("❌ Database not ready.")
            return
        await ctx.send("🔍 Scanning voor nieuwe level-5 spelers…")
        try:
            await self._do_level5_notify()
            await ctx.send("✅ Scan klaar.")
        except Exception as exc:
            logger.exception("lvl5_scan: manual trigger failed")
            await ctx.send(f"❌ Fout tijdens scan: {exc}")


async def setup(bot) -> None:
    """Add the CitizenTasks cog to the bot.

    Registered guild-only (same reasoning as cogs/owner.py's setup()) so
    /syncnicknames is instantly available and doesn't eat into the 100
    global slash-command budget for a command that only ever makes sense
    for this one guild's NL members anyway. A cog added without `guilds=`
    registers globally by default, which both counts against that 91/100
    global-command budget AND can take up to an hour to actually show up
    for users even after a successful tree.sync() — confirmed live:
    /syncnicknames stayed invisible after `!sync guild` specifically
    because that only re-syncs the guild-scoped pool, never global commands.
    """
    guild_id = int(bot.config.get("guild_id") or 0)
    guilds = [discord.Object(id=guild_id)] if guild_id else []
    await bot.add_cog(CitizenTasks(bot), guilds=guilds or None)
