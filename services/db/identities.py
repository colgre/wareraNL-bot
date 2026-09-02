"""Discord ↔ in-game identity mapping DB methods (identity_links table)."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class IdentityLinksMixin:
    """identity_links table operations."""

    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def upsert_identity_link(
        self,
        discord_user_id: str,
        guild_id: str,
        in_game_user_id: str,
        nationality: str,
        request_type: str,
        approved_by_discord_id: str,
        approved_at: str,
        embassy_country: Optional[str] = None,
    ) -> None:
        """Insert or update a Discord/in-game identity mapping.

        Scoped to (discord_user_id, guild_id) — the same Discord user can
        hold independent links in different guilds this bot serves (e.g.
        production + war guild) without one overwriting the other.
        """
        await self._conn.execute(
            "INSERT INTO identity_links ("
            "discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
            "embassy_country, approved_by_discord_id, approved_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(discord_user_id, guild_id) DO UPDATE SET "
            "in_game_user_id=excluded.in_game_user_id, "
            "nationality=excluded.nationality, "
            "request_type=excluded.request_type, "
            "embassy_country=excluded.embassy_country, "
            "approved_by_discord_id=excluded.approved_by_discord_id, "
            "approved_at=excluded.approved_at, "
            "updated_at=excluded.updated_at",
            (
                discord_user_id,
                guild_id,
                in_game_user_id,
                nationality,
                request_type,
                embassy_country,
                approved_by_discord_id,
                approved_at,
                approved_at,
            ),
        )
        await self._conn.commit()

    async def get_identity_link_by_discord(
        self, discord_user_id: str, guild_id: Optional[str] = None
    ) -> Optional[dict]:
        """Return one identity mapping for a Discord user (optionally scoped to guild).

        Without guild_id, a user with links in more than one guild can now
        have more than one matching row (see the table's composite primary
        key) — this returns whichever was updated most recently, matching
        the "last write wins" behaviour every unscoped caller already
        depended on back when discord_user_id alone was the primary key.
        """
        if guild_id:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links WHERE discord_user_id = ? AND guild_id = ?"
            )
            params = (discord_user_id, guild_id)
        else:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links WHERE discord_user_id = ? "
                "ORDER BY updated_at DESC LIMIT 1"
            )
            params = (discord_user_id,)

        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "discord_user_id": row[0],
            "guild_id": row[1],
            "in_game_user_id": row[2],
            "nationality": row[3],
            "request_type": row[4],
            "embassy_country": row[5],
            "approved_by_discord_id": row[6],
            "approved_at": row[7],
            "updated_at": row[8],
        }

    async def get_identity_links_by_ingame(
        self, in_game_user_id: str, guild_id: Optional[str] = None
    ) -> list[dict]:
        """Return all Discord mappings for an in-game ID (optionally scoped to guild)."""
        if guild_id:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links WHERE in_game_user_id = ? AND guild_id = ? "
                "ORDER BY updated_at DESC"
            )
            params = (in_game_user_id, guild_id)
        else:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links WHERE in_game_user_id = ? "
                "ORDER BY updated_at DESC"
            )
            params = (in_game_user_id,)

        results: list[dict] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                results.append(
                    {
                        "discord_user_id": row[0],
                        "guild_id": row[1],
                        "in_game_user_id": row[2],
                        "nationality": row[3],
                        "request_type": row[4],
                        "embassy_country": row[5],
                        "approved_by_discord_id": row[6],
                        "approved_at": row[7],
                        "updated_at": row[8],
                    }
                )
        return results

    async def get_discord_ids_by_ingame_user_ids(
        self, guild_id: str, in_game_user_ids: list[str]
    ) -> dict[str, str]:
        """Return {in_game_user_id: discord_user_id} for IDs in one guild.

        If multiple Discord IDs exist for one in-game ID, the most recently
        updated mapping wins.
        """
        if not in_game_user_ids:
            return {}

        placeholders = ",".join("?" for _ in in_game_user_ids)
        sql = (
            "SELECT in_game_user_id, discord_user_id "
            "FROM identity_links "
            "WHERE guild_id = ? AND in_game_user_id IN ("
            f"{placeholders}"
            ") ORDER BY updated_at DESC"
        )
        params: tuple[str, ...] = (guild_id, *in_game_user_ids)

        results: dict[str, str] = {}
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                in_game_id = str(row[0])
                if in_game_id not in results:
                    results[in_game_id] = str(row[1])
        return results

    async def get_citizen_names_by_discord_ids(
        self, discord_ids: list[str | int]
    ) -> dict[str, str]:
        """Return ``{discord_user_id: citizen_name}`` for a batch of Discord IDs.

        Joins ``identity_links`` → ``citizen_levels``.  IDs that have no
        identity link or no matching ``citizen_levels`` row are omitted.
        """
        if not discord_ids:
            return {}
        str_ids = [str(d) for d in discord_ids]
        placeholders = ",".join("?" for _ in str_ids)
        sql = (
            "SELECT il.discord_user_id, cl.citizen_name "
            "FROM identity_links il "
            "JOIN citizen_levels cl ON cl.user_id = il.in_game_user_id "
            f"WHERE il.discord_user_id IN ({placeholders})"
            "  AND cl.citizen_name IS NOT NULL"
        )
        result: dict[str, str] = {}
        async with self._conn.execute(sql, tuple(str_ids)) as cur:
            async for row in cur:
                result[str(row[0])] = str(row[1])
        return result

    async def count_identity_links(
        self, guild_id: Optional[str] = None, nationality: Optional[str] = None
    ) -> int:
        """Count identity mappings with optional guild/nationality filters."""
        clauses: list[str] = []
        params: list[str] = []
        if guild_id:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        if nationality:
            clauses.append("LOWER(nationality) = LOWER(?)")
            params.append(nationality)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT COUNT(*) FROM identity_links{where}"
        async with self._conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        return int(row[0] if row else 0)

    async def identity_counts_by_nationality(
        self, guild_id: Optional[str] = None
    ) -> list[tuple[str, int]]:
        """Return counts grouped by nationality."""
        if guild_id:
            sql = (
                "SELECT nationality, COUNT(*) FROM identity_links "
                "WHERE guild_id = ? GROUP BY nationality ORDER BY COUNT(*) DESC"
            )
            params = (guild_id,)
        else:
            sql = (
                "SELECT nationality, COUNT(*) FROM identity_links "
                "GROUP BY nationality ORDER BY COUNT(*) DESC"
            )
            params = ()

        rows: list[tuple[str, int]] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                rows.append((str(row[0] or "unknown"), int(row[1] or 0)))
        return rows

    async def count_identity_ingame_conflicts(
        self, guild_id: Optional[str] = None
    ) -> int:
        """Count in-game IDs linked to more than one Discord user."""
        if guild_id:
            sql = (
                "SELECT COUNT(*) FROM ("
                "SELECT in_game_user_id FROM identity_links "
                "WHERE guild_id = ? GROUP BY in_game_user_id HAVING COUNT(*) > 1"
                ")"
            )
            params = (guild_id,)
        else:
            sql = (
                "SELECT COUNT(*) FROM ("
                "SELECT in_game_user_id FROM identity_links "
                "GROUP BY in_game_user_id HAVING COUNT(*) > 1"
                ")"
            )
            params = ()

        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row[0] if row else 0)

    async def get_recent_identity_links(
        self, guild_id: Optional[str] = None, limit: int = 10
    ) -> list[dict]:
        """Return recently updated identity mappings."""
        lim = max(1, min(int(limit), 50))
        if guild_id:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links WHERE guild_id = ? ORDER BY updated_at DESC LIMIT ?"
            )
            params = (guild_id, lim)
        else:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links ORDER BY updated_at DESC LIMIT ?"
            )
            params = (lim,)

        results: list[dict] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                results.append(
                    {
                        "discord_user_id": row[0],
                        "guild_id": row[1],
                        "in_game_user_id": row[2],
                        "nationality": row[3],
                        "request_type": row[4],
                        "embassy_country": row[5],
                        "approved_by_discord_id": row[6],
                        "approved_at": row[7],
                        "updated_at": row[8],
                    }
                )
        return results

    async def get_identity_links_for_guild(self, guild_id: str) -> list[dict]:
        """Return all identity mappings for one guild."""
        sql = (
            "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
            "embassy_country, approved_by_discord_id, approved_at, updated_at "
            "FROM identity_links WHERE guild_id = ?"
        )
        results: list[dict] = []
        async with self._conn.execute(sql, (guild_id,)) as cur:
            async for row in cur:
                results.append(
                    {
                        "discord_user_id": row[0],
                        "guild_id": row[1],
                        "in_game_user_id": row[2],
                        "nationality": row[3],
                        "request_type": row[4],
                        "embassy_country": row[5],
                        "approved_by_discord_id": row[6],
                        "approved_at": row[7],
                        "updated_at": row[8],
                    }
                )
        return results
    
    async def get_all_identity_links(self) -> list[dict]:
        """Return every row in identity_links across all guilds."""
        sql = (
            "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
            "embassy_country, approved_by_discord_id, approved_at, updated_at "
            "FROM identity_links"
        )
        results: list[dict] = []
        async with self._conn.execute(sql) as cur:
            async for row in cur:
                results.append(
                    {
                        "discord_user_id": row[0],
                        "guild_id": row[1],
                        "in_game_user_id": row[2],
                        "nationality": row[3],
                        "request_type": row[4],
                        "embassy_country": row[5],
                        "approved_by_discord_id": row[6],
                        "approved_at": row[7],
                        "updated_at": row[8],
                    }
                )
        return results

    async def get_discord_to_ingame_map(self) -> dict[str, str]:
        """Return {discord_user_id: in_game_user_id} for all identity links (all guilds)."""
        sql = "SELECT discord_user_id, in_game_user_id FROM identity_links"
        result: dict[str, str] = {}
        async with self._conn.execute(sql) as cur:
            async for row in cur:
                result[str(row[0])] = str(row[1])
        return result

    async def delete_identity_link(
        self, discord_user_id: str, guild_id: Optional[str] = None
    ) -> None:
        """Delete an identity mapping by Discord user ID.

        Without guild_id, deletes that user's link in EVERY guild — pass
        guild_id to remove only the mapping for one specific guild (e.g. an
        admin clearing a bad mapping on their own server shouldn't also
        wipe that user's unrelated link in another guild this bot serves).
        """
        if guild_id:
            await self._conn.execute(
                "DELETE FROM identity_links WHERE discord_user_id = ? AND guild_id = ?",
                (discord_user_id, guild_id),
            )
        else:
            await self._conn.execute(
                "DELETE FROM identity_links WHERE discord_user_id = ?", (discord_user_id,)
            )
        await self._conn.commit()

    async def get_identity_links_by_guild(
        self, guild_id: str, limit: Optional[int] = None
    ) -> list[dict]:
        """Return identity mappings for a guild, optionally limited."""
        sql = (
            "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
            "embassy_country, approved_by_discord_id, approved_at, updated_at "
            "FROM identity_links WHERE guild_id = ?"
        )
        params = (guild_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)

        results: list[dict] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                results.append(
                    {
                        "discord_user_id": row[0],
                        "guild_id": row[1],
                        "in_game_user_id": row[2],
                        "nationality": row[3],
                        "request_type": row[4],
                        "embassy_country": row[5],
                        "approved_by_discord_id": row[6],
                        "approved_at": row[7],
                        "updated_at": row[8],
                    }
                )
        return results


    # ── Pending ticket deletions ─────────────────────────────────────── #

    async def add_pending_ticket_deletion(
        self, channel_id: str, guild_id: str, approved_at: str, delete_at: str
    ) -> None:
        """Record a ticket channel that should be deleted at *delete_at*."""
        await self._conn.execute(
            "INSERT OR REPLACE INTO pending_ticket_deletions "
            "(channel_id, guild_id, approved_at, delete_at) VALUES (?, ?, ?, ?)",
            (channel_id, guild_id, approved_at, delete_at),
        )
        await self._conn.commit()

    async def remove_pending_ticket_deletion(self, channel_id: str) -> None:
        """Remove a pending deletion record (called once the channel is gone)."""
        await self._conn.execute(
            "DELETE FROM pending_ticket_deletions WHERE channel_id = ?",
            (channel_id,),
        )
        await self._conn.commit()

    async def get_pending_ticket_deletions(self) -> list[dict]:
        """Return all pending deletion records as list of dicts."""
        results: list[dict] = []
        async with self._conn.execute(
            "SELECT channel_id, guild_id, approved_at, delete_at "
            "FROM pending_ticket_deletions"
        ) as cur:
            async for row in cur:
                results.append(
                    {
                        "channel_id": row[0],
                        "guild_id": row[1],
                        "approved_at": row[2],
                        "delete_at": row[3],
                    }
                )
        return results

    # ── Ticket log ───────────────────────────────────────────────────── #

    async def log_ticket_created(
        self,
        guild_id: str,
        channel_id: str,
        request_type: str,
        discord_user_id: str,
        created_at: str,
    ) -> None:
        """Record that a verification ticket of *request_type* was opened."""
        await self._conn.execute(
            "INSERT INTO ticket_log "
            "(guild_id, channel_id, request_type, discord_user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, channel_id, request_type, discord_user_id, created_at),
        )
        await self._conn.commit()

    async def get_earliest_ticket_log_at(self, guild_id: str) -> Optional[str]:
        """Return the ISO timestamp of the oldest logged ticket for *guild_id*, if any."""
        async with self._conn.execute(
            "SELECT MIN(created_at) FROM ticket_log WHERE guild_id = ?",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def get_ticket_counts_since(
        self, guild_id: str, since: str
    ) -> dict[str, int]:
        """Return ticket counts per request_type for *guild_id* created at/after *since*."""
        counts: dict[str, int] = {}
        async with self._conn.execute(
            "SELECT request_type, COUNT(*) FROM ticket_log "
            "WHERE guild_id = ? AND created_at >= ? "
            "GROUP BY request_type",
            (guild_id, since),
        ) as cur:
            async for row in cur:
                counts[row[0]] = row[1]
        return counts
