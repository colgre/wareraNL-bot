"""Citizen wealth DB mixin."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class WealthMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def upsert_citizen_wealth(
        self,
        user_id: str,
        country_id: str,
        citizen_name: Optional[str],
        wealth_active: float,
        wealth_inactive: float,
        updated_at: str,
        wealth_companies: float = 0.0,
        wealth_items: float = 0.0,
        wealth_money: float = 0.0,
        wealth_equipments: float = 0.0,
        wealth_weapons: float = 0.0,
    ) -> None:
        """Insert or update a citizen's wealth record.

        wealth_companies/items/money/equipments/weapons is the breakdown
        from user.getUserById's stats.wealth (see cogs/tasks/wealth.py) —
        defaults to 0 for any caller that only has the combined total.
        """
        wealth_total = wealth_active + wealth_inactive
        await self._conn.execute(
            "INSERT INTO citizen_wealth"
            " (user_id, country_id, citizen_name, wealth_active, wealth_inactive_companies, wealth_total,"
            "  wealth_companies, wealth_items, wealth_money, wealth_equipments, wealth_weapons, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            "   country_id                = excluded.country_id,"
            "   citizen_name              = COALESCE(excluded.citizen_name, citizen_wealth.citizen_name),"
            "   wealth_active             = excluded.wealth_active,"
            "   wealth_inactive_companies = excluded.wealth_inactive_companies,"
            "   wealth_total              = excluded.wealth_total,"
            "   wealth_companies          = excluded.wealth_companies,"
            "   wealth_items              = excluded.wealth_items,"
            "   wealth_money              = excluded.wealth_money,"
            "   wealth_equipments         = excluded.wealth_equipments,"
            "   wealth_weapons            = excluded.wealth_weapons,"
            "   updated_at                = excluded.updated_at",
            (
                user_id, country_id, citizen_name, wealth_active, wealth_inactive, wealth_total,
                wealth_companies, wealth_items, wealth_money, wealth_equipments, wealth_weapons, updated_at,
            ),
        )

    async def flush_citizen_wealth(self) -> None:
        """Commit pending wealth writes."""
        await self._conn.commit()

    async def get_wealth_ranking(self, country_id: str, limit: int = 10) -> list[dict]:
        """Return top `limit` citizens sorted by total wealth descending."""
        sql = (
            "SELECT user_id, citizen_name, wealth_active, wealth_inactive_companies,"
            " wealth_total, updated_at"
            " FROM citizen_wealth"
            " WHERE country_id = ?"
            " ORDER BY wealth_total DESC"
            " LIMIT ?"
        )
        rows: list[dict] = []
        async with self._conn.execute(sql, (country_id, limit)) as cur:
            async for row in cur:
                rows.append({
                    "user_id": row[0],
                    "citizen_name": row[1],
                    "wealth_active": row[2],
                    "wealth_inactive_companies": row[3],
                    "wealth_total": row[4],
                    "updated_at": row[5],
                })
        return rows

    async def search_citizen_wealth(
        self,
        name_query: str,
        country_id: str,
        limit: int = 10,
    ) -> list[dict]:
        """Search citizens by name (case-insensitive substring), sorted by total wealth."""
        sql = (
            "SELECT user_id, citizen_name, wealth_active, wealth_inactive_companies,"
            " wealth_total, updated_at"
            " FROM citizen_wealth"
            " WHERE country_id = ? AND LOWER(citizen_name) LIKE LOWER(?)"
            " ORDER BY wealth_total DESC"
            " LIMIT ?"
        )
        pattern = f"%{name_query}%"
        rows: list[dict] = []
        async with self._conn.execute(sql, (country_id, pattern, limit)) as cur:
            async for row in cur:
                rows.append({
                    "user_id": row[0],
                    "citizen_name": row[1],
                    "wealth_active": row[2],
                    "wealth_inactive_companies": row[3],
                    "wealth_total": row[4],
                    "updated_at": row[5],
                })
        return rows

    async def get_citizen_wealth_rank(self, user_id: str, country_id: str) -> Optional[int]:
        """Return the 1-based rank of a citizen by total wealth, or None if not found."""
        sql = (
            "SELECT rank FROM ("
            "  SELECT user_id, ROW_NUMBER() OVER (ORDER BY wealth_total DESC) AS rank"
            "  FROM citizen_wealth WHERE country_id = ?"
            ") WHERE user_id = ?"
        )
        async with self._conn.execute(sql, (country_id, user_id)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    # ── Daily wealth history ──────────────────────────────────────────────

    async def insert_wealth_snapshot(
        self,
        user_id: str,
        country_id: str,
        citizen_name: Optional[str],
        wealth_total: float,
        snapshot_date: str,
        wealth_companies: Optional[float] = None,
        wealth_items: Optional[float] = None,
        wealth_money: Optional[float] = None,
        wealth_equipments: Optional[float] = None,
        wealth_weapons: Optional[float] = None,
    ) -> None:
        """Insert (or replace) a daily wealth snapshot for a citizen.

        ``snapshot_date`` should be an ISO date string (YYYY-MM-DD, UTC).
        Calling this twice on the same day for the same user will update
        the value (REPLACE semantics via the composite primary key).

        wealth_companies/items/money/equipments/weapons is the breakdown
        from user.getUserById's stats.wealth — left NULL (not defaulted to
        0) when the caller doesn't have it, so a snapshot predating this
        breakdown can be told apart from a genuine zero.
        """
        await self._conn.execute(
            "INSERT OR REPLACE INTO citizen_wealth_history"
            " (user_id, country_id, citizen_name, wealth_total,"
            "  wealth_companies, wealth_items, wealth_money, wealth_equipments, wealth_weapons, snapshot_date)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, country_id, citizen_name, wealth_total,
                wealth_companies, wealth_items, wealth_money, wealth_equipments, wealth_weapons, snapshot_date,
            ),
        )

    async def flush_wealth_history(self) -> None:
        """Commit pending history writes."""
        await self._conn.commit()

    async def get_wealth_increase_ranking(
        self,
        country_id: str,
        days: int,
        limit: int = 10,
    ) -> list[dict]:
        """Return top `limit` citizens by wealth increase over the last `days` days.

        Only citizens who have a snapshot both today (most recent) and at least
        `days` days ago are included.  Sorted by increase descending.
        """
        sql = (
            "SELECT"
            "  recent.user_id,"
            "  COALESCE(recent.citizen_name, old.citizen_name) AS citizen_name,"
            "  recent.wealth_total AS wealth_now,"
            "  old.wealth_total    AS wealth_then,"
            "  (recent.wealth_total - old.wealth_total) AS increase"
            " FROM ("
            "  SELECT user_id, citizen_name, wealth_total, snapshot_date"
            "  FROM citizen_wealth_history"
            "  WHERE country_id = ?"
            "    AND snapshot_date = ("
            "      SELECT MAX(snapshot_date) FROM citizen_wealth_history"
            "      WHERE country_id = ?"
            "    )"
            " ) AS recent"
            " JOIN ("
            "  SELECT user_id, citizen_name, wealth_total, snapshot_date"
            "  FROM citizen_wealth_history"
            "  WHERE country_id = ?"
            "    AND snapshot_date <= DATE("
            "      (SELECT MAX(snapshot_date) FROM citizen_wealth_history WHERE country_id = ?),"
            "      ? || ' days'"
            "    )"
            "  GROUP BY user_id"
            "  HAVING snapshot_date = MAX(snapshot_date)"
            " ) AS old ON recent.user_id = old.user_id"
            " ORDER BY increase DESC"
            " LIMIT ?"
        )
        rows: list[dict] = []
        # The DATE modifier needs a negative offset to go backwards
        offset = f"-{days}"
        async with self._conn.execute(
            sql, (country_id, country_id, country_id, country_id, offset, limit)
        ) as cur:
            async for row in cur:
                rows.append({
                    "user_id": row[0],
                    "citizen_name": row[1],
                    "wealth_now": row[2],
                    "wealth_then": row[3],
                    "increase": row[4],
                })
        return rows

    async def get_wealth_history_oldest_date(self, country_id: str) -> Optional[str]:
        """Return the earliest snapshot_date available for ``country_id``, or None."""
        async with self._conn.execute(
            "SELECT MIN(snapshot_date) FROM citizen_wealth_history WHERE country_id = ?",
            (country_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None
