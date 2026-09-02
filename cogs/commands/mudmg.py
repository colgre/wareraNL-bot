"""Slash command /mudmg — Dutch MU damage overview or per-member breakdown.

Usage
-----
/mudmg              — overview of all Dutch MUs grouped by category (Elite, Eco, Casual)
/mudmg mu:Name      — per-member weekly damage table for the given MU
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

_UID_RE = re.compile(r'^[0-9a-f]{20,}$', re.ASCII)


def _looks_like_uid(s: str | None) -> bool:
    """Return True if *s* is a raw hex user-ID rather than a human-readable name."""
    return bool(s and _UID_RE.match(s))

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase
from cogs.tasks.mus import mus_path
from cogs.tasks.war_guild_divisions import DIVISION_MUS
from services.damage_calc import fmt_damage

_TZ_NL = ZoneInfo("Europe/Amsterdam")


def _nl_week_range(weeks_ago: int = 1) -> tuple[str, str]:
    """Return (start_iso, end_iso) UTC strings for a past WarEra weekly damage window.

    WarEra resets weekly damage on Monday 02:00 NL time.
    weeks_ago=1 → last week's window (Mon 02:00 → Mon 02:00).
    """
    now_nl = datetime.now(_TZ_NL)
    days_since_monday = now_nl.weekday()  # Monday = 0
    this_monday_nl = (now_nl - timedelta(days=days_since_monday)).replace(
        hour=2, minute=0, second=0, microsecond=0
    )
    end_nl = this_monday_nl - timedelta(weeks=weeks_ago - 1)
    start_nl = end_nl - timedelta(weeks=1)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return (
        start_nl.astimezone(timezone.utc).strftime(fmt),
        end_nl.astimezone(timezone.utc).strftime(fmt),
    )

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

# ── Category display config ──────────────────────────────────────────────────

_CATEGORY_ORDER = ["Elite", "Eco", "Standaard"]
_CATEGORY_LABEL: dict[str, str] = {
    "Elite": "🎖️ Elite MU's",
    "Eco": "🏭 Eco MU's",
    "Standaard": "🛡️ Casual MU's",
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _load_mus(testing: bool = False) -> list[dict]:
    """Load MU entries from templates/mus.json."""
    path = mus_path(testing)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("embeds", [])


def _mu_rankings(mu_data: dict) -> tuple[float, float]:
    """Extract (weekly_damage, total_damage) from a mu.getById payload."""
    if not isinstance(mu_data, dict):
        return 0.0, 0.0

    # The payload may be wrapped under a nested "mu" key
    obj = mu_data.get("mu") if isinstance(mu_data.get("mu"), dict) else mu_data

    rankings = obj.get("rankings") if isinstance(obj, dict) else None
    if not isinstance(rankings, dict):
        return 0.0, 0.0

    weekly_obj = rankings.get("muWeeklyDamages") or {}
    total_obj = rankings.get("muDamages") or {}

    weekly = float(weekly_obj.get("value") or 0)
    total = float(total_obj.get("value") or 0)
    return weekly, total


def _mu_members(mu_data: dict) -> list[str]:
    """Extract the list of member user-IDs from a mu.getById payload."""
    if not isinstance(mu_data, dict):
        return []
    obj = mu_data.get("mu") if isinstance(mu_data.get("mu"), dict) else mu_data
    if not isinstance(obj, dict):
        return []
    members = obj.get("members") or []
    return [str(m) for m in members if m]


def _overview_table(
    rows: list[tuple[str, float, float]], subtotals: tuple[float, float]
) -> str:
    """Render category rows as a fixed-width code-block table.

    rows: [(name, weekly_damage, total_damage), ...]
    subtotals: (weekly_subtotal, total_subtotal)
    """
    if not rows:
        return "*Geen data*"

    name_w = min(max(len(r[0]) for r in rows), 26)
    name_w = max(name_w, len("Subtotaal"))
    W = 10
    T = 10

    header = f"{'Naam':<{name_w}}  {'Wekelijks':>{W}}  {'Totaal':>{T}}"
    sep = "\u2500" * len(header)
    lines = [header, sep]

    for name, weekly, total in rows:
        display = name[:name_w] if len(name) > name_w else name
        w_str = fmt_damage(weekly) if weekly else "\u2014"
        t_str = fmt_damage(total) if total else "\u2014"
        lines.append(f"{display:<{name_w}}  {w_str:>{W}}  {t_str:>{T}}")

    lines.append(sep)
    sw, st = subtotals
    lines.append(
        f"{'Subtotaal':<{name_w}}  {fmt_damage(sw) if sw else chr(0x2014):>{W}}  "
        f"{fmt_damage(st) if st else chr(0x2014):>{T}}"
    )

    return "```\n" + "\n".join(lines) + "\n```"


def _extract_name_map(resp: object) -> dict[str, str]:
    """Parse a ranking response into {user_id: citizen_name}, skipping hex-only IDs."""
    result: dict[str, str] = {}
    if not isinstance(resp, dict):
        return result
    data: object = resp
    for key in ("result", "data"):
        if isinstance(data, dict) and key in data:
            inner = data[key]  # type: ignore[index]
            if isinstance(inner, dict):
                data = inner
    items: list = []
    if isinstance(data, dict):
        for key in ("items", "ranking", "rankings", "data", "results"):
            v = data.get(key)  # type: ignore[union-attr]
            if isinstance(v, list):
                items = v
                break
    for item in items:
        if not isinstance(item, dict):
            continue
        uid = item.get("user")
        if isinstance(uid, dict):
            uid = uid.get("_id") or uid.get("id") or uid.get("userId")
        if not uid or not isinstance(uid, str):
            continue
        name: str | None = None
        for key in ("username", "name", "citizenName"):
            v = item.get(key)
            if isinstance(v, str) and v:
                name = v
                break
        user_obj = item.get("user")
        if not name and isinstance(user_obj, dict):
            for key in ("username", "name"):
                v = user_obj.get(key)
                if isinstance(v, str) and v:
                    name = v
                    break
        if name and not _looks_like_uid(name):
            result[uid] = name
    return result


def _extract_total_damage_map(resp: object) -> dict[str, float]:
    """Parse a ranking.getRanking[userDamages] response into {user_id: total_damage}."""
    result: dict[str, float] = {}
    if not isinstance(resp, dict):
        return result
    # unwrap tRPC envelopes
    data: object = resp
    for key in ("result", "data"):
        if isinstance(data, dict) and key in data:
            inner = data[key]  # type: ignore[index]
            if isinstance(inner, dict):
                data = inner
    # find items list
    items: list = []
    if isinstance(data, dict):
        for key in ("items", "ranking", "rankings", "data", "results"):
            v = data.get(key)  # type: ignore[union-attr]
            if isinstance(v, list):
                items = v
                break
    for item in items:
        if not isinstance(item, dict):
            continue
        uid = item.get("user")
        val = item.get("value")
        if uid and isinstance(val, (int, float)):
            result[str(uid)] = float(val)
    return result


# ── Autocomplete ─────────────────────────────────────────────────────────────


async def mu_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest Dutch MU names for the 'mu' parameter."""
    all_names = [name for names in DIVISION_MUS.values() for name in names]
    q = current.strip().lower()
    return [
        app_commands.Choice(name=n, value=n)
        for n in all_names
        if q in n.lower()
    ][:25]


# ── Cog ──────────────────────────────────────────────────────────────────────


class MudmgCog(CommandCogBase, name="mudmg"):
    """Cog for the /mudmg command."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot

    async def _build_division_entries(self) -> list[dict]:
        """Build MU entry list from DIVISION_MUS with IDs from DB (known_mus) or mus.json fallback."""
        # Fallback: name→id from mus.json
        json_id_map: dict[str, str] = {}
        json_thumb_map: dict[str, str] = {}
        try:
            for e in _load_mus(getattr(self.bot, "testing", False)):
                key = e["name"].lower()
                json_id_map[key] = e["id"]
                if e.get("thumbnail"):
                    json_thumb_map[key] = e["thumbnail"]
        except Exception:
            pass

        # Primary: name→id from known_mus DB table
        db_id_map: dict[str, str] = {}
        if self._db:
            try:
                for mu_id, mu_name, _country_id in await self._db.get_all_known_mu_ids():
                    db_id_map[mu_name.lower()] = mu_id
            except Exception:
                pass

        entries = []
        for div, names in DIVISION_MUS.items():
            for name in names:
                key = name.lower()
                mu_id = db_id_map.get(key) or json_id_map.get(key)
                if not mu_id:
                    logger.warning("mudmg: no ID found for MU %r (not in DB or mus.json)", name)
                    continue
                entry: dict = {"id": mu_id, "name": name, "type": f"D{div}"}
                if key in json_thumb_map:
                    entry["thumbnail"] = json_thumb_map[key]
                entries.append(entry)
        return entries

    # ── Command ───────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="mudmg",
        description="Toon schadeoverzicht van Nederlandse Militaire Eenheden.",
    )
    @app_commands.describe(
        mu="Optioneel: naam van een specifieke MU voor een ledendetailoverzicht.",
        sorteren="Sorteren op wekelijkse schade (standaard) of totale schade.",
        vorige_week="Toon de schade van vorige week in plaats van deze week.",
    )
    @app_commands.autocomplete(mu=mu_autocomplete)
    @app_commands.choices(sorteren=[
        app_commands.Choice(name="Wekelijks (standaard)", value="weekly"),
        app_commands.Choice(name="Totaal", value="total"),
    ])
    async def mudmg(
        self,
        ctx: Context,
        mu: Optional[str] = None,
        sorteren: str = "weekly",
        vorige_week: bool = False,
    ) -> None:
        """Show MU damage overview or per-member breakdown."""
        if not self._client:
            await self._send_api_offline(ctx)
            return

        if hasattr(ctx, "defer"):
            await ctx.defer()

        entries = await self._build_division_entries()
        if not entries:
            await ctx.send("Geen MU-configuratie gevonden.")
            return

        if mu:
            await self._send_mu_detail(ctx, mu, entries, last_week=vorige_week)
        else:
            await self._send_mu_overview(ctx, entries, sort_by_total=sorteren == "total" and not vorige_week, last_week=vorige_week)

    # ── Overview (no MU arg) ──────────────────────────────────────────────────

    async def _send_mu_overview(
        self, ctx: Context, entries: list[dict], sort_by_total: bool = False, last_week: bool = False
    ) -> None:
        """Fetch all MU stats and send a flat ranked overview."""
        rows: list[tuple[str, str, float, float]] = []
        grand_weekly = 0.0
        grand_total = 0.0

        if last_week:
            start_iso, end_iso = _nl_week_range(weeks_ago=1)
            mu_ids = [e["id"] for e in entries]
            try:
                dmg_map = await self._db.get_mu_damage_in_range(mu_ids, start_iso, end_iso)
            except Exception as exc:
                logger.warning("mudmg overview last_week: DB query failed: %s", exc)
                await self._send_api_offline(ctx)
                return
            for entry in entries:
                w = dmg_map.get(entry["id"], 0.0)
                cat = entry.get("type", "Standaard")
                rows.append((entry["name"], cat, w, 0.0))
                grand_weekly += w
        else:
            inputs = [{"muId": e["id"]} for e in entries]
            try:
                results = await self._client.batch_get("/mu.getById", inputs)
            except Exception as exc:
                logger.warning("mudmg overview: batch_get failed: %s", exc)
                await self._send_api_offline(ctx)
                return
            for entry, payload in zip(entries, results):
                w, t = _mu_rankings(payload if isinstance(payload, dict) else {})
                cat = entry.get("type", "Standaard")
                rows.append((entry["name"], cat, w, t))
                grand_weekly += w
                grand_total += t

        # Sort by selected column descending
        sort_col = 3 if sort_by_total else 2
        rows.sort(key=lambda r: r[sort_col], reverse=True)

        # Build code-block table that fits in an embed description (max 4096 chars)
        _CAT_ICON = {"D1": "🟡", "D2": "🔵", "D3": "🟢", "D4": "🔴", "D5": "🟣"}
        name_w = min(max((len(r[0]) for r in rows), default=4), 22)
        W = 10
        T = 10
        header = f"{'#':>2}  {'Naam':<{name_w}}  {'Wekelijks':>{W}}  {'Totaal':>{T}}"
        sep = "\u2500" * len(header)
        lines = [header, sep]
        for i, (name, cat, weekly, total) in enumerate(rows, 1):
            icon = _CAT_ICON.get(cat, "  ")
            display = name[:name_w] if len(name) > name_w else name
            w_str = fmt_damage(weekly) if weekly else "\u2014"
            t_str = fmt_damage(total) if total else "\u2014"
            lines.append(f"{i:>2}  {display:<{name_w}}  {w_str:>{W}}  {t_str:>{T}}  {icon}")
        lines.append(sep)
        gw_str = fmt_damage(grand_weekly) if grand_weekly else "\u2014"
        gt_str = fmt_damage(grand_total) if grand_total else "\u2014"
        lines.append(f"{'':>2}  {'Totaal NL':<{name_w}}  {gw_str:>{W}}  {gt_str:>{T}}")

        table = "```\n" + "\n".join(lines) + "\n```"

        # \u2500\u2500 Division subtotals \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        div_weekly: dict[str, float] = {}
        div_total: dict[str, float] = {}
        for name, cat, weekly, total in rows:
            div_weekly[cat] = div_weekly.get(cat, 0.0) + weekly
            div_total[cat] = div_total.get(cat, 0.0) + total

        def _div_sort_key(cat: str) -> tuple[int, str]:
            m = re.match(r"^D(\d+)$", cat)
            return (int(m.group(1)), "") if m else (999, cat)

        div_cats = sorted(div_weekly.keys(), key=_div_sort_key)
        div_name_w = max((len(c) for c in div_cats), default=4)
        div_header = f"{'Divisie':<{div_name_w}}  {'Wekelijks':>{W}}  {'Totaal':>{T}}"
        div_sep = "\u2500" * len(div_header)
        div_lines = [div_header, div_sep]
        for cat in div_cats:
            icon = _CAT_ICON.get(cat, "  ")
            dw, dt = div_weekly[cat], div_total[cat]
            dw_str = fmt_damage(dw) if dw else "\u2014"
            dt_str = fmt_damage(dt) if dt else "\u2014"
            div_lines.append(f"{cat:<{div_name_w}}  {dw_str:>{W}}  {dt_str:>{T}}  {icon}")
        div_table = "**Divisietotalen**\n```\n" + "\n".join(div_lines) + "\n```"

        # Split into multiple embeds if description exceeds 4096 chars (very
        # unlikely) \u2014 reserve room on the last chunk for div_table below it.
        _MAX_DESC = 4000 - len(div_table)
        chunks: list[str] = []
        if len(table) <= _MAX_DESC:
            chunks = [table]
        else:
            chunk_lines: list[str] = [lines[0], lines[1]]
            for line in lines[2:]:
                candidate = "```\n" + "\n".join(chunk_lines + [line]) + "\n```"
                if len(candidate) > _MAX_DESC and len(chunk_lines) > 2:
                    chunks.append("```\n" + "\n".join(chunk_lines) + "\n```")
                    chunk_lines = [lines[0], lines[1], line]
                else:
                    chunk_lines.append(line)
            if chunk_lines:
                chunks.append("```\n" + "\n".join(chunk_lines) + "\n```")

        legend = "🟡 D1  •  🔵 D2  •  🟢 D3  •  🔴 D4  •  🟣 D5"
        if last_week:
            start_iso, _ = _nl_week_range(weeks_ago=1)
            week_label = datetime.fromisoformat(start_iso).strftime("week van %d-%m")
            sort_label = f"schade vorige week ({week_label})"
        else:
            sort_label = "totale schade" if sort_by_total else "wekelijkse schade"
        for i, chunk in enumerate(chunks):
            title = f"⚔️ Nederlandse MUs — Ranking {sort_label}"
            if len(chunks) > 1:
                title += f" ({i + 1}/{len(chunks)})"
            description = chunk
            if i == len(chunks) - 1:
                description += f"\n{div_table}"
            embed = discord.Embed(
                title=title,
                description=description,
                colour=self._embed_colour(),
            )
            if i == len(chunks) - 1:
                embed.set_footer(text=legend)
            await ctx.send(embed=embed)

    # ── Detail (MU arg) ───────────────────────────────────────────────────────

    async def _send_mu_detail(
        self, ctx: Context, mu_name: str, entries: list[dict], last_week: bool = False
    ) -> None:
        """Fetch members of one MU and show per-member weekly + total damage."""
        # Fuzzy match: exact name first, then substring
        q = mu_name.strip().lower()
        entry = next(
            (e for e in entries if e["name"].lower() == q),
            next((e for e in entries if q in e["name"].lower()), None),
        )
        if entry is None:
            await ctx.send(
                f"Geen MU gevonden met de naam **{mu_name}**.\n"
                "Gebruik het autocomplete-menu voor een juiste naam."
            )
            return

        # Fetch MU data and global total-damage ranking concurrently
        async def _fetch_mu() -> object:
            try:
                res = await self._client.batch_get(
                    "/mu.getById", [{"muId": entry["id"]}]
                )
                return res[0] if res else None
            except Exception as exc:
                logger.warning(
                    "mudmg detail: batch_get failed for %s: %s", entry["id"], exc
                )
                return None

        async def _fetch_total_ranking() -> object:
            try:
                return await self._client.post(
                    "/ranking.getRanking",
                    json={"rankingType": "userDamages"},
                )
            except Exception as exc:
                logger.warning("mudmg detail: userDamages fetch failed: %s", exc)
                return None

        mu_data, total_resp = await asyncio.gather(_fetch_mu(), _fetch_total_ranking())

        if not isinstance(mu_data, dict):
            await self._send_api_offline(ctx)
            return

        weekly_total, dmg_total = _mu_rankings(mu_data)
        members = _mu_members(mu_data)

        # Build total damage lookup and name lookup from the ranking response
        total_map = _extract_total_damage_map(total_resp) if not last_week else {}
        ranking_name_map = _extract_name_map(total_resp) if total_resp and not last_week else {}

        # Look up per-member damage, levels, and names from the DB
        weekly_map: dict = {}
        level_map: dict = {}
        name_map: dict[str, str] = {}
        if self._db and members:
            if last_week:
                start_iso, end_iso = _nl_week_range(weeks_ago=1)
                try:
                    weekly_map = await self._db.get_player_damage_in_range(members, start_iso, end_iso)
                except Exception as exc:
                    logger.warning("mudmg detail last_week: DB lookup failed: %s", exc)
            else:
                try:
                    weekly_map = await self._db.get_weekly_damages_for_users(members)
                except Exception as exc:
                    logger.warning("mudmg detail: DB lookup failed: %s", exc)
            try:
                level_map = await self._db.get_levels_for_users(members)
            except Exception as exc:
                logger.warning("mudmg detail: level DB lookup failed: %s", exc)
            try:
                name_map = await self._db.get_names_for_users(members)
            except Exception as exc:
                logger.warning("mudmg detail: name DB lookup failed: %s", exc)

        # Build rows: (citizen_name, weekly_dmg, total_dmg, level), sorted by weekly desc
        rows: list[tuple[str, float, float, int]] = []
        for uid in members:
            name: Optional[str] = None
            weekly = 0.0
            if uid in weekly_map:
                candidate, weekly = weekly_map[uid]
                if not _looks_like_uid(candidate):
                    name = candidate
            if not name:
                name = ranking_name_map.get(uid)
            if not name:
                n = name_map.get(uid)
                if not _looks_like_uid(n):
                    name = n
            total_dmg = total_map.get(uid, 0.0)
            level = level_map.get(uid, 0)
            if name or total_dmg or weekly:
                rows.append((name or "Onbekend", weekly, total_dmg, level))
        rows.sort(key=lambda r: r[1], reverse=True)

        # Format as a fixed-width code-block table
        # # + Naam + Wekelijks + Totaal + Lvl + Per lvl
        if rows:
            name_w = min(max(len(r[0]) for r in rows), 14)
            name_w = max(name_w, 4)
            W = 9
            T = 9
            L = 3
            E = 8
            header = (
                f"{'#':>3}  {'Naam':<{name_w}}  {'Wekelijks':>{W}}  {'Totaal':>{T}}"
                f"  {'Lvl':>{L}}  {'W/Per lvl':>{E}}"
            )
            sep = "\u2500" * len(header)
            tbl_lines = [header, sep]
            for i, (name, weekly, total, level) in enumerate(rows, 1):
                display = name[:name_w] if len(name) > name_w else name
                w_str = fmt_damage(weekly) if weekly else "\u2014"
                t_str = fmt_damage(total) if total else "\u2014"
                l_str = str(level) if level else "\u2014"
                e_str = fmt_damage(weekly / level) if weekly and level else "\u2014"
                tbl_lines.append(
                    f"{i:>3}  {display:<{name_w}}  {w_str:>{W}}  {t_str:>{T}}"
                    f"  {l_str:>{L}}  {e_str:>{E}}"
                )
            table = "```\n" + "\n".join(tbl_lines) + "\n```"
        else:
            table = (
                "*Geen schadedata beschikbaar voor leden van deze MU.\n"
                "Enkel NL-burgers die via /peil zijn opgehaald zijn zichtbaar.*"
            )

        members_shown = len(rows)
        members_total = len(members)

        if last_week:
            start_iso, _ = _nl_week_range(weeks_ago=1)
            week_label = datetime.fromisoformat(start_iso).strftime("week van %d-%m")
            title_suffix = f"— Ledenschade ({week_label})"
            footer_parts = [f"Schade uit database ({week_label})"]
        else:
            w_str = fmt_damage(weekly_total) if weekly_total else "—"
            t_str = fmt_damage(dmg_total) if dmg_total else "—"
            title_suffix = "— Ledenschade"
            footer_parts = [f"MU totaal — week: {w_str} | totaal: {t_str}"]
        if members_total:
            footer_parts.append(f"{members_shown}/{members_total} leden met schadedata")
        footer_line = " · ".join(footer_parts)

        content = f"**⚔️ {entry['name']} {title_suffix}**\n{table}\n_{footer_line}_"
        await ctx.send(content)



async def setup(bot) -> None:
    """Add the MudmgCog to the bot."""
    await bot.add_cog(MudmgCog(bot))
