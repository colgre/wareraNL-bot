"""Database connection management and schema initialization."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger("services.db")


class DatabaseBase:
    """Connection management and schema initialization.

    Subclasses (via :class:`~services.db.Database`) share ``self._conn``.
    """

    def __init__(self, path: str = "database/external.db") -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def setup(self) -> None:
        """Open the SQLite connection, create all tables, and apply migrations."""
        # isolation_level=None puts the connection in autocommit mode, which is
        # what makes busy_timeout actually work.
        #
        # With the default (isolation_level=""), Python opens an implicit
        # DEFERRED transaction before DML and holds it until commit().  Because
        # this connection is shared by every cog, reads and writes from many
        # concurrent tasks land inside that one long-lived transaction.  If any
        # other process commits while it is open, the next write has to upgrade
        # a now-stale read snapshot, and SQLite fails it *immediately* with
        # SQLITE_BUSY_SNAPSHOT ("database is locked") without ever consulting
        # busy_timeout — waiting cannot un-stale a snapshot, so no timeout value
        # can help.  That is the cause of the instant "database is locked"
        # errors, and no amount of PRAGMA tuning fixes it.
        #
        # In autocommit mode each statement is its own transaction that takes
        # the write lock up front, so contention becomes an ordinary lock wait
        # that busy_timeout resolves.
        #
        # Do NOT wrap a loop of awaited writes in an explicit BEGIN/COMMIT to
        # "batch" them — self._conn is shared by every concurrent coroutine in
        # the process, and each `await` between BEGIN and COMMIT lets other
        # tasks interleave their own statements inside that open transaction
        # (see git history: this caused "cannot commit - no transaction is
        # active" once a periodic WAL checkpoint collided with an open batch).
        # Per-statement autocommit is what's actually safe here.
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)

        # Enable WAL mode — allows concurrent readers + one writer without
        # blocking each other, which eliminates most "database is locked" errors
        # when the data-fetcher and discord-bot write simultaneously.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # Wait up to 60 s when the DB is locked before raising an error.
        # The full_fetcher stamps citizen_refresh_last_run so the discord bot
        # skips its own all-countries sweep, eliminating most write-write
        # contention.  60 s covers the brief NL-refresh overlap that still
        # happens every hour.
        await self._conn.execute("PRAGMA busy_timeout=60000")      # 60 s
        # Performance tuning: map the whole DB into the OS page cache (avoids
        # the internal 8 MB page cache round-trip for reads).  2 GB limit is
        # enough for the current 2.3 GB db; the OS maps only what it needs.
        await self._conn.execute("PRAGMA mmap_size=2147483648")   # 2 GB
        # Keep a 64 MB in-process page cache as well (fallback / write buffer).
        await self._conn.execute("PRAGMA cache_size=-65536")       # 64 MB
        # synchronous=NORMAL is the recommended setting for WAL: commits no
        # longer fsync the WAL on every transaction (only at checkpoints).
        # Durability is unchanged for process crashes; only an OS/power loss can
        # lose the last few transactions — acceptable for a rebuildable cache.
        # FULL (the default) made every commit wait on an fsync, which held the
        # single writer lock long enough for other processes to hit their
        # busy_timeout.
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        # Leave wal_autocheckpoint at the 1000-page default.  Lowering it makes
        # things worse, not better: with ~10 processes on this file there is
        # never a moment without an active reader, so each PASSIVE checkpoint
        # copies what it can and then stops at the oldest reader's snapshot
        # without ever resetting the WAL — more attempts just means more
        # contention.  Actually resetting the WAL requires the TRUNCATE
        # checkpoint the data-fetcher runs between sweeps.
        await self._conn.execute("PRAGMA wal_autocheckpoint=1000")

        # Run main schema (all CREATE TABLE IF NOT EXISTS)
        schema_path = Path("database/schema.sql")
        with schema_path.open("r", encoding="utf-8") as f:
            await self._conn.executescript(f.read())
        await self._conn.commit()

        # Apply incremental column additions (safe no-ops if already present)
        await self._apply_migrations()

        logger.info("Database initialized at %s", self.path)

    async def _apply_migrations(self) -> None:
        """Add columns that were introduced after the initial schema."""
        migrations: list[tuple[str, str]] = [
            # specialization_top — bonus breakdown columns
            ("specialization_top", "strategic_bonus REAL"),
            ("specialization_top", "ethic_bonus REAL"),
            ("specialization_top", "ethic_deposit_bonus REAL"),
            # deposit_top — breakdown columns
            ("deposit_top", "region_name TEXT"),
            ("deposit_top", "deposit_bonus REAL"),
            ("deposit_top", "ethic_deposit_bonus REAL"),
            # citizen_levels — extended fields
            ("citizen_levels", "skill_mode TEXT"),
            ("citizen_levels", "last_skills_reset_at TEXT"),
            ("citizen_levels", "citizen_name TEXT"),
            ("citizen_levels", "last_login_at TEXT"),
            ("citizen_levels", "mu_id TEXT"),
            ("citizen_levels", "mu_name TEXT"),
            # processed_battles — country identifiers added post-launch
            ("processed_battles", "attacker_country_id TEXT"),
            ("processed_battles", "defender_country_id TEXT"),
            # known_mus — home country added post-launch
            ("known_mus", "country_id TEXT"),
            # citizen_luck — elite case columns added post-launch
            ("citizen_luck", "elite_luck_score REAL"),
            ("citizen_luck", "elite_opens_count INTEGER"),
            ("citizen_luck", "elite_rarity_json TEXT"),
            # global_citizen_luck — elite case columns added post-launch
            ("global_citizen_luck", "elite_luck_score REAL"),
            ("global_citizen_luck", "elite_opens_count INTEGER"),
            ("global_citizen_luck", "elite_rarity_json TEXT"),
            # citizen_luck / global_citizen_luck — newest openCase transaction
            # _id counted so far, so the sweeps and /geluk /globalluck's live
            # option can fetch only NEW transactions instead of a player's
            # entire lifetime history on every run.
            ("citizen_luck", "last_seen_transaction_id TEXT"),
            ("global_citizen_luck", "last_seen_transaction_id TEXT"),
            # company_bonus_watchers — game_user_id cached at runtime
            ("company_bonus_watchers", "game_user_id TEXT"),
            # discord_allies — display label added post-launch
            ("discord_allies", "country_name TEXT"),
            # pill_reminders — in_game_user_id added post-launch; expires_at made nullable
            ("pill_reminders", "in_game_user_id TEXT NOT NULL DEFAULT ''"),
            # mu_auction_win_subs — initialized flag added to prevent posting old auctions
            ("mu_auction_win_subs", "initialized INTEGER NOT NULL DEFAULT 0"),
            # mu_auction_win_subs — cutoff_at: createdAt of newest contract at subscribe time
            ("mu_auction_win_subs", "cutoff_at TEXT"),
            # mu_auction_win_subs — opt-in ping of the MU's war-guild member role
            ("mu_auction_win_subs", "ping_enabled INTEGER NOT NULL DEFAULT 0"),
            # avatar URLs — added to citizen_levels and known_mus
            ("citizen_levels", "avatar_url TEXT"),
            ("known_mus", "avatar_url TEXT"),
            # specialization_top — last-notified record (only updated on notification, never by poll)
            ("specialization_top", "last_notified_country TEXT"),
            ("specialization_top", "last_notified_bonus REAL"),
            # company census — count of companies with at least one worker,
            # added after the tables shipped. Rows written before this default
            # to 0, so a 7-day delta on staffed counts is only meaningful once
            # a full week of sweeps has run with the column present.
            ("company_census", "staffed_count INTEGER NOT NULL DEFAULT 0"),
            ("company_owners", "staffed_count INTEGER NOT NULL DEFAULT 0"),
            # citizen_levels — mirrors getUserLite's isActive, so a company's
            # owner being inactive can be checked without an extra API call.
            # Defaults to 1: every existing row was populated by the
            # active-only per-country listing.
            ("citizen_levels", "is_active INTEGER NOT NULL DEFAULT 1"),
            # citizen_levels — mirrors getUserLite's infos.isBanned, so a
            # company's worker being banned can likewise be checked for free.
            ("citizen_levels", "is_banned INTEGER NOT NULL DEFAULT 0"),
            # citizen_wealth / citizen_wealth_history — wealth breakdown by
            # category (user.getUserById's stats.wealth), added so the
            # player page can show/hide company wealth specifically instead
            # of only ever the combined total.
            ("citizen_wealth", "wealth_companies REAL NOT NULL DEFAULT 0"),
            ("citizen_wealth", "wealth_items REAL NOT NULL DEFAULT 0"),
            ("citizen_wealth", "wealth_money REAL NOT NULL DEFAULT 0"),
            ("citizen_wealth", "wealth_equipments REAL NOT NULL DEFAULT 0"),
            ("citizen_wealth", "wealth_weapons REAL NOT NULL DEFAULT 0"),
            ("citizen_wealth_history", "wealth_companies REAL"),
            ("citizen_wealth_history", "wealth_items REAL"),
            ("citizen_wealth_history", "wealth_money REAL"),
            ("citizen_wealth_history", "wealth_equipments REAL"),
            ("citizen_wealth_history", "wealth_weapons REAL"),
        ]
        for table, column_def in migrations:
            try:
                await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
                await self._conn.commit()
            except Exception:
                pass  # column already exists — ignore

    async def checkpoint(self, mode: str = "PASSIVE") -> None:
        """Run a WAL checkpoint. Safe to call even if nothing is pending."""
        if self._conn:
            await self._conn.execute(f"PRAGMA wal_checkpoint({mode})")

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
