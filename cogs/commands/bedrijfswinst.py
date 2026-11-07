"""
This module defines the BedrijfswinstCog, which provides the /bedrijfswinst command to show
whether a user is making a profit on their employees across all their companies.

For each company the command shows, per employee:
    revenue  = sell_price × (company_production_bonus_pct + employee_fidelity_pct) / 100
    profit   = revenue − employee_wage

and a per-company and grand total summary.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
from typing import Optional

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase, citizen_autocomplete, strip_division_prefix

logger = logging.getLogger("discord_bot")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unwrap(resp: object) -> object:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


def _fmt_cc(value: float) -> str:
    """Format a currency value with 3 decimal places and a CC suffix."""
    return f"{value:,.3f} CC"


def _production_multiplier(raw: object) -> float:
    """Normalise a production-bonus value to a plain multiplier (e.g. 105 → 1.05, 1.05 → 1.05).

    The API can return either a percentage integer (105 = 5 % above base) or
    a plain multiplier (1.05).  Values > 10 are treated as percentages.
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return v / 100.0 if v > 10 else v


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class BedrijfswinstCog(CommandCogBase, name="bedrijfswinst"):
    """Employee profit analyser per company."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Internal API helpers
    # ------------------------------------------------------------------

    async def _search_user(self, username: str) -> list[str]:
        """Return up to 5 candidate user IDs for *username*."""
        try:
            raw = await self._client.get(
                "/search.searchAnything",
                params={"input": json.dumps({"searchText": username})},
            )
            data = _unwrap(raw)
            ids: list = data.get("userIds", []) if isinstance(data, dict) else []
            return ids[:5]
        except Exception as exc:
            logger.warning(
                "bedrijfswinst: user search failed for %r: %s", username, exc
            )
            return []

    async def _get_user_profile(self, user_id: str) -> Optional[dict]:
        try:
            raw = await self._client.get(
                "/user.getUserLite",
                params={"input": json.dumps({"userId": user_id})},
            )
            data = _unwrap(raw) if isinstance(raw, dict) else None
            return data
        except Exception as exc:
            logger.warning("bedrijfswinst: getUserLite failed for %s: %s", user_id, exc)
            return None

    async def _resolve_user(self, query: str) -> tuple[Optional[str], Optional[dict]]:
        """Resolve *query* → (user_id, profile).

        Resolution order:
        1. Exact case-insensitive match in citizen_levels DB (autocomplete source).
        2. API search → exact username match.
        3. API search → best fuzzy ratio match.
        4. DB fuzzy fallback (only when API returns no candidates).
        """
        s_low = query.lower().strip()

        # 1. DB exact match — avoids the API returning a similarly-named player.
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

        # API returned nothing — try fuzzy match in local citizen cache
        if not user_ids:
            if db is not None:
                nl_country_id = self.config.get("nl_country_id") or self.config.get(
                    "country_id"
                )
                try:
                    match = await db.fuzzy_citizen_by_name(
                        query, country_id=nl_country_id
                    )
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

        # Exact match wins
        for uid, profile in candidates:
            if (profile.get("username") or "").lower().strip() == s_low:
                return uid, profile

        # Closest ratio
        best_uid, best_profile, best_ratio = None, None, -1.0
        for uid, profile in candidates:
            ratio = difflib.SequenceMatcher(
                None, s_low, (profile.get("username") or "").lower().strip()
            ).ratio()
            if ratio > best_ratio:
                best_ratio, best_uid, best_profile = ratio, uid, profile
        return best_uid, best_profile

    async def _get_company_ids(self, user_id: str) -> list[str]:
        """Return all company IDs owned by *user_id* (getCompanies returns IDs only)."""
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
                logger.warning("bedrijfswinst: getCompanies failed: %s", exc)
                break

            if not isinstance(data, dict):
                break

            items = data.get("items") or []
            if not isinstance(items, list):
                break
            ids.extend(str(i) for i in items if i)
            cursor = data.get("nextCursor") or data.get("cursor")
            if not cursor or not items:
                break
            await asyncio.sleep(0)
        return ids

    async def _get_company_details(
        self, company_ids: list[str]
    ) -> list[Optional[dict]]:
        """Fetch full company objects for *company_ids* using tRPC batching.

        Returns a list of the same length; entries are ``None`` for failed lookups.
        """
        inputs = [{"companyId": cid} for cid in company_ids]
        try:
            results = await asyncio.wait_for(
                self._client.batch_get("company.getById", inputs),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning("bedrijfswinst: batch_get timed out — returning all None")
            return [None] * len(company_ids)
        except Exception as exc:
            logger.warning("bedrijfswinst: batch_get failed: %s", exc)
            return [None] * len(company_ids)

        out: list[Optional[dict]] = []
        for raw in results:
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            out.append(data if isinstance(data, dict) else None)
        return out

    async def _get_item_prices(self) -> dict[str, float]:
        """Return a mapping of item_code → average market price from getPrices (fallback)."""
        try:
            raw = await asyncio.wait_for(
                self._client.get(
                    "/itemTrading.getPrices",
                    params={"input": "{}"},
                ),
                timeout=15.0,
            )
            data = _unwrap(raw)
        except TimeoutError:
            logger.warning(
                "bedrijfswinst: getPrices timed out after 15s — returning empty prices"
            )
            return {}
        except Exception as exc:
            logger.warning("bedrijfswinst: getPrices failed: %s", exc)
            return {}

        prices: dict[str, float] = {}
        if isinstance(data, dict):
            for code, info in data.items():
                if isinstance(info, dict):
                    price = (
                        info.get("averagePrice")
                        or info.get("average")
                        or info.get("sell")
                        or info.get("price")
                        or info.get("value")
                        or 0
                    )
                else:
                    price = info
                try:
                    prices[code] = float(price)
                except (TypeError, ValueError):
                    pass
        elif isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    code = entry.get("code") or entry.get("itemCode") or ""
                    price = (
                        entry.get("averagePrice")
                        or entry.get("average")
                        or entry.get("sell")
                        or entry.get("price")
                        or entry.get("value")
                        or 0
                    )
                    if code:
                        try:
                            prices[code] = float(price)
                        except (TypeError, ValueError):
                            pass
        return prices

    async def _get_highest_bid_prices(
        self, item_codes: list[str], fallback: dict[str, float]
    ) -> dict[str, float]:
        """Return item_code → highest buy-order price via tradingOrder.getTopOrders.

        Fetches all item codes in parallel.  Falls back to *fallback* (average prices)
        for items where no buy orders exist.
        """
        if not item_codes or not self._client:
            return fallback

        def _parse_top_orders(resp: object) -> float:
            """Extract highest buy-order price from a getTopOrders response."""
            if isinstance(resp, dict):
                inner = (resp.get("result") or {}).get("data")
                if isinstance(inner, dict):
                    resp = inner
            if not isinstance(resp, dict):
                return 0.0
            for buy_key in ("buyOrders", "buy_orders", "bids", "buy"):
                orders = resp.get(buy_key)
                if isinstance(orders, list) and orders:
                    raw = (
                        orders[0].get("price")
                        or orders[0].get("unitPrice")
                        or orders[0].get("value")
                    )
                    try:
                        return float(raw)
                    except (TypeError, ValueError):
                        pass
            return 0.0

        async def _fetch_one(code: str) -> tuple[str, float]:
            try:
                resp = await asyncio.wait_for(
                    self._client.post(
                        "/tradingOrder.getTopOrders",
                        json={"itemCode": code, "limit": 1},
                    ),
                    timeout=10.0,
                )
                return code, _parse_top_orders(resp)
            except Exception:
                return code, 0.0

        results = await asyncio.gather(*[_fetch_one(c) for c in item_codes])
        prices: dict[str, float] = dict(fallback)  # start with averages as base
        for code, bid in results:
            if bid > 0:
                prices[code] = bid  # override with highest bid when available
        return prices

    async def _get_production_points(
        self,
    ) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        """Return (prod_points, prod_needs) from the game config.

        prod_points: {item_code: productionPoints}  — production-action points
            needed to make one unit (e.g. steel=10, iron=1).
        prod_needs:  {item_code: {input_item: qty}} — raw materials consumed per
            unit produced (e.g. steel={"iron": 10.0}).

        Both dicts fall back to safe defaults on failure.
        """
        try:
            raw = await asyncio.wait_for(
                self._client.get("/gameConfig.getGameConfig", params={"input": "{}"}),
                timeout=10.0,
            )
            data = _unwrap(raw)
            items_cfg = (data or {}).get("items", {}) if isinstance(data, dict) else {}
            prod_points: dict[str, float] = {}
            prod_needs: dict[str, dict[str, float]] = {}
            for code, info in items_cfg.items():
                if not isinstance(info, dict):
                    continue
                try:
                    pp = float(info.get("productionPoints") or 1)
                    prod_points[code] = pp if pp > 0 else 1.0
                except (TypeError, ValueError):
                    prod_points[code] = 1.0
                raw_needs = info.get("productionNeeds") or {}
                if isinstance(raw_needs, dict) and raw_needs:
                    prod_needs[code] = {
                        mat: float(qty) for mat, qty in raw_needs.items()
                    }
            return prod_points, prod_needs
        except Exception as exc:
            logger.warning(
                "bedrijfswinst: getGameConfig failed: %s — using defaults", exc
            )
            return {}, {}

    @staticmethod
    def _extract_eco_skill(data: dict, skill_name: str) -> float:
        """Extract the level/value of a named eco skill from a getUserLite response.

        The ``skills`` field can be a dict-of-dicts, a flat dict, or a list-of-dicts;
        mirrors the logic in CitizenCache._extract_skill_mode.
        Returns 0.0 when the skill is not found.
        """
        skills = data.get("skills")
        if skills is None:
            return 0.0

        def _pick(entry: dict) -> float:
            # prefer "level" but don't skip it when it is 0 (falsy)
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

    async def _get_company_production_bonuses(
        self, company_ids: list[str]
    ) -> dict[str, float]:
        """Return {company_id: total_production_bonus_pct} from company.getProductionBonus."""
        if not company_ids:
            return {}
        inputs = [{"companyId": cid} for cid in company_ids]
        try:
            results = await asyncio.wait_for(
                self._client.batch_get("company.getProductionBonus", inputs),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning("bedrijfswinst: getProductionBonus batch timed out")
            return {}
        except Exception as exc:
            logger.warning("bedrijfswinst: getProductionBonus batch failed: %s", exc)
            return {}
        out: dict[str, float] = {}
        for cid, raw in zip(company_ids, results):
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            if isinstance(data, dict):
                try:
                    out[cid] = float(data.get("total") or 0)
                except (TypeError, ValueError):
                    out[cid] = 0.0
            else:
                out[cid] = 0.0
        return out

    async def _get_worker_profiles(self, user_ids: list[str]) -> dict[str, dict]:
        """Return a {user_id: {username, energy, production}} mapping via batch getUserLite."""
        if not user_ids:
            return {}
        inputs = [{"userId": uid} for uid in user_ids]
        try:
            results = await asyncio.wait_for(
                self._client.batch_get("user.getUserLite", inputs),
                timeout=15.0,
            )
        except Exception as exc:
            logger.warning("bedrijfswinst: batch getUserLite failed: %s", exc)
            return {}
        out: dict[str, dict] = {}
        for uid, raw in zip(user_ids, results):
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            if isinstance(data, dict):
                out[uid] = {
                    "username": data.get("username") or uid,
                    "energy": self._extract_eco_skill(data, "energy"),
                    "production": self._extract_eco_skill(data, "production"),
                }
        return out

    # ------------------------------------------------------------------
    # Slash command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bedrijfswinst",
        description="Toon de winstgevendheid van werknemers per bedrijf van een speler.",
    )
    @app_commands.describe(
        speler="WarEra-gebruikersnaam van de speler (leeg = jijzelf)",
    )
    @app_commands.autocomplete(speler=citizen_autocomplete)
    async def bedrijfswinst(
        self,
        interaction: discord.Interaction,
        speler: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        if not self._client or self._client.is_available is False:
            await self._send_api_offline(interaction)
            return

        # Resolve player ------------------------------------------------
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

        company_ids = await self._get_company_ids(user_id)

        if not company_ids:
            await interaction.followup.send(
                f"**{discord.utils.escape_markdown(username)}** heeft geen bedrijven.",
                ephemeral=True,
            )
            return

        company_details = await self._get_company_details(company_ids)
        companies = [c for c in company_details if c is not None]

        if not companies:
            await interaction.followup.send(
                f"**{discord.utils.escape_markdown(username)}** heeft geen bedrijven (gegevens niet beschikbaar).",
                ephemeral=True,
            )
            return

        item_codes_needed = list({c.get("itemCode") or "" for c in companies} - {""})

        # worker.getWorkers, the deposit / strategic-resource / ethics production
        # bonus lookups and the specialization tables are gone from the game
        # (v0.27 + the Nov-2026 strategic resources rework): the only production
        # bonus left is company.getProductionBonus's `total`.
        (
            ae_prod_bonuses_map,
            (_avg_prices, (prod_points, prod_needs)),
        ) = await asyncio.gather(
            self._get_company_production_bonuses([c.get("_id", "") for c in companies]),
            asyncio.gather(
                self._get_item_prices(),
                self._get_production_points(),
            ),
        )
        # Overwrite average prices with highest buy-order prices where available
        item_prices = await self._get_highest_bid_prices(item_codes_needed, _avg_prices)

        # Compute total automated engine PP/day across all companies.
        # Level N gives N PP/hr → N × 24 PP/day, multiplied by the company's total
        # production bonus (as returned by company.getProductionBonus).
        # Disabled companies (disabledAt is set) are excluded.
        _AE_CC_PER_PP = 0.08
        total_ae_pp_day = 0.0
        for company in companies:
            if company.get("disabledAt"):
                continue
            cid = company.get("_id", "")
            ae_level = int(
                (company.get("activeUpgradeLevels") or {}).get("automatedEngine") or 0
            )
            if ae_level <= 0:
                continue
            ae_bonus_pct = ae_prod_bonuses_map.get(cid, 0.0)
            total_ae_pp_day += ae_level * 24 * (1.0 + ae_bonus_pct / 100.0)

        # No job market any more, so no workers to list (see above).
        workers_by_id: dict[str, list[dict]] = {}
        prod_bonus_by_id: dict[str, dict] = {
            c.get("_id", ""): {
                "region_pct": ae_prod_bonuses_map.get(c.get("_id", ""), 0.0),
                "country_pct": 0.0,
                "ethics_pct": 0.0,
                "total_mult": 1.0 + ae_prod_bonuses_map.get(c.get("_id", ""), 0.0) / 100.0,
            }
            for c in companies
        }

        all_worker_ids = list(
            {
                emp.get("user")
                for workers in workers_by_id.values()
                for emp in workers
                if emp.get("user")
            }
        )
        worker_profiles = await self._get_worker_profiles(all_worker_ids)

        # Build embeds
        try:
            colour = self._embed_colour()
        except Exception:
            colour = discord.Colour.gold()

        total_revenue_all = 0.0
        total_mat_cost_all = 0.0
        total_wage_all = 0.0
        total_profit_day_all = 0.0
        embeds: list[discord.Embed] = []

        try:
            # {company_name: (item_code, breakeven_f0, breakeven_f10)}
            breakeven_overview: list[tuple[str, str, float, float]] = []

            def _fmt_t(v: float) -> str:
                """Format a CC value for table display — no 'CC' suffix."""
                return f"{v:.3f}"

            for company in companies:
                company_id_co = company.get("_id", "")
                company_name: str = company.get("name") or "Onbekend bedrijf"
                item_code: str = company.get("itemCode") or ""
                sell_price: float = item_prices.get(item_code, 0.0)
                # pts of work required to produce one item; raw materials consumed per item
                pp_pts: float = prod_points.get(item_code) or 1.0
                item_mat_needs: dict[str, float] = prod_needs.get(item_code, {})
                # Production bonuses from region + country (ethics / strategic resources)
                _pb = prod_bonus_by_id.get(
                    company_id_co,
                    {
                        "region_pct": 0.0,
                        "country_pct": 0.0,
                        "ethics_pct": 0.0,
                        "total_mult": 1.0,
                    },
                )
                region_pct: float = _pb["region_pct"]
                country_pct: float = _pb["country_pct"]
                ethics_pct: float = _pb.get("ethics_pct", 0.0)
                prod_bonus_mult: float = _pb["total_mult"]
                workers: list[dict] = workers_by_id.get(company_id_co, [])

                if not workers:
                    # Still compute breakeven for the overview embed
                    _bk_items_pre = 1.0 / pp_pts
                    _bk_mat_pre = sum(
                        float(qty) * item_prices.get(mat, 0.0)
                        for mat, qty in item_mat_needs.items()
                    )
                    _bk_margin_pre = sell_price - _bk_mat_pre
                    breakeven_overview.append(
                        (
                            company_name,
                            item_code,
                            _bk_items_pre * (prod_bonus_mult + 0.00) * _bk_margin_pre,
                            _bk_items_pre * (prod_bonus_mult + 0.10) * _bk_margin_pre,
                        )
                    )
                    continue

                embed = discord.Embed(
                    title=f"{discord.utils.escape_markdown(username)} — inkomsten werknemers bij bedrijven",
                    colour=colour,
                )
                bonus_footer_parts: list[str] = []
                if region_pct:
                    bonus_footer_parts.append(f"+{region_pct:.0f}% dep")
                if country_pct:
                    bonus_footer_parts.append(f"+{country_pct:.0f}% SR")
                if ethics_pct:
                    bonus_footer_parts.append(f"+{ethics_pct:.0f}% eth")
                total_bonus_pct = region_pct + country_pct + ethics_pct
                bonus_footer = (
                    f" | +{total_bonus_pct:.0f}%"
                    if total_bonus_pct
                    else ""
                )
                pp_footer = f" | {pp_pts:.0f} PP/item"
                if item_mat_needs:
                    _mat_parts = []
                    _mat_total = 0.0
                    for mat, qty in item_mat_needs.items():
                        _unit = item_prices.get(mat, 0.0)
                        _cost = float(qty) * _unit
                        _mat_total += _cost
                        _mat_parts.append(f"{float(qty):.0f}× {mat} ({_fmt_cc(_unit)})")
                    mat_line = (
                        "mat: "
                        + ", ".join(_mat_parts)
                        + f" = {_fmt_cc(_mat_total)}"
                    )
                else:
                    mat_line = ""
                _footer_line1 = f"{item_code} | {_fmt_cc(sell_price)}{bonus_footer}{pp_footer}"
                embed.set_footer(
                    text=_footer_line1 if not mat_line else f"{_footer_line1}\n{mat_line}"
                )

                total_revenue_co = 0.0
                total_mat_cost_co = 0.0
                total_wage_co = 0.0
                total_profit_day_co = 0.0
                # (indicator, name, fidelity_int, wage_rate, profit_per_pp, profit_per_day)
                rows: list[tuple] = []

                for emp in workers:
                    emp_uid = emp.get("user") or ""
                    profile = worker_profiles.get(emp_uid) or {}
                    emp_name: str = profile.get("username") or emp_uid or "?"
                    fidelity = float(emp.get("fidelity") or 0)
                    # fidelity % and production % are additive, not multiplicative
                    combined_mult = prod_bonus_mult + fidelity * 0.01

                    # Employee skills
                    # The API may return the upgrade level (0-10) OR the computed value
                    # (PP total / energy pool) directly.  Values > 10 are the computed result.
                    raw_prod = float(
                        profile.get("production") or emp.get("production") or 0
                    )
                    emp_pp = raw_prod if raw_prod > 10 else 10.0 + raw_prod * 3.0

                    # energy: base pool 30 + 10/upgrade; max level 10 → pool 130
                    # If API returns pool directly (e.g. 30 for 0 upgrades), raw > 10 is false
                    # for base case — so we special-case: multiples of 10 in range [30,130]
                    # are almost certainly the pool value.
                    raw_energy = float(profile.get("energy") or emp.get("energy") or 0)
                    if raw_energy > 10:
                        emp_energy_pool = raw_energy  # already the pool
                    else:
                        emp_energy_pool = (
                            30.0 + raw_energy * 10.0
                        )  # convert level to pool
                    actions_per_day = emp_energy_pool * 0.24

                    # Total PP the employee delivers per day
                    total_pp_day = emp_pp * actions_per_day

                    # items produced per PP — base rate times combined bonus (additive pcts)
                    items_per_pp = 1.0 / pp_pts
                    items_produced_per_pp = items_per_pp * combined_mult

                    # Gross revenue per PP (before material costs)
                    gross_rev_per_pp = items_produced_per_pp * sell_price

                    # Raw material cost per PP scales with production (more items = more mats)
                    mat_cost_per_pp = items_produced_per_pp * sum(
                        float(qty) * item_prices.get(mat, 0.0)
                        for mat, qty in item_mat_needs.items()
                    )

                    # Net revenue per PP (after material costs)
                    revenue_per_pp = gross_rev_per_pp - mat_cost_per_pp

                    revenue = revenue_per_pp * total_pp_day

                    # Salary per day: wage (CC/PP) × employee PP/dag
                    wage_rate = float(emp.get("wage") or 0.0)
                    wage_cost = wage_rate * total_pp_day

                    profit_per_pp = revenue_per_pp - wage_rate
                    profit_per_day = profit_per_pp * total_pp_day
                    total_revenue_co += revenue
                    total_mat_cost_co += mat_cost_per_pp * total_pp_day
                    total_wage_co += wage_cost
                    total_profit_day_co += profit_per_day

                    indicator = "+" if profit_per_day >= 0 else "-"
                    rows.append(
                        (
                            indicator,
                            emp_name,
                            int(fidelity),
                            wage_rate,
                            profit_per_pp,
                            profit_per_day,
                        )
                    )

                total_revenue_all += total_revenue_co
                total_mat_cost_all += total_mat_cost_co
                total_wage_all += total_wage_co
                total_profit_day_all += total_profit_day_co

                # Breakeven wage: max CC/PP at which a hire is still profitable.
                # Computed for fidelity 0 and 10.  Merged into the employee table.
                _bk_items_per_pp = 1.0 / pp_pts
                _bk_mat_per_item = sum(
                    float(qty) * item_prices.get(mat, 0.0)
                    for mat, qty in item_mat_needs.items()
                )
                # margin per item (sell - materials); production bonus scales both equally
                _bk_margin_per_item = sell_price - _bk_mat_per_item
                breakeven_f0 = (
                    _bk_items_per_pp * (prod_bonus_mult + 0.00) * _bk_margin_per_item
                )
                breakeven_f10 = (
                    _bk_items_per_pp * (prod_bonus_mult + 0.10) * _bk_margin_per_item
                )

                breakeven_overview.append(
                    (
                        company_name,
                        item_code,
                        breakeven_f0,
                        breakeven_f10,
                    )
                )

                # Build a combined aligned table: employees + breakeven section
                _NAME_MAX = 8
                _ANSI_GRN = "\u001b[0;32m"
                _ANSI_RED = "\u001b[0;31m"
                _ANSI_RST = "\u001b[0m"

                def _trunc(s: str) -> str:
                    return s if len(s) <= _NAME_MAX else s[: _NAME_MAX - 1] + "…"

                def _ansi_val(s: str, val: float) -> str:
                    c = _ANSI_GRN if val >= 0 else _ANSI_RED
                    return f"{c}{s}{_ANSI_RST}"

                # Each row: (name, fid, wage_str, pp_val, pp_str, day_val, day_str)
                emp_rows_fmt = [
                    (
                        _trunc(r[1]),
                        r[2],
                        _fmt_t(r[3]),
                        r[4],
                        _fmt_t(r[4]),
                        r[5],
                        _fmt_t(r[5]),
                    )
                    for r in rows
                ]
                name_w = max((len(r[0]) for r in emp_rows_fmt), default=4)
                wage_w = max((len(r[2]) for r in emp_rows_fmt), default=5)
                pp_w = (
                    max((len(r[4]) for r in emp_rows_fmt), default=1)
                    if emp_rows_fmt
                    else 1
                )
                day_w = (
                    max((len(r[6]) for r in emp_rows_fmt), default=1)
                    if emp_rows_fmt
                    else 1
                )
                # header — units shown here (CC implied), not in cells
                _W_wage = max(wage_w, 4)  # "loon" = 4
                _W_pp = max(pp_w, 4)  # "w/PP" = 4
                _W_day = max(day_w, 5)  # "w/dag" = 5
                hdr = (
                    f"{'Naam':<{name_w}} {'fid':>3}"
                    f" {'loon':>{_W_wage}}"
                    f" {'w/PP':>{_W_pp}}"
                    f" {'w/dag':>{_W_day}}"
                )
                sep_w = len(hdr)
                sep = "─" * sep_w

                def _trow(name, fid, wage_s, pp_val, pp_s, day_val, day_s):
                    pp_padded = f"{pp_s:>{_W_pp}}"
                    day_padded = f"{day_s:>{_W_day}}"
                    if pp_val is not None:
                        pp_padded = _ansi_val(pp_padded, pp_val)
                    if day_val is not None:
                        day_padded = _ansi_val(day_padded, day_val)
                    return (
                        f"{name:<{name_w}} {fid:>3}"
                        f" {wage_s:>{_W_wage}}"
                        f" {pp_padded}"
                        f" {day_padded}"
                    )

                n_emp = len(emp_rows_fmt)
                table_lines = [hdr, sep]
                if emp_rows_fmt:
                    table_lines += [_trow(*r) for r in emp_rows_fmt]
                else:
                    table_lines.append("Geen werknemers")
                embed.description = (
                    f"**{discord.utils.escape_markdown(company_name)}** — {n_emp} werknemers\n"
                    "```ansi\n" + "\n".join(table_lines) + "\n```"
                )

                embeds.append(embed)

            if not embeds and not breakeven_overview:
                await interaction.followup.send(
                    f"**{discord.utils.escape_markdown(username)}** heeft geen bedrijven.",
                    ephemeral=True,
                )
                return

            # Grand-total summary — only shown when there are companies with workers
            all_embeds: list[discord.Embed] = []
            if embeds:
                companies_with_workers = sum(
                    1 for c in companies if workers_by_id.get(c.get("_id", ""))
                )
                _S_GRN = "\u001b[0;32m"
                _S_RED = "\u001b[0;31m"
                _S_RST = "\u001b[0m"
                rev_s = _fmt_cc(total_revenue_all + total_mat_cost_all)
                mat_s = _fmt_cc(total_mat_cost_all)
                wage_s = _fmt_cc(total_wage_all)
                profit_s = _fmt_cc(total_profit_day_all)
                _lbl_w = len("Grondstofkosten/dag:")  # longest label = 20
                _val_w = max(len(rev_s), len(mat_s), len(wage_s), len(profit_s))
                _sep = "─" * (_lbl_w + 1 + _val_w)
                _p_color = _S_GRN if total_profit_day_all >= 0 else _S_RED
                _profit_pfx = "+" if total_profit_day_all >= 0 else "-"
                _profit_row = f"{_profit_pfx} {'Netto winst/dag:':<{_lbl_w - 2}} {profit_s:>{_val_w}}"
                summary_table = "\n".join(
                    [
                        f"{'Totale omzet/dag:':<{_lbl_w}} {rev_s:>{_val_w}}",
                        f"{'Grondstofkosten/dag:':<{_lbl_w}} {mat_s:>{_val_w}}",
                        f"{'Loonkosten/dag:':<{_lbl_w}} {wage_s:>{_val_w}}",
                        _sep,
                        f"{_p_color}{_profit_row}{_S_RST}",
                    ]
                )
                _ae_cc_day = total_ae_pp_day * _AE_CC_PER_PP
                _ae_line = (
                    f"🤖 **Automated engine:** {total_ae_pp_day:,.1f} PP/dag → {_fmt_cc(_ae_cc_day)}/dag (@ {_AE_CC_PER_PP} CC/PP)\n\n"
                    if total_ae_pp_day > 0
                    else ""
                )
                summary = discord.Embed(
                    title=f"{discord.utils.escape_markdown(username)} — inkomsten werknemers bij bedrijven",
                    description=(
                        f"{_ae_line}"
                        f"*Inkomsten van werknemers (automated engine niet meegeteld):*\n\n"
                        f"```ansi\n{summary_table}\n```"
                    ),
                    colour=colour,
                )
                if avatar_url:
                    summary.set_thumbnail(url=avatar_url)
                summary.set_footer(
                    text=f"{companies_with_workers}/{len(companies)} bedrijven hebben werknemers"
                )
                all_embeds = [summary] + embeds
            else:
                _ae_cc_day_no_emp = total_ae_pp_day * _AE_CC_PER_PP
                _ae_line_no_emp = (
                    f"🤖 **Automated engine:** {total_ae_pp_day:,.1f} PP/dag → {_fmt_cc(_ae_cc_day_no_emp)}/dag (@ {_AE_CC_PER_PP} CC/PP)\n\n"
                    if total_ae_pp_day > 0
                    else ""
                )
                no_emp_embed = discord.Embed(
                    title=f"{discord.utils.escape_markdown(username)} — inkomsten werknemers bij bedrijven",
                    description=f"{_ae_line_no_emp}*Deze speler heeft geen werknemers in dienst.*",
                    colour=colour,
                )
                if avatar_url:
                    no_emp_embed.set_thumbnail(url=avatar_url)
                no_emp_embed.set_footer(
                    text=f"0/{len(companies)} bedrijven hebben werknemers"
                )
                all_embeds = [no_emp_embed]

            # Breakeven overview: one embed listing every company
            if breakeven_overview:
                _B_NAME_MAX = 8
                _B_ITEM_MAX = 7

                def _btrunc(s: str) -> str:
                    return (
                        s if len(s) <= _B_NAME_MAX else s[: _B_NAME_MAX - 1] + "\u2026"
                    )

                def _bitem(s: str) -> str:
                    return (
                        s if len(s) <= _B_ITEM_MAX else s[: _B_ITEM_MAX - 1] + "\u2026"
                    )

                bk_name_w = max(len(_btrunc(r[0])) for r in breakeven_overview)
                bk_item_w = max(len(_bitem(r[1])) for r in breakeven_overview)
                bk_f0_w = max(len(_fmt_t(r[2])) for r in breakeven_overview)
                bk_f10_w = max(len(_fmt_t(r[3])) for r in breakeven_overview)
                bk_hdr = (
                    f" {'Bedrijf':<{bk_name_w}} {'item':<{bk_item_w}}"
                    f" {'f0/10':>{max(bk_f0_w, 5)}}"
                    f" {'f10/10':>{max(bk_f10_w, 6)}}"
                )
                bk_sep = "\u2500" * len(bk_hdr)
                bk_lines = [bk_hdr, bk_sep]
                for co_name, co_item, bf0, bf10 in breakeven_overview:
                    bk_lines.append(
                        f" {_btrunc(co_name):<{bk_name_w}} {_bitem(co_item):<{bk_item_w}}"
                        f" {_fmt_t(bf0):>{max(bk_f0_w, 5)}}"
                        f" {_fmt_t(bf10):>{max(bk_f10_w, 6)}}"
                    )
                bk_embed = discord.Embed(
                    title=f"{discord.utils.escape_markdown(username)} — breakeven loon per bedrijf (CC/PP)",
                    description="```\n" + "\n".join(bk_lines) + "\n```",
                    colour=colour,
                )
                all_embeds.append(bk_embed)

            for i in range(0, len(all_embeds), 10):
                await interaction.followup.send(embeds=all_embeds[i : i + 10])

        except Exception as exc:
            logger.exception("bedrijfswinst: unhandled error in embed/send: %s", exc)
            await interaction.followup.send(
                f"❌ Er is een fout opgetreden: `{exc}`",
                ephemeral=True,
            )


async def setup(bot) -> None:
    """Add the bedrijfswinst command cog to the bot."""
    await bot.add_cog(BedrijfswinstCog(bot))
