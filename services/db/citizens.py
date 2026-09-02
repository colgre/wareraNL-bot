"""Citizen-level DB methods (citizen_levels table)."""

from __future__ import annotations

import difflib
from datetime import datetime, timezone
from typing import Optional

import aiosqlite


class CitizensMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase
    """citizen_levels table operations."""

    async def upsert_citizen_level(
        self,
        user_id: str,
        country_id: str,
        level: Optional[int],
        updated_at: str,
        skill_mode: Optional[str] = None,
        last_skills_reset_at: Optional[str] = None,
        citizen_name: Optional[str] = None,
        last_login_at: Optional[str] = None,
        mu_id: Optional[str] = None,
        mu_name: Optional[str] = None,
        avatar_url: Optional[str] = None,
        is_active: bool = True,
        is_banned: bool = False,
    ) -> None:
        """Insert or update a citizen's level and related info.

        mu_id / mu_name are preserved from existing row when the caller
        passes None, so a plain citizen-level refresh never wipes MU data.
        ``is_active`` / ``is_banned`` are always overwritten (not preserved)
        — unlike the other optional fields, every caller has a fresh value
        straight from ``getUserLite`` for the citizen it's writing, so
        there's nothing to fall back to.
        """
        await self._conn.execute(
            "INSERT INTO citizen_levels"
            "(user_id, country_id, level, skill_mode, last_skills_reset_at, "
            "citizen_name, last_login_at, mu_id, mu_name, updated_at, avatar_url, "
            "is_active, is_banned)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            "  country_id           = excluded.country_id,"
            "  level                = excluded.level,"
            "  skill_mode           = COALESCE(excluded.skill_mode, citizen_levels.skill_mode),"
            "  last_skills_reset_at = COALESCE(excluded.last_skills_reset_at, citizen_levels.last_skills_reset_at),"
            "  citizen_name         = COALESCE(excluded.citizen_name, citizen_levels.citizen_name),"
            "  last_login_at        = COALESCE(excluded.last_login_at, citizen_levels.last_login_at),"
            "  mu_id      = COALESCE(excluded.mu_id,      citizen_levels.mu_id),"
            "  mu_name    = COALESCE(excluded.mu_name,    citizen_levels.mu_name),"
            "  avatar_url = COALESCE(excluded.avatar_url, citizen_levels.avatar_url),"
            "  is_active  = excluded.is_active,"
            "  is_banned  = excluded.is_banned,"
            "  updated_at = excluded.updated_at",
            (
                user_id,
                country_id,
                level,
                skill_mode,
                last_skills_reset_at,
                citizen_name,
                last_login_at,
                mu_id,
                mu_name,
                updated_at,
                avatar_url,
                1 if is_active else 0,
                1 if is_banned else 0,
            ),
        )

    async def update_citizen_mu(
        self,
        user_id: str,
        mu_id: Optional[str],
        mu_name: Optional[str],
        country_id: Optional[str] = None,
    ) -> None:
        """Update a citizen's military unit information.

        If *country_id* is supplied and the citizen is not yet in citizen_levels,
        a stub row is inserted so MU members who joined after the last country
        refresh are still counted.
        """
        if country_id:
            now_iso = datetime.now(timezone.utc).isoformat()
            await self._conn.execute(
                "INSERT INTO citizen_levels (user_id, country_id, mu_id, mu_name, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "mu_id = excluded.mu_id, "
                "mu_name = excluded.mu_name, "
                "updated_at = excluded.updated_at",
                (user_id, country_id, mu_id, mu_name, now_iso),
            )
        else:
            await self._conn.execute(
                "UPDATE citizen_levels SET mu_id = ?, mu_name = ? WHERE user_id = ?",
                (mu_id, mu_name, user_id),
            )

    async def clear_all_citizen_mus(self) -> None:
        """Clear mu_id and mu_name for every row in citizen_levels.

        Called before a full MU membership sweep so that citizens who have
        left all known MUs end up with NULL rather than stale data.
        """
        await self._conn.execute(
            "UPDATE citizen_levels SET mu_id = NULL, mu_name = NULL"
        )
        await self._conn.commit()

    async def clear_citizen_mus_for_country(self, country_id: str) -> None:
        """Clear military unit information for all citizens in a specific country."""
        await self._conn.execute(
            "UPDATE citizen_levels SET mu_id = NULL, mu_name = NULL WHERE country_id = ?",
            (country_id,),
        )
        await self._conn.commit()

    async def clear_citizen_mus_for_known_mu_ids(
        self, country_id: str, mu_ids: list[str]
    ) -> None:
        """Clear MU info only for citizens in *country_id* whose current mu_id is in *mu_ids*.

        This is a targeted alternative to clear_citizen_mus_for_country: it leaves
        citizens in foreign MUs untouched so their MU assignment (set by the citizen
        refresh from their profile) is preserved across poller runs.
        """
        if not mu_ids:
            return
        placeholders = ",".join("?" * len(mu_ids))
        await self._conn.execute(
            f"UPDATE citizen_levels SET mu_id = NULL, mu_name = NULL "
            f"WHERE country_id = ? AND mu_id IN ({placeholders})",
            (country_id, *mu_ids),
        )
        await self._conn.commit()

    async def flush_citizen_levels(self) -> None:
        """Commit any pending changes to the citizen_levels table."""
        await self._conn.commit()

    async def search_citizen_names(
        self, query: str, limit: int = 25
    ) -> list[tuple[str, str]]:
        """Return (citizen_name, user_id) pairs where citizen_name matches query.

        Case-insensitive prefix + substring match, ordered by name.
        """
        q = query.strip()
        if not q:
            # Return a broad sample when no query typed yet
            rows: list[tuple[str, str]] = []
            async with self._conn.execute(
                "SELECT citizen_name, user_id FROM citizen_levels"
                " WHERE citizen_name IS NOT NULL"
                " ORDER BY citizen_name LIMIT ?",
                (limit,),
            ) as cur:
                async for row in cur:
                    rows.append((row[0], row[1]))
            return rows
        pattern = f"%{q}%"
        rows = []
        async with self._conn.execute(
            "SELECT citizen_name, user_id FROM citizen_levels"
            " WHERE citizen_name IS NOT NULL"
            "   AND citizen_name LIKE ? ESCAPE '\\'"
            " ORDER BY"
            "   CASE WHEN lower(citizen_name) = lower(?) THEN 0"
            "        WHEN lower(citizen_name) LIKE lower(?) || '%' THEN 1"
            "        ELSE 2 END,"
            "   citizen_name"
            " LIMIT ?",
            (pattern, q, q, limit),
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1]))
        return rows

    async def delete_citizens_for_country(self, country_id: str) -> None:
        """Delete all citizen level records for a specific country."""
        await self._conn.execute(
            "DELETE FROM citizen_levels WHERE country_id = ?", (country_id,)
        )
        await self._conn.commit()

    async def prune_stale_citizens(self, country_id: str, updated_at: str) -> int:
        """Delete citizens for *country_id* whose updated_at is older than *updated_at*.

        Used after a full country refresh so citizens who left the country are
        removed while preserving MU data that was written by a concurrent task.

        Only prunes ``is_active = 1`` rows. The per-country refresh this
        follows (``user.getUsersByCountry``) never lists inactive players in
        the first place — see ``services/citizen_cache.py`` — so a row that
        was backfilled by ID and marked inactive would otherwise look
        "not seen this sweep" and get deleted every single hour, only to be
        rediscovered as "missing" and re-fetched by
        ``full_fetcher.fetch_missing_owner_citizenships`` right after. That
        churn was confirmed live: the exact same ~5,800 owners were reported
        "missing" on every sweep. Excluding inactive rows from this delete
        makes their ``is_active`` flag stick, which is what lets
        ``fetch_company_census`` check owner activity at census time instead
        of only after that hour's backfill has already run.
        Returns the number of rows deleted.
        """
        cursor = await self._conn.execute(
            "DELETE FROM citizen_levels WHERE country_id = ? AND updated_at < ? "
            "AND is_active = 1",
            (country_id, updated_at),
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    async def get_level_distribution(
        self, country_id: Optional[str]
    ) -> tuple[dict[int, int], dict[int, int], Optional[str]]:
        """Return (level_counts, active_counts, last_updated_at).

        active_counts contains only citizens whose last_login_at is within
        the last 24 hours.
        """
        from datetime import datetime, timedelta, timezone

        counts: dict[int, int] = {}
        active_counts: dict[int, int] = {}
        last_updated: Optional[str] = None
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        if country_id:
            sql = "SELECT level, updated_at, last_login_at FROM citizen_levels WHERE country_id = ?"
            params: tuple = (country_id,)
        else:
            sql = "SELECT level, updated_at, last_login_at FROM citizen_levels"
            params = ()
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                lvl, updated_at, last_login_at = row
                if lvl is not None:
                    lvl = int(lvl)
                    counts[lvl] = counts.get(lvl, 0) + 1
                    if last_login_at and last_login_at >= cutoff:
                        active_counts[lvl] = active_counts.get(lvl, 0) + 1
                if last_updated is None or updated_at > last_updated:
                    last_updated = updated_at
        return counts, active_counts, last_updated

    async def get_skill_mode_distribution(
        self, country_id: Optional[str]
    ) -> tuple[int, int, int, Optional[str]]:
        """Return (eco_count, war_count, unknown_count, last_updated)."""
        eco = war = unknown = 0
        last_updated: Optional[str] = None
        if country_id:
            sql = (
                "SELECT skill_mode, updated_at FROM citizen_levels WHERE country_id = ?"
            )
            params: tuple = (country_id,)
        else:
            sql = "SELECT skill_mode, updated_at FROM citizen_levels"
            params = ()
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                mode, upd = row
                if mode == "eco":
                    eco += 1
                elif mode == "war":
                    war += 1
                else:
                    unknown += 1
                if last_updated is None or (upd and upd > last_updated):
                    last_updated = upd
        return eco, war, unknown, last_updated

    async def get_skill_mode_by_level_buckets(
        self, country_id: Optional[str]
    ) -> tuple[dict[int, dict[str, int]], Optional[str]]:
        """Eco/war/unknown counts per 5-level bucket."""
        if country_id:
            sql = "SELECT level, skill_mode, updated_at FROM citizen_levels WHERE country_id = ?"
            params: tuple = (country_id,)
        else:
            sql = "SELECT level, skill_mode, updated_at FROM citizen_levels"
            params = ()
        buckets: dict[int, dict[str, int]] = {}
        last_updated: Optional[str] = None
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                level, mode, upd = row
                bucket = ((int(level or 1) - 1) // 5) * 5 + 1
                if bucket not in buckets:
                    buckets[bucket] = {"eco": 0, "war": 0, "unknown": 0}
                if mode == "eco":
                    buckets[bucket]["eco"] += 1
                elif mode == "war":
                    buckets[bucket]["war"] += 1
                else:
                    buckets[bucket]["unknown"] += 1
                if last_updated is None or (upd and upd > last_updated):
                    last_updated = upd
        return buckets, last_updated

    async def get_skill_mode_by_mu(self, country_id: Optional[str]) -> dict[str, dict]:
        """Eco/war/unknown counts + player list grouped by MU name."""
        if country_id:
            sql = (
                "SELECT mu_name, citizen_name, level, skill_mode "
                "FROM citizen_levels WHERE country_id = ? ORDER BY mu_name, level DESC"
            )
            params: tuple = (country_id,)
        else:
            sql = (
                "SELECT mu_name, citizen_name, level, skill_mode "
                "FROM citizen_levels ORDER BY mu_name, level DESC"
            )
            params = ()
        mus: dict[str, dict] = {}
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                mu, name, level, mode = row
                key = mu or ""
                if key not in mus:
                    mus[key] = {"eco": 0, "war": 0, "unknown": 0, "players": []}
                if mode == "eco":
                    mus[key]["eco"] += 1
                elif mode == "war":
                    mus[key]["war"] += 1
                else:
                    mus[key]["unknown"] += 1
                mus[key]["players"].append(
                    {
                        "citizen_name": name or "?",
                        "level": level,
                        "skill_mode": mode,
                    }
                )
        return mus

    async def get_citizen_cooldowns_by_mu(
        self, country_id: Optional[str]
    ) -> dict[str, dict]:
        """Skill-reset cooldown stats + player list grouped by MU name."""
        now = datetime.now(timezone.utc)
        if country_id:
            sql = (
                "SELECT mu_name, citizen_name, level, last_skills_reset_at "
                "FROM citizen_levels WHERE country_id = ? ORDER BY mu_name, level DESC"
            )
            params: tuple = (country_id,)
        else:
            sql = (
                "SELECT mu_name, citizen_name, level, last_skills_reset_at "
                "FROM citizen_levels ORDER BY mu_name, level DESC"
            )
            params = ()
        mus: dict[str, dict] = {}
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                mu, name, level, reset_at = row
                key = mu or ""
                if key not in mus:
                    mus[key] = {
                        "count": 0,
                        "sum_days": 0.0,
                        "available": 0,
                        "no_data": 0,
                        "players": [],
                    }
                b = mus[key]
                days_ago: Optional[float] = None
                can_reset = True
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        can_reset = days_ago >= 7
                        b["count"] += 1
                        b["sum_days"] += days_ago
                        if can_reset:
                            b["available"] += 1
                    except Exception:
                        b["no_data"] += 1
                        b["available"] += 1
                else:
                    b["no_data"] += 1
                    b["available"] += 1
                b["players"].append(
                    {
                        "citizen_name": name or "?",
                        "level": level,
                        "days_ago": days_ago,
                        "can_reset": can_reset,
                    }
                )
        return mus

    async def get_skill_reset_cooldown_by_level_buckets(
        self, country_id: Optional[str]
    ) -> tuple[dict[int, dict], Optional[str]]:
        """Skill-reset cooldown per 5-level bucket (eco/unknown only)."""
        if country_id:
            sql = (
                "SELECT level, last_skills_reset_at, updated_at FROM citizen_levels "
                "WHERE country_id = ? AND (skill_mode IS NULL OR skill_mode != 'war')"
            )
            params: tuple = (country_id,)
        else:
            sql = (
                "SELECT level, last_skills_reset_at, updated_at FROM citizen_levels "
                "WHERE skill_mode IS NULL OR skill_mode != 'war'"
            )
            params = ()
        now = datetime.now(timezone.utc)
        buckets: dict[int, dict] = {}
        last_updated: Optional[str] = None
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                level, reset_at, upd = row
                bucket = ((int(level or 1) - 1) // 5) * 5 + 1
                if bucket not in buckets:
                    buckets[bucket] = {
                        "count": 0,
                        "sum_days": 0.0,
                        "available": 0,
                        "no_data": 0,
                    }
                b = buckets[bucket]
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        b["count"] += 1
                        b["sum_days"] += days_ago
                        if days_ago >= 7:
                            b["available"] += 1
                    except Exception:
                        b["no_data"] += 1
                        b["available"] += 1
                else:
                    b["no_data"] += 1
                    b["available"] += 1
                if last_updated is None or (upd and upd > last_updated):
                    last_updated = upd
        result: dict[int, dict] = {}
        for bkt, b in buckets.items():
            result[bkt] = {
                "count": b["count"],
                "avg_days_ago": b["sum_days"] / b["count"] if b["count"] else 0.0,
                "available": b["available"],
                "no_data": b["no_data"],
            }
        return result, last_updated

    async def get_citizens_cooldown_list(
        self, country_id: str, limit: int = 50
    ) -> list[dict]:
        """Citizens for a country sorted by level DESC with cooldown data."""
        now = datetime.now(timezone.utc)
        sql = """
            SELECT user_id, citizen_name, level, last_skills_reset_at
            FROM citizen_levels
            WHERE country_id = ?
            ORDER BY level DESC, user_id
            LIMIT ?
        """
        rows: list[dict] = []
        async with self._conn.execute(sql, (country_id, limit)) as cur:
            async for row in cur:
                uid, name, level, reset_at = row
                days_ago: Optional[float] = None
                can_reset = True
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        can_reset = days_ago >= 7
                    except Exception:
                        pass
                rows.append(
                    {
                        "user_id": uid,
                        "citizen_name": name or uid,
                        "level": level,
                        "last_skills_reset_at": reset_at,
                        "days_ago": days_ago,
                        "can_reset": can_reset,
                    }
                )
        return rows

    async def find_citizen_cooldown(self, query: str) -> list[dict]:
        """Search citizens by name (partial) or exact user_id — cooldown data."""
        now = datetime.now(timezone.utc)
        sql = """
            SELECT user_id, citizen_name, level, country_id, last_skills_reset_at
            FROM citizen_levels
            WHERE user_id = ? OR lower(citizen_name) LIKE lower(?)
            ORDER BY level DESC
            LIMIT 10
        """
        rows: list[dict] = []
        async with self._conn.execute(sql, (query, f"%{query}%")) as cur:
            async for row in cur:
                uid, name, level, country_id, reset_at = row
                days_ago: Optional[float] = None
                can_reset = True
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        can_reset = days_ago >= 7
                    except Exception:
                        pass
                rows.append(
                    {
                        "user_id": uid,
                        "citizen_name": name or uid,
                        "level": level,
                        "country_id": country_id,
                        "last_skills_reset_at": reset_at,
                        "days_ago": days_ago,
                        "can_reset": can_reset,
                    }
                )
        return rows

    async def find_citizen_readiness(self, query: str) -> list[dict]:
        """Search citizens by name/id — skill mode + cooldown data."""
        now = datetime.now(timezone.utc)
        sql = """
            SELECT user_id, citizen_name, level, country_id, skill_mode, last_skills_reset_at
            FROM citizen_levels
            WHERE user_id = ? OR lower(citizen_name) LIKE lower(?)
            ORDER BY level DESC
            LIMIT 10
        """
        rows: list[dict] = []
        async with self._conn.execute(sql, (query, f"%{query}%")) as cur:
            async for row in cur:
                uid, name, level, country_id, mode, reset_at = row
                days_ago: Optional[float] = None
                can_reset = True
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        can_reset = days_ago >= 7
                    except Exception:
                        pass
                rows.append(
                    {
                        "user_id": uid,
                        "citizen_name": name or uid,
                        "level": level,
                        "country_id": country_id,
                        "skill_mode": mode,
                        "last_skills_reset_at": reset_at,
                        "days_ago": days_ago,
                        "can_reset": can_reset,
                    }
                )
        return rows

    async def get_mu_readiness_players(
        self, mu_query: str, country_id: Optional[str] = None
    ) -> tuple[Optional[str], list[dict]]:
        """Return (matched_mu_name, players) for the best-matching MU."""
        now = datetime.now(timezone.utc)
        if country_id:
            sql_mu = (
                "SELECT DISTINCT mu_name FROM citizen_levels "
                "WHERE country_id = ? AND lower(mu_name) LIKE lower(?) AND mu_name IS NOT NULL"
            )
            params_mu: tuple = (country_id, f"%{mu_query}%")
        else:
            sql_mu = (
                "SELECT DISTINCT mu_name FROM citizen_levels "
                "WHERE lower(mu_name) LIKE lower(?) AND mu_name IS NOT NULL"
            )
            params_mu = (f"%{mu_query}%",)
        mu_names: list[str] = []
        async with self._conn.execute(sql_mu, params_mu) as cur:
            async for row in cur:
                if row[0]:
                    mu_names.append(row[0])
        if not mu_names:
            return None, []
        exact = next((m for m in mu_names if m.lower() == mu_query.lower()), None)
        mu_name = exact or mu_names[0]
        if country_id:
            sql = (
                "SELECT citizen_name, level, skill_mode, last_skills_reset_at "
                "FROM citizen_levels WHERE mu_name = ? AND country_id = ? ORDER BY level DESC"
            )
            params2: tuple = (mu_name, country_id)
        else:
            sql = (
                "SELECT citizen_name, level, skill_mode, last_skills_reset_at "
                "FROM citizen_levels WHERE mu_name = ? ORDER BY level DESC"
            )
            params2 = (mu_name,)
        players: list[dict] = []
        async with self._conn.execute(sql, params2) as cur:
            async for row in cur:
                name, level, mode, reset_at = row
                days_ago: Optional[float] = None
                can_reset = True
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        can_reset = days_ago >= 7
                    except Exception:
                        pass
                players.append(
                    {
                        "citizen_name": name or "?",
                        "level": level,
                        "skill_mode": mode,
                        "days_ago": days_ago,
                        "can_reset": can_reset,
                    }
                )
        return mu_name, players

    async def get_distinct_mu_names(
        self, country_id: Optional[str] = None
    ) -> list[str]:
        """All distinct non-null MU names, optionally filtered by country."""
        if country_id:
            sql = (
                "SELECT DISTINCT mu_name FROM citizen_levels "
                "WHERE country_id = ? AND mu_name IS NOT NULL ORDER BY mu_name"
            )
            params: tuple = (country_id,)
        else:
            sql = "SELECT DISTINCT mu_name FROM citizen_levels WHERE mu_name IS NOT NULL ORDER BY mu_name"
            params = ()
        names: list[str] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                if row[0]:
                    names.append(row[0])
        return names

    async def get_all_mu_readiness(
        self, country_id: Optional[str] = None, use_membership: bool = True
    ) -> dict[str, dict]:
        """Readiness stats for every distinct MU, keyed by mu_name.

        Each value: war, total, can_reset, waiting_days, war_15, war_20

        By default, uses citizen_mu_membership (authoritative, populated by the
        war guild's MU sweep) joined with citizen_levels for skill/level data,
        falling back to citizen_levels.mu_id/mu_name (populated by /peil mus on
        any bot) for citizens whose MU membership isn't present in
        citizen_mu_membership — e.g. on bots where the war-guild sweep never
        runs.

        If *use_membership* is False, citizen_mu_membership is ignored entirely
        and only citizen_levels.mu_id/mu_name is used. This gives identical
        results across bots regardless of whether the war-guild sweep runs,
        since citizen_levels.mu_id is refreshed the same way (via /peil mus and
        the hourly division MU refresh) on every bot.
        """
        now = datetime.now(timezone.utc)
        if use_membership:
            base_sql = (
                "SELECT mu_name, skill_mode, last_skills_reset_at, level, country_id FROM ("
                "  SELECT cmu.mu_name AS mu_name, cl.skill_mode, cl.last_skills_reset_at, "
                "         cl.level, cl.country_id "
                "  FROM citizen_mu_membership cmu "
                "  JOIN citizen_levels cl ON cl.user_id = cmu.in_game_user_id "
                "  UNION ALL "
                "  SELECT cl.mu_name AS mu_name, cl.skill_mode, cl.last_skills_reset_at, "
                "         cl.level, cl.country_id "
                "  FROM citizen_levels cl "
                "  WHERE cl.mu_id IS NOT NULL AND cl.mu_name IS NOT NULL "
                "    AND cl.user_id NOT IN (SELECT in_game_user_id FROM citizen_mu_membership)"
                ")"
            )
            if country_id:
                sql = base_sql + " WHERE country_id = ?"
                params: tuple = (country_id,)
            else:
                sql = base_sql
                params = ()
        else:
            base_sql = (
                "SELECT mu_name, skill_mode, last_skills_reset_at, level, country_id "
                "FROM citizen_levels "
                "WHERE mu_id IS NOT NULL AND mu_name IS NOT NULL"
            )
            if country_id:
                sql = base_sql + " AND country_id = ?"
                params = (country_id,)
            else:
                sql = base_sql
                params = ()
        mus: dict[str, dict] = {}
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                mu_name, mode, reset_at, level, _country_id = row
                level_int = int(level) if level is not None else 0
                if mu_name not in mus:
                    mus[mu_name] = {
                        "war": 0,
                        "total": 0,
                        "can_reset": 0,
                        "waiting_days": [],
                        "war_15": 0,
                        "war_20": 0,
                    }
                m = mus[mu_name]
                m["total"] += 1
                if mode == "war":
                    m["war"] += 1
                    if level_int >= 15:
                        m["war_15"] += 1
                    if level_int >= 20:
                        m["war_20"] += 1
                else:
                    can_reset = True
                    if reset_at:
                        try:
                            ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                            days_ago = (now - ts).total_seconds() / 86400
                            can_reset = days_ago >= 7
                            if not can_reset:
                                m["waiting_days"].append(days_ago)
                        except Exception:
                            pass
                    if can_reset:
                        m["can_reset"] += 1
        return mus

    async def fuzzy_citizen_by_name(
        self,
        query: str,
        country_id: Optional[str] = None,
        cutoff: float = 0.55,
    ) -> Optional[tuple[str, str]]:
        """Return (user_id, citizen_name) for the closest fuzzy match to *query*.

        Queries citizen_levels for all known names and picks the best match using
        difflib SequenceMatcher.  Returns None when nothing exceeds *cutoff*.
        """
        if country_id:
            sql = (
                "SELECT user_id, citizen_name FROM citizen_levels "
                "WHERE country_id = ? AND citizen_name IS NOT NULL"
            )
            params: tuple = (country_id,)
        else:
            sql = "SELECT user_id, citizen_name FROM citizen_levels WHERE citizen_name IS NOT NULL"
            params = ()
        rows: list[tuple[str, str]] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                rows.append((row[0], row[1]))
        if not rows:
            return None
        q_low = query.lower().strip()
        best: Optional[tuple[str, str]] = None
        best_ratio = cutoff
        for uid, name in rows:
            ratio = difflib.SequenceMatcher(None, q_low, name.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = (uid, name)
        return best
    
    # ── Eco donations ──────────────────────────────────────────────────

    async def get_citizen_by_name_exact(
        self, name: str
    ) -> Optional[tuple[str, str]]:
        """Return (user_id, citizen_name) for a case-insensitive exact name match."""
        sql = (
            "SELECT user_id, citizen_name FROM citizen_levels "
            "WHERE lower(citizen_name) = lower(?) LIMIT 1"
        )
        async with self._conn.execute(sql, (name,)) as cur:
            row = await cur.fetchone()
            if row and row[0]:
                return (row[0], row[1])
        return None

    async def get_citizen_name_by_id(self, user_id: str) -> Optional[str]:
        """Return the citizen name for a given user_id, or None if not found."""
        sql = "SELECT citizen_name FROM citizen_levels WHERE user_id = ?"
        async with self._conn.execute(sql, (user_id,)) as cur:
            row = await cur.fetchone()
            if row and row[0]:
                return row[0]
        return None

    async def get_citizen_name_mu_map(
        self, country_id: str
    ) -> dict[str, tuple[str | None, str | None]]:
        """Return {user_id: (citizen_name, mu_name)} for all citizens in *country_id*.

        Used by eco_donations overview to resolve player names and MU names in bulk
        without individual per-player queries.
        """
        result: dict[str, tuple[str | None, str | None]] = {}
        async with self._conn.execute(
            "SELECT user_id, citizen_name, mu_name FROM citizen_levels WHERE country_id = ?",
            (country_id,),
        ) as cur:
            async for row in cur:
                result[str(row[0])] = (row[1], row[2])
        return result

    async def get_citizen_mus(self) -> list[tuple[str, str]]:
        """Return [(user_id, mu_id)] for all citizens with a non-null mu_id."""
        rows: list[tuple[str, str]] = []
        async with self._conn.execute(
            "SELECT user_id, mu_id FROM citizen_levels WHERE mu_id IS NOT NULL"
        ) as cur:
            async for row in cur:
                rows.append((str(row[0]), str(row[1])))
        return rows

    async def get_citizen_mu_names_for_users(
        self, user_ids: list[str]
    ) -> dict[str, str]:
        """Return {user_id: mu_name} for the given user_ids (skips nulls).

        Used as a per-citizen fallback when citizen_mu_membership is incomplete.
        """
        if not user_ids:
            return {}
        placeholders = ",".join("?" * len(user_ids))
        result: dict[str, str] = {}
        async with self._conn.execute(
            f"SELECT user_id, mu_name FROM citizen_levels "
            f"WHERE user_id IN ({placeholders}) AND mu_name IS NOT NULL",
            user_ids,
        ) as cur:
            async for row in cur:
                result[str(row[0])] = str(row[1])
        return result

    async def get_citizen_mu_ids_for_users(
        self, user_ids: list[str]
    ) -> dict[str, str]:
        """Return {user_id: mu_id} for the given user_ids (skips nulls).

        ID-based sibling of get_citizen_mu_names_for_users — division
        matching should key off mu_id, not mu_name, since a MU's name can
        change in-game (renamed) while its ID never does. Matching by name
        went stale silently after such a rename (confirmed: "De Munterij"
        renamed to "De Pomperij" broke every permission tied to the old
        name).
        """
        if not user_ids:
            return {}
        placeholders = ",".join("?" * len(user_ids))
        result: dict[str, str] = {}
        async with self._conn.execute(
            f"SELECT user_id, mu_id FROM citizen_levels "
            f"WHERE user_id IN ({placeholders}) AND mu_id IS NOT NULL",
            user_ids,
        ) as cur:
            async for row in cur:
                result[str(row[0])] = str(row[1])
        return result

    async def get_nl_citizens_mu_info(
        self, country_id: str
    ) -> list[tuple[str, str | None]]:
        """Return [(user_id, mu_id_or_None)] for all citizens in *country_id*.

        Citizens with no MU have mu_id=None.  Used to determine which Discord
        members should have a MU role and which should have none.
        """
        rows: list[tuple[str, str | None]] = []
        async with self._conn.execute(
            "SELECT user_id, mu_id FROM citizen_levels WHERE country_id = ?",
            (country_id,),
        ) as cur:
            async for row in cur:
                rows.append((str(row[0]), str(row[1]) if row[1] else None))
        return rows

    async def get_active_nl_citizens(
        self, country_id: str, min_level: int = 20, max_hours_inactive: int = 72
    ) -> list[tuple[str, str, int]]:
        """Return [(user_id, citizen_name, level)] for all active NL citizens (no MU filter)."""
        rows: list[tuple[str, str, int]] = []
        async with self._conn.execute(
            """
            SELECT user_id, COALESCE(citizen_name, user_id), level
            FROM citizen_levels
            WHERE country_id = ?
              AND level >= ?
              AND last_login_at IS NOT NULL
              AND datetime(last_login_at) >= datetime('now', ? || ' hours')
            ORDER BY level DESC
            """,
            (country_id, min_level, f"-{max_hours_inactive}"),
        ) as cur:
            async for row in cur:
                rows.append((str(row[0]), str(row[1]), int(row[2] or 0)))
        return rows

    async def get_active_nl_citizens_without_mu(
        self, country_id: str, min_level: int = 20, max_hours_inactive: int = 72
    ) -> list[tuple[str, str, int]]:
        """Return [(user_id, citizen_name, level)] for NL citizens who:
        - are at least *min_level*
        - logged in within the last *max_hours_inactive* hours
        - have no MU (mu_id IS NULL or empty)
        """
        rows: list[tuple[str, str, int]] = []
        async with self._conn.execute(
            """
            SELECT user_id, COALESCE(citizen_name, user_id), level
            FROM citizen_levels
            WHERE country_id = ?
              AND level >= ?
              AND (mu_id IS NULL OR mu_id = '')
              AND last_login_at IS NOT NULL
              AND datetime(last_login_at) >= datetime('now', ? || ' hours')
            ORDER BY level DESC
            """,
            (country_id, min_level, f"-{max_hours_inactive}"),
        ) as cur:
            async for row in cur:
                rows.append((str(row[0]), str(row[1]), int(row[2] or 0)))
        return rows

    async def get_distinct_mu_ids_for_country(
        self, country_id: str
    ) -> list[tuple[str, str]]:
        """Return [(mu_id, mu_name)] for distinct non-null MUs of citizens in *country_id*."""
        rows: list[tuple[str, str]] = []
        async with self._conn.execute(
            "SELECT DISTINCT mu_id, mu_name FROM citizen_levels "
            "WHERE country_id = ? AND mu_id IS NOT NULL",
            (country_id,),
        ) as cur:
            async for row in cur:
                rows.append((str(row[0]), str(row[1] or "")))
        return rows

    async def get_citizen_names_by_ids(
        self, user_ids: list[str]
    ) -> dict[str, str]:
        """Return {user_id: citizen_name} for each user_id found in citizen_levels."""
        if not user_ids:
            return {}
        placeholders = ",".join("?" for _ in user_ids)
        result: dict[str, str] = {}
        async with self._conn.execute(
            f"SELECT user_id, citizen_name FROM citizen_levels"
            f" WHERE user_id IN ({placeholders}) AND citizen_name IS NOT NULL",
            user_ids,
        ) as cur:
            async for row in cur:
                result[str(row[0])] = str(row[1])
        return result

    async def get_citizen_details_by_ids(
        self, user_ids: list[str]
    ) -> dict[str, dict]:
        """Return {user_id: {country_id, citizen_name, last_login_at}} for each id."""
        if not user_ids:
            return {}
        placeholders = ",".join("?" for _ in user_ids)
        result: dict[str, dict] = {}
        async with self._conn.execute(
            f"SELECT user_id, country_id, citizen_name, last_login_at"
            f" FROM citizen_levels WHERE user_id IN ({placeholders})",
            user_ids,
        ) as cur:
            async for row in cur:
                result[str(row[0])] = {
                    "country_id": row[1],
                    "citizen_name": row[2],
                    "last_login_at": row[3],
                }
        return result

    # ── Weekly damage ──────────────────────────────────────────────────

    async def get_nl_citizen_ids(self, country_id: str) -> list[tuple[str, str]]:
        """Return [(user_id, citizen_name)] for all citizens of *country_id*."""
        sql = (
            "SELECT user_id, COALESCE(citizen_name, user_id) "
            "FROM citizen_levels WHERE country_id = ?"
        )
        rows: list[tuple[str, str]] = []
        async with self._conn.execute(sql, (country_id,)) as cur:
            async for row in cur:
                rows.append((str(row[0]), str(row[1])))
        return rows

    async def get_citizens_in_mus(self, mu_names: list[str]) -> list[tuple[str, str, str]]:
        """Return [(user_id, citizen_name, country_id)] for citizens in any of the given MU names."""
        if not mu_names:
            return []
        placeholders = ",".join("?" for _ in mu_names)
        sql = (
            f"SELECT user_id, COALESCE(citizen_name, user_id), country_id "
            f"FROM citizen_levels WHERE mu_name IN ({placeholders})"
        )
        rows: list[tuple[str, str, str]] = []
        async with self._conn.execute(sql, tuple(mu_names)) as cur:
            async for row in cur:
                rows.append((str(row[0]), str(row[1]), str(row[2])))
        return rows

    async def get_citizen_countries_by_ids(
        self, user_ids: list[str]
    ) -> dict[str, tuple[str, str | None]]:
        """Return {in_game_user_id: (country_id, last_login_at)} for the given user IDs."""
        if not user_ids:
            return {}
        placeholders = ",".join("?" for _ in user_ids)
        sql = (
            f"SELECT user_id, country_id, last_login_at FROM citizen_levels "
            f"WHERE user_id IN ({placeholders})"
        )
        result: dict[str, tuple[str, str | None]] = {}
        async with self._conn.execute(sql, tuple(user_ids)) as cur:
            async for row in cur:
                result[str(row[0])] = (str(row[1]), row[2])
        return result

    async def get_citizen_countries_by_names(
        self, names: list[str]
    ) -> dict[str, tuple[str, str | None]]:
        """Return {lowercase_name: (country_id, last_login_at)} for the given citizen names.

        Case-insensitive lookup against citizen_levels.citizen_name.
        last_login_at is an ISO timestamp string or None if unknown.
        Names not found in the DB are absent from the result.
        """
        if not names:
            return {}
        lower_names = [n.lower() for n in names]
        placeholders = ",".join("?" for _ in lower_names)
        sql = (
            f"SELECT citizen_name, country_id, last_login_at FROM citizen_levels "
            f"WHERE lower(citizen_name) IN ({placeholders})"
        )
        result: dict[str, tuple[str, str | None]] = {}
        async with self._conn.execute(sql, tuple(lower_names)) as cur:
            async for row in cur:
                if row[0]:
                    result[row[0].lower()] = (str(row[1]), row[2])
        return result

    async def upsert_weekly_damage(
        self,
        user_id: str,
        citizen_name: Optional[str],
        country_id: str,
        weekly_damage: float,
        updated_at: str,
    ) -> None:
        """Insert or update a citizen's weekly damage record."""
        await self._conn.execute(
            "INSERT INTO citizen_weekly_damages"
            " (user_id, citizen_name, country_id, weekly_damage, updated_at)"
            " VALUES(?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            "  citizen_name  = COALESCE(excluded.citizen_name, citizen_weekly_damages.citizen_name),"
            "  country_id    = excluded.country_id,"
            "  weekly_damage = excluded.weekly_damage,"
            "  updated_at    = excluded.updated_at",
            (user_id, citizen_name, country_id, weekly_damage, updated_at),
        )

    async def flush_weekly_damages(self) -> None:
        """Commit pending weekly-damage changes."""
        await self._conn.commit()

    async def get_top_weekly_damages(
        self, country_id: str, limit: int = 10
    ) -> tuple[list[tuple[str, str, float]], Optional[str]]:
        """Return (rows, last_updated) for the top *limit* weekly-damage citizens.

        rows: [(user_id, citizen_name, weekly_damage)] sorted descending.
        last_updated: ISO timestamp of last refresh, or None.
        """
        sql = (
            "SELECT user_id, COALESCE(citizen_name, user_id), weekly_damage, updated_at "
            "FROM citizen_weekly_damages "
            "WHERE country_id = ? AND weekly_damage > 0 "
            "ORDER BY weekly_damage DESC LIMIT ?"
        )
        rows: list[tuple[str, str, float]] = []
        last_updated: Optional[str] = None
        async with self._conn.execute(sql, (country_id, limit)) as cur:
            async for row in cur:
                rows.append((str(row[0]), str(row[1]), float(row[2])))
                if last_updated is None:
                    last_updated = row[3]
        return rows, last_updated

    async def get_weekly_damage_for_player(
        self, country_id: str, query: str
    ) -> Optional[tuple[str, str, float, str]]:
        """Return (user_id, citizen_name, weekly_damage, updated_at) for one player.

        Matches by user_id or citizen_name (case-insensitive, partial).
        """
        sql = (
            "SELECT user_id, COALESCE(citizen_name, user_id), weekly_damage, updated_at "
            "FROM citizen_weekly_damages "
            "WHERE country_id = ? AND ("
            "  user_id = ? OR lower(citizen_name) = lower(?)"
            "  OR lower(citizen_name) LIKE lower(?) "
            ") ORDER BY weekly_damage DESC LIMIT 1"
        )
        async with self._conn.execute(
            sql, (country_id, query, query, f"%{query}%")
        ) as cur:
            row = await cur.fetchone()
        if row:
            return str(row[0]), str(row[1]), float(row[2]), str(row[3])
        return None

    async def get_player_nl_rank(self, country_id: str, user_id: str) -> Optional[int]:
        """Return the 1-based NL rank of *user_id* by weekly_damage, or None."""
        sql = (
            "SELECT COUNT(*) + 1 FROM citizen_weekly_damages "
            "WHERE country_id = ? AND weekly_damage > ("
            "  SELECT weekly_damage FROM citizen_weekly_damages "
            "  WHERE country_id = ? AND user_id = ?"
            ")"
        )
        async with self._conn.execute(sql, (country_id, country_id, user_id)) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else None
    async def get_weekly_damages_for_users(
        self, user_ids: list[str]
    ) -> dict[str, tuple[str | None, float]]:
        """Return {user_id: (citizen_name_or_None, weekly_damage)} for each id in *user_ids*.

        citizen_name is None when the DB row has no name stored.
        Users not found in the DB (or with 0 damage) are omitted from the result.
        """
        if not user_ids:
            return {}
        placeholders = ",".join("?" for _ in user_ids)
        sql = (
            "SELECT user_id, citizen_name, weekly_damage "
            f"FROM citizen_weekly_damages WHERE user_id IN ({placeholders})"
        )
        result: dict[str, tuple[str | None, float]] = {}
        async with self._conn.execute(sql, user_ids) as cur:
            async for row in cur:
                result[str(row[0])] = (str(row[1]) if row[1] else None, float(row[2]))
        return result

    async def get_levels_for_users(self, user_ids: list[str]) -> dict[str, int]:
        """Return {user_id: level} for each id found in citizen_levels."""
        if not user_ids:
            return {}
        placeholders = ",".join("?" for _ in user_ids)
        sql = (
            f"SELECT user_id, level FROM citizen_levels "
            f"WHERE user_id IN ({placeholders}) AND level IS NOT NULL"
        )
        result: dict[str, int] = {}
        async with self._conn.execute(sql, user_ids) as cur:
            async for row in cur:
                result[str(row[0])] = int(row[1])
        return result

    async def get_names_for_users(self, user_ids: list[str]) -> dict[str, str]:
        """Return {user_id: citizen_name} for each id found in citizen_levels."""
        if not user_ids:
            return {}
        placeholders = ",".join("?" for _ in user_ids)
        sql = (
            f"SELECT user_id, citizen_name FROM citizen_levels "
            f"WHERE user_id IN ({placeholders}) AND citizen_name IS NOT NULL"
        )
        result: dict[str, str] = {}
        async with self._conn.execute(sql, user_ids) as cur:
            async for row in cur:
                result[str(row[0])] = str(row[1])
        return result

    async def get_top_countries_by_population(self, limit: int) -> list[dict]:
        """Return the top countries ranked by active citizens (logged in within 72 h)."""
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT country_id, COUNT(*) AS pop "
            "FROM citizen_levels "
            "WHERE last_login_at >= datetime('now', '-72 hours') "
            "GROUP BY country_id ORDER BY pop DESC LIMIT ?",
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append({"country_id": row[0], "population": row[1]})
        return rows

    async def get_tracked_country_ids(self) -> list[str]:
        """Return all distinct country_ids that have citizens in citizen_levels."""
        ids: list[str] = []
        async with self._conn.execute(
            "SELECT DISTINCT country_id FROM citizen_levels"
        ) as cur:
            async for row in cur:
                ids.append(row[0])
        return ids

    async def get_country_ids_for_users(
        self, user_ids: list[str]
    ) -> dict[str, str]:
        """Return {user_id: country_id} for every user_id found in citizen_levels."""
        if not user_ids:
            return {}
        placeholders = ",".join("?" * len(user_ids))
        result: dict[str, str] = {}
        async with self._conn.execute(
            f"SELECT user_id, country_id FROM citizen_levels"
            f" WHERE user_id IN ({placeholders})",
            user_ids,
        ) as cur:
            async for row in cur:
                result[row[0]] = row[1]
        return result

    async def get_user_ids_for_mus(self, mu_names: list[str]) -> list[str]:
        """Return user_ids from citizen_levels for players in any of the given MUs."""
        if not mu_names:
            return []
        placeholders = ",".join("?" * len(mu_names))
        rows: list[str] = []
        async with self._conn.execute(
            f"SELECT user_id FROM citizen_levels WHERE mu_name IN ({placeholders})",
            tuple(mu_names),
        ) as cur:
            async for row in cur:
                rows.append(row[0])
        return rows

    async def get_user_ids_for_group(
        self,
        *,
        country_id: Optional[str] = None,
        mu_name: Optional[str] = None,
    ) -> list[str]:
        """Return user_ids from citizen_levels matching the given country/MU filter."""
        if mu_name:
            sql = "SELECT user_id FROM citizen_levels WHERE mu_name = ?"
            params: tuple = (mu_name,)
        elif country_id:
            sql = "SELECT user_id FROM citizen_levels WHERE country_id = ?"
            params = (country_id,)
        else:
            return []
        rows: list[str] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                rows.append(row[0])
        return rows

    async def citizen_exists_in_country(self, user_id: str, country_id: str) -> bool:
        """Return True if *user_id* appears in citizen_levels with *country_id*."""
        async with self._conn.execute(
            "SELECT 1 FROM citizen_levels WHERE user_id = ? AND country_id = ? LIMIT 1",
            (user_id, country_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_citizens_in_country(
        self, country_id: str
    ) -> list[tuple[str, str, Optional[str]]]:
        """Return [(user_id, country_id, citizen_name)] for all citizens in *country_id*."""
        rows: list[tuple[str, str, Optional[str]]] = []
        async with self._conn.execute(
            "SELECT user_id, country_id, citizen_name FROM citizen_levels WHERE country_id = ?",
            (country_id,),
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1], row[2]))
        return rows

    async def get_all_citizens_for_tips_scan(
        self,
    ) -> list[tuple[str, str, Optional[str]]]:
        """Return [(user_id, country_id, citizen_name)] for ALL citizens in citizen_levels."""
        rows: list[tuple[str, str, Optional[str]]] = []
        async with self._conn.execute(
            "SELECT user_id, country_id, citizen_name FROM citizen_levels"
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1], row[2]))
        return rows

    async def get_citizens_without_discord_link(
        self, country_id: str, min_level: int = 1
    ) -> list[tuple[str, str, int]]:
        """Return [(user_id, citizen_name, level)] for citizens in *country_id* at
        *min_level* or above that have no entry in the identity_links table."""
        rows: list[tuple[str, str, int]] = []
        async with self._conn.execute(
            """
            SELECT cl.user_id, cl.citizen_name, cl.level
            FROM citizen_levels cl
            WHERE cl.country_id = ?
              AND cl.level >= ?
              AND cl.user_id NOT IN (SELECT in_game_user_id FROM identity_links)
            ORDER BY cl.level DESC
            """,
            (country_id, min_level),
        ) as cur:
            async for row in cur:
                rows.append((str(row[0]), row[1] or "", int(row[2] or 0)))
        return rows

    async def get_inactive_citizens_in_country(
        self, country_id: str, min_days_inactive: int
    ) -> list[tuple[str, str, int, str | None]]:
        """Return [(user_id, citizen_name, days_inactive, discord_user_id_or_None)] for
        all citizens in *country_id* who are inactive.  discord_user_id is None when
        no identity link exists for that citizen."""
        rows: list[tuple[str, str, int, str | None]] = []
        async with self._conn.execute(
            """
            SELECT cl.user_id, cl.citizen_name,
                   CAST((julianday('now') - julianday(cl.last_login_at)) AS INTEGER) AS days_inactive,
                   il.discord_user_id
            FROM citizen_levels cl
            LEFT JOIN identity_links il ON il.in_game_user_id = cl.user_id
            WHERE cl.country_id = ?
              AND cl.last_login_at IS NOT NULL
              AND julianday('now') - julianday(cl.last_login_at) > ?
            ORDER BY days_inactive DESC
            """,
            (country_id, min_days_inactive),
        ) as cur:
            async for row in cur:
                rows.append((str(row[0]), row[1] or "", int(row[2] or 0), str(row[3]) if row[3] else None))

    async def any_citizen_in_country(self, user_ids: list[str], country_id: str) -> bool:
        """Return True if ANY of *user_ids* appears in citizen_levels with *country_id*.

        Uses a single query with an IN clause; safe for large lists via chunking.
        Returns False for an empty list.
        """
        if not user_ids:
            return False
        # SQLite supports up to ~999 bound parameters; chunk defensively.
        chunk_size = 500
        for i in range(0, len(user_ids), chunk_size):
            chunk = user_ids[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            async with self._conn.execute(
                f"SELECT 1 FROM citizen_levels WHERE user_id IN ({placeholders})"
                " AND country_id = ? LIMIT 1",
                (*chunk, country_id),
            ) as cur:
                if await cur.fetchone() is not None:
                    return True
        return False

    async def get_citizen_mu_from_levels(self, user_id: str) -> tuple[str, str] | None:
        """Return (mu_id, mu_name) from citizen_levels for *user_id*, or None.

        Used as a fallback when citizen_mu_membership has no row for this user.
        """
        async with self._conn.execute(
            "SELECT mu_id, mu_name FROM citizen_levels"
            " WHERE user_id = ? AND mu_id IS NOT NULL AND mu_name IS NOT NULL",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return (str(row[0]), str(row[1])) if row else None