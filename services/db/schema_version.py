"""Shared schema version stamp for database/external.db.

Both services/db/base.py's Database.setup() and bot.py's own ad-hoc
init_db() connect to this same file and run the same (idempotent, but
expensive) schema.sql + migrations setup on every process start. Stamping
PRAGMA user_version once that setup succeeds lets a later start skip the
whole thing with a single cheap read when nothing changed, instead of
re-running 100+ write-locked CREATE TABLE/INDEX/ALTER statements that this
file's two processes would otherwise also contend with each other over on
every simultaneous restart.

Bump this by 1 whenever schema.sql or either process's migrations list
changes, so the next deploy re-runs the full setup exactly once (to apply
the change) and then goes back to skipping it.
"""

SCHEMA_VERSION = 4
