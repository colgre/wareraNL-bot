"""
Base class shared by all command cogs.

Provides safe access to shared services and common Discord utilities.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Union
from zoneinfo import ZoneInfo

_TZ_NL = ZoneInfo("Europe/Amsterdam")


def fmt_nl_time(utc_iso: str, fmt: str = "%d-%m-%Y %H:%M") -> str:
    """Convert a UTC ISO timestamp string to Dutch time (CET/CEST)."""
    if not utc_iso:
        return ""
    try:
        s = utc_iso.strip().replace("Z", "+00:00")
        if "+" not in s and len(s) <= 19:
            s += "+00:00"
        dt = datetime.fromisoformat(s)
        nl_dt = dt.astimezone(_TZ_NL)
        return nl_dt.strftime(fmt) + " " + nl_dt.strftime("%Z")
    except (ValueError, TypeError):
        return utc_iso[:16].replace("T", " ")

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

_DIVISION_PREFIX_RE = re.compile(r"^\[D\d\] ")


def strip_division_prefix(name: str) -> str:
    """Strip a war-guild division prefix (e.g. '[D1] ') from a display name."""
    return _DIVISION_PREFIX_RE.sub("", name)

from services.country_utils import (
    _COUNTRY_ALIASES,
    ALL_COUNTRY_NAMES,
    extract_country_list,
)


async def citizen_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Module-level autocomplete callback for player name parameters.

    Queries citizen_levels table (populated by /peil burgers) for a
    case-insensitive substring match. Falls back to empty list if DB
    is not yet available.
    """
    db = getattr(interaction.client, "_ext_db", None)
    if not db:
        return []
    try:
        matches = await db.search_citizen_names(current, limit=25)
        return [app_commands.Choice(name=name, value=name) for name, _uid in matches]
    except Exception:
        return []


async def country_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Module-level autocomplete callback for country name parameters.

    Names come from ``country_snapshots`` — refreshed hourly by the fetcher —
    so countries added or renamed in-game are usually offered immediately.
    The static ``ALL_COUNTRY_NAMES`` list is merged in on top of that rather
    than only used when the DB is completely empty: a DB that already has
    most countries but hasn't picked up a brand-new one yet (confirmed live:
    this silently hid both Liechtenstein and, later, Maldives — in the
    Maldives case even after the static list was updated, since the old
    all-or-nothing fallback never kicked in with 180 other countries already
    present) would otherwise never show it until the next fetcher run.

    Matches English names as usual, and also Dutch names (showing them as
    "English (Dutch)" so the user can see both).
    Use this directly in ``@app_commands.autocomplete(country=country_autocomplete)``.
    """
    names: list[str] = []
    db = getattr(interaction.client, "_ext_db", None)
    if db:
        try:
            name_map = await db.get_country_name_map()
            names = list(name_map.values())
        except Exception:
            names = []

    seen_lower = {n.lower() for n in names}
    for name in ALL_COUNTRY_NAMES:
        if name.lower() not in seen_lower:
            names.append(name)
            seen_lower.add(name.lower())
    names.sort(key=str.casefold)

    q = current.strip().lower()
    results: list[app_commands.Choice[str]] = []
    seen: set[str] = set()

    # English name matches
    for name in names:
        if q in name.lower() and name not in seen:
            results.append(app_commands.Choice(name=name, value=name))
            seen.add(name)

    # Dutch alias matches — show as "English (Dutch)" with English value
    if len(results) < 25:
        for dutch_lower, english_lower in _COUNTRY_ALIASES.items():
            if q in dutch_lower and len(results) < 25:
                english = next(
                    (n for n in names if n.lower() == english_lower), None
                )
                if english and english not in seen:
                    results.append(
                        app_commands.Choice(
                            name=f"{english} ({dutch_lower.title()})",
                            value=english,
                        )
                    )
                    seen.add(english)

    return results[:25]


async def dreigingsniveau_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Module-level autocomplete callback for dreigingsniveau parameters."""
    q = current.strip().lower()
    options = ["groen", "geel", "oranje", "rood"]
    return [
        app_commands.Choice(name=option.capitalize(), value=option)
        for option in options
        if q in option
    ]


class CommandCogBase(commands.Cog):
    """Mixin providing service properties and shared utilities for command cogs."""

    @property
    def _db(self):
        return getattr(self.bot, "_ext_db", None)

    @property
    def _client(self):
        return getattr(self.bot, "_ext_client", None)

    @property
    def config(self) -> dict:
        return getattr(self.bot, "config", {})

    def _embed_colour(self, key: str = "primary") -> discord.Colour:
        raw = (self.config.get("colors") or {}).get(key, "0xffb612")
        try:
            return discord.Colour(int(str(raw), 16))
        except Exception:
            return discord.Colour.gold()

    def _api_status_note(self) -> str:
        client = self._client
        if not client or not hasattr(client, "status_snapshot"):
            return ""
        try:
            status = client.status_snapshot()
        except Exception:
            return ""

        parts: list[str] = []
        last_success = status.get("last_success_at")
        if isinstance(last_success, datetime):
            ts = int(last_success.timestamp())
            parts.append(f"Laatste succesvolle API-call: <t:{ts}:R>.")
        last_failure = status.get("last_failure_at")
        if isinstance(last_failure, datetime):
            ts = int(last_failure.timestamp())
            parts.append(f"Laatste fout: <t:{ts}:R>.")
        error = status.get("last_error")
        if error:
            parts.append(f"Foutmelding: `{str(error)[:180]}`")
        return "\n".join(parts)

    def _api_offline_embed(self, note: str = "") -> discord.Embed:
        """Return a standardised 'API offline' embed."""
        desc = (
            "⚠️ De WarEra API is momenteel niet beschikbaar.\n"
            "Als deze command lokale cache of databasehistorie heeft, gebruiken we die "
            "waar mogelijk. Dan is de data niet realtime."
        )
        status_note = self._api_status_note()
        if status_note:
            desc += f"\n\n{status_note}"
        if note:
            desc += f"\n\n{note}"
        embed = discord.Embed(
            title="🔌 API Offline",
            description=desc,
            colour=self._embed_colour("warning"),
        )
        embed.set_footer(text="WareraNL Bot")
        return embed

    async def _send_api_offline(
        self,
        ctx: Union[Context, discord.Interaction],
        note: str = "",
    ) -> None:
        """Send a standardised 'API offline' message to *ctx* or *interaction*."""
        embed = self._api_offline_embed(note)
        if isinstance(ctx, discord.Interaction):
            if ctx.response.is_done():
                await ctx.followup.send(embed=embed)
            else:
                await ctx.response.send_message(embed=embed, ephemeral=True)
        else:
            await ctx.send(embed=embed)

    async def _fetch_country_list(self, ctx: Union[Context, discord.Interaction]) -> list[dict] | None:
        """Fetch and unwrap the country list; sends an API-offline embed on failure."""
        if not self._client or self._client.is_available is False:
            await self._send_api_offline(ctx)
            return None
        try:
            resp = await self._client.get("/country.getAllCountries")
        except Exception:
            await self._send_api_offline(ctx)
            return None
        result = extract_country_list(resp)
        if not result:
            await self._send_api_offline(ctx, "Lege landenlijst ontvangen van de API.")
            return None
        return result
