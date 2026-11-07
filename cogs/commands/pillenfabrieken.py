"""Slash command /pillenfabrieken — find cocaine factories NOT in NL-controlled regions.

Scans every company in the game, filters for those that produce cocain (item code
``cocain``), and reports the ones whose region is not owned/occupied by the
Netherlands.  Those players should move their factory to an NL region to benefit
from the highest cocaine production bonus.

Usage
-----
/pillenfabrieken    — show all cocain factories outside NL-controlled territory
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

_ITEM_CODE = "cocain"
_COMPANIES_PER_PAGE = 100
_COMPANY_BATCH = 50  # batch size for company.getById
_USER_BATCH = 50  # batch size for user.getUserLite
_MAX_PAGES = 500  # safety cap (500 × 100 = 50 000 companies max)
_ACTIVE_HOURS = 72  # players inactive longer than this are ignored
_PP_PER_PILL = 200  # PP cost to produce one pill

# ── Helpers ──────────────────────────────────────────────────────────────────


def _unwrap(resp: object) -> dict:
    """Strip a single tRPC result/data envelope."""
    if isinstance(resp, dict):
        inner = resp.get("result")
        if isinstance(inner, dict):
            data = inner.get("data")
            if data is not None:
                return data  # type: ignore[return-value]
            return inner
        for k in ("data",):
            v = resp.get(k)
            if v is not None:
                return v  # type: ignore[return-value]
    return resp  # type: ignore[return-value]


def _company_region(company: dict) -> str:
    """Return the region ID of a company (API uses several field names)."""
    return (
        company.get("region")
        or company.get("regionId")
        or company.get("region_id")
        or ""
    )


def _company_owner(company: dict) -> str:
    """Return the owner user-ID of a company."""
    return (
        company.get("user")
        or company.get("userId")
        or company.get("owner")
        or company.get("ownerId")
        or ""
    )


def _last_connection(obj: object) -> str | None:
    """Extract lastConnectionAt from a getUserLite response."""
    if not isinstance(obj, dict):
        return None
    dates = obj.get("dates")
    if isinstance(dates, dict):
        return dates.get("lastConnectionAt")
    return obj.get("lastConnectionAt") or obj.get("lastLoginAt")


def _is_active(last_ts: str | None, now: datetime) -> bool:
    """Return True if *last_ts* is within _ACTIVE_HOURS of *now*."""
    if not last_ts:
        return False
    try:
        dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        return (now - dt) <= timedelta(hours=_ACTIVE_HOURS)
    except (ValueError, TypeError):
        return False


def _extract_eco_skill(data: dict, skill_name: str) -> float:
    """Extract skill level (0-10) for *skill_name* from a getUserLite response."""
    skills = data.get("skills")
    if skills is None:
        return 0.0

    def _pick(entry: dict) -> float:
        v = entry.get("level")
        if v is None:
            v = entry.get("value")
        return float(v) if v is not None else 0.0

    if isinstance(skills, dict):
        entry = skills.get(skill_name)
        if isinstance(entry, dict):
            return _pick(entry)
        if isinstance(entry, (int, float)):
            return float(entry)
    elif isinstance(skills, list):
        for entry in skills:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("skill") or entry.get("type")
            if name == skill_name:
                return _pick(entry)
    return 0.0


# ── Cog ──────────────────────────────────────────────────────────────────────


class PillenfabriekenCog(CommandCogBase, name="pillenfabrieken"):
    """Cog for the /pillenfabrieken command."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _get_nl_region_ids(self) -> set[str]:
        """Return a set of region IDs that are currently owned/occupied by NL."""
        nl_id = self.config.get("nl_country_id", "")
        if not nl_id:
            return set()
        try:
            raw = await self._client.get("/region.getRegionsObject")
            regions: dict = _unwrap(raw) if isinstance(raw, dict) else {}
            # Also try .result.data nesting a second level deep
            if not isinstance(regions, dict) or not regions:
                regions = {}
            return {
                rid
                for rid, robj in regions.items()
                if isinstance(robj, dict) and robj.get("country") == nl_id
            }
        except Exception as exc:
            logger.warning("pillenfabrieken: getRegionsObject failed: %s", exc)
            return set()

    async def _paginate_all_company_ids(self) -> list[str]:
        """Paginate through company.getCompanies (no filter) and collect all IDs."""
        ids: list[str] = []
        cursor: str | None = None
        for _page in range(_MAX_PAGES):
            payload: dict = {"perPage": _COMPANIES_PER_PAGE}
            if cursor:
                payload["cursor"] = cursor
            try:
                raw = await self._client.get(
                    "/company.getCompanies",
                    params={"input": json.dumps(payload)},
                )
                data = _unwrap(raw)
            except Exception as exc:
                logger.warning("pillenfabrieken: getCompanies page failed: %s", exc)
                break
            if not isinstance(data, dict):
                break
            page_ids = data.get("items") or []
            if not isinstance(page_ids, list):
                break
            ids.extend(str(i) for i in page_ids if i)
            cursor = data.get("nextCursor") or data.get("cursor")
            if not cursor or not page_ids:
                break
            await asyncio.sleep(0)  # yield to event loop between pages
        return ids

    async def _fetch_company_details(self, company_ids: list[str]) -> list[dict | None]:
        """Batch-fetch full company objects for *company_ids*."""
        if not company_ids:
            return []
        inputs = [{"companyId": cid} for cid in company_ids]
        try:
            results = await asyncio.wait_for(
                self._client.batch_get(
                    "/company.getById", inputs, batch_size=_COMPANY_BATCH
                ),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.warning("pillenfabrieken: batch_get companies timed out")
            return [None] * len(company_ids)
        except Exception as exc:
            logger.warning("pillenfabrieken: batch_get companies failed: %s", exc)
            return [None] * len(company_ids)

        out: list[dict | None] = []
        for raw in results:
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            out.append(data if isinstance(data, dict) else None)
        return out

    async def _fetch_user_profiles(
        self, user_ids: list[str]
    ) -> dict[str, tuple[str, int, str | None]]:
        """Return {user_id: (username, level, last_connection_ts)} for each id."""
        if not user_ids:
            return {}
        inputs = [{"userId": uid} for uid in user_ids]
        try:
            results = await asyncio.wait_for(
                self._client.batch_get(
                    "/user.getUserLite", inputs, batch_size=_USER_BATCH
                ),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.warning("pillenfabrieken: batch_get users timed out")
            return {}
        except Exception as exc:
            logger.warning("pillenfabrieken: batch_get users failed: %s", exc)
            return {}

        out: dict[str, tuple[str, int, str | None]] = {}
        for uid, raw in zip(user_ids, results):
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            if not isinstance(data, dict):
                continue
            name = data.get("username") or uid
            level = int(data.get("level") or data.get("characterLevel") or 0)
            if not level:
                lev_obj = data.get("leveling") or data.get("details") or {}
                if isinstance(lev_obj, dict):
                    level = int(lev_obj.get("level") or 0)
            out[uid] = (str(name), level, _last_connection(data))
        return out

    async def _fetch_worker_skill_profiles(
        self, user_ids: list[str]
    ) -> dict[str, dict]:
        """Return {user_id: {energy: float, production: float}} via getUserLite batch."""
        if not user_ids:
            return {}
        inputs = [{"userId": uid} for uid in user_ids]
        try:
            results = await asyncio.wait_for(
                self._client.batch_get(
                    "/user.getUserLite", inputs, batch_size=_USER_BATCH
                ),
                timeout=60.0,
            )
        except Exception as exc:
            logger.warning("pillenfabrieken: batch_get worker skills failed: %s", exc)
            return {}
        out: dict[str, dict] = {}
        for uid, raw in zip(user_ids, results):
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            if not isinstance(data, dict):
                continue
            out[uid] = {
                "energy": _extract_eco_skill(data, "energy"),
                "production": _extract_eco_skill(data, "production"),
            }
        return out

    async def _fetch_production_bonuses(
        self, companies: list[dict]
    ) -> dict[str, float]:
        """Return {company_id → total production bonus %} from company.getProductionBonus.

        Deposits, ethics and strategic-resource production bonuses are gone from
        the game, so the per-company `total` is all there is (0 right now).
        """
        cids = [str(c.get("_id") or c.get("id") or "") for c in companies]
        cids = [cid for cid in cids if cid]
        if not cids or not self._client:
            return {}
        try:
            results = await asyncio.wait_for(
                self._client.batch_get(
                    "company.getProductionBonus", [{"companyId": cid} for cid in cids]
                ),
                timeout=30.0,
            )
        except Exception as exc:
            logger.warning("pillenfabrieken: getProductionBonus batch failed: %s", exc)
            return {}
        out: dict[str, float] = {}
        for cid, raw in zip(cids, results):
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            if isinstance(data, dict):
                try:
                    out[cid] = float(data.get("total") or 0)
                except (TypeError, ValueError):
                    pass
        return out

    @commands.hybrid_command(
        name="pillenfabrieken",
        description="Toon coca-fabrieken die NIET in een door Nederland bezettte regio zitten.",
    )
    async def pillenfabrieken(self, ctx: Context) -> None:
        """Scan all companies for cocain producers outside NL-controlled regions."""
        if not self._client:
            await self._send_api_offline(ctx)
            return

        if hasattr(ctx, "defer"):
            await ctx.defer()

        # ── 1. NL-owned regions + all company IDs (concurrently) ─────────────
        nl_regions, all_ids = await asyncio.gather(
            self._get_nl_region_ids(),
            self._paginate_all_company_ids(),
        )

        if not all_ids:
            await self._send_api_offline(ctx, "Kon geen bedrijfslijst ophalen van de API.")
            return

        total_companies = len(all_ids)
        logger.info("pillenfabrieken: scanning %d companies", total_companies)

        # ── 2. Fetch full details for all companies ───────────────────────────
        details = await self._fetch_company_details(all_ids)
        company_by_id: dict[str, dict] = {
            str(c.get("_id") or c.get("id") or ""): c
            for c in details
            if isinstance(c, dict)
        }
        # ── 3. Filter: cocain producers outside NL ───────────────────────────
        # offenders: [(company_name, owner_uid, company_id)]
        offenders: list[tuple[str, str, str]] = []
        nl_factory_count = 0
        for company in details:
            if not isinstance(company, dict):
                continue
            if (company.get("itemCode") or "") != _ITEM_CODE:
                continue
            region_id = _company_region(company)
            if region_id and region_id in nl_regions:
                nl_factory_count += 1
            else:
                owner_uid = _company_owner(company)
                comp_name = company.get("name") or "—"
                comp_id = str(company.get("_id") or company.get("id") or "")
                offenders.append((comp_name, owner_uid, comp_id))

        if not offenders:
            embed = discord.Embed(
                title="💊 Pillenfabrieken buiten Nederland",
                description=(
                    "✅ Alle coca-fabrieken in het spel produceren al in "
                    "een door Nederland bezette regio."
                ),
                colour=self._embed_colour(),
            )
            embed.set_footer(
                text=f"{total_companies} bedrijven gescand · {len(nl_regions)} NL-regio's"
            )
            await ctx.send(embed=embed)
            return

        total_factories = nl_factory_count + len(offenders)

        # ── 4. Resolve owner profiles ─────────────────────────────────────────
        unique_owners = list({uid for _, uid, _ in offenders if uid})
        profiles = await self._fetch_user_profiles(unique_owners)

        # Filter offenders to active owners only
        now = datetime.now(timezone.utc)
        active_owner_ids = {
            uid for uid, (_, _, ts) in profiles.items() if _is_active(ts, now)
        }
        offenders = [
            (name, uid, cid) for name, uid, cid in offenders if uid in active_owner_ids
        ]

        if not offenders:
            embed = discord.Embed(
                title="💊 Pillenfabrieken buiten Nederland",
                description=(
                    "✅ Geen actieve spelers hebben een coca-fabriek buiten NL-grondgebied."
                ),
                colour=self._embed_colour(),
            )
            embed.set_footer(
                text=f"{total_companies} bedrijven gescand · {len(nl_regions)} NL-regio's"
            )
            await ctx.send(embed=embed)
            return

        # ── 5. Workers + production bonuses + PP/day calculation ──────────────────
        offender_company_ids = [cid for _, _, cid in offenders if cid]
        offender_companies = [
            company_by_id[cid] for cid in offender_company_ids if cid in company_by_id
        ]

        # worker.getWorkers is gone (the job market was replaced by hired
        # employees), so there are no workers to list any more.
        worker_pairs_raw: list[tuple[str, dict]] = []
        prod_bonuses = await self._fetch_production_bonuses(offender_companies)

        # Deduplicate workers (keep raw emp_dict for skill fallback)
        seen_wids: set[str] = set()
        unique_pairs: list[tuple[str, dict]] = []
        for uid, emp_dict in worker_pairs_raw:
            if uid not in seen_wids:
                seen_wids.add(uid)
                unique_pairs.append((uid, emp_dict))
        unique_worker_ids = [uid for uid, _ in unique_pairs]

        # Fetch last-login profiles + energy/production skills concurrently
        worker_profiles, worker_skill_profiles = await asyncio.gather(
            self._fetch_user_profiles(unique_worker_ids),
            self._fetch_worker_skill_profiles(unique_worker_ids),
        )
        active_employee_count = sum(
            1
            for uid, _ in unique_pairs
            if uid in worker_profiles and _is_active(worker_profiles[uid][2], now)
        )

        # ── 5b. Company AE PP/day ───────────────────────────────────────────
        total_company_pp = 0.0
        for _, _, cid in offenders:
            comp = company_by_id.get(cid, {})
            upgrades = comp.get("activeUpgradeLevels") or {}
            ae_lvl = int(upgrades.get("automatedEngine") or 0)
            if ae_lvl == 0:
                ae_lvl = 1  # API level 0 means the engine exists at level 1
            bonus_pct = prod_bonuses.get(cid, 0.0)
            total_company_pp += ae_lvl * 24.0 * (1.0 + bonus_pct / 100.0)

        # ── 5c. Employee manual PP/day ───────────────────────────────────────
        total_emp_pp = 0.0
        for uid, emp_dict in unique_pairs:
            prof = worker_profiles.get(uid)
            if not prof or not _is_active(prof[2], now):
                continue
            skills = worker_skill_profiles.get(uid, {})
            raw_energy = float(skills.get("energy") or emp_dict.get("energy") or 0)
            raw_prod = float(
                skills.get("production") or emp_dict.get("production") or 0
            )
            # Convert skill level (0-10) to pool/value; fall through if API returned pool directly
            emp_energy_pool = (
                raw_energy if raw_energy > 10 else 30.0 + raw_energy * 10.0
            )
            emp_pp_val = raw_prod if raw_prod > 10 else 10.0 + raw_prod * 3.0
            total_emp_pp += emp_energy_pool * 0.24 * emp_pp_val

        company_pills_day = total_company_pp / _PP_PER_PILL
        emp_pills_day = total_emp_pp / _PP_PER_PILL
        total_pills_day = company_pills_day + emp_pills_day

        # ── 6. Build table rows: (username, level, company_name) ─────────────────
        # Keep order: sort by level desc, then username asc
        rows: list[tuple[str, int, str]] = []
        for comp_name, owner_uid, _ in offenders:
            if owner_uid and owner_uid in profiles:
                username, level, _ = profiles[owner_uid]
            else:
                username = owner_uid or "Onbekend"
                level = 0
            rows.append((username, level, comp_name))
        rows.sort(key=lambda r: (-r[1], r[0].lower()))

        # ── 7. Render fixed-width code-block table ────────────────────────────────
        # Target max line width ≈ 43 chars (Discord embed code block wraps at ~46):
        #   3 (#) + 2 + 16 (Speler) + 2 + 3 (Lvl) + 2 + 15 (Bedrijf) = 43
        NAME_W = 16
        COMP_W = 15
        LVL_W = 3

        header = (
            f"{'#':>3}  {'Speler':<{NAME_W}}  {'Lvl':>{LVL_W}}  {'Bedrijf':<{COMP_W}}"
        )
        sep = "\u2500" * len(header)

        # Build individual data lines (no header/sep/code-block yet)
        data_lines: list[str] = []
        for i, (username, level, comp_name) in enumerate(rows, 1):
            u = username[:NAME_W] if len(username) > NAME_W else username
            c = comp_name[:COMP_W] if len(comp_name) > COMP_W else comp_name
            lv = str(level) if level else "?"
            data_lines.append(f"{i:>3}  {u:<{NAME_W}}  {lv:>{LVL_W}}  {c:<{COMP_W}}")

        # Split data lines into pages so each code-block fits in 4096 chars.
        # Overhead per page: len("```\n") + len(header) + 1 + len(sep) + 1 + len("```") = ~100
        _OVERHEAD = len("```\n") + len(header) + 1 + len(sep) + 1 + len("```")

        # Summary shown above the table on page 0 — subtract its length from that page's budget.
        summary = (
            f"**Totaal:** {total_factories} coca-fabrieken  ·  "
            f"**In NL:** {nl_factory_count}  ·  "
            f"**Buiten NL (actief):** {len(offenders)}  ·  "
            f"**Actieve werknemers:** {active_employee_count}"
        )
        _SUMMARY_OVERHEAD = len(summary) + 1  # +1 for the separating newline

        _MAX_DESC = 4096 - _OVERHEAD  # pages 1+
        _MAX_DESC_P0 = 4096 - _OVERHEAD - _SUMMARY_OVERHEAD  # page 0

        pages: list[list[str]] = []
        current: list[str] = []
        current_len = 0
        for line in data_lines:
            line_len = len(line) + 1  # +1 for newline
            page_limit = _MAX_DESC_P0 if not pages else _MAX_DESC
            if current and current_len + line_len > page_limit:
                pages.append(current)
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len
        if current:
            pages.append(current)

        footer_text = (
            f"{len(rows)} actieve spelers met coca-fabriek buiten NL · "
            f"{active_employee_count} actieve werknemers · "
            f"{total_companies} bedrijven gescand · "
            f"{len(unique_owners)} eigenaren gecontroleerd · "
            f"actief = ingelogd in afgelopen {_ACTIVE_HOURS}u"
        )
        colour = self._embed_colour()
        title = "💊 Pillenfabrieken buiten Nederland"

        embeds: list[discord.Embed] = []
        for page_idx, page_lines in enumerate(pages):
            code_block = (
                "```\n" + header + "\n" + sep + "\n" + "\n".join(page_lines) + "\n```"
            )
            description = (summary + "\n" + code_block) if page_idx == 0 else code_block
            embed = discord.Embed(
                title=title if page_idx == 0 else f"{title} (vervolg {page_idx + 1})",
                description=description,
                colour=colour,
            )
            if page_idx == len(pages) - 1:
                embed.set_footer(text=footer_text)
            embeds.append(embed)

        # Send one embed per message — the combined 6000-char limit per message
        # makes batching impractical when each embed already has ~4000-char descriptions.
        for embed in embeds:
            await ctx.send(embed=embed)

        # ── 8. Production stats embed ──────────────────────────────────────────
        def _fmt(n: float) -> str:
            return f"{int(n):,}".replace(",", "\u202f")  # narrow no-break space

        C1, C2, C3 = 14, 12, 10
        hdr_row = f"{'Bron':<{C1}}  {'PP/dag':>{C2}}  {'Pills/dag':>{C3}}"
        div = "\u2500" * len(hdr_row)
        row_comp = f"{'AE fabrieken':<{C1}}  {_fmt(total_company_pp):>{C2}}  {_fmt(company_pills_day):>{C3}}"
        row_emp = f"{'Werknemers':<{C1}}  {_fmt(total_emp_pp):>{C2}}  {_fmt(emp_pills_day):>{C3}}"
        row_tot = f"{'Totaal':<{C1}}  {_fmt(total_company_pp + total_emp_pp):>{C2}}  {_fmt(total_pills_day):>{C3}}"

        prod_block = (
            "```\n"
            + hdr_row
            + "\n"
            + div
            + "\n"
            + row_comp
            + "\n"
            + row_emp
            + "\n"
            + div
            + "\n"
            + row_tot
            + "\n"
            + "```"
        )
        prod_embed = discord.Embed(
            title="\u2696\ufe0f Pillenproductie per dag (fabrieken buiten NL)",
            description=prod_block,
            colour=colour,
        )
        prod_embed.set_footer(
            text=(
                f"1 pill = {_PP_PER_PILL} PP \u00b7 "
                f"AE PP = level \u00d7 24/dag \u00b7 "
                f"alleen actieve spelers (ingelogd \u2264{_ACTIVE_HOURS}u geleden)"
            )
        )
        await ctx.send(embed=prod_embed)


async def setup(bot) -> None:
    """Add the PillenfabriekenCog to the bot."""
    await bot.add_cog(PillenfabriekenCog(bot))
