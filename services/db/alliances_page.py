"""Data for the /alliances page — entirely assembled from tables other
sweeps already keep fresh, so this never makes a live API call of its own:

  - alliance_countries    (full_fetcher.py's alliance.getManyPaginated sweep)
  - country_snapshots     (full_fetcher.py's country.getAllCountries sweep;
                            raw_json carries the same rankings.* fields as
                            country.getCountryById — weeklyCountryDamages,
                            weeklyCountryDamagesPerCitizen,
                            countryActivePopulation)
  - citizen_levels        (full_fetcher.py's per-country citizen sweep) —
                            war-mode % is computed here, restricted to
                            level >= 20 (see get_alliances_overview's
                            docstring for why)
  - country_proxy_status  (full_fetcher.py's third-party proxy-country
                            sweep) — a country's "proxy" figures are the
                            summed stats of every OTHER country whose
                            origin_id points back to it.
"""

from __future__ import annotations

import json
from typing import Optional

import aiosqlite

from services import strategic_resources as strategic

_MIN_LEVEL_FOR_READINESS = 20


class AlliancesPageMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def get_alliances_overview(self) -> list[dict]:
        """Every alliance, its member countries, and each country's own +
        proxy-aggregated stats.

        War-mode counts/percentages only ever consider citizens level 20 or
        above — below that, skill_mode is frequently still unset/default
        and not a meaningful signal of real war-readiness.

        Returns a list of dicts:
            {alliance_id, alliance_name,
             strategic: [strategic_resources.summarize() rows for the whole
                         alliance — every non-suspended member's copies],
             countries: [ {
                country_id, country_name, suspended,
                resources: {resource: copies in its own regions},
                population, weekly_damage, damage_per_citizen,
                war_count, war_total, war_pct,
                proxy_population, proxy_weekly_damage, proxy_damage_per_citizen,
                proxy_war_count, proxy_war_total, proxy_war_pct,
                proxy_country_ids: [...],
            }, ... ] }
        """
        alliance_rows: list[tuple[str, str, str, bool]] = []
        async with self._conn.execute(
            "SELECT alliance_id, alliance_name, country_id, suspended FROM alliance_countries"
        ) as cur:
            alliance_rows = [(r[0], r[1], r[2], bool(r[3])) async for r in cur]

        country_stats: dict[str, dict] = {}
        async with self._conn.execute(
            "SELECT country_id, name, raw_json FROM country_snapshots"
        ) as cur:
            async for country_id, name, raw_json in cur:
                stats = {
                    "name": name, "population": None, "weekly_damage": None, "damage_per_citizen": None,
                    "resources": {},
                }
                if raw_json:
                    try:
                        raw = json.loads(raw_json) or {}
                    except (ValueError, TypeError):
                        raw = {}
                    rankings = raw.get("rankings") or {}
                    stats["resources"] = strategic.copies_from_country(raw.get("strategicResources"))
                    stats["population"] = (rankings.get("countryActivePopulation") or {}).get("value")
                    stats["weekly_damage"] = (rankings.get("weeklyCountryDamages") or {}).get("value")
                    stats["damage_per_citizen"] = (rankings.get("weeklyCountryDamagesPerCitizen") or {}).get("value")
                country_stats[str(country_id)] = stats

        readiness: dict[str, tuple[int, int]] = {}  # country_id -> (war_count, total)
        async with self._conn.execute(
            """
            SELECT country_id,
                   SUM(CASE WHEN skill_mode = 'war' THEN 1 ELSE 0 END) AS war_count,
                   COUNT(*) AS total
              FROM citizen_levels
             WHERE level >= ? AND country_id IS NOT NULL AND country_id != ''
             GROUP BY country_id
            """,
            (_MIN_LEVEL_FOR_READINESS,),
        ) as cur:
            async for country_id, war_count, total in cur:
                readiness[str(country_id)] = (int(war_count or 0), int(total or 0))

        proxies_by_origin: dict[str, list[str]] = {}
        async with self._conn.execute(
            "SELECT country_id, origin_id FROM country_proxy_status"
        ) as cur:
            async for proxy_id, origin_id in cur:
                proxies_by_origin.setdefault(str(origin_id), []).append(str(proxy_id))

        def _country_row(country_id: str, suspended: bool = False) -> dict:
            stats = country_stats.get(country_id, {})
            war_count, war_total = readiness.get(country_id, (0, 0))

            proxy_ids = proxies_by_origin.get(country_id, [])
            proxy_population = 0.0
            proxy_weekly_damage = 0.0
            proxy_war_count = 0
            proxy_war_total = 0
            any_proxy_stats = False
            for pid in proxy_ids:
                pstats = country_stats.get(pid)
                if pstats and pstats.get("population") is not None:
                    any_proxy_stats = True
                    proxy_population += pstats.get("population") or 0
                    proxy_weekly_damage += pstats.get("weekly_damage") or 0
                pw, pt = readiness.get(pid, (0, 0))
                proxy_war_count += pw
                proxy_war_total += pt

            population = stats.get("population")
            weekly_damage = stats.get("weekly_damage")
            combined_population = (population or 0) + proxy_population
            combined_weekly_damage = (weekly_damage or 0) + proxy_weekly_damage
            combined_war_count = war_count + proxy_war_count
            combined_war_total = war_total + proxy_war_total

            return {
                "country_id": country_id,
                "country_name": stats.get("name") or country_id,
                "suspended": suspended,
                "resources": stats.get("resources") or {},
                "population": population,
                "weekly_damage": weekly_damage,
                "damage_per_citizen": stats.get("damage_per_citizen"),
                "war_count": war_count,
                "war_total": war_total,
                "war_pct": (war_count / war_total * 100) if war_total else None,
                "proxy_country_ids": proxy_ids,
                "proxy_population": proxy_population if any_proxy_stats else None,
                "proxy_weekly_damage": proxy_weekly_damage if any_proxy_stats else None,
                "proxy_damage_per_citizen": (
                    proxy_weekly_damage / proxy_population if any_proxy_stats and proxy_population else None
                ),
                "proxy_war_count": proxy_war_count if proxy_ids else None,
                "proxy_war_total": proxy_war_total if proxy_ids else None,
                "proxy_war_pct": (proxy_war_count / proxy_war_total * 100) if proxy_war_total else None,
                # Country + all its proxies combined — always populated (falls
                # back to just the country's own numbers when it has no
                # proxies), unlike the proxy_* fields above which stay None
                # with nothing to show.
                "combined_population": combined_population if (population is not None or any_proxy_stats) else None,
                "combined_weekly_damage": combined_weekly_damage if (weekly_damage is not None or any_proxy_stats) else None,
                "combined_damage_per_citizen": (
                    combined_weekly_damage / combined_population if combined_population else None
                ),
                "combined_war_count": combined_war_count,
                "combined_war_total": combined_war_total,
                "combined_war_pct": (combined_war_count / combined_war_total * 100) if combined_war_total else None,
            }

        grouped: dict[str, dict] = {}
        for aid, aname, cid, suspended in alliance_rows:
            aid = str(aid)
            entry = grouped.setdefault(aid, {"alliance_id": aid, "alliance_name": aname, "countries": []})
            entry["countries"].append(_country_row(str(cid), suspended))

        result = list(grouped.values())
        for entry in result:
            entry["countries"].sort(key=lambda c: c["population"] or 0, reverse=True)
            # Every member gets the whole alliance's bonus; a suspended member
            # neither shares in it nor adds to it.
            entry["strategic"] = strategic.summarize(strategic.sum_copies(
                c["resources"] for c in entry["countries"] if not c["suspended"]
            ))
        result.sort(key=lambda a: sum(c["population"] or 0 for c in a["countries"]), reverse=True)

        # Countries not in any alliance — always last, not folded into the
        # population-sorted ordering above like a real alliance would be.
        in_alliance = {str(cid) for _, _, cid, _ in alliance_rows}
        unaffiliated_ids = sorted(set(country_stats) - in_alliance)
        if unaffiliated_ids:
            unaffiliated = [_country_row(cid) for cid in unaffiliated_ids]
            unaffiliated.sort(key=lambda c: c["population"] or 0, reverse=True)
            result.append({
                "alliance_id": "__none__",
                "alliance_name": "No alliance",
                "countries": unaffiliated,
            })
        return result
