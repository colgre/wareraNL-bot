"""
/ecobuild <speler?> — Optimal eco skill distribution for maximum daily production.

Shows:
- Current eco skill levels (Entrepreneurship, Energy, Production, Companies)
- Current daily production from manual work + automated engines (AE)
- Optimal SP distribution for max production (assuming full skill reset)

Formulas
--------
- Daily work actions (entrepreneurship): floor(0.24 × ent_value)
- Manual production per day:             daily_works × production_value
- AE production per day (per company):   engine_level × 24 (max level 7 = 168 pts/day)
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import math
from typing import Optional

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase, citizen_autocomplete, strip_division_prefix

logger = logging.getLogger("discord_bot")

# ---------------------------------------------------------------------------
# Skill data (from gameConfig.getGameConfig — hardcoded for performance)
# ---------------------------------------------------------------------------

# Value per skill level 0–10
_SKILL_VALUES: dict[str, list[int]] = {
    "entrepreneurship": [30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80],
    "energy":           [30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130],
    "production":       [10, 13, 16, 19, 22, 25, 28, 31, 34, 37, 40],
    "companies":        [2,  3,  4,  5,  6,  7,  8,  9,  10, 11, 12],
}

# Cumulative SP cost to reach level N (levels 0–10)
_CUMUL_COST: list[int] = [0, 1, 3, 6, 10, 15, 21, 28, 36, 45, 55]

_COST_PER_ACTION: int = 10        # energy/entrepreneurship consumed per work action
_REGEN_DIVISOR: int = 10          # bar regenerates value/10 per hour (10 %/hr)
_ENGINE_DAILY_PER_LEVEL: int = 24  # automated engine: 24 pts per level per day
_ENGINE_MAX_LEVEL: int = 7         # max upgradeable level

# Steel cost to upgrade AE TO the given level (from the level below).
# Companies always start with AE level 1 (24 PP/day, free).
# Level 0 in the API = level 1 for advice purposes (just hasn't been recorded yet).
_AE_UPGRADE_STEEL: dict[int, int] = {2: 20, 3: 40, 4: 80, 5: 160, 6: 320, 7: 640}


# ---------------------------------------------------------------------------
# Pure calculation helpers
# ---------------------------------------------------------------------------


def _daily_works(skill_value: int) -> int:
    """Integer floor of daily work actions (for display only)."""
    return math.floor(skill_value / _REGEN_DIVISOR * 24 / _COST_PER_ACTION)


def _daily_works_float(skill_value: int) -> float:
    """Non-floored daily work actions for accurate PP/day calculation.

    Formula: (value / 10) × 24 / 10  =  value × 0.24
    """
    return skill_value / _REGEN_DIVISOR * 24 / _COST_PER_ACTION


def _engine_cycle_daily(engine_level: int) -> int:
    """Automated engine production per day, capped at max level."""
    return _ENGINE_DAILY_PER_LEVEL * min(max(0, engine_level), _ENGINE_MAX_LEVEL)


def _daily_prod(
    ent_val: int,
    energy_val: int,
    prod_val: int,
    engine_daily_total: int,
    ent_prod_mult: float = 1.0,
) -> float:
    """Total daily production points: manual work (ent + energy bars) + all engines.

    *ent_prod_mult* applies the company production bonus to entrepreneurship
    work actions only — energy work is unaffected by the production bonus.
    """
    ent_pp = _daily_works_float(ent_val) * prod_val * ent_prod_mult
    energy_pp = _daily_works_float(energy_val) * prod_val
    return ent_pp + energy_pp + engine_daily_total


def _eco_sp_budget(profile: dict) -> int:
    """Total SP the player has ever earned (= full redistribution budget).

    Assumes the player resets ALL skills (including combat) and redistributes
    everything into eco skills.  Uses leveling.totalSkillPoints when available,
    falling back to spentSkillPoints + availableSkillPoints.
    """
    leveling = profile.get("leveling") or {}
    total = leveling.get("totalSkillPoints")
    if total is None:
        spent = leveling.get("spentSkillPoints", 0) or 0
        avail = leveling.get("availableSkillPoints", 0) or 0
        total = spent + avail
    return int(total)


def _skill_table(
    rows: list[tuple[str, str, str]],
    total_pp: float,
    sp_info: str = "",
) -> str:
    """Render a fixed-width table inside a Discord code block.

    rows: list of (skill_name, level_str, pp_str).
    sp_info: optional SP line appended below the total row.
    """
    W1, W2, W3 = 22, 3, 6
    sep = "\u2500" * (W1 + 2 + W2 + 2 + W3)
    header = f"{'Skill':<{W1}}  {'Lvl':>{W2}}  {'PP/dag':>{W3}}"
    lines = [header, sep]
    for name, lvl, pp in rows:
        lines.append(f"{name:<{W1}}  {lvl:>{W2}}  {pp:>{W3}}")
    lines.append(sep)
    lines.append(f"{'Totaal':<{W1}}  {'':>{W2}}  {f'{total_pp:.0f}':>{W3}}")
    if sp_info:
        lines.append(sp_info)
    return "```\n" + "\n".join(lines) + "\n```"


def _extract_skill(profile: dict, skill_name: str) -> tuple[int, int]:
    """Return (level, effective_value) for *skill_name* from a getUserLite response."""
    entry = (profile.get("skills") or {}).get(skill_name)
    if isinstance(entry, dict):
        lvl = max(0, entry.get("level", 0) or 0)
        val = entry.get("value") or _SKILL_VALUES[skill_name][min(lvl, 10)]
        return min(lvl, 10), int(val)
    return 0, _SKILL_VALUES[skill_name][0]


def _next_company_cost(n_owned: int) -> int:
    """Concrete cost to buy the next company when the player already owns *n_owned*.

    1st company: free (everyone starts with one).
    2nd: 100, 3rd: 150, 4th: 200, …  (+50 per step).
    """
    return 100 + max(0, n_owned - 1) * 50


def _build_ae_advice(
    engine_info: list[tuple[str, str, int, int, float]],
    n_owned: int,
    comp_skill_lvl: int,
    steel_price: float = 1.0,
    concrete_price: float = 1.0,
    max_steps: int = 12,
    free_sp: int = 0,
) -> list[tuple]:
    """Return a cost-efficiency-ordered list of recommended actions.

    Each step is ranked by PP-gain-per-coin so the most profitable investment
    always comes first.  At most one company purchase is included.

    When the player has free (unspent) SP, upgrading the Companies skill to
    unlock an extra company slot is treated as a regular candidate in the
    efficiency ranking, emitted as 'company_with_skill'.

    Tuple types:
      ("ae",               name, from_lvl, to_lvl, steel_cost, pp_gain)
      ("company",          co_num, concrete, target_ae, steel, pp_gain)
      ("company_with_skill", co_num, new_comp_lvl, sp_cost, concrete, target_ae, steel, pp_gain)
      ("maxed",)           -- all companies at max AE
      ("sp_limited",)      -- companies skill blocks further expansion and no free SP
    """
    sim_comp_lvl = comp_skill_lvl          # tracks the simulated Companies skill level
    max_companies = _SKILL_VALUES["companies"][sim_comp_lvl]

    # sim: (name, ae_lvl, ae_daily, bonus_pct)
    sim: list[tuple[str, int, int, float]] = [
        (name, max(1, eng_lvl), ae_daily, bonus_pct)
        for name, _, eng_lvl, ae_daily, bonus_pct in engine_info
    ]
    n_sim = n_owned
    company_bought = False
    sp_remaining = free_sp           # SP budget available for Companies skill upgrades
    actions: list[tuple] = []

    # Estimate PP gain for a hypothetical new company (AE level 1)
    avg_bonus = (sum(b for _, _, _, b in sim) / len(sim)) if sim else 0.0

    for _ in range(max_steps + 5):
        if len(actions) >= max_steps:
            break

        # ── Collect candidates ────────────────────────────────────────────
        # Each entry: (efficiency_pp_per_coin, option_key, option_data)
        candidates: list[tuple[float, str, tuple]] = []

        # AE upgrade options
        for i, (name, ae_lvl, ae_daily, bonus_pct) in enumerate(sim):
            if ae_lvl < _ENGINE_MAX_LEVEL:
                next_lvl = ae_lvl + 1
                steel = _AE_UPGRADE_STEEL.get(next_lvl)
                if steel is not None:
                    new_daily = round(next_lvl * 24 * (1 + bonus_pct / 100))
                    pp_gain = max(1, new_daily - ae_daily)
                    cost_coins = steel * steel_price
                    eff = pp_gain / cost_coins if cost_coins > 0 else 0.0
                    candidates.append((eff, "ae", (i, name, ae_lvl, next_lvl, steel, pp_gain)))

        # Company purchase option (at most one)
        if not company_bought:
            if n_sim < max_companies:
                # Skill cap allows it — regular purchase
                next_cost = _next_company_cost(n_sim)
                target_ae = min((ae_lvl for _, ae_lvl, _, _ in sim), default=4) if sim else 4
                target_ae = max(1, min(target_ae, _ENGINE_MAX_LEVEL))
                total_steel = sum(
                    _AE_UPGRADE_STEEL.get(lvl, 0) for lvl in range(2, target_ae + 1)
                )
                new_pp = max(1, round(target_ae * 24 * (1 + avg_bonus / 100)))
                total_coin_cost = next_cost * concrete_price + total_steel * steel_price
                eff = new_pp / total_coin_cost if total_coin_cost > 0 else 0.0
                candidates.append(
                    (eff, "company", (n_sim + 1, next_cost, target_ae, total_steel, new_pp))
                )
            elif sim_comp_lvl < 10:
                # At skill cap — check if we can level Companies with free SP
                next_comp_lvl = sim_comp_lvl + 1
                sp_needed = _CUMUL_COST[next_comp_lvl] - _CUMUL_COST[sim_comp_lvl]
                new_max = _SKILL_VALUES["companies"][next_comp_lvl]
                if n_sim < new_max:
                    next_cost = _next_company_cost(n_sim)
                    target_ae = min((ae_lvl for _, ae_lvl, _, _ in sim), default=4) if sim else 4
                    target_ae = max(1, min(target_ae, _ENGINE_MAX_LEVEL))
                    total_steel = sum(
                        _AE_UPGRADE_STEEL.get(lvl, 0) for lvl in range(2, target_ae + 1)
                    )
                    new_pp = max(1, round(target_ae * 24 * (1 + avg_bonus / 100)))
                    total_coin_cost = next_cost * concrete_price + total_steel * steel_price
                    eff = new_pp / total_coin_cost if total_coin_cost > 0 else 0.0
                    if sp_remaining >= sp_needed:
                        candidates.append((
                            eff, "company_skill",
                            (n_sim + 1, next_comp_lvl, sp_needed, next_cost, target_ae, total_steel, new_pp),
                        ))
                    else:
                        # Not enough free SP, but still rank it by concrete+steel cost;
                        # it will render with an SP warning note
                        candidates.append((
                            eff, "company_needs_skill_ranked",
                            (n_sim + 1, next_comp_lvl, sp_needed, next_cost, target_ae, total_steel, new_pp),
                        ))
                elif not candidates:
                    actions.append(("sp_limited",))
                    break
            else:
                if not candidates:
                    actions.append(("sp_limited",))
                    break

        if not candidates:
            actions.append(("maxed",))
            break

        # ── Pick the most efficient candidate ─────────────────────────────
        best = max(candidates, key=lambda x: x[0])
        _, kind, data = best

        if kind == "ae":
            i, name, from_l, to_l, steel, pp_gain = data
            actions.append(("ae", name, from_l, to_l, steel, pp_gain))
            cur = sim[i]
            sim[i] = (cur[0], to_l, round(to_l * 24 * (1 + cur[3] / 100)), cur[3])
        elif kind == "company":
            co_num, concrete, target_ae, total_steel, pp_gain = data
            actions.append(("company", co_num, concrete, target_ae, total_steel, pp_gain))
            new_daily = round(target_ae * 24 * (1 + avg_bonus / 100))
            sim.append((f"Bedrijf {co_num}", target_ae, new_daily, avg_bonus))
            n_sim += 1
            company_bought = True
            avg_bonus = sum(b for _, _, _, b in sim) / len(sim)
        elif kind == "company_skill":
            co_num, new_comp_lvl, sp_cost, concrete, target_ae, total_steel, pp_gain = data
            actions.append(("company_with_skill", co_num, new_comp_lvl, sp_cost,
                             concrete, target_ae, total_steel, pp_gain))
            sim_comp_lvl = new_comp_lvl
            max_companies = _SKILL_VALUES["companies"][sim_comp_lvl]
            sp_remaining -= sp_cost
            new_daily = round(target_ae * 24 * (1 + avg_bonus / 100))
            sim.append((f"Bedrijf {co_num}", target_ae, new_daily, avg_bonus))
            n_sim += 1
            company_bought = True
            avg_bonus = sum(b for _, _, _, b in sim) / len(sim)
        elif kind == "company_needs_skill_ranked":
            co_num, need_comp_lvl, inc_sp, concrete, target_ae, total_steel, pp_gain = data
            actions.append(("company_needs_skill", co_num, need_comp_lvl, inc_sp,
                             concrete, target_ae, total_steel, pp_gain))
            # Mark bought so it doesn't re-enter candidates in subsequent iterations
            company_bought = True

    return actions


def _optimize_production_v2(
    eco_budget: int,
    company_ae_sorted: list[int],
    ent_prod_mult: float = 1.0,
) -> tuple[int, int, int, int, int, float] | None:
    """Find the best (ent_lvl, energy_lvl, prod_lvl, comp_lvl, n_companies, daily).

    Both entrepreneurship and energy bars contribute to manual work actions, so
    the optimizer allocates SP across all four eco skill axes plus companies.

    company_ae_sorted: per-company AE daily production, sorted descending
                       (best companies first — we always keep the top-N).
    ent_prod_mult: production bonus multiplier for entrepreneurship work only.
    """
    n_owned = len(company_ae_sorted)
    best: tuple[int, int, int, int, int, float] | None = None

    for n_active in range(n_owned + 1):
        min_comp_lvl = next(
            (lvl for lvl in range(11) if _SKILL_VALUES["companies"][lvl] >= max(n_active, 2)),
            10,
        )
        comp_sp = _CUMUL_COST[min_comp_lvl]
        if comp_sp > eco_budget:
            continue

        ae_total = sum(company_ae_sorted[:n_active])
        remaining = eco_budget - comp_sp

        for e_lvl in range(11):
            e_cost = _CUMUL_COST[e_lvl]
            if e_cost > remaining:
                break
            for en_lvl in range(11):
                en_cost = _CUMUL_COST[en_lvl]
                if e_cost + en_cost > remaining:
                    break
                for p_lvl in range(11):
                    if e_cost + en_cost + _CUMUL_COST[p_lvl] <= remaining:
                        daily = _daily_prod(
                            _SKILL_VALUES["entrepreneurship"][e_lvl],
                            _SKILL_VALUES["energy"][en_lvl],
                            _SKILL_VALUES["production"][p_lvl],
                            ae_total,
                            ent_prod_mult,
                        )
                        if best is None:
                            best = (e_lvl, en_lvl, p_lvl, min_comp_lvl, n_active, daily)
                        elif daily > best[5]:
                            best = (e_lvl, en_lvl, p_lvl, min_comp_lvl, n_active, daily)

    return best


def _optimize_eco_skills(
    free_budget: int,
    engine_daily_total: int,
    ent_prod_mult: float = 1.0,
) -> tuple[int, int, int, float] | None:
    """Find the best (ent_lvl, energy_lvl, prod_lvl, daily_pp) with fixed engine contribution.

    Companies and workers SP are already locked in; this optimizer only allocates
    the remaining *free_budget* across entrepreneurship, energy, and production.
    """
    best: tuple[int, int, int, float] | None = None
    best_daily: float = -1.0
    for ent_l in range(11):
        if _CUMUL_COST[ent_l] > free_budget:
            break
        for en_l in range(11):
            if _CUMUL_COST[ent_l] + _CUMUL_COST[en_l] > free_budget:
                break
            for p_l in range(11):
                if _CUMUL_COST[ent_l] + _CUMUL_COST[en_l] + _CUMUL_COST[p_l] > free_budget:
                    break
                daily = _daily_prod(
                    _SKILL_VALUES["entrepreneurship"][ent_l],
                    _SKILL_VALUES["energy"][en_l],
                    _SKILL_VALUES["production"][p_l],
                    engine_daily_total,
                    ent_prod_mult,
                )
                if daily > best_daily:
                    best = (ent_l, en_l, p_l, daily)
                    best_daily = daily
    return best



def _unwrap(resp: object) -> object:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class EcoBuildCog(CommandCogBase, name="ecobuild"):
    """Eco skill build optimizer (/ecobuild)."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    async def _search_user(self, username: str) -> list[str]:
        """Return up to 5 candidate user IDs matching *username*."""
        try:
            raw = await self._client.get(
                "/search.searchAnything",
                params={"input": json.dumps({"searchText": username})},
            )
            data = _unwrap(raw)
            ids: list = data.get("userIds", []) if isinstance(data, dict) else []
            return ids[:5]
        except Exception as exc:
            logger.warning("ecobuild: search failed for %r: %s", username, exc)
            return []

    async def _get_user_profile(self, user_id: str) -> Optional[dict]:
        try:
            raw = await self._client.get(
                "/user.getUserLite",
                params={"input": json.dumps({"userId": user_id})},
            )
            data = _unwrap(raw)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("ecobuild: getUserLite failed for %s: %s", user_id, exc)
            return None

    async def _resolve_user(self, query: str) -> tuple[Optional[str], Optional[dict]]:
        """Resolve *query* → (user_id, profile) using DB exact-match then API search.

        Resolution order:
        1. Exact case-insensitive match in citizen_levels DB (autocomplete source —
           always correct when the user picked a suggestion).
        2. API /search.searchAnything → exact username match in results.
        3. API results → best fuzzy SequenceMatcher ratio match.
        4. DB fuzzy fallback (only when API returns no candidates at all).
        """
        s_low = query.lower().strip()

        # 1. DB exact match — the autocomplete is sourced from citizen_levels,
        #    so this is the most reliable path and avoids the API returning
        #    a similarly-prefixed player instead.
        db = self._db
        if db is not None:
            try:
                db_exact = await db.get_citizen_by_name_exact(query)
                if db_exact:
                    uid, _ = db_exact
                    profile = await self._get_user_profile(uid)
                    if profile is not None:
                        return uid, profile
            except Exception:
                pass

        # 2 & 3. API search
        user_ids = await self._search_user(query)

        if not user_ids:
            # 4. DB fuzzy fallback
            if db is not None:
                nl_country_id = self.config.get("nl_country_id") or self.config.get("country_id")
                try:
                    match = await db.fuzzy_citizen_by_name(query, country_id=nl_country_id)
                    if match:
                        uid, _ = match
                        profile = await self._get_user_profile(uid)
                        if profile is not None:
                            return uid, profile
                except Exception:
                    pass
            return None, None

        candidates: list[tuple[str, dict]] = []
        for uid in user_ids:
            profile = await self._get_user_profile(uid)
            if profile is not None:
                candidates.append((uid, profile))

        # Exact match
        for uid, profile in candidates:
            if (profile.get("username") or "").lower().strip() == s_low:
                return uid, profile

        # Best ratio match
        best_uid, best_profile, best_ratio = None, None, -1.0
        for uid, profile in candidates:
            ratio = difflib.SequenceMatcher(
                None, s_low, (profile.get("username") or "").lower().strip()
            ).ratio()
            if ratio > best_ratio:
                best_ratio, best_uid, best_profile = ratio, uid, profile
        return best_uid, best_profile

    async def _get_company_ids(self, user_id: str) -> list[str]:
        """Return all company IDs owned by *user_id* (paginated)."""
        ids: list[str] = []
        cursor: Optional[str] = None
        while True:
            payload: dict = {"userId": user_id, "perPage": 100}
            if cursor:
                payload["cursor"] = cursor
            try:
                raw = await self._client.get(
                    "/company.getCompanies",
                    params={"input": json.dumps(payload)},
                )
                data = _unwrap(raw)
            except Exception as exc:
                logger.warning("ecobuild: getCompanies failed: %s", exc)
                break
            if not isinstance(data, dict):
                break
            items = data.get("items") or []
            ids.extend(str(i) for i in items if i)
            cursor = data.get("nextCursor") or data.get("cursor")
            if not cursor or not items:
                break
            await asyncio.sleep(0)
        return ids

    async def _get_ae_bonuses(self, companies: list[dict]) -> dict[str, float]:
        """Return {company _id → total production bonus %} from company.getProductionBonus.

        Deposits, ethics and the old strategic-resource production bonus no
        longer exist (v0.27 and the Nov-2026 strategic resources rework), so the
        game's own per-company `total` is the only source left — it is 0 on
        every company at the moment.
        """
        if not self._client or self._client.is_available is False:
            return {}
        cids = [
            str(c.get("_id") or c.get("id") or "")
            for c in companies
            if c.get("_id") or c.get("id")
        ]
        if not cids:
            return {}
        try:
            results = await asyncio.wait_for(
                self._client.batch_get(
                    "company.getProductionBonus", [{"companyId": cid} for cid in cids]
                ),
                timeout=30.0,
            )
        except Exception as exc:
            logger.warning("ecobuild: getProductionBonus batch failed: %s", exc)
            return {}
        bonus_map: dict[str, float] = {}
        for cid, raw in zip(cids, results):
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            if isinstance(data, dict):
                try:
                    bonus_map[cid] = float(data.get("total") or 0)
                except (TypeError, ValueError):
                    pass
        return bonus_map

    async def _get_company_details(self, company_ids: list[str]) -> list[Optional[dict]]:
        """Return full company objects for *company_ids* via tRPC batch."""
        if not company_ids:
            return []
        inputs = [{"companyId": cid} for cid in company_ids]
        try:
            results = await asyncio.wait_for(
                self._client.batch_get("company.getById", inputs),
                timeout=30.0,
            )
        except Exception as exc:
            logger.warning("ecobuild: batch_get failed: %s", exc)
            return [None] * len(company_ids)
        out: list[Optional[dict]] = []
        for raw in results:
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            out.append(data if isinstance(data, dict) else None)
        return out

    # ------------------------------------------------------------------
    # Generic (level-based) calculation
    # ------------------------------------------------------------------

    async def _ecobuild_generic(
        self,
        interaction: discord.Interaction,
        player_level: int,
        n_companies: int,
        ae_level: int = 1,
        prod_bonus_pct: float = 0.0,
    ) -> None:
        """Compute and display optimal eco build for a given level and company count."""
        player_level = max(1, player_level)
        n_companies = max(0, min(n_companies, 12))

        # 4 SP per level (verified from damage_calc.py)
        total_sp = player_level * 4

        # Companies skill: minimum level needed to run n_companies companies
        if n_companies == 0:
            comp_lvl = 0
        else:
            comp_lvl = next(
                (lvl for lvl in range(11) if _SKILL_VALUES["companies"][lvl] >= n_companies),
                10,
            )

        # Assume no workers (management) skill for generic calculation
        workers_lvl = 0
        fixed_sp = _CUMUL_COST[comp_lvl] + _CUMUL_COST[workers_lvl]
        free_sp = max(0, total_sp - fixed_sp)

        # Use the provided AE level and best production bonus for every company slot
        ae_base_daily = _engine_cycle_daily(ae_level)
        ae_daily_with_bonus = round(ae_base_daily * (1 + prod_bonus_pct / 100))
        engine_daily_total = n_companies * ae_daily_with_bonus

        best_build = _optimize_eco_skills(free_sp, engine_daily_total)

        color = self._embed_colour()
        embed = discord.Embed(
            title="Eco Build (generiek)",
            description=f"Level {player_level}  \u2022  {n_companies} {'bedrijven' if n_companies != 1 else 'bedrijf'}  \u2022  AE niveau {ae_level}" + (f"  \u2022  +{prod_bonus_pct:.0f}% productiebonus" if prod_bonus_pct > 0 else ""),
            color=color,
        )

        if best_build is None:
            embed.description = (embed.description or "") + "\n\n_Niet genoeg SP voor een zinvolle verdeling._"
        else:
            b_ent, b_en, b_prod, b_daily = best_build
            b_ent_v = _SKILL_VALUES["entrepreneurship"][b_ent]
            b_en_v  = _SKILL_VALUES["energy"][b_en]
            b_prod_v = _SKILL_VALUES["production"][b_prod]
            b_ent_pp = _daily_works_float(b_ent_v) * b_prod_v
            b_en_pp  = _daily_works_float(b_en_v) * b_prod_v
            opt_rows: list[tuple[str, str, str]] = [
                (f"Entrepreneurship ({b_ent_v})", str(b_ent), f"{b_ent_pp:.0f}"),
                (f"Energy ({b_en_v})",            str(b_en),  f"{b_en_pp:.0f}"),
                (f"Production ({b_prod_v})",      str(b_prod), "\u2014"),
                (f"Bedrijven ({n_companies})",    str(comp_lvl), f"{engine_daily_total:.0f}"),
                ("Medewerkers",                   str(workers_lvl), "\u2014"),
            ]
            sp_used = _CUMUL_COST[b_ent] + _CUMUL_COST[b_en] + _CUMUL_COST[b_prod] + fixed_sp
            sp_info = f"SP gebruikt: {sp_used}/{total_sp}"
            embed.add_field(
                name="\u2699\ufe0f Optimale verdeling",
                value=_skill_table(opt_rows, b_daily, sp_info)[:1024],
                inline=False,
            )

        embed.set_footer(text=f"SP = skill points  |  PP/dag = productie punten per dag  |  AE op niveau {ae_level} per bedrijf" + (f" (+{prod_bonus_pct:.0f}% productiebonus)" if prod_bonus_pct > 0 else ""))
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ecobuild",
        description="Show optimal eco skill distribution for max daily production.",
    )
    @app_commands.describe(
        speler="WarEra username (leave empty for yourself)",
        level="Player level (use instead of a username for a generic calculation)",
        bedrijven="Number of companies (used together with level for a generic calculation)",
    )
    @app_commands.autocomplete(speler=citizen_autocomplete)
    async def ecobuild(
        self,
        interaction: discord.Interaction,
        speler: Optional[str] = None,
        level: Optional[int] = None,
        bedrijven: Optional[int] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        if not self._client or self._client.is_available is False:
            await self._send_api_offline(interaction)
            return

        # ── Generic mode: level and/or bedrijven provided ────────────────
        if level is not None or bedrijven is not None:
            gen_level = level
            gen_bedrijven = bedrijven
            gen_ae_lvl = 1  # default: AE level 1

            # Always resolve player profile to fill missing values and get AE data
            query = speler or strip_division_prefix(interaction.user.display_name)
            gen_user_id, gen_profile = await self._resolve_user(query)
            if gen_profile is not None:
                leveling_g: dict = gen_profile.get("leveling") or {}
                if gen_level is None:
                    gen_level = int(leveling_g.get("level") or 1)

            # Fetch company data to find the most common AE level, total count and best bonus
            gen_best_bonus_pct: float = 0.0
            if gen_user_id is not None:
                gen_company_ids = await self._get_company_ids(gen_user_id)
                if gen_company_ids:
                    gen_details = await self._get_company_details(gen_company_ids)
                    gen_valid = [c for c in gen_details if c is not None]
                    ae_lvls = [
                        max(1, int((c.get("activeUpgradeLevels") or {}).get("automatedEngine") or 0))
                        for c in gen_valid
                    ]
                    if ae_lvls:
                        gen_ae_lvl = max(set(ae_lvls), key=ae_lvls.count)
                    # Use total owned companies (active + inactive) when bedrijven was not given
                    if gen_bedrijven is None:
                        gen_bedrijven = len(gen_company_ids)
                    # Fetch production bonuses and take the highest
                    gen_ae_bonuses = await self._get_ae_bonuses(gen_valid)
                    if gen_ae_bonuses:
                        gen_best_bonus_pct = max(gen_ae_bonuses.values())

            # Fall back to companies skill value if still None (player could not be resolved)
            if gen_bedrijven is None and gen_profile is not None:
                comp_lvl_g, _ = _extract_skill(gen_profile, "companies")
                gen_bedrijven = _SKILL_VALUES["companies"][comp_lvl_g]

            await self._ecobuild_generic(interaction, gen_level or 1, gen_bedrijven or 0, gen_ae_lvl, gen_best_bonus_pct)
            return

        query = speler or strip_division_prefix(interaction.user.display_name)
        user_id, profile = await self._resolve_user(query)

        if user_id is None or profile is None:
            await interaction.followup.send(
                f"❌ Speler **{discord.utils.escape_markdown(query)}** niet gevonden.",
                ephemeral=True,
            )
            return

        username: str = profile.get("username") or query
        avatar_url: str = profile.get("avatarUrl") or ""
        leveling: dict = profile.get("leveling") or {}

        # ── Extract current eco skills ────────────────────────────────────
        ent_lvl, ent_val = _extract_skill(profile, "entrepreneurship")
        energy_lvl, energy_val = _extract_skill(profile, "energy")
        prod_lvl, prod_val = _extract_skill(profile, "production")
        comp_lvl, _ = _extract_skill(profile, "companies")

        player_level: int = leveling.get("level", 0) or 0

        # ── Fetch owned companies ─────────────────────────────────────────
        company_ids = await self._get_company_ids(user_id)
        companies: list[dict] = []
        if company_ids:
            details = await self._get_company_details(company_ids)
            companies = [c for c in details if c is not None]

        # ── Calculate engine daily totals ─────────────────────────────────
        ae_bonuses = await self._get_ae_bonuses(companies)

        engine_daily_total = 0
        engine_info: list[tuple[str, str, int, int, float]] = []  # (name, item, eng_lvl, daily, bonus_pct)
        for c in companies:
            cid = str(c.get("_id") or c.get("id") or "")
            upgrades = c.get("activeUpgradeLevels") or {}
            eng_lvl = int(upgrades.get("automatedEngine") or 0)
            prod_bonus_pct = ae_bonuses.get(cid, 0.0)
            ae_base = _engine_cycle_daily(eng_lvl)
            ae_daily = round(ae_base * (1 + prod_bonus_pct / 100))
            engine_daily_total += ae_daily
            engine_info.append((
                c.get("name") or "?",
                c.get("itemCode") or "?",
                eng_lvl,
                ae_daily,
                prod_bonus_pct,
            ))

        # ── Cap active companies to current companies skill level ──────────
        # The companies skill value determines how many companies a player can run.
        # Companies above the cap are disabled by the game (lowest AE output first).
        comp_val = _SKILL_VALUES["companies"][comp_lvl]
        n_active = min(comp_val, len(companies))
        engine_info_by_daily = sorted(engine_info, key=lambda x: -x[3])
        engine_daily_total = sum(x[3] for x in engine_info_by_daily[:n_active])

        # ── Best production bonus (applied to entrepreneurship manually worked companies) ──
        # A player working their own company benefits from that company's production bonus.
        # We use the highest bonus across all owned companies (player picks the best one to work).
        best_ent_prod_mult = 1.0 + max(ae_bonuses.values(), default=0.0) / 100.0

        # ── SP budget: total minus SP locked in companies + workers ──────────
        # The "employees/workers" skill is stored as "management" in the API.
        _mgmt_entry = (profile.get("skills") or {}).get("management")
        if isinstance(_mgmt_entry, dict):
            workers_lvl = max(0, min(_mgmt_entry.get("level", 0) or 0, 10))
        elif isinstance(_mgmt_entry, (int, float)):
            workers_lvl = max(0, min(int(_mgmt_entry), 10))
        else:
            workers_lvl = 0

        total_sp = _eco_sp_budget(profile)
        fixed_sp = _CUMUL_COST[comp_lvl] + _CUMUL_COST[workers_lvl]
        free_sp = max(0, total_sp - fixed_sp)

        # SP currently invested in the three self-work skills
        cur_eco_sp = _CUMUL_COST[ent_lvl] + _CUMUL_COST[energy_lvl] + _CUMUL_COST[prod_lvl]
        # SP not in eco skills — may be unspent, in war skills, or a mix
        extra_sp = free_sp - cur_eco_sp
        avail_sp = int(leveling.get("availableSkillPoints", 0) or 0)
        # Unspent SP contributes to the gap first; the remainder is in war skills
        war_sp = max(0, extra_sp - avail_sp)
        unspent_sp = max(0, min(extra_sp, avail_sp))

        # ── Current daily production ──────────────────────────────────────
        cur_ent_pp = _daily_works_float(ent_val) * prod_val * best_ent_prod_mult
        cur_en_pp = _daily_works_float(energy_val) * prod_val
        current_total = cur_ent_pp + cur_en_pp + engine_daily_total

        # ── Run optimizer for optimal build using all owned companies ──────
        # Advise using all owned companies (active + inactive); find the minimum
        # companies skill level needed to accommodate all of them.
        opt_n_companies = len(companies)
        if opt_n_companies > 0:
            opt_comp_lvl = next(
                (lvl for lvl in range(11) if _SKILL_VALUES["companies"][lvl] >= opt_n_companies),
                10,
            )
        else:
            opt_comp_lvl = comp_lvl
        opt_fixed_sp = _CUMUL_COST[opt_comp_lvl] + _CUMUL_COST[workers_lvl]
        opt_free_sp = max(0, total_sp - opt_fixed_sp)
        # Sum AE output of all owned companies (sorted best-first)
        opt_engine_daily = sum(x[3] for x in engine_info_by_daily)
        best_build = _optimize_eco_skills(opt_free_sp, opt_engine_daily, best_ent_prod_mult)

        # ── Build embed ───────────────────────────────────────────────────
        color = self._embed_colour()
        embed = discord.Embed(
            title=f"Eco Build — {discord.utils.escape_markdown(username)}",
            description=f"Level {player_level}",
            color=color,
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        # ── Huidige eco skills ───────────────────────────────────────────
        cur_rows: list[tuple[str, str, str]] = [
            (f"Entrepreneurship ({ent_val})", str(ent_lvl), f"{cur_ent_pp:.0f}"),
            (f"Energy ({energy_val})",        str(energy_lvl), f"{cur_en_pp:.0f}"),
            (f"Production ({prod_val})",      str(prod_lvl), "\u2014"),
            (f"Bedrijven ({n_active})",       str(comp_lvl), f"{engine_daily_total:.0f}"),
            ("Medewerkers",                   str(workers_lvl), "\u2014"),
        ]
        cur_table = _skill_table(cur_rows, current_total)
        if war_sp > 0:
            cur_table += f"\n⚠️ {war_sp} SP in oorlogsskills — herbesteedbaar aan eco"
        if unspent_sp > 0:
            cur_table += f"\n💡 {unspent_sp} SP onbesteed — kan worden ingezet in eco"
        embed.add_field(
            name="\U0001f4ca Huidige eco skills",
            value=cur_table,
            inline=False,
        )

        # ── Optimal build ──────────────────────────────────────────────
        if best_build is not None:
            b_ent, b_en, b_prod, b_daily = best_build
            b_ent_v = _SKILL_VALUES["entrepreneurship"][b_ent]
            b_en_v  = _SKILL_VALUES["energy"][b_en]
            b_prod_v = _SKILL_VALUES["production"][b_prod]
            b_ent_pp = _daily_works_float(b_ent_v) * b_prod_v * best_ent_prod_mult
            b_en_pp  = _daily_works_float(b_en_v) * b_prod_v
            improvement = b_daily - current_total
            improvement_str = (
                f"+{improvement:.0f} PP/dag vs huidig" if improvement > 0 else "al optimaal!"
            )
            opt_rows: list[tuple[str, str, str]] = [
                (f"Entrepreneurship ({b_ent_v})", str(b_ent), f"{b_ent_pp:.0f}"),
                (f"Energy ({b_en_v})",            str(b_en),  f"{b_en_pp:.0f}"),
                (f"Production ({b_prod_v})",      str(b_prod), "\u2014"),
                (f"Bedrijven ({opt_n_companies})", str(opt_comp_lvl), f"{opt_engine_daily:.0f}"),
                ("Medewerkers",                   str(workers_lvl), "\u2014"),
            ]
            embed.add_field(
                name=f"\u2699\ufe0f Optimale verdeling  [{improvement_str}]",
                value=_skill_table(opt_rows, b_daily)[:1024],
                inline=False,
            )

        if opt_n_companies < 12:
            embed.add_field(
                name="\U0001f4c8 Tip: bouw meer bedrijven!",
                value=(
                    "Het kopen van extra bedrijven is veruit de meest winstgevende investering. "
                    "Bedrijven leveren elke dag passieve PP op, hoe meer bedrijven hoe beter."
                ),
                inline=False,
            )

        embed.set_footer(
            text="SP = skill points  |  PP/dag = productie punten per dag",
        )

        await interaction.followup.send(embed=embed)


async def setup(bot) -> None:
    """Register the EcoBuildCog."""
    await bot.add_cog(EcoBuildCog(bot))
