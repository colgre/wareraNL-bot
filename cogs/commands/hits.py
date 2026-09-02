"""Slash command /hits — waar heeft een speler al geraakt in actieve gevechten?

Fetches every currently active battle (battle.getBattles, isActive: true,
single page of up to 100 — same assumption cogs/commands/gevechten.py
already makes about that being enough to cover all live battles), then
calls battleLootSummary.getByBattleAndUser(battleId, userId) once per
battle for the target player to see whether they've dealt any damage there
yet.

Confirmed live (reported directly by testing the endpoint): when a player
hasn't hit in a given battle, the endpoint doesn't return clean "no data"
JSON — it returns something resp.json() can't parse. services.api_client's
APIClient.get() already treats that as non-fatal (falls back to resp.text()
instead of raising) for a 2xx response, and a non-2xx response raises,
which is caught here too. Either way there's no richer signal available at
this endpoint to distinguish "confirmed no hit" from "malformed/empty
response" — both are treated as "not hit yet".

Optional loot=True adds, for every battle already hit in, whether the
player is currently in line for round loot and/or full-battle loot, and if
not, how much more damage they need.

Confirmed live this can't come from battleLootSummary's own "poolLoot"
field: that only lists loot already *finalized* (past, completed rounds —
and only once the whole battle itself has ended, for the battle-wide pool).
For the still-active round and the still-ongoing battle, battleRanking.
getRanking (dataType=damage, type=user, side=attacker|defender, with
either roundId or battleId — never both) already returns a live, so-far
projection: every entry is sorted by damage descending and carries a
"lootItem" once its damage clears that scope's current cutoff. There's no
"which side is this player on" or "give me just this user" filter on this
endpoint (confirmed live — extra params are silently ignored), so this
pages through the ranking (capped at _MAX_RANKING_PAGES x 100 entries per
side, trying the player's own country's side first as a hint, then the
other side) looking for two things at once: the player's own entry, and
the transition point from "has lootItem" to doesn't — the lowest damage
value that still earned loot, i.e. the target the player needs to clear.
Battles with more participants than the page cap covers on the relevant
side won't resolve; that shows as "onbekend" rather than a guess — this is
a real ceiling, not a bug: the endpoint has no "look up just this user" or
"which side is this user on" filter (confirmed live), so the only way to
find a low-ranked player is to page through everyone ranked above them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import (
    CommandCogBase,
    citizen_autocomplete,
    strip_division_prefix,
)

logger = logging.getLogger("discord_bot")

_BATTLE_URL = "https://app.warera.io/battle/{battle_id}"
_REQUEST_DELAY = 0.15
_DESCRIPTION_CHAR_LIMIT = 3900  # embed description hard limit is 4096; leave headroom
_MAX_RANKING_PAGES = 6  # per (scope, side) — bounds worst-case API calls per hit battle


def _unwrap(resp: object) -> object:
    if not isinstance(resp, dict):
        return resp
    d = resp.get("result", resp)
    if isinstance(d, dict):
        return d.get("data", d)
    return d


def _battle_label(battle: dict, country_names: dict[str, str]) -> str:
    def_id = str((battle.get("defender") or {}).get("country") or "")
    att_id = str((battle.get("attacker") or {}).get("country") or "")
    def_name = country_names.get(def_id, def_id or "?")
    att_name = country_names.get(att_id, att_id or "?")
    return f"{def_name} vs {att_name}"


def _chunk_section(title: str, lines: list[str]) -> list[str]:
    """Split a titled list of lines into one or more embed-description-sized
    chunks, breaking only between lines — never mid-line — so a long loot
    annotation can't get cut off halfway through a markdown link.

    Previously this just hard-sliced the final combined description to
    _DESCRIPTION_CHAR_LIMIT chars, which could (a) cut a line in half,
    producing a broken, unclosed [label](url), and (b) silently drop an
    entire section (confirmed live: "Nog niet geraakt" could vanish
    completely once "Geraakt" alone — with loot=True's much longer
    per-line annotations — already used up the whole budget).
    """
    header = f"**{title} ({len(lines)})**"
    if not lines:
        return [f"{header}\n—"]
    chunks: list[str] = []
    current = header
    for line in lines:
        candidate = f"{current}\n{line}"
        if len(candidate) > _DESCRIPTION_CHAR_LIMIT and current != header:
            chunks.append(current)
            current = f"{header} (vervolg)\n{line}"
        else:
            current = candidate
    chunks.append(current)
    return chunks


def _fmt_int(n: object) -> str:
    return f"{int(n):,}".replace(",", ".")


async def _scan_ranking(
    client, *, battle_id: str | None, round_id: str | None, side: str, user_id: str
) -> tuple[float | None, float | None, bool | None]:
    """Page through battleRanking.getRanking (capped) for one scope+side.

    Looks for two things at once: the target player's own entry, and the
    loot cutoff — the lowest damage value that still earned loot. Returns
    (cutoff_value, player_value, player_has_loot); any can be None if not
    found within the page cap.
    """
    cursor: str | None = None
    cutoff_value: float | None = None
    last_loot_value: float | None = None
    player_value: float | None = None
    player_has_loot: bool | None = None

    for _page in range(_MAX_RANKING_PAGES):
        payload: dict = {"dataType": "damage", "type": "user", "side": side, "limit": 100}
        if round_id:
            payload["roundId"] = round_id
        else:
            payload["battleId"] = battle_id
        if cursor:
            payload["cursor"] = cursor
        try:
            raw = await client.get("/battleRanking.getRanking", params={"input": json.dumps(payload)})
        except Exception:
            break
        data = _unwrap(raw)
        entries: list[dict] = data.get("items", []) if isinstance(data, dict) else []
        if not entries:
            break

        for e in entries:
            got_loot = bool(e.get("lootItem"))
            if got_loot:
                last_loot_value = e.get("value")
            elif cutoff_value is None and last_loot_value is not None:
                cutoff_value = last_loot_value
            if str(e.get("user")) == user_id:
                player_value = e.get("value")
                player_has_loot = got_loot

        if cutoff_value is not None and player_value is not None:
            break

        cursor = data.get("nextCursor") if isinstance(data, dict) else None
        if not cursor:
            # Reached the end without ever seeing a no-loot entry — every
            # participant who dealt damage got loot; the lowest value seen
            # is the empirical cutoff.
            if cutoff_value is None:
                cutoff_value = last_loot_value
            break
        await asyncio.sleep(_REQUEST_DELAY)

    return cutoff_value, player_value, player_has_loot


async def _loot_status(
    client, battle: dict, user_id: str, country_id: str | None
) -> tuple[str, str]:
    """Return (round_status, battle_status) display strings for one hit battle."""
    battle_id = str(battle.get("_id", ""))
    rounds = battle.get("rounds") or []
    round_id = rounds[-1] if rounds else None

    att_country = str((battle.get("attacker") or {}).get("country") or "")
    def_country = str((battle.get("defender") or {}).get("country") or "")
    side_hint: str | None = None
    if country_id and country_id == att_country:
        side_hint = "attacker"
    elif country_id and country_id == def_country:
        side_hint = "defender"

    sides: list[str] = []
    for s in (side_hint, "attacker", "defender"):
        if s and s not in sides:
            sides.append(s)

    round_cutoff = round_value = None
    round_has_loot: bool | None = None
    battle_cutoff = battle_value = None
    battle_has_loot: bool | None = None

    for side in sides:
        if round_id and round_value is None:
            c, v, hl = await _scan_ranking(
                client, battle_id=None, round_id=round_id, side=side, user_id=user_id
            )
            if v is not None:
                round_cutoff, round_value, round_has_loot = c, v, hl
            elif round_cutoff is None:
                round_cutoff = c
        if battle_value is None:
            c, v, hl = await _scan_ranking(
                client, battle_id=battle_id, round_id=None, side=side, user_id=user_id
            )
            if v is not None:
                battle_cutoff, battle_value, battle_has_loot = c, v, hl
            elif battle_cutoff is None:
                battle_cutoff = c
        if round_value is not None and battle_value is not None:
            break

    return (
        _loot_status_text(round_has_loot, round_value, round_cutoff),
        _loot_status_text(battle_has_loot, battle_value, battle_cutoff),
    )


def _loot_status_text(
    has_loot: bool | None, player_value: float | None, cutoff_value: float | None
) -> str:
    if has_loot is True:
        return "🎁 loot"
    if has_loot is False and cutoff_value is not None and player_value is not None:
        needed = max(0, int(cutoff_value) - int(player_value))
        if needed <= 0:
            return "🎁 loot"
        return f"nog {_fmt_int(needed)} nodig"
    return "onbekend"


class HitsCog(CommandCogBase, name="hits"):
    """Slash command /hits — per-speler overzicht van actieve gevechten wel/niet geraakt."""

    def __init__(self, bot) -> None:
        self.bot = bot

    async def _resolve_target(
        self, ctx: Context, speler: Optional[str]
    ) -> tuple[str, str] | None:
        """Return (user_id, citizen_name) for *speler*, or the caller when None.

        Same two-tier caller resolution as /paraatheid's _resolve_caller_mu:
        Discord display name (minus war-guild division prefix) first, then
        identity_links scoped to this guild as a fallback.
        """
        if speler:
            try:
                citizen = await self._db.get_citizen_by_name_exact(speler)
            except Exception:
                citizen = None
            if citizen:
                return citizen[0], citizen[1]
            try:
                matches = await self._db.find_citizen_readiness(speler)
            except Exception:
                matches = []
            if matches:
                m = matches[0]
                return m["user_id"], m["citizen_name"]
            return None

        name = strip_division_prefix(ctx.author.display_name).strip()
        if name:
            try:
                citizen = await self._db.get_citizen_by_name_exact(name)
            except Exception:
                citizen = None
            if citizen:
                return citizen[0], citizen[1]

        if ctx.guild:
            try:
                link = await self._db.get_identity_link_by_discord(
                    str(ctx.author.id), str(ctx.guild.id)
                )
            except Exception:
                link = None
            user_id = (link or {}).get("in_game_user_id")
            if user_id:
                cit_name = await self._db.get_citizen_name_by_id(user_id)
                return user_id, cit_name or user_id
        return None

    @commands.hybrid_command(
        name="hits",
        description="Toon in welke actieve gevechten een speler al geraakt heeft, en waar nog niet.",
    )
    @app_commands.describe(
        speler="Zoek een speler op naam (standaard: jezelf).",
        loot="Toon per geraakt gevecht of er ronde-/gevechtsloot binnen bereik is, en zo niet hoeveel schade nog nodig is.",
    )
    @app_commands.autocomplete(speler=citizen_autocomplete)
    async def hits(
        self, ctx: Context, speler: Optional[str] = None, loot: bool = False
    ) -> None:
        if not self._db or not self._client:
            await ctx.send("Database of API niet beschikbaar.")
            return

        target = await self._resolve_target(ctx, speler)
        if target is None:
            await ctx.send(
                "Speler niet gevonden. Geef een naam op met `speler:`, of zorg dat je geverifieerd bent."
            )
            return
        user_id, citizen_name = target

        if hasattr(ctx, "defer"):
            await ctx.defer()

        try:
            raw = await self._client.get(
                "/battle.getBattles",
                params={"input": json.dumps({"isActive": True, "limit": 100})},
            )
        except Exception as exc:
            logger.warning("hits: getBattles failed: %s", exc)
            await ctx.send("Kon actieve gevechten niet ophalen.")
            return

        data = _unwrap(raw)
        if isinstance(data, dict):
            battles: list[dict] = [b for b in data.get("items", []) if isinstance(b, dict)]
        elif isinstance(data, list):
            battles = [b for b in data if isinstance(b, dict)]
        else:
            battles = []

        if not battles:
            await ctx.send("Geen actieve gevechten gevonden.")
            return

        try:
            country_names = await self._db.get_country_name_map()
        except Exception:
            country_names = {}

        country_id: str | None = None
        if loot:
            try:
                countries = await self._db.get_citizen_countries_by_ids([user_id])
                entry = countries.get(user_id)
                country_id = entry[0] if entry else None
            except Exception:
                country_id = None

        hit_lines: list[str] = []
        no_hit_lines: list[str] = []

        for i, battle in enumerate(battles):
            battle_id = str(battle.get("_id", ""))
            if not battle_id:
                continue
            if i > 0:
                await asyncio.sleep(_REQUEST_DELAY)

            summary: object = None
            try:
                raw_summary = await self._client.get(
                    "/battleLootSummary.getByBattleAndUser",
                    params={"input": json.dumps({"battleId": battle_id, "userId": user_id})},
                )
                summary = _unwrap(raw_summary)
            except Exception:
                summary = None

            url = _BATTLE_URL.format(battle_id=battle_id)
            label = _battle_label(battle, country_names)

            if isinstance(summary, dict) and summary:
                # Confirmed live: the field is "totalDmg" (not "totalDamage",
                # despite that being the name used in cogs/tasks/daily_dmg.py's
                # docstring) — kept as a fallback in case the API is inconsistent.
                dmg = summary.get("totalDmg") or summary.get("totalDamage")
                if dmg:
                    line = f"[{label}]({url}) — {_fmt_int(dmg)}"
                else:
                    line = f"[{label}]({url})"
                if loot:
                    round_status, battle_status = await _loot_status(
                        self._client, battle, user_id, country_id
                    )
                    line += f" | Ronde: {round_status} | Totaal: {battle_status}"
                hit_lines.append(line)
            else:
                no_hit_lines.append(f"[{label}]({url})")

        chunks = _chunk_section("✅ Geraakt", hit_lines) + _chunk_section(
            "❌ Nog niet geraakt", no_hit_lines
        )
        for i, chunk in enumerate(chunks):
            title = f"⚔️ Hits — {citizen_name}"
            if len(chunks) > 1:
                title += f" ({i + 1}/{len(chunks)})"
            embed = discord.Embed(title=title, description=chunk, colour=self._embed_colour())
            await ctx.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(HitsCog(bot))
