"""DB methods for division_mu_overrides — runtime edits to DIVISION_MU_IDS."""

from __future__ import annotations

import aiosqlite


class DivisionOverridesMixin:
    """division_mu_overrides table operations."""

    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def upsert_division_mu_override(
        self, mu_id: str, mu_name: str, division: int
    ) -> None:
        """Persist a runtime add/move/remove edit. division=0 means removed.

        Keyed on mu_id, not mu_name — a MU's name can change in-game while
        its ID never does. The table's primary key is still mu_name for
        historical reasons, so any stale row left over from this MU's old
        name is deleted first to avoid leaving an orphaned duplicate behind
        after a rename.
        """
        await self._conn.execute(
            "DELETE FROM division_mu_overrides WHERE mu_id = ?", (mu_id,)
        )
        await self._conn.execute(
            "INSERT INTO division_mu_overrides (mu_id, mu_name, division) VALUES (?, ?, ?) "
            "ON CONFLICT(mu_name) DO UPDATE SET mu_id = excluded.mu_id, division = excluded.division",
            (mu_id, mu_name, division),
        )
        await self._conn.commit()

    async def get_all_division_mu_overrides(self) -> list[tuple[str, str, int]]:
        """Return [(mu_id, mu_name, division)] for every stored override.

        mu_id is "" for legacy rows saved before this column existed —
        callers should resolve and backfill it via known_mus.
        """
        async with self._conn.execute(
            "SELECT mu_id, mu_name, division FROM division_mu_overrides"
        ) as cur:
            return [(row[0] or "", row[1], row[2]) async for row in cur]
