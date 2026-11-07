"""Database mixin for the Nigeria bot's /damage-projection command.

Two tables, both piggybacked on work the fetcher already does every hour:

``alliance_countries``
    Written from a single ``alliance.getManyPaginated`` call (the whole game
    currently has ~12 alliances / ~133 alliance-country pairs, so this is one
    cheap request per sweep).

``citizen_combat_state``
    Current health/hunger per citizen. Populated inside the *existing* hourly
    citizen sweep (:class:`services.citizen_cache.CitizenCache`), which already
    fetches ``user.getUserLite`` for every citizen in the game — the response
    already carries ``skills.health`` / ``skills.hunger``, it just wasn't being
    persisted before. No extra API calls.

Skill-mode (eco/war) comes from the existing ``citizen_levels.skill_mode``
column, and buff/debuff timing from the existing ``citizen_pill_tracking``
table — both already filled in by the same sweep, game-wide. So the only
genuinely new fetching this feature needs is the alliance list.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

logger = logging.getLogger("services.db.damage_projection")

# Matches services/db/pill_tracking.py — a buff lasts 8h, and 15.5h of debuff
# follows once it ends. Duplicated here (rather than imported) because a
# constant this small isn't worth a cross-module dependency, and both call
# sites already carry the same comment pointing at each other.
_DEBUFF_DURATION = 15.5 * 3600  # seconds


class DamageProjectionMixin:
    """CRUD + query helpers for alliance damage-projection data."""

    # ── alliance -> country map ──────────────────────────────────────────────

    async def save_alliance_countries(
        self, rows: Iterable[tuple], updated_at: str
    ) -> int:
        """Replace the alliance→country map with *rows* = (alliance_id, name,
        country_id[, suspended]); suspended defaults to False.

        Wiped and rewritten whole rather than upserted: membership is small
        (~130 rows) and can change (an alliance disbanding, a country leaving),
        so a stale row must not survive past the sweep that no longer sees it.
        """
        payload = [
            (str(row[0]), str(row[1]), str(row[2]), updated_at, int(bool(row[3])) if len(row) > 3 else 0)
            for row in rows
            if row[0] and row[2]
        ]
        await self._conn.execute("DELETE FROM alliance_countries")
        if payload:
            await self._conn.executemany(
                "INSERT INTO alliance_countries "
                "(alliance_id, alliance_name, country_id, updated_at, suspended) "
                "VALUES (?, ?, ?, ?, ?)",
                payload,
            )
        await self._conn.commit()
        return len(payload)

    async def get_alliances_with_countries(self) -> list[tuple[str, str, list[str]]]:
        """Return ``[(alliance_id, alliance_name, [country_id, ...])]``."""
        async with self._conn.execute(
            "SELECT alliance_id, alliance_name, country_id FROM alliance_countries"
        ) as cur:
            rows = await cur.fetchall()
        grouped: dict[str, tuple[str, list[str]]] = {}
        for aid, name, cid in rows:
            aid = str(aid)
            if aid not in grouped:
                grouped[aid] = (str(name), [])
            grouped[aid][1].append(str(cid))
        return [(aid, name, cids) for aid, (name, cids) in grouped.items()]

    # ── combat state (health/hunger) ─────────────────────────────────────────

    async def bulk_upsert_combat_state(
        self, rows: Iterable[tuple[str, str, Optional[float], Optional[float], Optional[float], Optional[float]]],
        updated_at: str,
    ) -> int:
        """Upsert ``(user_id, country_id, health_cur, health_max, hunger_cur, hunger_max)``."""
        payload = [
            (str(uid), str(cid), hc, hm, gc, gm, updated_at)
            for uid, cid, hc, hm, gc, gm in rows
            if uid
        ]
        if not payload:
            return 0
        await self._conn.executemany(
            "INSERT INTO citizen_combat_state "
            "(user_id, country_id, health_cur, health_max, hunger_cur, hunger_max, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  country_id = excluded.country_id, health_cur = excluded.health_cur, "
            "  health_max = excluded.health_max, hunger_cur = excluded.hunger_cur, "
            "  hunger_max = excluded.hunger_max, updated_at = excluded.updated_at",
            payload,
        )
        await self._conn.commit()
        return len(payload)

    # ── damage projection aggregate ──────────────────────────────────────────

    async def get_damage_projection_by_country(
        self, country_ids: list[str]
    ) -> dict[str, dict]:
        """Per-country damage-projection stats for *country_ids*, in one query.

        Returns ``{country_id: {total_players, war_players, war_health,
        war_hunger, buff_count, buff_secs_sum, debuff_count, debuff_secs_sum,
        neither_count}}``.

        Sums (not pre-computed averages) are returned for buff/debuff seconds
        so a caller combining several countries (e.g. summing a whole
        alliance) can compute a correctly weighted average afterwards, rather
        than averaging pre-averaged numbers.

        "War" reuses ``citizen_levels.skill_mode`` — the same points-weighted
        eco/war classification already shown by ``/paraatheid`` — so a player
        counted as "paraat" there is counted as a war player here too. A NULL
        skill_mode (a citizen with zero skill points spent) counts toward
        ``total_players`` but not ``war_players``.
        """
        if not country_ids:
            return {}
        placeholders = ",".join("?" * len(country_ids))
        now = int(time.time())

        stats: dict[str, dict] = {
            cid: {
                "total_players": 0,
                "war_players": 0,
                "war_health": 0.0,
                "war_hunger": 0.0,
                "buff_count": 0,
                "buff_secs_sum": 0.0,
                "debuff_count": 0,
                "debuff_secs_sum": 0.0,
                "neither_count": 0,
            }
            for cid in country_ids
        }

        async with self._conn.execute(
            "SELECT cl.country_id, cl.skill_mode, cs.health_cur, cs.hunger_cur, "
            "       pt.buff_expires_at "
            "FROM citizen_levels cl "
            "LEFT JOIN citizen_combat_state cs ON cs.user_id = cl.user_id "
            "LEFT JOIN citizen_pill_tracking pt ON pt.user_id = cl.user_id "
            f"WHERE cl.country_id IN ({placeholders})",
            country_ids,
        ) as cur:
            async for country_id, skill_mode, health_cur, hunger_cur, expires_at in cur:
                bucket = stats.get(str(country_id))
                if bucket is None:
                    continue  # defensive: shouldn't happen given the WHERE clause
                bucket["total_players"] += 1
                if skill_mode != "war":
                    continue
                bucket["war_players"] += 1
                bucket["war_health"] += float(health_cur or 0.0)
                bucket["war_hunger"] += float(hunger_cur or 0.0)
                if expires_at is None:
                    bucket["neither_count"] += 1
                elif expires_at > now:
                    bucket["buff_count"] += 1
                    bucket["buff_secs_sum"] += float(expires_at - now)
                elif expires_at + _DEBUFF_DURATION > now:
                    bucket["debuff_count"] += 1
                    bucket["debuff_secs_sum"] += float(expires_at + _DEBUFF_DURATION - now)
                else:
                    bucket["neither_count"] += 1

        return stats
