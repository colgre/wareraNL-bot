-- WarEra Discord Bot — SQLite schema
-- All tables are created here with IF NOT EXISTS guards.
-- Column-level migrations (ALTER TABLE) are applied at startup in services/db/base.py.

-- ── State & jobs ──────────────────────────────────────────────────────────────

-- poll_state: key/value store for background task timestamps and init flags
CREATE TABLE IF NOT EXISTS poll_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- jobs: background job tracking (progress, status)
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    status      TEXT,
    progress    INTEGER,
    result_path TEXT
);

-- ── Production ────────────────────────────────────────────────────────────────

-- country_snapshots: latest production snapshot per country (raw API JSON)
CREATE TABLE IF NOT EXISTS country_snapshots (
    country_id       TEXT PRIMARY KEY,
    code             TEXT,
    name             TEXT,
    specialized_item TEXT,
    production_bonus REAL,
    raw_json         TEXT,
    updated_at       TEXT
);

-- specialization_top: current best permanent bonus per specialization item
--   strategic_bonus + ethic_bonus + ethic_deposit_bonus = production_bonus
CREATE TABLE IF NOT EXISTS specialization_top (
    item                TEXT PRIMARY KEY,
    country_id          TEXT,
    country_name        TEXT,
    production_bonus    REAL,
    strategic_bonus     REAL,
    ethic_bonus         REAL,
    ethic_deposit_bonus REAL,
    updated_at          TEXT
);

-- country_item_ethic: ethics bonus per (item, country) pair seen in recommended lists
--   populated by the production poller from all recommended-region entries
CREATE TABLE IF NOT EXISTS country_item_ethic (
    item            TEXT NOT NULL,
    country_id      TEXT NOT NULL,
    strategic_bonus REAL,
    ethic_bonus     REAL,
    updated_at      TEXT,
    PRIMARY KEY (item, country_id)
);

-- deposit_top: current best deposit bonus region per specialization item
CREATE TABLE IF NOT EXISTS deposit_top (
    item                TEXT PRIMARY KEY,
    region_id           TEXT,
    region_name         TEXT,
    country_id          TEXT,
    country_name        TEXT,
    bonus               INTEGER,
    deposit_bonus       REAL,
    ethic_deposit_bonus REAL,
    permanent_bonus     REAL,
    deposit_end_at      TEXT,
    updated_at          TEXT
);

-- ── MU Registry ──────────────────────────────────────────────────────────────

-- known_mus: registry of all MUs in the game, populated by mu.getManyPaginated
CREATE TABLE IF NOT EXISTS known_mus (
    mu_id      TEXT PRIMARY KEY,
    mu_name    TEXT NOT NULL,
    updated_at TEXT,
    avatar_url TEXT
);

-- war_mu_roles: Discord role IDs created for Dutch-owned MUs in the war guild
--   Only used by the war_sync cog; safe to exist on the production bot (empty).
--   role_type 'category' is a pseudo-entry (discord_role_id actually holds a
--   CategoryChannel id, not a role id) used by war_guild_divisions.py to
--   find a MU's Discord category by ID rather than by name — survives any
--   future rename of either the category or the MU.
CREATE TABLE IF NOT EXISTS war_mu_roles (
    mu_id           TEXT NOT NULL,
    role_type       TEXT NOT NULL,  -- 'owner', 'commander', 'member', 'category'
    discord_role_id TEXT NOT NULL,
    guild_id        TEXT NOT NULL,
    mu_name         TEXT,
    PRIMARY KEY (mu_id, role_type, guild_id)
);

-- division_mu_overrides: runtime add/move/remove edits to DIVISION_MU_IDS
--   (cogs/tasks/war_guild_divisions.py), made via /mudivisie instead of
--   hand-editing the source file. division 0 means "removed" (excluded even
--   if the MU is still hardcoded in DIVISION_MU_IDS). Applied on top of the
--   hardcoded dict at startup so they survive restarts. mu_id is the real
--   key (a MU's name can change in-game while its ID never does); mu_name
--   is kept for display and stays the PRIMARY KEY for historical reasons —
--   see upsert_division_mu_override for how a rename is handled without
--   leaving a stale duplicate row behind.
CREATE TABLE IF NOT EXISTS division_mu_overrides (
    mu_name  TEXT PRIMARY KEY,
    mu_id    TEXT,
    division INTEGER NOT NULL
);

-- ── Citizens ──────────────────────────────────────────────────────────────────

-- citizen_levels: hourly snapshot of level, skill mode, and MU per citizen
--   is_active mirrors getUserLite's "isActive" — false for players
--   user.getUsersByCountry silently excludes from its own listing (see
--   services/citizen_cache.py). Defaults to 1 because every row populated by
--   the normal per-country sweep is, by construction, active.
--   is_banned mirrors getUserLite's "infos.isBanned". Defaults to 0; a banned
--   player can also be inactive (confirmed live), so it needs the same
--   by-ID backfill path as is_active to ever be known for such players.
CREATE TABLE IF NOT EXISTS citizen_levels (
    user_id              TEXT PRIMARY KEY,
    country_id           TEXT NOT NULL,
    level                INTEGER,
    skill_mode           TEXT,
    last_skills_reset_at TEXT,
    citizen_name         TEXT,
    last_login_at        TEXT,
    mu_id                TEXT,
    mu_name              TEXT,
    updated_at           TEXT NOT NULL,
    avatar_url           TEXT,
    is_active            INTEGER NOT NULL DEFAULT 1,
    is_banned            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_citizen_levels_country ON citizen_levels(country_id);
CREATE INDEX IF NOT EXISTS idx_citizen_levels_mu_name ON citizen_levels(mu_name) WHERE mu_name IS NOT NULL;

-- identity_links: mapping between Discord identities and in-game identities
--   updated on verification approvals in welcome flow
--   PRIMARY KEY is (discord_user_id, guild_id), NOT discord_user_id alone —
--   the same Discord user can be verified independently in more than one
--   guild this bot serves (e.g. the production guild and the war guild).
--   A discord_user_id-only key meant re-verifying in a second guild
--   silently overwrote (and orphaned) the link from the first, which is
--   why the production guild's nickname sync had zero rows to work from
--   despite /approve being used there for months — confirmed as a real
--   incident. See bot.py's init_db() for the migration off the old
--   single-column key on existing databases.
CREATE TABLE IF NOT EXISTS identity_links (
    discord_user_id        TEXT NOT NULL,
    guild_id               TEXT NOT NULL,
    in_game_user_id        TEXT NOT NULL,
    nationality            TEXT NOT NULL,
    request_type           TEXT NOT NULL,
    embassy_country        TEXT,
    approved_by_discord_id TEXT NOT NULL,
    approved_at            TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    PRIMARY KEY (discord_user_id, guild_id)
);
CREATE INDEX IF NOT EXISTS idx_identity_links_ingame ON identity_links(in_game_user_id);

-- ── Events ────────────────────────────────────────────────────────────────────

-- pending_ticket_deletions: tracks approved ticket channels that should be
--   deleted after a delay.  Rows are inserted when /approve is called and
--   removed once the channel has been deleted.  On bot restart only channels
--   recorded here are rescheduled for deletion — open (unapproved) tickets are
--   left untouched.
CREATE TABLE IF NOT EXISTS pending_ticket_deletions (
    channel_id  TEXT PRIMARY KEY,
    guild_id    TEXT NOT NULL,
    approved_at TEXT NOT NULL,  -- ISO-8601 UTC timestamp of /approve call
    delete_at   TEXT NOT NULL   -- ISO-8601 UTC timestamp when deletion is due
);

-- ticket_log: historical record of every verification ticket opened, used
--   for /ticketstats reporting. Logging started when this table was
--   introduced — tickets created before that are not represented.
CREATE TABLE IF NOT EXISTS ticket_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    request_type    TEXT NOT NULL,
    discord_user_id TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_log_guild_created ON ticket_log(guild_id, created_at);

-- seen_articles: deduplication for posted articles
CREATE TABLE IF NOT EXISTS seen_articles (
    article_id TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL
);

-- seen_events: deduplication for posted game events
CREATE TABLE IF NOT EXISTS seen_events (
    event_id TEXT PRIMARY KEY,
    seen_at  TEXT NOT NULL
);

-- war_events: historical archive of all posted war/battle events
CREATE TABLE IF NOT EXISTS war_events (
    event_id            TEXT PRIMARY KEY,
    event_type          TEXT NOT NULL,
    battle_id           TEXT,
    war_id              TEXT,
    attacker_country_id TEXT,
    defender_country_id TEXT,
    region_id           TEXT,
    region_name         TEXT,
    attacker_name       TEXT,
    defender_name       TEXT,
    created_at          TEXT,
    raw_json            TEXT
);

-- ── Luck ──────────────────────────────────────────────────────────────────────

-- citizen_luck: case-opening luck scores per citizen
--   luck_score: weighted z-score (0 = average, positive = luckier)
CREATE TABLE IF NOT EXISTS citizen_luck (
    user_id      TEXT PRIMARY KEY,
    country_id   TEXT NOT NULL,
    citizen_name TEXT,
    luck_score   REAL NOT NULL,
    opens_count  INTEGER NOT NULL,
    rarity_json  TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_citizen_luck_country ON citizen_luck(country_id);

-- ── Resistance ────────────────────────────────────────────────────────────────

-- resistance_state: current resistance bar for NL-occupied foreign regions
CREATE TABLE IF NOT EXISTS resistance_state (
    region_id         TEXT PRIMARY KEY,
    region_name       TEXT,
    occupying_country TEXT,
    resistance_value  REAL,
    resistance_max    REAL DEFAULT 100.0,
    updated_at        TEXT
);


-- ── Giveaways ─────────────────────────────────────────────────────────────────

-- giveaways: rewards inventory for citizens
CREATE TABLE IF NOT EXISTS wallets (
    user_id TEXT PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- wallet_transactions: ledger of all wallet changes (positive and negative)
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    tx_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES wallets(user_id)
);

-- ── Weekly damage ────────────────────────────────────────────────────────────

-- citizen_weekly_damages: latest weekly battle damage per citizen (all countries)
CREATE TABLE IF NOT EXISTS citizen_weekly_damages (
    user_id       TEXT PRIMARY KEY,
    citizen_name  TEXT,
    country_id    TEXT NOT NULL,
    weekly_damage REAL NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weekly_damages_country ON citizen_weekly_damages(country_id);

-- citizen_weekly_damage_history: per-player weekly damage, one row per game week.
--   Populated hourly by the full_fetcher from ranking.getRanking
--   (rankingType=weeklyUserDamages), which covers every player in the game.
--   week_start is the YYYY-MM-DD Monday of the game week (Monday 02:00 UTC
--   boundary — see services/game_time.py).  country_id / mu_id are snapshotted
--   at write time so historical rankings stay correct after a player moves.
CREATE TABLE IF NOT EXISTS citizen_weekly_damage_history (
    user_id       TEXT NOT NULL,
    week_start    TEXT NOT NULL,
    citizen_name  TEXT,
    country_id    TEXT,
    mu_id         TEXT,
    mu_name       TEXT,
    weekly_damage REAL NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, week_start)
);
CREATE INDEX IF NOT EXISTS idx_wdh_week_country ON citizen_weekly_damage_history(week_start, country_id);
CREATE INDEX IF NOT EXISTS idx_wdh_week_mu      ON citizen_weekly_damage_history(week_start, mu_id);
CREATE INDEX IF NOT EXISTS idx_wdh_user         ON citizen_weekly_damage_history(user_id);

-- citizen_daily_damage: per-player damage per game day (02:00–02:00 UTC).
--   Derived from the hourly weekly-damage snapshots rather than from
--   individual battles: damage = weekly_end - baseline, where baseline is the
--   weekly counter's value when the game day opened.  Because the weekly
--   counter resets at Monday 02:00 — exactly a day boundary — a week rollover
--   simply means baseline 0 for that Monday.
CREATE TABLE IF NOT EXISTS citizen_daily_damage (
    user_id      TEXT NOT NULL,
    game_date    TEXT NOT NULL,
    week_start   TEXT NOT NULL,
    citizen_name TEXT,
    country_id   TEXT,
    mu_id        TEXT,
    mu_name      TEXT,
    baseline     REAL NOT NULL DEFAULT 0,
    weekly_end   REAL NOT NULL DEFAULT 0,
    damage       REAL NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, game_date)
);
CREATE INDEX IF NOT EXISTS idx_cdd_date_country ON citizen_daily_damage(game_date, country_id);
CREATE INDEX IF NOT EXISTS idx_cdd_date_mu      ON citizen_daily_damage(game_date, mu_id);
CREATE INDEX IF NOT EXISTS idx_cdd_user         ON citizen_daily_damage(user_id);

-- ── Global luck ────────────────────────────────────────────────────────────────

-- global_citizen_luck: case-opening luck scores across all countries
CREATE TABLE IF NOT EXISTS global_citizen_luck (
    user_id      TEXT PRIMARY KEY,
    country_id   TEXT NOT NULL,
    citizen_name TEXT,
    luck_score   REAL NOT NULL,
    opens_count  INTEGER NOT NULL,
    rarity_json  TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_global_luck_score ON global_citizen_luck(luck_score);
CREATE INDEX IF NOT EXISTS idx_global_luck_country ON global_citizen_luck(country_id);

-- ── Legacy (krypton template) ─────────────────────────────────────────────────

-- ── Battle rankings accumulator ──────────────────────────────────────────────

-- battle_hits: per-player damage per battle side, written by the daily poller
CREATE TABLE IF NOT EXISTS battle_hits (
    battle_id         TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    side              TEXT NOT NULL,      -- 'attacker' | 'defender'
    damage            REAL NOT NULL DEFAULT 0,
    rank              INTEGER,
    battle_created_at TEXT NOT NULL,      -- ISO-8601 UTC from battle.createdAt
    recorded_at       TEXT NOT NULL,
    PRIMARY KEY (battle_id, user_id, side)
);
CREATE INDEX IF NOT EXISTS idx_battle_hits_user         ON battle_hits(user_id);
CREATE INDEX IF NOT EXISTS idx_battle_hits_created      ON battle_hits(battle_created_at);
CREATE INDEX IF NOT EXISTS idx_battle_hits_battle       ON battle_hits(battle_id);
-- Covering index for list_available_weeks: allows week-per-user lookup without full row scan
CREATE INDEX IF NOT EXISTS idx_battle_hits_user_created ON battle_hits(user_id, battle_created_at);

-- processed_battles: deduplication tracker for the daily battle poller
CREATE TABLE IF NOT EXISTS processed_battles (
    battle_id         TEXT PRIMARY KEY,
    battle_created_at TEXT,
    attacker_damage   REAL,
    defender_damage   REAL,
    processed_at      TEXT NOT NULL
);

-- warns: moderation warn log used by database/__init__.py DatabaseManager
CREATE TABLE IF NOT EXISTS warns (
    id           INTEGER,
    user_id      TEXT    NOT NULL,
    server_id    TEXT    NOT NULL,
    moderator_id TEXT    NOT NULL,
    reason       TEXT    NOT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id, user_id, server_id)
);

-- battle_drops: loot items dropped during battles (from battleRanking.getRanking)
CREATE TABLE IF NOT EXISTS battle_drops (
    id               TEXT PRIMARY KEY,   -- lootItem._id
    battle_id        TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    side             TEXT NOT NULL,       -- 'attacker' | 'defender'
    rank             INTEGER,
    damage_dealt     REAL,
    item_code        TEXT,
    item_skills_json TEXT,
    dropped_at       TEXT,
    recorded_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_battle_drops_user    ON battle_drops(user_id);
CREATE INDEX IF NOT EXISTS idx_battle_drops_battle  ON battle_drops(battle_id);

-- battle_mu_hits: per-MU damage per battle side, fetched via type="mu" rankings
CREATE TABLE IF NOT EXISTS battle_mu_hits (
    battle_id             TEXT NOT NULL,
    mu_id                 TEXT NOT NULL,
    side                  TEXT NOT NULL,       -- 'attacker' | 'defender'
    mu_name               TEXT,
    damage                REAL NOT NULL DEFAULT 0,
    battle_created_at     TEXT,
    recorded_at           TEXT,
    attacker_country_id   TEXT,
    defender_country_id   TEXT,
    PRIMARY KEY (battle_id, mu_id, side)
);
CREATE INDEX IF NOT EXISTS idx_battle_mu_hits_mu      ON battle_mu_hits(mu_id);
CREATE INDEX IF NOT EXISTS idx_battle_mu_hits_created ON battle_mu_hits(battle_created_at);

-- battle_country_hits: per-country damage per battle side, fetched via type="country" rankings
--   Reflects player nationality (same as the game's own country leaderboard), NOT
--   which country was the attacker/defender side.
CREATE TABLE IF NOT EXISTS battle_country_hits (
    battle_id         TEXT NOT NULL,
    country_id        TEXT NOT NULL,
    side              TEXT NOT NULL,       -- 'attacker' | 'defender'
    damage            REAL NOT NULL DEFAULT 0,
    battle_created_at TEXT,
    recorded_at       TEXT,
    PRIMARY KEY (battle_id, country_id, side)
);
CREATE INDEX IF NOT EXISTS idx_battle_country_hits_country  ON battle_country_hits(country_id);
CREATE INDEX IF NOT EXISTS idx_battle_country_hits_created  ON battle_country_hits(battle_created_at);

-- mu_battle_member_damage: per-member damage per battle, populated by the web
--   gevechten service when an MU's gevechten tab is visited.  Acts as a lazy
--   cache: re-fetched if the battle is still active and data is stale.
CREATE TABLE IF NOT EXISTS mu_battle_member_damage (
    battle_id         TEXT NOT NULL,
    mu_id             TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    damage            REAL NOT NULL DEFAULT 0,
    battle_created_at TEXT,
    recorded_at       TEXT NOT NULL,
    PRIMARY KEY (battle_id, mu_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_mu_bmember_mu     ON mu_battle_member_damage(mu_id);
CREATE INDEX IF NOT EXISTS idx_mu_bmember_battle ON mu_battle_member_damage(battle_id, mu_id);

-- ── Pill tracking ─────────────────────────────────────────────────────────────

-- citizen_pill_tracking: tracks the last known pill buff expiry per citizen.
--   Updated hourly during the NL citizen refresh by scanning getUserLite for all
--   NL players.  buff_expires_at is the Unix timestamp when the active/last buff
--   ended (or will end).  When buff_expires_at + 57600 (16h) > now, the player
--   is in debuff.  NULL means no pill has ever been observed.
CREATE TABLE IF NOT EXISTS citizen_pill_tracking (
    user_id         TEXT PRIMARY KEY,
    country_id      TEXT NOT NULL,
    buff_expires_at INTEGER,             -- Unix timestamp (seconds); NULL = never seen
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pill_tracking_country ON citizen_pill_tracking(country_id);

-- ── Level-5 notifications ─────────────────────────────────────────────────────

-- lvl5_tracker: hourly threshold tracker — records the last-seen level for every
--   NL citizen so we can detect the <5 → ≥5 crossing.  notified=1 means we
--   already posted this citizen in the admin channel; notified stays 1 forever.
CREATE TABLE IF NOT EXISTS lvl5_tracker (
    user_id          TEXT PRIMARY KEY,
    last_seen_level  INTEGER NOT NULL,
    notified         INTEGER NOT NULL DEFAULT 0,  -- 0 = not yet posted, 1 = already posted
    updated_at       TEXT NOT NULL
);

-- ── Article tips ──────────────────────────────────────────────────────────────

-- article_tips: individual article tip transactions (outgoing from the tipper's perspective)
--   Populated by /peil artikelen which scans known citizens' transactions.
--   amount is always positive (abs(money)); tip_at comes from transaction.createdAt.
CREATE TABLE IF NOT EXISTS article_tips (
    user_id      TEXT NOT NULL,
    country_id   TEXT,
    citizen_name TEXT,
    amount       REAL NOT NULL,
    tip_at       TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, tip_at)
);
CREATE INDEX IF NOT EXISTS idx_article_tips_date    ON article_tips(tip_at);
CREATE INDEX IF NOT EXISTS idx_article_tips_country ON article_tips(country_id);

-- article_tip_scans: tracks the last time a citizen was scanned for article tips.
--   Used for incremental scanning: citizens with no tips are skipped until
--   RESCAN_DAYS have passed since their last scan.
CREATE TABLE IF NOT EXISTS article_tip_scans (
    user_id        TEXT PRIMARY KEY,
    last_scanned_at TEXT NOT NULL
);

-- ── Bedrijven bonus check (company bonus monitor) ────────────────────────────────────

-- company_bonus_watchers: Discord users who want to be notified when one of their
--   companies has a 0% production bonus in its current region.
CREATE TABLE IF NOT EXISTS company_bonus_watchers (
    discord_user_id  TEXT PRIMARY KEY,
    discord_username TEXT NOT NULL,
    game_username    TEXT NOT NULL,
    game_user_id     TEXT,               -- resolved in-game ID (cached)
    guild_id         TEXT NOT NULL,
    added_at         TEXT NOT NULL
);

-- company_bonus_alerts: tracks which (user, company, region) combinations have
--   already been pinged, so we don't spam.  When the company moves to a new
--   region the old row is deleted, allowing a fresh alert if the new region
--   also has 0% bonus.
CREATE TABLE IF NOT EXISTS company_bonus_alerts (
    discord_user_id TEXT NOT NULL,
    company_id      TEXT NOT NULL,
    region_id       TEXT NOT NULL,
    alerted_at      TEXT NOT NULL,
    PRIMARY KEY (discord_user_id, company_id)
);

-- ── Company move advisor ─────────────────────────────────────────────────────

-- company_move_advice_watchers: Discord users who want to be notified when one of
--   their companies could profitably move to a region with a higher production bonus.
CREATE TABLE IF NOT EXISTS company_move_advice_watchers (
    discord_user_id  TEXT PRIMARY KEY,
    discord_username TEXT NOT NULL,
    game_username    TEXT NOT NULL,
    game_user_id     TEXT,               -- resolved in-game ID (cached)
    guild_id         TEXT NOT NULL,
    added_at         TEXT NOT NULL
);

-- company_move_advice_alerts: tracks which (user, company, source_region → target_region)
--   combinations have already been advised, so we don't spam.  Cleared when the company
--   moves to a new region or when the subscription is removed.
CREATE TABLE IF NOT EXISTS company_move_advice_alerts (
    discord_user_id    TEXT NOT NULL,
    company_id         TEXT NOT NULL,
    source_region_id   TEXT NOT NULL,   -- company's region when advice was given
    target_region_id   TEXT NOT NULL,   -- advised target region
    alerted_at         TEXT NOT NULL,
    PRIMARY KEY (discord_user_id, company_id)
);

-- discord_allies: manually maintained list of countries that are allied via Discord
--   (not necessarily reflected in the game API).  Used by the bounty poller to
--   suppress alerts for battles these countries are engaged in.
CREATE TABLE IF NOT EXISTS discord_allies (
    country_id   TEXT PRIMARY KEY,
    country_name TEXT,          -- optional display label (e.g. "België")
    added_by     TEXT NOT NULL,  -- Discord user ID who added the entry
    added_at     TEXT NOT NULL   -- ISO timestamp
);

-- ── Pill buff reminders ───────────────────────────────────────────────────────

-- pill_reminders: one row per Discord user subscribed to pill buff reminders.
--   in_game_user_id links to their WarEra account.
--   expires_at is a Unix timestamp (seconds UTC) set by the hourly API scan;
--   NULL means no active pill buff detected yet.
--   reminded is set to 1 once the 10-minute DM has been sent.
CREATE TABLE IF NOT EXISTS pill_reminders (
    discord_user_id TEXT PRIMARY KEY,
    in_game_user_id TEXT NOT NULL DEFAULT '',
    expires_at      INTEGER,
    reminded        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pill_reminders_expires ON pill_reminders(expires_at);

-- pill_reminders_30: same as pill_reminders but fires 30 minutes before expiry.
CREATE TABLE IF NOT EXISTS pill_reminders_30 (
    discord_user_id TEXT PRIMARY KEY,
    in_game_user_id TEXT NOT NULL DEFAULT '',
    expires_at      INTEGER,
    reminded        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pill_reminders_30_expires ON pill_reminders_30(expires_at);

-- ── Citizen Wealth ────────────────────────────────────────────────────────────

-- citizen_wealth: wealth per NL citizen (active + inactive company wealth)
--   wealth_active:             personal wallet + wealth of active companies (from userWealth ranking)
--   wealth_inactive_companies: sum of balance of disabled/inactive companies
--   wealth_total:              wealth_active + wealth_inactive_companies
--   wealth_companies/items/money/equipments/weapons: the breakdown from
--     user.getUserById's stats.wealth (added later — see cogs/tasks/wealth.py).
--     wealth_companies + items + money + equipments + weapons == wealth_total.
CREATE TABLE IF NOT EXISTS citizen_wealth (
    user_id                   TEXT PRIMARY KEY,
    country_id                TEXT NOT NULL,
    citizen_name              TEXT,
    wealth_active             REAL NOT NULL DEFAULT 0,
    wealth_inactive_companies REAL NOT NULL DEFAULT 0,
    wealth_total              REAL NOT NULL DEFAULT 0,
    wealth_companies          REAL NOT NULL DEFAULT 0,
    wealth_items              REAL NOT NULL DEFAULT 0,
    wealth_money              REAL NOT NULL DEFAULT 0,
    wealth_equipments         REAL NOT NULL DEFAULT 0,
    wealth_weapons            REAL NOT NULL DEFAULT 0,
    updated_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_citizen_wealth_country ON citizen_wealth(country_id);
CREATE INDEX IF NOT EXISTS idx_citizen_wealth_total   ON citizen_wealth(wealth_total DESC);

-- citizen_wealth_history: daily snapshots of each NL citizen's total wealth.
--   One row per (user_id, snapshot_date). Used to compute wealth increase
--   over a configurable number of days in the /wealth command.
-- wealth_companies/items/money/equipments/weapons: same breakdown as
-- citizen_wealth above, captured daily so history can be replayed with a
-- category excluded (e.g. "hide company wealth") instead of only ever
-- showing the combined total. NULL (not 0) for snapshots taken before this
-- breakdown started being tracked, so old rows can be told apart from a
-- genuine zero.
CREATE TABLE IF NOT EXISTS citizen_wealth_history (
    user_id       TEXT NOT NULL,
    country_id    TEXT NOT NULL,
    citizen_name  TEXT,
    wealth_total  REAL NOT NULL DEFAULT 0,
    wealth_companies  REAL,
    wealth_items      REAL,
    wealth_money      REAL,
    wealth_equipments REAL,
    wealth_weapons    REAL,
    snapshot_date TEXT NOT NULL,  -- ISO date: YYYY-MM-DD (UTC)
    PRIMARY KEY (user_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_wealth_history_country_date
    ON citizen_wealth_history(country_id, snapshot_date);

-- ── Event gems ────────────────────────────────────────────────────────────────

-- event_gems: gem balance per Discord user, awarded via Discord events.
--   Gems are gifted in-game once a threshold is reached; this table tracks
--   the pending balance.
CREATE TABLE IF NOT EXISTS event_gems (
    discord_user_id  TEXT PRIMARY KEY,
    discord_username TEXT NOT NULL,
    guild_id         TEXT NOT NULL,
    gems             INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL
);

-- player_tx_cache: cached transaction aggregates per player for /transacties.
--   On each command invocation only *new* transactions (createdAt > newest_tx_at)
--   are fetched, then merged into the stored aggregates.
CREATE TABLE IF NOT EXISTS player_tx_cache (
    user_id        TEXT PRIMARY KEY,
    username       TEXT NOT NULL,
    counts_json    TEXT NOT NULL DEFAULT '{}',   -- JSON dict type→count
    totals_json    TEXT NOT NULL DEFAULT '{}',   -- JSON dict type→total_cc
    breakdown_json TEXT NOT NULL DEFAULT '{}',   -- JSON dict type→subkey→[count,total]
    newest_tx_at   TEXT,                         -- ISO timestamp of newest cached tx
    total_tx       INTEGER NOT NULL DEFAULT 0,
    truncated      INTEGER NOT NULL DEFAULT 0,   -- 1 if original fetch was capped
    updated_at     TEXT NOT NULL
);

-- ── Item market trades ────────────────────────────────────────────────────────

-- item_trades: itemMarket transactions polled from /transaction.getPaginatedTransactions.
--   Used by /marktprijs to rank similar historical trades and estimate a price.
--   Skill columns are NULL when the source payload didn't include that stat.
CREATE TABLE IF NOT EXISTS item_trades (
    tx_id             TEXT PRIMARY KEY,            -- mongo _id from transaction
    created_at        TEXT NOT NULL,               -- ISO timestamp of sale
    offer_created_at  TEXT,                        -- when listing was posted
    item_code         TEXT NOT NULL,
    item_type         TEXT,                        -- e.g. "equipment"
    quantity          INTEGER NOT NULL DEFAULT 1,
    price             REAL NOT NULL,               -- total money paid (coins)
    state             INTEGER,                     -- current durability
    max_state         INTEGER,                     -- max durability
    attack            INTEGER,
    critical_chance   INTEGER,
    critical_damages  INTEGER,
    armor             INTEGER,
    precision_        INTEGER,                     -- 'precision' is SQL reserved
    dodge             INTEGER,
    buyer_id          TEXT,
    seller_id         TEXT,
    raw_json          TEXT NOT NULL,               -- full payload for audit
    ingested_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_item_trades_code_ts ON item_trades(item_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_item_trades_ts      ON item_trades(created_at DESC);

-- item_price_history: hourly snapshot of itemTrading.getPrices (fungible
-- resources: iron, bread, ammo, cases, ...). The API only exposes the
-- current price, not history, so this table is what powers the price
-- chart on the /markt/items pages. Populated by cogs/tasks/item_price_sync.py.
CREATE TABLE IF NOT EXISTS item_price_history (
    item_code   TEXT NOT NULL,
    price       REAL NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (item_code, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_item_price_history_code_ts ON item_price_history(item_code, captured_at DESC);

-- ── Daily damage accumulator ─────────────────────────────────────────────────

-- daily_dmg_hits: per-player damage per battle, populated hourly by daily_dmg_task.
--   Uses battleLootSummary.getByBattleAndUser for NL citizens. Scope is limited to
--   NL citizens; country/MU aggregation is derived by joining citizen_levels.
CREATE TABLE IF NOT EXISTS daily_dmg_hits (
    round_id     TEXT NOT NULL,
    battle_id    TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    total_damage REAL NOT NULL DEFAULT 0,
    hits         INTEGER,
    cases        INTEGER,
    round_date   TEXT NOT NULL,   -- 'YYYY-MM-DD' derived from round ObjectId timestamp (UTC)
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (round_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_dmg_hits_date ON daily_dmg_hits(round_date);
CREATE INDEX IF NOT EXISTS idx_daily_dmg_hits_user ON daily_dmg_hits(user_id);

-- daily_dmg_processed: deduplication log for the hourly daily_dmg_task.
CREATE TABLE IF NOT EXISTS daily_dmg_processed (
    battle_id    TEXT PRIMARY KEY,
    battle_date  TEXT,
    processed_at TEXT NOT NULL
);

-- ── War guild status ─────────────────────────────────────────────────────────

-- war_status_choices: per-player war-readiness choice set via buttons in the war guild
CREATE TABLE IF NOT EXISTS war_status_choices (
    discord_user_id TEXT PRIMARY KEY,
    choice          TEXT NOT NULL,    -- 'ready' | 'eco'
    updated_at      TEXT NOT NULL
);

-- citizen_mu_membership: authoritative user→MU mapping from the war-guild MU scan.
-- Populated/replaced each scan cycle; used as fallback when citizen_levels.mu_name is null
-- (e.g. for MU owners who don't follow daily orders and thus have no mu_name in the API).
CREATE TABLE IF NOT EXISTS citizen_mu_membership (
    in_game_user_id TEXT PRIMARY KEY,
    mu_id           TEXT NOT NULL,
    mu_name         TEXT NOT NULL,
    role_type       TEXT NOT NULL,    -- 'owner' | 'commander' | 'member'
    updated_at      TEXT NOT NULL
);

-- ── Web data freshness ────────────────────────────────────────────────────────
-- Tracks when each named dataset was last refreshed by a task or manual trigger.
-- Used by the rijksoverheid-web website to display "Last updated" and to power
-- the manual refresh queue.
CREATE TABLE IF NOT EXISTS data_freshness (
    dataset           TEXT PRIMARY KEY,
    last_started_at   TEXT,
    last_finished_at  TEXT,
    last_status       TEXT,        -- 'ok' | 'error' | 'running'
    last_error        TEXT,
    source            TEXT,        -- 'task' | 'manual_web' | 'manual_discord'
    duration_ms       INTEGER
);

-- ── MU channel subscriptions ─────────────────────────────────────────────────

-- mu_weekly_report_subs: channels subscribed to automatic weekly MU damage reports
--   posted every Monday at 01:00 NL time (just before the 02:00 weekly-damage reset).
--   last_posted_at guards against double-posts on bot restart within the same hour.
CREATE TABLE IF NOT EXISTS mu_weekly_report_subs (
    channel_id     TEXT NOT NULL,
    guild_id       TEXT NOT NULL,
    mu_name        TEXT NOT NULL,
    mu_id          TEXT NOT NULL,
    last_posted_at TEXT,
    added_at       TEXT NOT NULL,
    PRIMARY KEY (channel_id, mu_name)
);

-- mu_auction_win_subs: channels that receive a ping when a specific MU wins an auction.
--   seen_ids is a JSON list of already-notified auction IDs (trimmed to last 500).
--   cutoff_at is the createdAt of the newest contract known at subscribe time;
--   only contracts with createdAt strictly after this value are posted.
CREATE TABLE IF NOT EXISTS mu_auction_win_subs (
    channel_id TEXT NOT NULL,
    guild_id   TEXT NOT NULL,
    mu_name    TEXT NOT NULL,
    mu_id      TEXT NOT NULL,
    seen_ids   TEXT NOT NULL DEFAULT '[]',
    initialized INTEGER NOT NULL DEFAULT 0,
    cutoff_at  TEXT,
    added_at   TEXT NOT NULL,
    ping_enabled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, mu_name)
);

-- ── Eco donations cache ───────────────────────────────────────────────────────
-- Hourly snapshot of NL donation transactions, populated by eco_donations_poller.
-- The /eco_donaties command reads from this table instead of calling the API live.
CREATE TABLE IF NOT EXISTS eco_donations (
    txn_id       TEXT PRIMARY KEY,   -- SHA1 of (user_id|created_at|amount)
    user_id      TEXT NOT NULL,
    citizen_name TEXT,               -- resolved from citizen_levels at insert time
    mu_name      TEXT,               -- resolved from citizen_levels at insert time
    amount       REAL NOT NULL,
    created_at   TEXT NOT NULL       -- ISO8601 UTC, e.g. 2026-05-01T12:34:56.000Z
);
CREATE INDEX IF NOT EXISTS idx_eco_donations_created_at ON eco_donations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_eco_donations_mu_name    ON eco_donations(mu_name);

-- ── Online history log ────────────────────────────────────────────────────────
-- Every 12 hours, snapshot whether each citizen was seen online (lastConnectionAt
-- within the past 12 hours). Used to draw an online-activity chart on player pages.
CREATE TABLE IF NOT EXISTS citizen_online_log (
    user_id    TEXT NOT NULL,
    checked_at TEXT NOT NULL,  -- ISO8601 UTC timestamp of the snapshot
    was_online INTEGER NOT NULL DEFAULT 0,  -- 1 = online in last 12h, 0 = offline
    PRIMARY KEY (user_id, checked_at)
);
CREATE INDEX IF NOT EXISTS idx_citizen_online_log_user ON citizen_online_log(user_id, checked_at DESC);

-- ── Country paraatheid (war-readiness) daily log ──────────────────────────────
-- Once per day, snapshot war/eco counts for every known country so we can draw
-- a "paraatheid over time" graph on the country detail page.
CREATE TABLE IF NOT EXISTS country_paraatheid_log (
    country_id    TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,  -- YYYY-MM-DD
    total_count   INTEGER NOT NULL DEFAULT 0,
    war_count     INTEGER NOT NULL DEFAULT 0,
    eco_count     INTEGER NOT NULL DEFAULT 0,
    war_pct       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (country_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_country_para_log ON country_paraatheid_log(country_id, snapshot_date DESC);

-- ── Citizen level history log ─────────────────────────────────────────────────
-- One row per level-up event (only inserted when the level changes from the
-- previous snapshot), so the table stays small and charts show step-wise growth.
CREATE TABLE IF NOT EXISTS citizen_level_log (
    user_id       TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,  -- YYYY-MM-DD of the day the new level was first seen
    level         INTEGER NOT NULL,
    PRIMARY KEY (user_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_citizen_level_log ON citizen_level_log(user_id, snapshot_date DESC);

-- ── Citizen name history ──────────────────────────────────────────────────────
-- One row per unique name a citizen has ever had (captured from today onwards).
-- inserted the first time we see the name, so first_seen_date is approximate.
CREATE TABLE IF NOT EXISTS citizen_name_history (
    user_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    first_seen_date TEXT NOT NULL,  -- YYYY-MM-DD
    PRIMARY KEY (user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_citizen_name_history_user ON citizen_name_history(user_id);
CREATE INDEX IF NOT EXISTS idx_citizen_name_history_name ON citizen_name_history(name COLLATE NOCASE);

-- ── Company census (hourly, all countries × all items) ────────────────────────
-- Written by services/full_fetcher.py, which sweeps every company in the game
-- once an hour.  A company counts towards a country when the region it sits in
-- is currently controlled by that country.  Read by the Nigeria bot's
-- /papierfabrieken command (and any future per-country/per-item command) so the
-- command answers instantly instead of re-scanning ~73k companies on demand.
CREATE TABLE IF NOT EXISTS company_census (
    captured_at   TEXT NOT NULL,     -- ISO-8601 UTC timestamp of the sweep
    country_id    TEXT NOT NULL,
    item_code     TEXT NOT NULL,
    company_count INTEGER NOT NULL DEFAULT 0,
    worker_count  INTEGER NOT NULL DEFAULT 0,
    -- How many of company_count have at least one worker. Cannot be derived
    -- from the two columns above, so it is counted during the sweep.
    staffed_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (captured_at, country_id, item_code)
);
CREATE INDEX IF NOT EXISTS idx_company_census_lookup
    ON company_census(country_id, item_code, captured_at DESC);

-- One row per sweep, so a command can report how many companies were actually
-- checked (details fetched) versus how many the API listed.  The two differ
-- because companies get sold/destroyed between the paginate and detail phases.
CREATE TABLE IF NOT EXISTS company_census_runs (
    captured_at       TEXT PRIMARY KEY,  -- matches company_census.captured_at
    listed_companies  INTEGER NOT NULL DEFAULT 0,
    checked_companies INTEGER NOT NULL DEFAULT 0,
    duration_ms       INTEGER
);

-- ── Company owners (latest sweep only) ────────────────────────────────────────
-- Written by services/full_fetcher.py from the same company.getById responses
-- the census already fetches, so it costs no extra API calls.  Unlike
-- company_census this keeps only the newest sweep: it answers "who owns the
-- factories right now", and per-owner history at ~47k rows/hour is not worth
-- the space.  Each sweep inserts its rows and then deletes every older
-- captured_at, so a reader always sees one complete snapshot.
CREATE TABLE IF NOT EXISTS company_owners (
    captured_at   TEXT NOT NULL,
    country_id    TEXT NOT NULL,
    item_code     TEXT NOT NULL,
    owner_id      TEXT NOT NULL,
    company_count INTEGER NOT NULL DEFAULT 0,
    worker_count  INTEGER NOT NULL DEFAULT 0,
    staffed_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (captured_at, country_id, item_code, owner_id)
);
CREATE INDEX IF NOT EXISTS idx_company_owners_pair
    ON company_owners(country_id, item_code, company_count DESC);
CREATE INDEX IF NOT EXISTS idx_company_owners_item
    ON company_owners(item_code, company_count DESC);

-- ── Wage income-tax tracking ──────────────────────────────────────────────────
-- When a worker is paid, the employer pays income tax to the country the
-- company sits in.  Wage transactions carry no company or item, only the
-- worker (sellerId) and employer (buyerId), so attribution goes:
--     wage.sellerId → worker_company_map → company → (country, item)
-- Populated by services/full_fetcher.py; read by /fabrieken.

-- Worker → company lookup, refreshed every sweep from worker.getWorkers.
-- Rows are upserted rather than wiped so a worker who has since left their job
-- can still resolve the wages they earned before leaving.
CREATE TABLE IF NOT EXISTS worker_company_map (
    worker_id  TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    country_id TEXT NOT NULL,
    item_code  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Latest income-tax percentage per country, snapshotted each sweep because the
-- rate can change and past days must keep the rate that was actually applied.
CREATE TABLE IF NOT EXISTS country_tax_rates (
    country_id TEXT PRIMARY KEY,
    income_tax REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- Company -> owner map (latest known owner per company), keyed by company_id
-- so company_tax_revenue rows (which carry only a company_id) can be traced
-- back to the owner's nationality. Built from the same company.getById
-- responses the census phase already reads, so it costs no extra API calls;
-- populated for every company seen, not just staffed ones. Read by the
-- Nigeria bot's /tax-breakdown command.
CREATE TABLE IF NOT EXISTS company_owner_map (
    company_id TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL,
    country_id TEXT NOT NULL,
    item_code  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_company_owner_map_owner ON company_owner_map(owner_id);

-- Tax revenue aggregated per day per (country, item, company).  Daily buckets
-- keep this bounded at roughly one row per staffed company per day (~10k),
-- instead of one row per wage transaction (~209k/day).
CREATE TABLE IF NOT EXISTS company_tax_revenue (
    day        TEXT NOT NULL,   -- YYYY-MM-DD, UTC, from the transaction's createdAt
    country_id TEXT NOT NULL,
    item_code  TEXT NOT NULL,
    company_id TEXT NOT NULL,
    tax_total  REAL NOT NULL DEFAULT 0,
    wage_total REAL NOT NULL DEFAULT 0,
    tx_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, country_id, item_code, company_id)
);
CREATE INDEX IF NOT EXISTS idx_company_tax_lookup
    ON company_tax_revenue(country_id, item_code, day);
CREATE INDEX IF NOT EXISTS idx_company_tax_day ON company_tax_revenue(day);

-- ── Damage projection (alliances) ──────────────────────────────────────────────
-- Feeds the Nigeria bot's /damage-projection command. Written by
-- services/full_fetcher.py (alliance list) and services/citizen_cache.py (combat
-- state, piggybacked on the hourly citizen sweep that already runs for every
-- country in the game).

-- Alliance -> member-country mapping. Wiped and rewritten whole each sweep
-- (~133 rows total across ~12 alliances), so membership changes are reflected
-- immediately rather than accumulating stale rows.
CREATE TABLE IF NOT EXISTS alliance_countries (
    alliance_id   TEXT NOT NULL,
    alliance_name TEXT NOT NULL,
    country_id    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (alliance_id, country_id)
);
CREATE INDEX IF NOT EXISTS idx_alliance_countries_country ON alliance_countries(country_id);

-- Current health/hunger per citizen. Kept separate from citizen_levels (rather
-- than adding columns there) because it's rewritten wholesale every sweep and
-- has different callers than the profile fields in citizen_levels.
CREATE TABLE IF NOT EXISTS citizen_combat_state (
    user_id     TEXT PRIMARY KEY,
    country_id  TEXT NOT NULL,
    health_cur  REAL,
    health_max  REAL,
    hunger_cur  REAL,
    hunger_max  REAL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_citizen_combat_state_country ON citizen_combat_state(country_id);

-- ── Extension (WarEra Ops) auth sessions ───────────────────────────────────────
-- One row per logged-in browser install. Backs the extension's whitelist-gated
-- Discord-OAuth login (see rijksoverheid_web/app/routers/extension_auth.py).
--
-- The refresh token handed to the extension is "{id}.{secret}" — id is this
-- row's primary key (O(1) lookup), secret is a high-entropy random string we
-- only ever store hashed (refresh_token_hash = sha256(secret)), same as a
-- password. Refresh tokens are single-use and rotated on every refresh; the
-- immediately-previous hash is kept in prev_token_hash for a short grace
-- window so a *reused* (i.e. shared/copied) token can be detected — if it's
-- presented again after rotation, that's proof two parties had a copy, and
-- the whole session is revoked, forcing a fresh Discord login.
--
-- Access tokens are NOT stored here — they're short-lived, stateless,
-- HMAC-signed tokens (see services.py: mint_access_token/verify_access_token)
-- verified without a DB hit on every API request.
CREATE TABLE IF NOT EXISTS extension_sessions (
    id                  TEXT PRIMARY KEY,   -- session id, also the refresh token's id prefix
    user_id             TEXT NOT NULL,      -- Discord snowflake
    username            TEXT NOT NULL,      -- cached Discord username, for admin visibility only
    refresh_token_hash  TEXT NOT NULL,      -- sha256 hex of the current valid refresh secret
    prev_token_hash     TEXT,               -- sha256 hex of the just-rotated-away secret (reuse detection)
    prev_token_expires_at TEXT,             -- grace-window deadline for prev_token_hash
    created_at          TEXT NOT NULL,
    last_used_at        TEXT NOT NULL,
    expires_at          TEXT NOT NULL,      -- sliding expiry, extended on every successful refresh
    revoked             INTEGER NOT NULL DEFAULT 0,
    user_agent          TEXT                -- best-effort device label for admin visibility
);
CREATE INDEX IF NOT EXISTS idx_extension_sessions_user ON extension_sessions(user_id);


-- ── Region status (base/bunker upgrades + resistance) ───────────────────────
-- Written hourly by services/full_fetcher.py:fetch_region_status(), read by
-- the extension's whitelisted-only /api/ext/regions/* endpoints (see
-- rijksoverheid_web/app/routers/extension_regions.py). Regions are a small,
-- fixed set (~726) that never appear/disappear mid-sweep, so — like
-- alliance_countries — each sweep deletes and reinserts whole rather than
-- upserting per-row; only the latest snapshot is kept, no history.
CREATE TABLE IF NOT EXISTS region_upgrade_status (
    region_id         TEXT NOT NULL,
    upgrade_type      TEXT NOT NULL,   -- 'base' or 'bunker'
    status            TEXT NOT NULL,   -- 'active' | 'pending' | 'disabled'
    level             INTEGER NOT NULL DEFAULT 0,
    will_be_active_at TEXT,            -- ISO-8601; only meaningful while status='pending'
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (region_id, upgrade_type)
);

CREATE TABLE IF NOT EXISTS region_resistance (
    region_id      TEXT PRIMARY KEY,
    resistance     REAL NOT NULL DEFAULT 0,
    resistance_max REAL NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);

-- ── Proxy/puppet-country status ─────────────────────────────────────────────
-- Written periodically by services/full_fetcher.py:fetch_country_proxy_status()
-- from a third-party detection service (PROXY_API_URL/PROXY_API_KEY, see .env)
-- — read by the extension's whitelisted-only /api/ext/countries/proxy endpoint
-- (see rijksoverheid_web/app/routers/extension_countries.py). Only countries
-- CURRENTLY flagged as a proxy get a row — like region_upgrade_status, each
-- sweep deletes and reinserts whole, so a country that stops being a proxy
-- simply disappears rather than lingering with a stale row.
CREATE TABLE IF NOT EXISTS country_proxy_status (
    country_id TEXT PRIMARY KEY,
    origin_id  TEXT NOT NULL,   -- country the majority of immigrants came from
    rate       REAL NOT NULL DEFAULT 0,  -- fraction (0..1) of citizens who are immigrants at all
    updated_at TEXT NOT NULL
);
