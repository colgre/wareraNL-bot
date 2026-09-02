"""
User database commands.

Commands and listeners:
  /ingameid - get in-game ID mapping for a Discord user
  /discordid - get Discord user mapping(s) for an in-game ID or profile URL
  /usercount - count mapped users (optionally filtered by nationality)
  /userdbhealth - overview of DB health and conflict indicators
  /userrecent - list recently approved mappings
"""

from __future__ import annotations

import csv
import datetime
import difflib
import io
import json
import logging
import re
import unicodedata
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")


class Users(CommandCogBase, name="users"):
    """Admin commands for Discord ↔ in-game identity mappings."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._fallback_db = None

    @staticmethod
    def _normalize_name(value: str) -> str:
        """Normalize a username for fuzzy matching across Discord/game formats."""
        ascii_value = (
            unicodedata.normalize("NFKD", str(value or ""))
            .encode("ascii", "ignore")
            .decode("ascii")
            .lower()
        )
        return "".join(ch for ch in ascii_value if ch.isalnum())

    def _best_citizen_match(
        self,
        member: discord.Member,
        citizens: list[tuple[str, str, str]],
    ) -> tuple[str, str, float, float, str] | None:
        """Return best citizen match for a member: (id, name, best, second, variant)."""
        variants: list[str] = []
        for raw in [member.display_name, member.nick, member.name]:
            norm = self._normalize_name(raw)
            if norm and norm not in variants:
                variants.append(norm)
        if not variants or not citizens:
            return None

        best_uid = ""
        best_name = ""
        best_variant = ""
        best_score = 0.0
        second_score = 0.0

        for uid, citizen_name, citizen_norm in citizens:
            if not citizen_norm:
                continue
            citizen_best = 0.0
            citizen_variant = ""
            for variant in variants:
                if variant == citizen_norm:
                    ratio = 1.0
                else:
                    ratio = difflib.SequenceMatcher(None, variant, citizen_norm).ratio()
                    if variant.startswith(citizen_norm) or citizen_norm.startswith(
                        variant
                    ):
                        ratio = min(1.0, ratio + 0.08)
                if ratio > citizen_best:
                    citizen_best = ratio
                    citizen_variant = variant

            if citizen_best > best_score:
                second_score = best_score
                best_score = citizen_best
                best_uid = uid
                best_name = citizen_name
                best_variant = citizen_variant
            elif citizen_best > second_score:
                second_score = citizen_best

        if not best_uid:
            return None
        return best_uid, best_name, best_score, second_score, best_variant

    async def _get_db(self):
        """Return shared external DB, closing any standalone fallback when services become ready."""
        shared = self._db  # property → bot._ext_db
        if shared is not None:
            if self._fallback_db is not None:
                try:
                    await self._fallback_db.close()
                except Exception:
                    pass
                self._fallback_db = None
            return shared
        if self._fallback_db is None:
            from services.db import Database
            db_path = self.config.get("external_db_path", "database/external.db")
            self._fallback_db = Database(db_path)
            await self._fallback_db.setup()
        return self._fallback_db

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

    async def _sync_mu_roles_for_guild(
        self, guild: discord.Guild, dry_run: bool = True
    ) -> dict[str, int]:
        """Assign MU roles to mapped Discord users based on citizen MU membership."""
        stats = {
            "citizens_with_mu": 0,
            "mapped_discord_users": 0,
            "members_found": 0,
            "roles_missing_in_guild": 0,
            "already_had_role": 0,
            "to_assign": 0,
            "assigned": 0,
            "to_remove": 0,
            "removed": 0,
            "member_not_found": 0,
            "errors": 0,
        }

        db = await self._get_db()
        guild_id = str(guild.id)

        mus_path = self._mus_template_path()
        template_raw = json.loads(mus_path.read_text(encoding="utf-8"))
        mu_to_role_id = self._mu_role_map_from_template(template_raw)
        if not mu_to_role_id:
            return stats

        # Refresh MU memberships from the API first so the DB reflects the
        # current state (catches players who recently switched MUs).
        citizen_cache = getattr(self.bot, "_ext_citizen_cache", None)
        nl_country_id = self.config.get("nl_country_id")
        if citizen_cache and nl_country_id:
            try:
                _mu_entries_for_refresh = [
                    (str(e.get("id", "")).strip(), str(e.get("name") or e.get("title") or ""))
                    for e in template_raw.get("embeds", [])
                    if str(e.get("id", "")).strip()
                ]
                if _mu_entries_for_refresh:
                    await citizen_cache.refresh_mu_memberships(
                        nl_country_id, _mu_entries_for_refresh
                    )
            except Exception:
                logger.warning("sync_mu_roles: refresh_mu_memberships failed, continuing with cached data", exc_info=True)

        citizen_mus = await db.get_citizen_mus()
        in_game_to_current_mu = {
            in_game_id: mu_id for in_game_id, mu_id in citizen_mus if in_game_id
        }
        citizens_with_known_mu = [
            (in_game_id, mu_id)
            for in_game_id, mu_id in citizen_mus
            if mu_id and mu_id in mu_to_role_id
        ]
        stats["citizens_with_mu"] = len(citizens_with_known_mu)
        if not citizens_with_known_mu:
            return stats

        in_game_ids = list(in_game_to_current_mu.keys())
        in_game_to_discord = await db.get_discord_ids_by_ingame_user_ids(
            guild_id=guild_id,
            in_game_user_ids=in_game_ids,
        )
        expected_role_by_member: dict[int, int | None] = {}
        for in_game_id, discord_id in in_game_to_discord.items():
            if not discord_id:
                continue
            try:
                member_id = int(discord_id)
            except (TypeError, ValueError):
                continue

            mu_id = in_game_to_current_mu.get(in_game_id)
            expected_role_id = mu_to_role_id.get(str(mu_id)) if mu_id else None
            if member_id not in expected_role_by_member:
                expected_role_by_member[member_id] = expected_role_id
            elif expected_role_by_member[member_id] is None and expected_role_id is not None:
                expected_role_by_member[member_id] = expected_role_id

        stats["mapped_discord_users"] = len(expected_role_by_member)
        if not in_game_to_discord:
            return stats

        mu_role_ids = {int(role_id) for role_id in mu_to_role_id.values()}
        work_items_add: list[tuple[discord.Member, discord.Role]] = []
        work_items_remove: list[tuple[discord.Member, discord.Role]] = []
        seen_assignments: set[tuple[int, int]] = set()
        seen_removals: set[tuple[int, int]] = set()

        for member_id, expected_role_id in expected_role_by_member.items():
            member = guild.get_member(member_id)
            if member is None:
                stats["member_not_found"] += 1
                continue
            stats["members_found"] += 1

            current_mu_roles = [role for role in member.roles if role.id in mu_role_ids]

            expected_role: discord.Role | None = None
            if expected_role_id:
                expected_role = guild.get_role(expected_role_id)
                if expected_role is None:
                    stats["roles_missing_in_guild"] += 1
                elif expected_role in member.roles:
                    stats["already_had_role"] += 1
                else:
                    assignment_key = (member.id, expected_role.id)
                    if assignment_key not in seen_assignments:
                        seen_assignments.add(assignment_key)
                        stats["to_assign"] += 1
                        work_items_add.append((member, expected_role))

            for role in current_mu_roles:
                if expected_role is not None and role.id == expected_role.id:
                    continue
                removal_key = (member.id, role.id)
                if removal_key in seen_removals:
                    continue
                seen_removals.add(removal_key)
                stats["to_remove"] += 1
                work_items_remove.append((member, role))

        if dry_run:
            return stats

        for member, role in work_items_add:
            try:
                await member.add_roles(role, reason="MU role sync from citizen MU membership")
                stats["assigned"] += 1
            except discord.HTTPException as exc:
                stats["errors"] += 1
                logger.warning(
                    "sync_mu_roles: failed to add role %s to %s (%s): %s",
                    role.id,
                    member.display_name,
                    member.id,
                    exc,
                )

        for member, role in work_items_remove:
            try:
                await member.remove_roles(
                    role,
                    reason="MU role sync: user no longer in this MU",
                )
                stats["removed"] += 1
            except discord.HTTPException as exc:
                stats["errors"] += 1
                logger.warning(
                    "sync_mu_roles: failed to remove role %s from %s (%s): %s",
                    role.id,
                    member.display_name,
                    member.id,
                    exc,
                )

        return stats

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Handle app command errors for this cog."""
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Je hebt geen toestemming om dit commando te gebruiken.",
                ephemeral=True,
            )
            return
        logger.exception("users command error: %s", error)
        if interaction.response.is_done():
            await interaction.followup.send(
                "Er ging iets mis bij het uitvoeren van dit commando.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Er ging iets mis bij het uitvoeren van dit commando.",
                ephemeral=True,
            )

    @staticmethod
    def _normalize_ingame_id(raw_value: str) -> str:
        """Accept in-game ID or WarEra profile URL and return plain ID."""
        raw = str(raw_value).strip()
        if not raw:
            raise ValueError("In-game ID cannot be empty.")

        match = re.match(
            r"^https?://app\.warera\.io/user/([^/?#]+)(?:[/?#].*)?$",
            raw,
            flags=re.IGNORECASE,
        )
        if match:
            normalized = match.group(1).strip()
        else:
            normalized = raw
            if "://" in raw:
                raise ValueError(
                    "Invalid WarEra profile URL. Use https://app.warera.io/user/{id} or provide the raw in-game ID."
                )

        if not normalized:
            raise ValueError("Could not extract an in-game ID from the input.")
        if len(normalized) > 64:
            raise ValueError("In-game ID is too long (max 64 characters).")
        return normalized

    @app_commands.command(
        name="ingameid",
        description="Toon de in-game ID die is gekoppeld aan een Discord gebruiker",
    )
    @app_commands.describe(user="Discord gebruiker")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ingame_id(self, interaction: discord.Interaction, user: discord.Member):
        db = await self._get_db()
        record = await db.get_identity_link_by_discord(
            discord_user_id=str(user.id), guild_id=str(interaction.guild_id)
        )
        if not record:
            await interaction.response.send_message(
                "Geen mapping gevonden voor deze gebruiker.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🔎 Mapping via Discord",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Discord", value=f"{user.mention} (`{user.id}`)", inline=False
        )
        embed.add_field(
            name="In-game ID", value=f"`{record['in_game_user_id']}`", inline=True
        )
        embed.add_field(name="Nationaliteit", value=record["nationality"], inline=True)
        embed.add_field(name="Type", value=record["request_type"], inline=True)
        embed.add_field(
            name="Goedgekeurd op", value=record["approved_at"], inline=False
        )
        embed.add_field(
            name="Goedgekeurd door",
            value=f"<@{record['approved_by_discord_id']}> (`{record['approved_by_discord_id']}`)",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="setmofa",
        description="Stel de MoFA-fallback in (normaal automatisch via de rol)"
    )
    @app_commands.describe(
        user="Discord gebruiker"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_mofa(self, interaction: discord.Interaction, user: discord.Member):
        """Set the MoFA fallback used when nobody holds the minister role.

        Embassy tickets normally resolve the MoFA live from the *Minister van
        Buitenlandse Zaken* role, so this only needs setting if that role is
        ever empty (see ``_resolve_mofa_line`` in cogs/welcome.py).
        """
        db = await self._get_db()
        in_game_id = await db.get_identity_link_by_discord(
            discord_user_id=str(user.id)
        )

        if not in_game_id or not in_game_id.get("in_game_user_id"):
            await interaction.response.send_message(
                f"De opgegeven gebruiker {user.mention} heeft geen gekoppelde in-game ID. "
                "Zorg ervoor dat deze gebruiker eerst een mapping heeft via `/linkid`.",
                ephemeral=True,
            )
            return

        config = self.config
        if "users" not in config:
            config["users"] = {}
        config["users"]["mofa"] = {
            "discord_id": str(user.id),
            "in_game_id": in_game_id.get("in_game_user_id"),
        }
        config_path = Path("config/config.json")
        config_path.write_text(json.dumps(config, indent=4), encoding="utf-8")

        await interaction.response.send_message(
            f"MoFA ingesteld op {user.mention} (`{user.id}`).",
            ephemeral=True,
        )

    @app_commands.command(
        name="linkid",
        description="Link een Discord gebruiker aan een in-game ID (of update bestaande mapping)",
    )
    @app_commands.describe(
        user="Discord gebruiker",
        in_game_id="In-game ID of profiel-URL (https://app.warera.io/user/{id})",
        nationality="Optioneel: nationaliteit (bijv. nederlander, belgian, foreigner)",
        request_type="Optioneel: request type (standaard: manual_link)",
        embassy_country="Optioneel: embassy-land (alleen voor embassy mappings)",
        force="Sta toe dat dit in-game ID al aan een andere Discord gebruiker hangt",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def link_id(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        in_game_id: str,
        nationality: str | None = None,
        request_type: str | None = None,
        embassy_country: str | None = None,
        force: bool = False,
    ) -> None:
        """Manually create or update a Discord ↔ in-game mapping."""
        try:
            normalized = self._normalize_ingame_id(in_game_id)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        discord_id = str(user.id)
        db = await self._get_db()

        existing_for_discord = await db.get_identity_link_by_discord(
            discord_user_id=discord_id,
            guild_id=guild_id,
        )
        existing_for_ingame = await db.get_identity_links_by_ingame(
            in_game_user_id=normalized,
            guild_id=guild_id,
        )
        conflicting_discord = next(
            (
                link.get("discord_user_id")
                for link in existing_for_ingame
                if str(link.get("discord_user_id")) != discord_id
            ),
            None,
        )

        if conflicting_discord and not force:
            await interaction.response.send_message(
                (
                    "Dit in-game ID is al gekoppeld aan een andere Discord gebruiker: "
                    f"<@{conflicting_discord}> (`{conflicting_discord}`). "
                    "Gebruik `force=True` als je deze mapping bewust wilt overschrijven."
                ),
                ephemeral=True,
            )
            return

        final_nationality = (
            str(nationality).strip().lower()
            if nationality and str(nationality).strip()
            else str((existing_for_discord or {}).get("nationality") or "manual")
        )
        final_request_type = (
            str(request_type).strip().lower()
            if request_type and str(request_type).strip()
            else str((existing_for_discord or {}).get("request_type") or "manual_link")
        )
        final_embassy_country = (
            str(embassy_country).strip()
            if embassy_country and embassy_country.strip()
            else None
        )
        approved_at = datetime.datetime.now(datetime.UTC).isoformat()

        await db.upsert_identity_link(
            discord_user_id=discord_id,
            guild_id=guild_id,
            in_game_user_id=normalized,
            nationality=final_nationality,
            request_type=final_request_type,
            embassy_country=final_embassy_country,
            approved_by_discord_id=str(interaction.user.id),
            approved_at=approved_at,
        )

        embed = discord.Embed(
            title="✅ Mapping opgeslagen",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Discord", value=f"{user.mention} (`{user.id}`)", inline=False
        )
        embed.add_field(name="In-game ID", value=f"`{normalized}`", inline=True)
        embed.add_field(name="Nationaliteit", value=final_nationality, inline=True)
        embed.add_field(name="Type", value=final_request_type, inline=True)
        if final_embassy_country:
            embed.add_field(
                name="Embassy-land",
                value=final_embassy_country,
                inline=True,
            )

        if existing_for_discord and existing_for_discord.get("in_game_user_id"):
            previous = str(existing_for_discord.get("in_game_user_id"))
            if previous != normalized:
                embed.add_field(
                    name="Vorige in-game ID", value=f"`{previous}`", inline=False
                )
            else:
                embed.add_field(
                    name="Info", value="Bestaande mapping bijgewerkt.", inline=False
                )
        else:
            embed.add_field(
                name="Info", value="Nieuwe mapping aangemaakt.", inline=False
            )

        if conflicting_discord and force:
            embed.add_field(
                name="⚠️ Force override",
                value=(
                    "Dit in-game ID stond ook op een andere Discord gebruiker. "
                    "Controleer met `/discordid` of aanvullende opschoning nodig is."
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.command(name="discordid")
    @commands.has_permissions(manage_guild=True)
    async def discord_id(self, ctx: Context, in_game_id: str):
        try:
            normalized = self._normalize_ingame_id(in_game_id)
        except ValueError as e:
            await ctx.send(str(e))
            return

        db = await self._get_db()
        links = await db.get_identity_links_by_ingame(
            in_game_user_id=normalized,
            guild_id=str(ctx.guild.id) if ctx.guild else "",
        )
        if not links:
            await ctx.send(f"Geen Discord mapping gevonden voor in-game ID `{normalized}`.")
            return

        embed = discord.Embed(
            title="🔎 Mapping via in-game ID",
            description=f"In-game ID: `{normalized}`",
            color=discord.Color.blurple(),
        )
        for link in links[:10]:
            embed.add_field(
                name=f"Discord: <@{link['discord_user_id']}>",
                value=(
                    f"ID: `{link['discord_user_id']}`\n"
                    f"Nationaliteit: {link['nationality']}\n"
                    f"Type: {link['request_type']}\n"
                    f"Updated: {link['updated_at']}"
                ),
                inline=False,
            )
        if len(links) > 10:
            embed.set_footer(text=f"Toont 10 van {len(links)} resultaten")
        await ctx.send(embed=embed)

    @app_commands.command(
        name="usercount",
        description="Toon aantal gebruikers in identity database (optioneel per nationaliteit)",
    )
    @app_commands.describe(
        nationality="Optioneel, bijv. nederlander, belgian, foreigner of een embassy-land"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def user_count(
        self, interaction: discord.Interaction, nationality: str | None = None
    ):
        db = await self._get_db()
        total = await db.count_identity_links(guild_id=str(interaction.guild_id))
        filtered = None
        if nationality:
            filtered = await db.count_identity_links(
                guild_id=str(interaction.guild_id),
                nationality=nationality.strip(),
            )

        embed = discord.Embed(title="📊 User DB aantallen", color=discord.Color.green())
        embed.add_field(name="Totaal", value=str(total), inline=True)
        if filtered is not None:
            embed.add_field(
                name=f"Filter: {nationality.strip().lower()}",
                value=str(filtered),
                inline=True,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="userdbhealth",
        description="Toon databasegezondheid voor identity mappings",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def user_db_health(self, interaction: discord.Interaction):
        db = await self._get_db()
        guild_id = str(interaction.guild_id)
        total = await db.count_identity_links(guild_id=guild_id)
        conflicts = await db.count_identity_ingame_conflicts(guild_id=guild_id)
        by_nat = await db.identity_counts_by_nationality(guild_id=guild_id)

        embed = discord.Embed(
            title="🩺 User DB Health",
            color=discord.Color.orange() if conflicts else discord.Color.green(),
        )
        embed.add_field(name="Mappings", value=str(total), inline=True)
        embed.add_field(
            name="In-game conflicts",
            value=str(conflicts),
            inline=True,
        )
        if by_nat:
            lines = [f"- {name}: {count}" for name, count in by_nat[:12]]
            embed.add_field(
                name="Per nationaliteit",
                value="\n".join(lines),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="userrecent",
        description="Toon recente identity mappings",
    )
    @app_commands.describe(limit="Aantal recente records (1-20, standaard 10)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def user_recent(self, interaction: discord.Interaction, limit: int = 10):
        db = await self._get_db()
        rows = await db.get_recent_identity_links(
            guild_id=str(interaction.guild_id),
            limit=max(1, min(limit, 20)),
        )
        if not rows:
            await interaction.response.send_message(
                "Nog geen identity mappings gevonden.", ephemeral=True
            )
            return

        lines = []
        for row in rows:
            lines.append(
                f"<@{row['discord_user_id']}> → `{row['in_game_user_id']}` "
                f"({row['nationality']}, {row['request_type']})"
            )
        embed = discord.Embed(
            title="🕒 Recente user mappings",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="userverifybackfillnl",
        description="Backfill Discord↔in-game mappings voor Nederlander-rol met fuzzy matching",
    )
    @app_commands.describe(
        apply="Schrijf mappings weg (standaard: alleen preview)",
        min_score="Minimale fuzzy score (0.50-1.00), standaard 0.90",
        refresh_nl="Eerst NL citizens verversen via API",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def user_verify_backfill_nl(
        self,
        interaction: discord.Interaction,
        apply: bool = False,
        min_score: float = 0.90,
        refresh_nl: bool = True,
    ) -> None:
        """Backfill identity links for members with the Nederlander role."""
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "Dit commando kan alleen in een server gebruikt worden.", ephemeral=True
            )
            return

        min_score = max(0.50, min(1.00, float(min_score)))
        nl_country_id = str(self.config.get("nl_country_id") or "").strip()
        nl_role_id = (self.config.get("roles") or {}).get("nederlander")
        if not nl_country_id:
            await interaction.followup.send(
                "`nl_country_id` ontbreekt in de configuratie.", ephemeral=True
            )
            return
        if not nl_role_id:
            await interaction.followup.send(
                "`roles.nederlander` ontbreekt in de configuratie.", ephemeral=True
            )
            return

        nl_role = guild.get_role(int(nl_role_id))
        if nl_role is None:
            await interaction.followup.send(
                "De Nederlander-rol kon niet worden gevonden in deze server.",
                ephemeral=True,
            )
            return

        refreshed = 0
        if refresh_nl:
            citizen_cache = getattr(self.bot, "_ext_citizen_cache", None)
            if not citizen_cache:
                await interaction.followup.send(
                    "Citizen cache service is niet beschikbaar.", ephemeral=True
                )
                return
            try:
                lock = getattr(self.bot, "_ext_heavy_api_lock", None)
                if lock:
                    async with lock:
                        refreshed = await citizen_cache.refresh_country(
                            nl_country_id,
                            "Netherlands",
                        )
                else:
                    refreshed = await citizen_cache.refresh_country(
                        nl_country_id,
                        "Netherlands",
                    )
            except Exception as exc:
                logger.exception("backfillnl: NL refresh failed")
                await interaction.followup.send(
                    f"NL refresh mislukt: `{exc}`", ephemeral=True
                )
                return

        db = await self._get_db()
        citizens_raw = await db.get_nl_citizen_ids(nl_country_id)
        citizens: list[tuple[str, str, str]] = [
            (uid, name, self._normalize_name(name)) for uid, name in citizens_raw
        ]
        nl_citizen_ids = {uid for uid, _name in citizens_raw}
        if not citizens:
            await interaction.followup.send(
                "Geen NL-citizens in de cache gevonden. Draai eerst een citizens refresh.",
                ephemeral=True,
            )
            return

        members = [m for m in nl_role.members if not m.bot]
        if not members:
            await interaction.followup.send(
                "Geen leden met de Nederlander-rol gevonden.", ephemeral=True
            )
            return

        approved_at = datetime.datetime.now(datetime.UTC).isoformat()
        rows: list[dict[str, str]] = []
        candidates: list[dict[str, object]] = []
        already_mapped = 0
        mapped_not_nl = 0

        for member in members:
            discord_id = str(member.id)
            existing = await db.get_identity_link_by_discord(
                discord_user_id=discord_id,
                guild_id=str(guild.id),
            )
            if existing and existing.get("in_game_user_id"):
                existing_ingame_id = str(existing.get("in_game_user_id"))
                if existing_ingame_id in nl_citizen_ids:
                    already_mapped += 1
                    status = "already_mapped"
                    note = "bestond al in identity_links"
                else:
                    mapped_not_nl += 1
                    status = "mapped_not_nl"
                    note = "bestaande mapping wijst naar niet-NL citizen"
                rows.append(
                    {
                        "discord_id": discord_id,
                        "discord_name": member.display_name,
                        "status": status,
                        "in_game_id": existing_ingame_id,
                        "in_game_name": "",
                        "score": "1.000",
                        "note": note,
                    }
                )
                continue

            match = self._best_citizen_match(member, citizens)
            if match is None:
                rows.append(
                    {
                        "discord_id": discord_id,
                        "discord_name": member.display_name,
                        "status": "no_match",
                        "in_game_id": "",
                        "in_game_name": "",
                        "score": "0.000",
                        "note": "geen bruikbare naamvariant",
                    }
                )
                continue

            in_game_id, in_game_name, best, second, variant = match
            candidates.append(
                {
                    "discord_id": discord_id,
                    "discord_name": member.display_name,
                    "in_game_id": in_game_id,
                    "in_game_name": in_game_name,
                    "score": best,
                    "second": second,
                    "variant": variant,
                }
            )

        candidates.sort(key=lambda item: float(item["score"]), reverse=True)
        chosen_ingame: set[str] = set()
        auto_linked = 0
        high_confidence = 0
        ambiguous = 0
        conflicts = 0

        for cand in candidates:
            discord_id = str(cand["discord_id"])
            in_game_id = str(cand["in_game_id"])
            score = float(cand["score"])
            second = float(cand["second"])
            margin = score - second

            status = "candidate"
            note = f"variant={cand['variant']} margin={margin:.3f}"

            if score < min_score:
                status = "ambiguous"
                note = f"score {score:.3f} < min_score {min_score:.3f}"
            elif margin < 0.08:
                status = "ambiguous"
                note = f"klein verschil met 2e match ({margin:.3f})"
            elif in_game_id in chosen_ingame:
                status = "conflict"
                note = "zelfde in-game ID al toegekend aan sterkere match"
            else:
                existing_ingame = await db.get_identity_links_by_ingame(
                    in_game_user_id=in_game_id,
                    guild_id=str(guild.id),
                )
                linked_other = next(
                    (
                        link.get("discord_user_id")
                        for link in existing_ingame
                        if str(link.get("discord_user_id")) != discord_id
                    ),
                    None,
                )
                if linked_other:
                    status = "conflict"
                    note = f"in-game ID al gekoppeld aan <@{linked_other}>"
                else:
                    high_confidence += 1
                    chosen_ingame.add(in_game_id)
                    if apply:
                        await db.upsert_identity_link(
                            discord_user_id=discord_id,
                            guild_id=str(guild.id),
                            in_game_user_id=in_game_id,
                            nationality="nederlander",
                            request_type="backfill_nederlander",
                            embassy_country=None,
                            approved_by_discord_id=str(interaction.user.id),
                            approved_at=approved_at,
                        )
                        status = "linked"
                        note = "automatisch gelinkt"
                        auto_linked += 1
                    else:
                        status = "review"
                        note = "hoog vertrouwen; klaar voor handmatige check"

            if status == "ambiguous":
                ambiguous += 1
            elif status == "conflict":
                conflicts += 1

            rows.append(
                {
                    "discord_id": discord_id,
                    "discord_name": str(cand["discord_name"]),
                    "status": status,
                    "in_game_id": in_game_id,
                    "in_game_name": str(cand["in_game_name"]),
                    "score": f"{score:.3f}",
                    "note": note,
                }
            )

        rows.sort(key=lambda r: (r["status"], r["discord_name"].lower()))

        csv_buf = io.StringIO()
        csv_buf.write(
            "discord_id,discord_name,status,in_game_id,in_game_name,score,note\n"
        )
        for row in rows:
            safe = []
            for key in (
                "discord_id",
                "discord_name",
                "status",
                "in_game_id",
                "in_game_name",
                "score",
                "note",
            ):
                value = str(row.get(key, "")).replace('"', '""')
                safe.append(f'"{value}"')
            csv_buf.write(",".join(safe) + "\n")

        embed = discord.Embed(
            title="🇳🇱 Nederlander backfill verificatie",
            color=discord.Color.green() if apply else discord.Color.orange(),
            description=(
                "Resultaat van fuzzy matching tussen Discord Nederlander-leden "
                "en NL citizens uit de cache."
            ),
        )
        embed.add_field(name="Nederlander-leden", value=str(len(members)), inline=True)
        embed.add_field(
            name="NL citizens (cache)", value=str(len(citizens)), inline=True
        )
        embed.add_field(name="Al gemapt", value=str(already_mapped), inline=True)
        embed.add_field(name="Mapped not NL", value=str(mapped_not_nl), inline=True)
        embed.add_field(name="High confidence", value=str(high_confidence), inline=True)
        embed.add_field(name="Ambiguous", value=str(ambiguous), inline=True)
        embed.add_field(name="Conflicts", value=str(conflicts), inline=True)
        if refresh_nl:
            embed.add_field(name="NL refreshed", value=str(refreshed), inline=True)
        if apply:
            embed.add_field(name="Nieuw gelinkt", value=str(auto_linked), inline=True)
            embed.set_footer(text="apply=true: links zijn opgeslagen in identity_links")
        else:
            embed.set_footer(
                text="apply=false: preview mode. Controleer CSV en voer daarna opnieuw uit met apply=true."
            )

        filename_ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        report_text = csv_buf.getvalue()
        default_report_path = Path(
            self.config.get(
                "nl_backfill_report_path",
                "output/nl_backfill_report_latest.csv",
            )
        )
        try:
            default_report_path.parent.mkdir(parents=True, exist_ok=True)
            default_report_path.write_text(report_text, encoding="utf-8")
            embed.add_field(
                name="Saved report",
                value=f"`{default_report_path}`",
                inline=False,
            )
        except Exception as exc:
            logger.warning("backfillnl: failed to save report to disk: %s", exc)

        report_file = discord.File(
            io.BytesIO(report_text.encode("utf-8")),
            filename=f"nl_backfill_report_{filename_ts}.csv",
        )
        await interaction.followup.send(embed=embed, file=report_file, ephemeral=True)

    @app_commands.command(
        name="userverifyapplynlcsv",
        description="Pas handmatig gereviewde NL backfill CSV toe op identity_links",
    )
    @app_commands.describe(
        reviewed_csv="Optioneel: CSV uit /userverifybackfillnl (anders default bestandspad)",
        dry_run="Alleen valideren/simuleren, niet wegschrijven",
        overwrite_existing="Sta overschrijven van bestaande Discord mapping toe",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def user_verify_apply_nl_csv(
        self,
        interaction: discord.Interaction,
        reviewed_csv: discord.Attachment | None = None,
        dry_run: bool = True,
        overwrite_existing: bool = False,
    ) -> None:
        """Apply reviewed NL backfill CSV rows to identity_links."""
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "Dit commando kan alleen in een server gebruikt worden.", ephemeral=True
            )
            return

        nl_country_id = str(self.config.get("nl_country_id") or "").strip()
        nl_role_id = (self.config.get("roles") or {}).get("nederlander")
        if not nl_country_id or not nl_role_id:
            await interaction.followup.send(
                "Configuratie mist `nl_country_id` of `roles.nederlander`.",
                ephemeral=True,
            )
            return

        if reviewed_csv is not None:
            try:
                raw_bytes = await reviewed_csv.read()
                text = raw_bytes.decode("utf-8-sig")
            except Exception as exc:
                await interaction.followup.send(
                    f"CSV kon niet worden gelezen als UTF-8: `{exc}`", ephemeral=True
                )
                return
            source_label = f"attachment `{reviewed_csv.filename}`"
        else:
            default_review_path = Path(
                self.config.get(
                    "nl_backfill_report_path",
                    "output/nl_backfill_report_latest.csv",
                )
            )
            try:
                text = default_review_path.read_text(encoding="utf-8-sig")
            except FileNotFoundError:
                await interaction.followup.send(
                    "Geen attachment meegegeven en default CSV bestaat niet: "
                    f"`{default_review_path}`",
                    ephemeral=True,
                )
                return
            except Exception as exc:
                await interaction.followup.send(
                    "Default CSV kon niet worden gelezen: "
                    f"`{default_review_path}` ({exc})",
                    ephemeral=True,
                )
                return
            source_label = f"default file `{default_review_path}`"

        reader = csv.DictReader(io.StringIO(text))
        required_cols = {"discord_id", "in_game_id"}
        header = set(reader.fieldnames or [])
        if not required_cols.issubset(header):
            await interaction.followup.send(
                "CSV mist verplichte kolommen: `discord_id`, `in_game_id`.",
                ephemeral=True,
            )
            return

        db = await self._get_db()
        nl_citizens = await db.get_nl_citizen_ids(nl_country_id)
        nl_citizen_ids = {uid for uid, _name in nl_citizens}

        nl_role = guild.get_role(int(nl_role_id))
        nl_member_ids = (
            {str(m.id) for m in nl_role.members if not m.bot} if nl_role else set()
        )

        approved_statuses = {"review", "approved", "apply", "linked", "manual"}
        approved_at = datetime.datetime.now(datetime.UTC).isoformat()

        scanned = 0
        to_apply = 0
        applied = 0
        skipped = 0
        conflicts = 0
        not_nl_citizen = 0
        not_nederlander_role = 0
        malformed = 0

        result_rows: list[dict[str, str]] = []

        for row in reader:
            scanned += 1
            discord_id = str((row.get("discord_id") or "")).strip()
            in_game_id = str((row.get("in_game_id") or "")).strip()
            status = str((row.get("status") or "review")).strip().lower()

            if not discord_id or not in_game_id:
                malformed += 1
                result_rows.append(
                    {
                        "discord_id": discord_id,
                        "in_game_id": in_game_id,
                        "status": "malformed",
                        "action": "skipped",
                        "note": "ontbrekende discord_id of in_game_id",
                    }
                )
                continue

            if status not in approved_statuses:
                skipped += 1
                result_rows.append(
                    {
                        "discord_id": discord_id,
                        "in_game_id": in_game_id,
                        "status": status,
                        "action": "skipped",
                        "note": "status niet gemarkeerd voor toepassen",
                    }
                )
                continue

            if in_game_id not in nl_citizen_ids:
                not_nl_citizen += 1
                result_rows.append(
                    {
                        "discord_id": discord_id,
                        "in_game_id": in_game_id,
                        "status": status,
                        "action": "rejected",
                        "note": "in-game ID staat niet in huidige NL citizens cache",
                    }
                )
                continue

            if discord_id not in nl_member_ids:
                not_nederlander_role += 1
                result_rows.append(
                    {
                        "discord_id": discord_id,
                        "in_game_id": in_game_id,
                        "status": status,
                        "action": "rejected",
                        "note": "Discord gebruiker heeft nu geen Nederlander-rol",
                    }
                )
                continue

            existing_discord = await db.get_identity_link_by_discord(
                discord_user_id=discord_id,
                guild_id=str(guild.id),
            )
            if existing_discord and existing_discord.get("in_game_user_id"):
                current = str(existing_discord.get("in_game_user_id"))
                if current != in_game_id and not overwrite_existing:
                    conflicts += 1
                    result_rows.append(
                        {
                            "discord_id": discord_id,
                            "in_game_id": in_game_id,
                            "status": status,
                            "action": "conflict",
                            "note": f"bestaande mapping: {current} (overwrite_existing=false)",
                        }
                    )
                    continue

            existing_ingame = await db.get_identity_links_by_ingame(
                in_game_user_id=in_game_id,
                guild_id=str(guild.id),
            )
            linked_other = next(
                (
                    link.get("discord_user_id")
                    for link in existing_ingame
                    if str(link.get("discord_user_id")) != discord_id
                ),
                None,
            )
            if linked_other:
                conflicts += 1
                result_rows.append(
                    {
                        "discord_id": discord_id,
                        "in_game_id": in_game_id,
                        "status": status,
                        "action": "conflict",
                        "note": f"in-game ID al gekoppeld aan {linked_other}",
                    }
                )
                continue

            to_apply += 1
            if not dry_run:
                await db.upsert_identity_link(
                    discord_user_id=discord_id,
                    guild_id=str(guild.id),
                    in_game_user_id=in_game_id,
                    nationality="nederlander",
                    request_type="backfill_nederlander_reviewed",
                    embassy_country=None,
                    approved_by_discord_id=str(interaction.user.id),
                    approved_at=approved_at,
                )
                applied += 1
                action = "applied"
                note = "mapping opgeslagen"
            else:
                action = "would_apply"
                note = "dry-run: mapping zou opgeslagen worden"

            result_rows.append(
                {
                    "discord_id": discord_id,
                    "in_game_id": in_game_id,
                    "status": status,
                    "action": action,
                    "note": note,
                }
            )

        out_csv = io.StringIO()
        out_csv.write("discord_id,in_game_id,status,action,note\n")
        for rr in result_rows:
            vals = []
            for key in ("discord_id", "in_game_id", "status", "action", "note"):
                # vals.append(f'"{str(rr.get(key, "")).replace('"', '""')}"')
                raw = str(rr.get(key, "")).replace('"', '""')
                vals.append(f'"{raw}"')
            out_csv.write(",".join(vals) + "\n")

        embed = discord.Embed(
            title="🧾 NL backfill CSV apply",
            color=discord.Color.orange() if dry_run else discord.Color.green(),
            description=(
                "Resultaat van toepassen van handmatig gereviewde CSV. "
                "Alleen expliciet goedgekeurde statussen zijn verwerkt."
            ),
        )
        embed.add_field(name="Rijen gescand", value=str(scanned), inline=True)
        embed.add_field(name="Bron", value=source_label, inline=False)
        embed.add_field(name="Klaar om toe te passen", value=str(to_apply), inline=True)
        embed.add_field(name="Toegepast", value=str(applied), inline=True)
        embed.add_field(name="Overgeslagen", value=str(skipped), inline=True)
        embed.add_field(name="Conflicts", value=str(conflicts), inline=True)
        embed.add_field(name="Malformed", value=str(malformed), inline=True)
        embed.add_field(name="Niet NL citizen", value=str(not_nl_citizen), inline=True)
        embed.add_field(
            name="Geen Nederlander-rol",
            value=str(not_nederlander_role),
            inline=True,
        )
        embed.set_footer(
            text=(
                "dry_run=true: niets weggeschreven"
                if dry_run
                else "dry_run=false: mappings opgeslagen"
            )
        )

        filename_ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        result_file = discord.File(
            io.BytesIO(out_csv.getvalue().encode("utf-8")),
            filename=f"nl_backfill_apply_result_{filename_ts}.csv",
        )
        await interaction.followup.send(embed=embed, file=result_file, ephemeral=True)

    @app_commands.command(
        name="mappings_not_nl",
        description="Toon mappings van gebruikers met Nederlander-rol naar niet-NL citizens",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def mappings_not_nl(self, interaction: discord.Interaction):
        db = await self._get_db()
        guild_id = str(interaction.guild_id)
        nl_country_id = str(self.config.get("nl_country_id") or "").strip()
        if not nl_country_id:
            await interaction.response.send_message(
                "Configuratie mist `nl_country_id`.", ephemeral=True
            )
            return

        nl_citizen_ids = {uid for uid, _ in await db.get_nl_citizen_ids(nl_country_id)}
        links = await db.get_identity_links_by_guild(guild_id=guild_id)
        not_nl_links = [
            link
            for link in links
            if link.get("nationality") == "nederlander"
            and str(link.get("in_game_user_id")) not in nl_citizen_ids
        ]
        if not not_nl_links:
            await interaction.response.send_message(
                "Geen mappings gevonden van Nederlander-rol naar niet-NL citizens.",
                ephemeral=True,
            )
            return
        
        embed = discord.Embed(
            title="🚩 Mappings Nederlander → niet-NL citizen",
            color=discord.Color.red(),
        )
        for link in not_nl_links[:10]:
            embed.add_field(
                name=f"<@{link['discord_user_id']}> → `{link['in_game_user_id']}`",
                value=(
                    f"Nationaliteit: {link['nationality']}\n"
                    f"Type: {link['request_type']}\n"
                    f"Updated: {link['updated_at']}"
                ),
                inline=False,
            )
        if len(not_nl_links) > 10:
            embed.set_footer(text=f"Toont 10 van {len(not_nl_links)} resultaten")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="syncmuroles",
        description="Synchroniseer MU-rollen op basis van citizen MU memberships",
    )
    @app_commands.describe(
        dry_run="Alleen tonen wat er zou gebeuren, zonder rollen toe te voegen"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def sync_mu_roles(
        self,
        interaction: discord.Interaction,
        dry_run: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "Dit commando kan alleen in een server gebruikt worden.", ephemeral=True
            )
            return

        try:
            stats = await self._sync_mu_roles_for_guild(guild=guild, dry_run=dry_run)
        except FileNotFoundError:
            await interaction.followup.send(
                f"MU template bestand niet gevonden: `{self._mus_template_path()}`",
                ephemeral=True,
            )
            return
        except json.JSONDecodeError:
            await interaction.followup.send(
                "MU template bevat ongeldige JSON.",
                ephemeral=True,
            )
            return
        except (OSError, ValueError, discord.DiscordException) as exc:
            logger.exception("sync_mu_roles: unexpected failure")
            await interaction.followup.send(
                f"Synchronisatie mislukt: {exc}",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🪖 MU role sync",
            color=discord.Color.orange() if dry_run else discord.Color.green(),
            description=(
                "Resultaat van MU role synchronisatie op basis van citizen MU memberships."
            ),
        )
        embed.add_field(
            name="Citizens met MU",
            value=str(stats["citizens_with_mu"]),
            inline=True,
        )
        embed.add_field(
            name="Mapped Discord users",
            value=str(stats["mapped_discord_users"]),
            inline=True,
        )
        embed.add_field(
            name="Members gevonden",
            value=str(stats["members_found"]),
            inline=True,
        )
        embed.add_field(
            name="Role ontbrak",
            value=str(stats["roles_missing_in_guild"]),
            inline=True,
        )
        embed.add_field(
            name="Had role al",
            value=str(stats["already_had_role"]),
            inline=True,
        )
        embed.add_field(
            name="Toe te wijzen",
            value=str(stats["to_assign"]),
            inline=True,
        )
        embed.add_field(
            name="Toegewezen",
            value=str(stats["assigned"]),
            inline=True,
        )
        embed.add_field(
            name="Te verwijderen",
            value=str(stats["to_remove"]),
            inline=True,
        )
        embed.add_field(
            name="Verwijderd",
            value=str(stats["removed"]),
            inline=True,
        )
        embed.add_field(
            name="Member niet gevonden",
            value=str(stats["member_not_found"]),
            inline=True,
        )
        embed.add_field(name="Fouten", value=str(stats["errors"]), inline=True)
        embed.set_footer(
            text=(
                "dry_run=true: geen rollen aangepast"
                if dry_run
                else "dry_run=false: rollen zijn waar nodig toegevoegd"
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


    @app_commands.command(
        name="remove_mapping",
        description="Verwijder een bestaande Discord↔in-game mapping",
    )
    @app_commands.describe(
        discord_user_id="Discord user ID (bijv. 123456789012345678)",
        reason="Reden voor verwijdering (optioneel)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_mapping(
        self,
        interaction: discord.Interaction,
        discord_user_id: str,
        reason: str = "Geen reden opgegeven",
    ) -> None:
        guild_id = str(interaction.guild_id)

        db = await self._get_db()
        existing = await db.get_identity_link_by_discord(
            discord_user_id=discord_user_id,
            guild_id=guild_id,
        )
        if not existing or not existing.get("in_game_user_id"):
            await interaction.response.send_message(
                "Er is geen bestaande mapping om te verwijderen.", ephemeral=True
            )
            return

        await db.delete_identity_link(
            discord_user_id=discord_user_id,
            guild_id=guild_id,
        )

        await interaction.response.send_message(
            f"Mapping voor <@{discord_user_id}> is verwijderd. Reden: {reason}",
            ephemeral=True,
        )

    @app_commands.command(
        name="filter-nederlanders",
        description="Verwijder Nederlander-rol van leden zonder NL citizen mapping",
    )
    @app_commands.describe(
        exceptions="Optioneel: comma-separated lijst van Discord user IDs die NIET gefilterd moeten worden",
        dry_run="Alleen previewen wie er gefilterd zou worden, zonder rollen te verwijderen",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def filter_nederlanders(
        self,
        interaction: discord.Interaction,
        exceptions: str | None = None,
        dry_run: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "Dit commando kan alleen in een server gebruikt worden.", ephemeral=True
            )
            return

        nl_country_id = str(self.config.get("nl_country_id") or "").strip()
        nl_role_id = (self.config.get("roles") or {}).get("nederlander")
        if not nl_country_id or not nl_role_id:
            await interaction.followup.send(
                "Configuratie mist `nl_country_id` of `roles.nederlander`.",
                ephemeral=True,
            )
            return

        nl_role = guild.get_role(int(nl_role_id))
        if nl_role is None:
            await interaction.followup.send(
                "De Nederlander-rol kon niet worden gevonden in deze server.",
                ephemeral=True,
            )
            return

        exception_ids = {e.strip() for e in (exceptions or "").split(",") if e.strip()}

        db = await self._get_db()
        nl_citizen_ids = {uid for uid, _ in await db.get_nl_citizen_ids(nl_country_id)}
        members_to_filter: list[discord.Member] = []
        for member in nl_role.members:
            if member.bot:
                continue
            discord_id = str(member.id)
            if discord_id in exception_ids:
                continue
            existing = await db.get_identity_link_by_discord(
                discord_user_id=discord_id,
                guild_id=str(guild.id),
            )
            print(existing)
            if not existing or str(existing.get("in_game_user_id")) not in nl_citizen_ids:
                members_to_filter.append(member)
        if not members_to_filter:
            await interaction.followup.send(
                "Geen leden gevonden die gefilterd moeten worden.", ephemeral=True
            )
            return
        
        doubt_role = guild.get_role(
            int(self.config.get("roles", {}).get("twijfelgeval", 0))
        )
        foreigner_role = guild.get_role(
            int(self.config.get("roles", {}).get("foreigner", 0))
        )
        if not dry_run and not doubt_role and not foreigner_role:
            await interaction.followup.send(
                "Geen twijfelgeval- of foreigner-rol gevonden in de configuratie. "
                "Rollen kunnen niet worden verwijderd.",
                ephemeral=True,
            )
            return

        if not dry_run:
            for member in members_to_filter:
                try:
                    # strip all roles
                    await member.edit(roles=[doubt_role, foreigner_role], reason="Filteren op NL citizen mapping")
                except Exception as exc:
                    logger.warning(
                        "Failed to remove NL role from %s (%s): %s",
                        member.display_name,
                        member.id,
                        exc,
                    )

        id_channel = guild.get_channel(
            int(self.config.get("channels", {}).get("id_controle", 0))
        )
        welcome_channel = guild.get_channel(
            int(self.config.get("channels", {}).get("welcome_buttons", 0))
        )
        if id_channel and isinstance(id_channel, discord.TextChannel):
            member_mentions = f"{doubt_role.mention}" if doubt_role else ""
            member_mentions += ", ".join(m.mention for m in members_to_filter)

            await id_channel.send(
                member_mentions
            )

            embed = discord.Embed(
                title="Nederlander verificatie",
                description=(
                    f"Onze bot heeft jullie Nederlander-rol verwijderd omdat "
                    f"er geen geldige mapping naar een NL citizen in onze "
                    f"database was. \n\n"
                    f"Voor verificatie vragen we jullie een ticket te openen "
                    f"via <#{welcome_channel.id}> en de instructies te volgen. \n\n"
                    f"Mogelijk ben je geflagged omdat je langere tijd geen WarEra gespeeld hebt. "
                    f"Als je het spel niet meer speelt willen we je vragen om de server te verlaten."
                )
            )

            await id_channel.send(embed=embed)

        await interaction.followup.send(
            f"{len(members_to_filter)} leden zouden gefilterd worden. "
            f"{'Rollen zijn verwijderd.' if not dry_run else 'Dry-run: geen rollen verwijderd.'}",
            ephemeral=True,
        )


async def setup(bot) -> None:
    await bot.add_cog(Users(bot))
