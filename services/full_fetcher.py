"""Standalone hourly all-countries data fetcher.

Runs as a dedicated process (separate from the Discord bot and the website)
that wakes up every hour, performs a complete sweep of API data for every
country, and writes the results into the shared SQLite database.

Why a separate container?
    * Keeps the Discord bot's task loop lean (and its crashes out of the
      data pipeline).
    * Lets us tune the sweep cadence independently.
    * Single, predictable owner of the API rate budget for full sweeps.

What is fetched, in order, every hour:
    1. ``country.getAllCountries`` → master country list (writes
       ``country_snapshots`` with the latest production_bonus / specialized_item).
    2. For each country: every citizen's ``user.getUserLite`` →
       ``citizen_levels`` (via :class:`services.citizen_cache.CitizenCache`).
    3. A census of every company in the game → ``company_census``, bucketed by
       the country controlling its region and by the item it produces. The
       same pass also writes ``company_owner_map`` (company → owner, for
       every company seen), which is what lets tax revenue later be traced
       back to the owner's nationality.
    4. New wage transactions → ``company_tax_revenue``, the income tax paid to
       each country per company and item.
    5. Every alliance and its member countries → ``alliance_countries``, for
       the Nigeria bot's /damage-projection command. Health/hunger for that
       same command is captured as a side effect of step 2's citizen sweep
       (see ``citizen_combat_state`` in services/citizen_cache.py).
    6. Company owners missing from step 2 → backfilled by ID. Step 2 only
       learns citizen IDs via ``user.getUsersByCountry``, which silently
       omits inactive players (``isActive: false``) — see
       ``fetch_missing_owner_citizenships`` for how this was found and why a
       direct-by-ID fetch is the only way to close the gap.
    7. Every region's resistance (``region.getAll``) and base/bunker upgrade
       status (``upgrade.getUpgradeByTypeAndEntity``) → ``region_resistance``
       / ``region_upgrade_status``, for the extension's whitelisted
       ``/api/ext/regions/*`` endpoints (see ``fetch_region_status``).
    8. Proxy/puppet-country status from a third-party detection API (NOT
       WarEra's own) → ``country_proxy_status``, for the extension's
       whitelisted ``/api/ext/countries/proxy`` endpoint (see
       ``fetch_country_proxy_status``). No-op unless PROXY_API_URL/
       PROXY_API_KEY are configured.

Each sweep step records progress through
:meth:`services.db.Database.mark_started` / :meth:`mark_finished` under
descriptive dataset keys (``"all_countries.snapshots"``,
``"all_countries.citizens"``) so the website's /admin and /paraatheid pages
can show freshness.

Configuration via environment:
    FULL_FETCH_INTERVAL_MINUTES (default 60)
    FULL_FETCH_RUN_AT_STARTUP   (default 1; set to 0 to wait for the first
                                 interval before doing any work)
    FULL_FETCH_COUNTRY_LIMIT    (default 0 = unlimited; useful for smoke tests)
    RW_DB_PATH, RW_API_BASE_URL, RW_API_KEYS_PATH — same as the website.
    PROXY_API_URL, PROXY_API_KEY — third-party proxy-country detection
                                 service (step 8); step is skipped if unset.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

from services.api_client import APIClient
from services.citizen_cache import CitizenCache
from services.country_utils import country_id as cid_of
from services.country_utils import extract_country_list
from services.db import Database
from services.db.company_census import CENSUS_RETENTION_DAYS
from services.db.company_tax import (
    TAX_RETENTION_DAYS,
    WORKER_MAP_RETENTION_DAYS,
)
from services.game_time import game_day, game_week_start

logger = logging.getLogger("full_fetcher")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


# Company census tuning. 100 is the API's max page size and the tRPC batch
# limit; at ~73k companies that is ~730 pagination calls plus ~730 batched
# detail calls, which completes in roughly two minutes.
_COMPANY_CENSUS_BATCH = 100
_COMPANY_CENSUS_MAX_PAGES = 2000  # safety cap: 2000 × 100 = 200k companies

# Wage-tax tuning. ~8,700 wage transactions an hour is ~87 pages, so 600 pages
# absorbs roughly seven hours of fetcher downtime before a gap is reported.
_WAGE_TAX_MAX_PAGES = 600

# Region status tuning. ~726 regions × 2 upgrade types (base, bunker) batched
# 100 per request is ~15 requests per type — cheap next to company_census.
_REGION_STATUS_BATCH = 100


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _seconds_until_next_aligned_run(minute_offset: int, min_gap_s: int = 300) -> float:
    """Return seconds to sleep until the next HH:{minute_offset:02d}:00 UTC.

    ``min_gap_s`` ensures we don't re-trigger immediately after a run that
    finished just before the target minute (default 5-minute guard).
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    candidate = now.replace(minute=minute_offset, second=0, microsecond=0)
    if candidate <= now + timedelta(seconds=min_gap_s):
        candidate += timedelta(hours=1)
    return (candidate - now).total_seconds()


def _load_api_keys(path: str) -> list[str]:
    """Same shape as the website's loader, but inlined to keep this script
    runnable without importing the website package (which depends on
    fastapi etc.).

    Falls back to the shared _api_keys.json when a dedicated per-service key
    file (see docker-compose.data-fetcher.yml) hasn't been set up on this
    machine yet — e.g. a standalone deployment that only runs this fetcher.
    """
    if not os.path.isfile(path):
        fallback = "_api_keys.json"
        if path != fallback and os.path.isfile(fallback):
            logger.warning("API keys file %s not found — falling back to %s", path, fallback)
            path = fallback
        else:
            logger.warning("API keys file not found at %s — running keyless", path)
            return []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:  # noqa: BLE001 — we want to log and continue
        logger.exception("Failed to load API keys from %s", path)
        return []
    if isinstance(data, list):
        return [str(k) for k in data if k]
    if isinstance(data, dict):
        keys = data.get("keys") or list(data.values())
        return [str(k) for k in keys if k]
    return []


# ── one-shot sweep steps ──────────────────────────────────────────────────────


async def fetch_country_snapshots(client: APIClient, db: Database) -> tuple[int, list[dict]]:
    """Fetch the master country list and upsert into ``country_snapshots``.

    Returns ``(count_written, country_list)`` where ``country_list`` is the
    canonical list used by the next steps.
    """
    dataset = "all_countries.snapshots"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        resp = await client.get("/country.getAllCountries")
        countries = extract_country_list(resp)
        if not countries:
            raise RuntimeError("getAllCountries returned no countries")

        updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        written = 0
        for c in countries:
            cid = cid_of(c)
            if not cid:
                continue
            await db.save_country_snapshot(
                country_id=cid,
                code=c.get("code"),
                name=c.get("name"),
                specialized_item=c.get("specializedItem") or c.get("specialized_item"),
                production_bonus=_to_float(c.get("productionBonus") or c.get("production_bonus")),
                raw_json=json.dumps(c, ensure_ascii=False),
                updated_at=updated_at,
            )
            written += 1
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info("country_snapshots: wrote %d rows (%.1fs)", written, duration_ms / 1000)
        return written, countries
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms)
        logger.exception("country_snapshots failed")
        return 0, []


async def fetch_alliance_countries(client: APIClient, db: Database) -> int:
    """Fetch every alliance and its member countries → ``alliance_countries``.

    ``alliance.getManyPaginated`` already embeds ``memberCountries`` on each
    alliance, so ``alliance.getById`` is never needed. The game currently has
    ~12 alliances (~133 alliance-country pairs total), so this is one cheap
    request per sweep — verified live that ``page`` is not honoured by the API
    (page 2 returns the same items as page 1), so a single call with a
    generous ``limit`` is relied on rather than looping pages; if the item
    count ever comes back equal to the limit, that's logged as a sign the
    result may be truncated.
    """
    dataset = "all_countries.alliances"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        limit = 100  # API hard-caps this at 100 (verified: 200 → 400 Bad Request)
        raw = await client.post(
            "/alliance.getManyPaginated", json={"page": 1, "limit": limit}
        )
        data = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("getManyPaginated returned no items")
        if len(items) >= limit:
            logger.warning(
                "alliance_countries: got %d items == limit %d — "
                "the alliance list may be truncated", len(items), limit,
            )

        now = datetime.now(timezone.utc).isoformat()
        rows: list[tuple[str, str, str, bool]] = []
        for alliance in items:
            if not isinstance(alliance, dict):
                continue
            aid = str(alliance.get("_id") or "")
            name = str(alliance.get("name") or aid)
            if not aid:
                continue
            for member in alliance.get("memberCountries") or []:
                if not isinstance(member, dict):
                    continue
                cid = member.get("country")
                if cid:
                    rows.append((aid, name, str(cid), bool(member.get("suspended"))))

        written = await db.save_alliance_countries(rows, now)
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "alliance_countries: %d alliances, %d alliance-country pairs (%.1fs)",
            len(items), written, duration_ms / 1000,
        )
        return written
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("alliance_countries sweep failed")
        return 0


# Both base and bunker upgrades cool down for upgrade.regionUpgradeCooldownHours
# (gameConfig: 8). A ruling party's militarism ethic used to shorten the base
# cooldown to 4h / 2h; party ethics were removed from the game, so it is now
# the same 8h everywhere.
_REGION_UPGRADE_COOLDOWN_HOURS = 8.0


async def fetch_region_status(client: APIClient, db: Database) -> tuple[int, int]:
    """Sweep every region's resistance and base/bunker upgrade status.

    Powers the extension's whitelisted-only /api/ext/regions/* endpoints
    (bases, bunkers, resistance) — see rijksoverheid_web/app/routers/
    extension_regions.py. ``region.getAll`` gives resistance/resistanceMax
    for free in the same response that lists every region, but its embedded
    ``upgradesV2.upgrades.{base,bunker}`` has no ``willBeActiveAt`` and is
    missing entirely for regions that have never built one. The authoritative
    per-region ``upgrade.getUpgradeByTypeAndEntity`` is used instead for
    upgrade status. A region that has never had a base/bunker built at all
    (never even started, not even to level 0) returns a 404 ("Upgrades not
    found") rather than a default record — confirmed live, and NOT rare
    (roughly a third of regions for "base", a seventh for "bunker" at time of
    writing) — so that's treated the same as an explicit disabled/level 0
    rather than silently dropping the region from the output.

    Also captures each upgrade's ``lastUpgradeAt`` plus the resolved cooldown
    length (``_REGION_UPGRADE_COOLDOWN_HOURS``), so the extension
    can show "still on cooldown" independent of active/pending/disabled
    status — the cooldown blocks starting a new upgrade *or* downgrade
    regardless of the upgrade's current state.

    Returns ``(resistance_rows_written, upgrade_rows_written)``.
    """
    dataset = "all_countries.region_status"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        raw = await client.get("/region.getAll")
        regions = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
        if not isinstance(regions, list) or not regions:
            raise RuntimeError("region.getAll returned no regions")

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        region_ids = [str(r.get("_id")) for r in regions if r.get("_id")]
        resistance_rows = [
            (str(r.get("_id")), _to_float(r.get("resistance")) or 0.0,
             _to_float(r.get("resistanceMax")) or 0.0)
            for r in regions if r.get("_id")
        ]
        res_written = await db.save_region_resistance(resistance_rows, now_iso)

        upgrade_written: dict[str, int] = {}
        for upgrade_type in ("base", "bunker"):
            inputs = [{"regionId": rid, "upgradeType": upgrade_type} for rid in region_ids]
            results = await client.batch_get(
                "/upgrade.getUpgradeByTypeAndEntity", inputs, batch_size=_REGION_STATUS_BATCH,
            )
            rows: list[tuple[str, str, int, str | None, str | None, float | None]] = []
            for rid, raw_up in zip(region_ids, results):
                up = _unwrap_trpc(raw_up) if isinstance(raw_up, dict) else raw_up
                if isinstance(up, dict):
                    status = str(up.get("status") or "disabled")
                    try:
                        level = int(up.get("level") or 0)
                    except (TypeError, ValueError):
                        level = 0
                    will_be_active_at = up.get("willBeActiveAt")
                    last_upgrade_at = up.get("lastUpgradeAt")
                else:
                    # 404 ("Upgrades not found") for a region that's never had
                    # this upgrade built at all — batch_get unwraps the error
                    # response to None. Same meaning as an explicit disabled/
                    # level 0, not a fetch failure to skip.
                    status, level, will_be_active_at, last_upgrade_at = "disabled", 0, None, None
                rows.append((
                    rid, status, level, will_be_active_at, last_upgrade_at,
                    _REGION_UPGRADE_COOLDOWN_HOURS,
                ))
            upgrade_written[upgrade_type] = await db.save_region_upgrade_status(
                upgrade_type, rows, now_iso
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "region_status: %d regions, resistance=%d base=%d bunker=%d (%.1fs)",
            len(region_ids), res_written, upgrade_written.get("base", 0),
            upgrade_written.get("bunker", 0), duration_ms / 1000,
        )
        return res_written, sum(upgrade_written.values())
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("region_status sweep failed")
        return 0, 0


# Timeout for the proxy-detection API — a single call already returns every
# country (~180) in one response (confirmed live), so this doesn't need
# batching/pagination like the WarEra API steps above. Observed live latency
# varies wildly (a few seconds warm, ~60s+ cold) — this runs in a background
# hourly sweep with nobody waiting synchronously on it, so a generous timeout
# costs nothing and a too-tight one would just make the step flaky.
_PROXY_API_TIMEOUT_S = 120.0


async def fetch_country_proxy_status(db: Database) -> int:
    """Sweep proxy/puppet-country status from the third-party detection API.

    Powers the extension's whitelisted-only /api/ext/countries/proxy endpoint
    (see rijksoverheid_web/app/routers/extension_countries.py). Unlike every
    other sweep step here, this doesn't touch WarEra's own API at all — it's a
    single authenticated GET against PROXY_API_URL (PROXY_API_KEY in .env),
    which returns EVERY country's status in one response, keyed by country id,
    each entry shaped like ``{country_id, total, immigrants, origins, rate,
    is_proxy}`` where ``origins`` is a ``[[countryId, name, count], ...]`` list
    already sorted by ``count`` descending (confirmed live) — so the "origin"
    a proxy country answers to is simply ``origins[0][0]``, no local ranking
    needed. Only entries with ``is_proxy`` true, and a resolvable top origin,
    are kept — see country_proxy_status's schema comment for why this is a
    whole-table replace rather than an upsert.

    Silently returns 0 (no sweep) if PROXY_API_URL/PROXY_API_KEY aren't
    configured, so this step is a no-op on any deployment that hasn't opted
    into it — same posture as the other optional FULL_FETCH_ENABLE_* steps.
    """
    api_url = os.getenv("PROXY_API_URL", "").rstrip("/")
    api_key = os.getenv("PROXY_API_KEY", "")
    if not api_url or not api_key:
        logger.info("country_proxy: PROXY_API_URL/PROXY_API_KEY not set, skipping")
        return 0

    dataset = "all_countries.country_proxy"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_PROXY_API_TIMEOUT_S) as http:
            resp = await http.get(
                f"{api_url}/countries/proxy", headers={"X-API-Key": api_key}
            )
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("countries/proxy returned an unexpected shape")

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows: list[tuple[str, str, float]] = []
        for country_id, entry in data.items():
            if not isinstance(entry, dict) or not entry.get("is_proxy"):
                continue
            origins = entry.get("origins") or []
            if not origins or not origins[0]:
                continue  # flagged a proxy but no attributable origin — nothing to color it as
            origin_id = origins[0][0]
            if not origin_id:
                continue
            rows.append((str(country_id), str(origin_id), float(entry.get("rate") or 0.0)))

        written = await db.save_country_proxy_status(rows, now_iso)
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "country_proxy: %d countries checked, %d proxies written (%.1fs)",
            len(data), written, duration_ms / 1000,
        )
        return written
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("country_proxy sweep failed")
        return 0


async def fetch_all_citizen_levels(
    cache: CitizenCache,
    db: Database,
    countries: list[dict],
    *,
    limit: int = 0,
) -> int:
    """Refresh ``citizen_levels`` for every country in ``countries``.

    ``limit`` > 0 caps the number of countries (smoke-test convenience).
    Returns the total number of citizens recorded.
    """
    dataset = "all_countries.citizens"
    await db.mark_started(dataset, source="full_fetcher")
    # Stamp the same key the discord bot checks so it skips its own sweep while
    # this one is (or was recently) running.
    await db.set_poll_state(
        "citizen_refresh_last_run",
        datetime.now(timezone.utc).isoformat(),
    )
    started = time.monotonic()
    total = 0
    if limit > 0:
        countries = countries[:limit]
    failed: list[str] = []
    for c in countries:
        cid = cid_of(c)
        name = str(c.get("name") or cid or "?")
        if not cid:
            continue
        try:
            t0 = time.monotonic()
            n = await cache.refresh_country(cid, name)
            total += n
            logger.info(
                "citizens %-20s wrote=%-5d (%.1fs)",
                name, n, time.monotonic() - t0,
            )
        except Exception:  # noqa: BLE001 — keep sweep alive
            logger.exception("citizen refresh failed for %s (%s)", name, cid)
            failed.append(name)

    duration_ms = int((time.monotonic() - started) * 1000)
    if failed and len(failed) == len(countries):
        await db.mark_finished(
            dataset, status="error",
            error=f"all {len(failed)} countries failed",
            duration_ms=duration_ms,
        )
    else:
        await db.mark_finished(
            dataset, status="ok",
            error=(f"{len(failed)} country failures: {','.join(failed[:5])}"
                   if failed else None),
            duration_ms=duration_ms,
        )
    logger.info(
        "citizen sweep done: total=%d, failures=%d (%.1fs)",
        total, len(failed), duration_ms / 1000,
    )
    return total


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── extra tiers ───────────────────────────────────────────────────────────────


def _unwrap_trpc(resp):
    """Unwrap a trpc {"result":{"data":...}} envelope (one or two levels)."""
    data = resp
    if isinstance(data, dict):
        for key in ("result", "data"):
            v = data.get(key)
            if isinstance(v, dict):
                data = v.get("data", v)
                break
    return data


async def fetch_recent_trades(client: APIClient, db: Database) -> int:
    """Pull the 100 most recent itemMarket transactions and upsert."""
    dataset = "all_countries.trades"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        raw = await client.post(
            "/transaction.getPaginatedTransactions",
            json={"limit": 100, "transactionType": "itemMarket"},
        )
        data = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
        items: list = []
        if isinstance(data, dict):
            items = (
                data.get("items")
                or data.get("transactions")
                or data.get("results")
                or []
            )
        elif isinstance(data, list):
            items = data

        if not items:
            await db.mark_finished(
                dataset, status="ok",
                duration_ms=int((time.monotonic() - started) * 1000),
                error="no trades returned",
            )
            return 0

        inserted, seen = await db.upsert_trades(items)
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info("trades: %d new / %d seen (%.1fs)",
                    inserted, seen, duration_ms / 1000)
        return inserted
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("trades sweep failed")
        return 0


def _ranking_entries(resp) -> list[dict]:
    """Return the flat entry list from a ranking.getRanking response."""
    data = _unwrap_trpc(resp)
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        for key in ("items", "ranking", "rankings", "data", "results"):
            v = data.get(key)
            if isinstance(v, list):
                return [e for e in v if isinstance(e, dict)]
    return []


def _entry_user_id(entry: dict) -> str | None:
    """Extract the citizen's account id from a ranking entry.

    ``user`` references the citizen; ``_id`` is the ranking record's own id,
    so ``user`` must win when both are present.
    """
    user = entry.get("user")
    if isinstance(user, str) and user:
        return user
    if isinstance(user, dict):
        for key in ("_id", "id", "userId"):
            v = user.get(key)
            if v:
                return str(v)
    for key in ("userId", "citizenId", "id", "_id"):
        v = entry.get(key)
        if v:
            return str(v)
    return None


def _entry_username(entry: dict) -> str | None:
    for key in ("username", "name", "citizenName"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            return v
    user = entry.get("user")
    if isinstance(user, dict):
        for key in ("username", "name"):
            v = user.get(key)
            if isinstance(v, str) and v:
                return v
    return None


def _entry_damage(entry: dict) -> float:
    for key in ("value", "damage", "totalDamage", "weeklyDamage",
                "weeklyBattleDamage", "amount"):
        v = entry.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


async def fetch_weekly_damages(client: APIClient, db: Database) -> int:
    """Snapshot the global weekly-damage ranking for every player.

    ``ranking.getRanking(weeklyUserDamages)`` returns every player in the game
    in one call, so this is cheap regardless of how many countries we track.
    Each entry is tagged with the player's country/MU from ``citizen_levels``
    and handed to :meth:`Database.apply_weekly_damage_snapshot`, which writes
    the weekly history row and derives the game day's damage from the delta.
    """
    dataset = "all_countries.weekly_damage"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        resp = await client.post(
            "/ranking.getRanking", json={"rankingType": "weeklyUserDamages"}
        )
        entries = _ranking_entries(resp)
        if not entries:
            raise RuntimeError("weeklyUserDamages returned no entries")

        # country/MU per player, so history stays correct after someone moves
        meta: dict[str, tuple] = {}
        async with db._conn.execute(
            "SELECT user_id, citizen_name, country_id, mu_id, mu_name FROM citizen_levels"
        ) as cur:
            async for row in cur:
                meta[str(row[0])] = (row[1], row[2], row[3], row[4])

        payload: list[dict] = []
        seen: set[str] = set()
        for e in entries:
            uid = _entry_user_id(e)
            if not uid or uid in seen:
                continue
            seen.add(uid)
            name, country_id, mu_id, mu_name = meta.get(uid, (None, None, None, None))
            payload.append({
                "user_id": uid,
                "citizen_name": _entry_username(e) or name,
                "country_id": country_id,
                "mu_id": mu_id,
                "mu_name": mu_name,
                "weekly_damage": _entry_damage(e),
            })

        now = datetime.now(timezone.utc)
        # Let the Discord bot's own weekly-damage task know this sweep owns
        # the data now, so it doesn't duplicate the work (or the API call).
        await db.set_poll_state("weekly_damage_fetcher_last_run", now.isoformat())
        counts = await db.apply_weekly_damage_snapshot(
            payload,
            game_date=game_day(now),
            week_start=game_week_start(now),
            updated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "weekly_damage: %d players (day=%s week=%s, %d prior days closed) (%.1fs)",
            counts["weekly"], game_day(now), game_week_start(now),
            counts["closed"], duration_ms / 1000,
        )
        return counts["weekly"]
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("weekly_damage sweep failed")
        return 0


async def fetch_all_mu_names(client: APIClient, db: Database) -> int:
    """Paginate mu.getManyPaginated globally and upsert into known_mus."""
    dataset = "all_countries.mu_registry"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    total = 0
    cursor: str | None = None
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        while True:
            params: dict = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = await client.get(
                    "/mu.getManyPaginated",
                    params={"input": json.dumps(params)},
                )
            except Exception:  # noqa: BLE001
                logger.exception("mu_registry: API call failed")
                break

            data_obj = _unwrap_trpc(resp)
            items: list = []
            next_cursor: str | None = None
            if isinstance(data_obj, list):
                items = data_obj
            elif isinstance(data_obj, dict):
                for key in ("items", "mus", "data"):
                    v = data_obj.get(key)
                    if isinstance(v, list):
                        items = v
                        break
                next_cursor = data_obj.get("nextCursor") or data_obj.get("cursor")

            for item in items:
                if not isinstance(item, dict):
                    continue
                mu_id = str(item.get("_id") or item.get("id") or "").strip()
                mu_name = str(item.get("name") or item.get("title") or "").strip()
                if not (mu_id and mu_name):
                    continue
                country_id: str | None = None
                country_obj = item.get("country") or item.get("nation")
                if isinstance(country_obj, dict):
                    country_id = (
                        str(country_obj.get("_id") or country_obj.get("id") or "").strip()
                        or None
                    )
                if not country_id:
                    raw_cid = (
                        item.get("countryId")
                        or item.get("country_id")
                        or item.get("nationId")
                    )
                    if raw_cid:
                        country_id = str(raw_cid).strip() or None
                await db.upsert_known_mu(mu_id, mu_name, now_iso, country_id)
                total += 1

            if items:
                await db.flush_known_mus()
            if not next_cursor or not items:
                break
            cursor = next_cursor

        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info("mu_registry: %d MUs upserted (%.1fs)",
                    total, duration_ms / 1000)
        return total
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("mu_registry sweep failed")
        return total


async def fetch_company_census(client: APIClient, db: Database) -> int:
    """Census every company in the game, bucketed by controlling country + item.

    Three phases:
      1. Paginate ``company.getCompanies`` (no filter) for every company ID.
      1.5. Reconcile against a per-known-citizen ``company.getCompanies``
           (filtered by ``userId``) to recover companies phase 1 misses — see
           below for why that happens at all.
      2. Batch ``company.getById`` to read each company's region, itemCode and
         workerCount, mapping region → country via ``region.getRegionsObject``.

    A company belongs to the country that *currently controls* its region, so
    the mapping is re-read every sweep rather than cached.

    Phase 1's unfiltered listing was confirmed live to silently omit
    companies that unambiguously exist (via ``company.getById`` directly, and
    via the ``userId``-filtered form of the very same endpoint, which does
    return them). It appears to paginate by ``updatedAt``, and a periodic
    game-engine tick stamps huge numbers of companies with the exact same
    ``updatedAt`` down to the millisecond — precisely the condition under
    which timestamp-keyset pagination can silently skip rows between page
    fetches. Phase 1.5 closes that gap for any company whose owner this sweep
    already knows about (i.e. anyone in ``citizen_levels``), at the cost of
    one batched-per-100 request per known citizen — chosen over a cheaper
    "only known owners" variant so a company owned by someone who has never
    shown up as an owner before isn't missed on its first sweep either.

    The same responses also carry the owner ID, so a per-owner breakdown is
    written to ``company_owners`` for free — no extra requests.

    Companies listed in phase 1 can be sold or destroyed before phase 2 reads
    them, so ``checked`` is normally a little lower than ``listed``; both are
    recorded.

    A company the owner has switched off (confirmed live via
    ``company.getById``) keeps existing — it's still listed in phase 1 and
    still returns a full payload in phase 2 — but carries a ``disabledAt``
    timestamp that an active company never has. Companies owned by an
    inactive player (confirmed live via ``user.getUserLite``'s ``isActive``)
    are excluded the same way, on the theory the user gave for both: neither
    is "used anymore". Both kinds are counted in ``checked`` (their details
    were successfully fetched) but excluded from every bucket:
    ``company_census``, ``company_owners`` and ``company_owner_map`` all only
    ever see companies that were active, with an active owner, at scan time —
    so ``/fabrieken`` and ``/productie`` don't inflate their totals with dead
    companies.

    A banned *worker* gets the same treatment, one level down: a company
    counts as having a worker only if it has at least one worker who isn't
    banned (confirmed live via ``user.getUserLite``'s ``infos.isBanned`` —
    also confirmed to coexist with ``isActive: false`` on the same account,
    so a banned worker is not necessarily caught by the owner check above
    even when they are one elsewhere). This needs each staffed company's
    worker *roster*, not just its ``workerCount``, so — unlike the
    disabled/inactive-owner checks above, which only need the company payload
    itself — the worker-roster fetch (``worker.getWorkers``, already made
    below to build ``worker_company_map`` for tax attribution) now has to
    happen *before* a company's counts are finalized, not after.

    Both the owner-activity and worker-banned checks read
    ``citizen_levels.is_active`` / ``.is_banned``, populated by the citizen
    sweep this function's caller already ran earlier in the same
    ``run_once()`` pass — no extra API calls. Both can only catch citizens
    this sweep already knows about; a citizen never seen before is still
    counted once, the same one-sweep lag ``/productie`` already documents for
    citizenship attribution in general — closed by
    ``fetch_missing_owner_citizenships`` / ``fetch_missing_worker_citizenships``
    running right after this function, for the *next* sweep.

    Also writes ``region_snapshots`` (every region's id/code/name/controlling
    country, from the same ``region.getRegionsObject`` call above) and a
    ``region_id`` on each ``company_owner_map`` row, so ``/fabrieken`` can
    break a country's companies down by region — no extra API calls, since
    both were already being read to resolve each company's country.

    Returns the number of census rows written.
    """
    dataset = "all_countries.company_census"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        # ── region → controlling country ─────────────────────────────────────
        raw_regions = await client.get("/region.getRegionsObject")
        regions = _unwrap_trpc(raw_regions) if isinstance(raw_regions, dict) else raw_regions
        if not isinstance(regions, dict) or not regions:
            raise RuntimeError("getRegionsObject returned no regions")
        region_country: dict[str, str] = {}
        # (region_id, code, name, country_id) — same response, kept for
        # region_snapshots so /fabrieken can label a region breakdown by name
        # instead of a raw id, at no extra API cost.
        region_snapshot_rows: list[tuple[str, str, str, str]] = []
        for rid, robj in regions.items():
            if not isinstance(robj, dict):
                continue
            country = robj.get("country")
            if isinstance(country, dict):
                country = country.get("_id") or country.get("id")
            if country:
                region_country[str(rid)] = str(country)
            region_snapshot_rows.append((
                str(rid), robj.get("code") or "", robj.get("name") or "",
                str(country) if country else "",
            ))

        # ── phase 1: every company ID ────────────────────────────────────────
        company_ids: list[str] = []
        cursor: str | None = None
        for _page in range(_COMPANY_CENSUS_MAX_PAGES):
            payload: dict = {"perPage": 100}
            if cursor:
                payload["cursor"] = cursor
            raw = await client.get(
                "/company.getCompanies", params={"input": json.dumps(payload)}
            )
            data = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
            if not isinstance(data, dict):
                break
            page_ids = data.get("items") or []
            if not isinstance(page_ids, list):
                break
            company_ids.extend(str(i) for i in page_ids if i)
            cursor = data.get("nextCursor") or data.get("cursor")
            if not cursor or not page_ids:
                break
        if not company_ids:
            raise RuntimeError("getCompanies returned no companies")
        logger.info("company_census: listed %d companies", len(company_ids))

        # ── phase 1.5: reconcile against per-citizen listings ───────────────
        # Confirmed live: the unfiltered listing above silently omits some
        # companies that unambiguously exist — verified both via
        # company.getById directly and via this same endpoint filtered by
        # userId, which *does* return them. The likely mechanism: this
        # endpoint paginates by updatedAt, and huge numbers of companies share
        # the exact same updatedAt timestamp down to the millisecond (a
        # periodic game-engine tick appears to stamp them all at once) —
        # exactly the condition under which timestamp-keyset pagination can
        # silently skip rows between page fetches. Re-querying per known
        # citizen uses a filtered, stable query instead, closing the gap for
        # any company whose owner this sweep already knows about. A company
        # whose owner has genuinely never been seen by any sweep is still
        # missed — the same one-sweep-lag shape as the other citizen-lookup
        # gaps in this file.
        known_owner_ids: list[str] = []
        async with db._conn.execute("SELECT user_id FROM citizen_levels") as cur:
            async for row in cur:
                known_owner_ids.append(str(row[0]))

        company_id_set = set(company_ids)
        reconciled_new = 0
        truncated_owners = 0
        for start in range(0, len(known_owner_ids), _COMPANY_CENSUS_BATCH):
            chunk = known_owner_ids[start : start + _COMPANY_CENSUS_BATCH]
            results = await client.batch_get(
                "/company.getCompanies",
                [{"userId": uid, "perPage": 100} for uid in chunk],
                batch_size=_COMPANY_CENSUS_BATCH,
            )
            for raw_owned in results:
                data = (
                    _unwrap_trpc(raw_owned) if isinstance(raw_owned, dict) else raw_owned
                )
                if not isinstance(data, dict):
                    continue
                owned_ids = data.get("items") or []
                if not isinstance(owned_ids, list):
                    continue
                # A citizen owning >100 companies would need a second page we
                # don't chase — rare enough (individual players, not
                # countries) that logging it is enough.
                if data.get("nextCursor") or data.get("cursor"):
                    truncated_owners += 1
                for cid in owned_ids:
                    cid = str(cid)
                    if cid and cid not in company_id_set:
                        company_id_set.add(cid)
                        company_ids.append(cid)
                        reconciled_new += 1

        if reconciled_new or truncated_owners:
            logger.info(
                "company_census: reconciliation found %d companies the "
                "unfiltered listing missed (%d owners truncated at 100 "
                "companies, not chased further)",
                reconciled_new, truncated_owners,
            )

        # {user_id: is_active} / {user_id: is_banned} for every citizen this
        # sweep already knows about (populated by the citizen sweep that ran
        # earlier this same run_once() pass, plus whatever inactive/banned
        # citizens survived from past sweeps now that prune_stale_citizens
        # leaves them alone) — read once up front so checking activity/ban
        # status below costs no API call.
        owner_active: dict[str, bool] = {}
        worker_banned: dict[str, bool] = {}
        async with db._conn.execute(
            "SELECT user_id, is_active, is_banned FROM citizen_levels"
        ) as cur:
            async for row in cur:
                uid = str(row[0])
                owner_active[uid] = bool(row[1])
                worker_banned[uid] = bool(row[2])

        # ── phase 2: details, in batches ─────────────────────────────────────
        # One entry per company that survives the disabled/inactive-owner
        # checks. Counts aren't bucketed yet — a banned-worker exclusion needs
        # each staffed company's worker roster first (see below), which the
        # simple workerCount field on this payload can't provide.
        active_companies: list[dict] = []
        checked = 0
        disabled = 0
        inactive_owner = 0
        for start in range(0, len(company_ids), _COMPANY_CENSUS_BATCH):
            chunk = company_ids[start : start + _COMPANY_CENSUS_BATCH]
            results = await client.batch_get(
                "/company.getById",
                [{"companyId": cid} for cid in chunk],
                batch_size=_COMPANY_CENSUS_BATCH,
            )
            for raw_company in results:
                company = (
                    _unwrap_trpc(raw_company)
                    if isinstance(raw_company, dict)
                    else raw_company
                )
                if not isinstance(company, dict):
                    continue  # deleted between phases, or a failed lookup
                checked += 1
                # A company the owner has switched off still exists (and is
                # still returned by getById) but produces nothing and pays no
                # wages — it only carries a "disabledAt" timestamp, present on
                # no other company. Counting it would inflate every /fabrieken
                # and /productie total with dead companies.
                if company.get("disabledAt"):
                    disabled += 1
                    continue
                region_id = str(company.get("region") or "")
                country_id = region_country.get(region_id)
                item_code = str(company.get("itemCode") or "")
                if not country_id or not item_code:
                    continue
                owner_id = str(company.get("user") or "")
                # A company owned by an inactive player is just as "not used
                # anymore" as a disabled one — same exclusion, same reasoning.
                # owner_active.get(...) is False only for owners this sweep
                # positively knows are inactive; unknown owners (None) are
                # counted, same one-sweep-lag tradeoff as elsewhere.
                if owner_id and owner_active.get(owner_id) is False:
                    inactive_owner += 1
                    continue
                try:
                    raw_workers = int(company.get("workerCount") or 0)
                except (TypeError, ValueError):
                    raw_workers = 0

                active_companies.append({
                    "country_id": country_id,
                    "item_code": item_code,
                    "owner_id": owner_id,
                    "company_id": str(company.get("_id") or ""),
                    "region_id": region_id,
                    "raw_workers": raw_workers,
                })

        # Worker rosters — needed both to build worker_company_map (tax
        # attribution) and, new here, to know which of a company's workers are
        # banned so the census can exclude them from its counts. Only
        # companies workerCount already says are staffed need a roster call.
        # Batched 100 per request, so ~10k staffed companies costs ~100
        # requests — unchanged from before this only fed tax attribution.
        roster_candidates = [
            c for c in active_companies if c["raw_workers"] > 0 and c["company_id"]
        ]
        worker_rows: list[tuple[str, str, str, str]] = []
        # {company_id: non-banned worker count}. A company absent from this
        # dict either had zero raw workers (falls back to raw_workers = 0
        # below, same result) or its roster fetch failed (falls back to
        # raw_workers too, so a transient API error under-fetches rather than
        # silently zeroing out a real company's worker count).
        effective_workers: dict[str, int] = {}
        banned_worker_hits = 0
        for start in range(0, len(roster_candidates), _COMPANY_CENSUS_BATCH):
            chunk = roster_candidates[start : start + _COMPANY_CENSUS_BATCH]
            results = await client.batch_get(
                "/worker.getWorkers",
                [{"companyId": c["company_id"]} for c in chunk],
                batch_size=_COMPANY_CENSUS_BATCH,
            )
            for c, raw_workers_resp in zip(chunk, results):
                data = (
                    _unwrap_trpc(raw_workers_resp)
                    if isinstance(raw_workers_resp, dict)
                    else raw_workers_resp
                )
                if not isinstance(data, dict):
                    continue
                non_banned = 0
                for w in data.get("workers") or []:
                    if not isinstance(w, dict):
                        continue
                    worker_id = str(w.get("user") or "")
                    if not worker_id:
                        continue
                    # worker_company_map keeps every worker regardless of ban
                    # status — wage-tax attribution is unaffected by this.
                    worker_rows.append(
                        (worker_id, c["company_id"], c["country_id"], c["item_code"])
                    )
                    if worker_banned.get(worker_id) is True:
                        banned_worker_hits += 1
                        continue
                    non_banned += 1
                effective_workers[c["company_id"]] = non_banned

        # ── bucket census counts, now that banned-worker-adjusted counts are
        # known for every staffed company ─────────────────────────────────
        counts: dict[tuple[str, str], list[int]] = {}
        owner_counts: dict[tuple[str, str, str], list[int]] = {}
        owner_map_rows: list[tuple[str, str, str, str, str]] = []
        for c in active_companies:
            workers = effective_workers.get(c["company_id"], c["raw_workers"])
            staffed = 1 if workers > 0 else 0

            bucket = counts.setdefault((c["country_id"], c["item_code"]), [0, 0, 0])
            bucket[0] += 1
            bucket[1] += workers
            bucket[2] += staffed

            if c["owner_id"]:
                obucket = owner_counts.setdefault(
                    (c["country_id"], c["item_code"], c["owner_id"]), [0, 0, 0]
                )
                obucket[0] += 1
                obucket[1] += workers
                obucket[2] += staffed
                if c["company_id"]:
                    owner_map_rows.append((
                        c["company_id"], c["owner_id"], c["country_id"],
                        c["item_code"], c["region_id"],
                    ))

        captured_at = datetime.now(timezone.utc).isoformat()
        duration_ms = int((time.monotonic() - started) * 1000)
        written = await db.save_company_census(
            captured_at,
            [(c, i, v[0], v[1], v[2]) for (c, i), v in counts.items()],
            listed_companies=len(company_ids),
            checked_companies=checked,
            duration_ms=duration_ms,
        )

        mapped = await db.save_worker_company_map(worker_rows, captured_at)
        worker_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=WORKER_MAP_RETENTION_DAYS)
        ).isoformat()
        await db.prune_worker_company_map(worker_cutoff)

        owner_rows = await db.save_company_owners(
            captured_at,
            [(c, i, o, v[0], v[1], v[2]) for (c, i, o), v in owner_counts.items()],
        )

        owner_map_written = await db.save_company_owner_map(owner_map_rows, captured_at)
        owner_map_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=TAX_RETENTION_DAYS)
        ).isoformat()
        pruned_owner_map = await db.prune_company_owner_map(owner_map_cutoff)

        # Every region, not just ones with companies — costs nothing extra,
        # since region.getRegionsObject above already returned all of them.
        region_snapshots_written = await db.save_region_snapshots(
            region_snapshot_rows, captured_at
        )

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=CENSUS_RETENTION_DAYS)
        ).isoformat()
        pruned = await db.prune_company_census(cutoff)

        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "company_census: listed=%d reconciled=%d checked=%d disabled=%d "
            "inactive_owner=%d banned_worker_hits=%d rows=%d owners=%d "
            "workers=%d owner_map=%d regions=%d pruned=%d pruned_owner_map=%d (%.1fs)",
            len(company_ids), reconciled_new, checked, disabled, inactive_owner,
            banned_worker_hits, written, owner_rows, mapped, owner_map_written,
            region_snapshots_written,
            pruned, pruned_owner_map, duration_ms / 1000,
        )
        return written
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("company_census sweep failed")
        return 0


async def fetch_missing_owner_citizenships(
    cache: CitizenCache, db: Database
) -> int:
    """Backfill ``citizen_levels`` for company owners the citizen sweep missed.

    ``user.getUsersByCountry`` — the endpoint the regular citizen sweep
    (:func:`fetch_all_citizen_levels`) uses to enumerate each country's
    citizens — was confirmed live to silently omit inactive players
    (``isActive: false``). Their profile is still fully live and fetchable by
    ID; a country-by-country sweep just never learns those IDs exist. Since
    ``company_owners`` (just written by :func:`fetch_company_census`) already
    has the *exact* set of owner IDs that matter, any of them missing from
    ``citizen_levels`` is fetched directly by ID here — no discovery needed,
    no dependence on the broken listing endpoint.

    This is what makes ``/productie`` on the Nigeria bot able to attribute a
    company to its owner's nationality even when the owner is inactive.
    """
    dataset = "all_countries.owner_citizenships"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        async with db._conn.execute(
            "SELECT DISTINCT co.owner_id FROM company_owners co "
            "LEFT JOIN citizen_levels cl ON cl.user_id = co.owner_id "
            "WHERE cl.user_id IS NULL"
        ) as cur:
            missing = [str(r[0]) for r in await cur.fetchall()]

        recorded = await cache.refresh_specific_users(missing)

        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "owner_citizenships: %d owners missing, %d recorded (%.1fs)",
            len(missing), recorded, duration_ms / 1000,
        )
        return recorded
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("owner_citizenships backfill failed")
        return 0


async def fetch_missing_worker_citizenships(
    cache: CitizenCache, db: Database
) -> int:
    """Backfill ``citizen_levels`` for company workers the citizen sweep missed.

    Mirrors :func:`fetch_missing_owner_citizenships` exactly, one hop further
    down the chain: ``worker_company_map`` (just refreshed by
    :func:`fetch_company_census`) has the exact set of worker IDs that
    matter, and any of them missing from ``citizen_levels`` is fetched
    directly by ID — the same gap (inactive players excluded from
    ``user.getUsersByCountry``) applies to workers as much as owners.

    This is what makes the banned-worker exclusion in
    :func:`fetch_company_census` able to see workers who are inactive *and*
    banned at once (confirmed live to coexist) — such a worker is otherwise
    never discovered by anything else that touches ``citizen_levels``, since
    they aren't necessarily a company owner too.
    """
    dataset = "all_countries.worker_citizenships"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        async with db._conn.execute(
            "SELECT DISTINCT wm.worker_id FROM worker_company_map wm "
            "LEFT JOIN citizen_levels cl ON cl.user_id = wm.worker_id "
            "WHERE cl.user_id IS NULL"
        ) as cur:
            missing = [str(r[0]) for r in await cur.fetchall()]

        recorded = await cache.refresh_specific_users(missing)

        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "worker_citizenships: %d workers missing, %d recorded (%.1fs)",
            len(missing), recorded, duration_ms / 1000,
        )
        return recorded
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("worker_citizenships backfill failed")
        return 0


async def fetch_wage_taxes(
    client: APIClient, db: Database, countries: list[dict]
) -> int:
    """Collect income tax paid on wages since the last sweep.

    Wage transactions carry only the worker (``sellerId``) and employer
    (``buyerId``), never a company or item, so each one is attributed through
    ``worker_company_map`` (built by :func:`fetch_company_census`) to get the
    company, its country and its item.  Tax is then
    ``money × country income_tax %``.

    Strictly incremental: pagination walks newest-first and stops at the stored
    watermark, so nothing is ever counted twice.  On the very first run no
    history is collected at all — the watermark is simply planted at the newest
    transaction and ``wage_tax_started_at`` recorded, because backfilling would
    mean walking the entire transaction log.

    Returns the number of wage transactions counted.
    """
    dataset = "all_countries.wage_taxes"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        # Tax rates ride along on the country list already fetched this sweep,
        # so keeping them current costs no extra request.
        now = datetime.now(timezone.utc)
        rate_rows: list[tuple[str, float]] = []
        for c in countries:
            cid = cid_of(c)
            taxes = c.get("taxes") if isinstance(c.get("taxes"), dict) else {}
            if cid:
                rate_rows.append((cid, _to_float(taxes.get("income")) or 0.0))
        await db.save_country_tax_rates(rate_rows, now.isoformat())
        rates = await db.get_country_tax_rates()

        watermark = await db.get_tax_watermark()
        first_run = watermark is None

        # Walk newest-first until we reach the last transaction we counted.
        new_items: list[dict] = []
        cursor: str | None = None
        newest_id: str | None = None
        hit_watermark = False
        for _page in range(_WAGE_TAX_MAX_PAGES):
            payload: dict = {"limit": 100, "transactionType": "wage"}
            if cursor:
                payload["cursor"] = cursor
            raw = await client.post(
                "/transaction.getPaginatedTransactions", json=payload
            )
            data = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
            if not isinstance(data, dict):
                break
            items = data.get("items") or []
            if not items:
                break
            if newest_id is None:
                newest_id = str(items[0].get("_id") or "")
            if first_run:
                break  # only needed the newest ID to plant the watermark
            for tx in items:
                if str(tx.get("_id") or "") == watermark:
                    hit_watermark = True
                    break
                new_items.append(tx)
            if hit_watermark:
                break
            cursor = data.get("nextCursor")
            if not cursor:
                break

        if first_run:
            if newest_id:
                await db.set_tax_watermark(newest_id)
            await db.set_tax_started_at(now.isoformat())
            duration_ms = int((time.monotonic() - started) * 1000)
            await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
            logger.info(
                "wage_taxes: first run — tracking starts now, no backfill "
                "(watermark=%s)", newest_id,
            )
            return 0

        if not hit_watermark and new_items:
            logger.warning(
                "wage_taxes: walked %d pages without reaching the watermark — "
                "some transactions were missed (fetcher down too long?)",
                _WAGE_TAX_MAX_PAGES,
            )

        worker_map = await db.get_worker_company_map()

        # (day, country, item, company) → [tax, wage, count]
        buckets: dict[tuple[str, str, str, str], list] = {}
        unattributed = 0
        for tx in new_items:
            worker_id = str(tx.get("sellerId") or "")
            entry = worker_map.get(worker_id)
            if entry is None:
                unattributed += 1
                continue
            company_id, country_id, item_code = entry
            money = _to_float(tx.get("money")) or 0.0
            if money <= 0:
                continue
            created = str(tx.get("createdAt") or "")
            day = created[:10]
            if len(day) != 10:
                continue
            tax = money * rates.get(country_id, 0.0) / 100.0
            key = (day, country_id, item_code, company_id)
            bucket = buckets.setdefault(key, [0.0, 0.0, 0])
            bucket[0] += tax
            bucket[1] += money
            bucket[2] += 1

        rows_written = await db.add_tax_revenue(
            [(d, c, i, comp, v[0], v[1], v[2]) for (d, c, i, comp), v in buckets.items()]
        )
        # Watermark only after the aggregates are committed: a crash in between
        # would re-count an hour, whereas the reverse order would silently drop
        # one, and a visible double is easier to notice than a silent hole.
        if newest_id:
            await db.set_tax_watermark(newest_id)

        cutoff_day = (now - timedelta(days=TAX_RETENTION_DAYS)).strftime("%Y-%m-%d")
        await db.prune_tax_revenue(cutoff_day)

        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "wage_taxes: %d new wage tx, %d attributed to %d buckets, "
            "%d unattributed (%.1f%%) (%.1fs)",
            len(new_items), len(new_items) - unattributed, rows_written,
            unattributed,
            100.0 * unattributed / len(new_items) if new_items else 0.0,
            duration_ms / 1000,
        )
        return len(new_items)
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("wage_taxes sweep failed")
        return 0


async def fetch_mu_memberships(cache: CitizenCache, db: Database) -> tuple[int, int]:
    """Sweep every known MU's membership list (slow — gated by env flag).

    Clears all MU assignments first so citizens who left every known MU end up
    with NULL rather than retaining stale data.
    """
    dataset = "all_countries.mu_memberships"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        # Clear first so citizens not in any known MU don't retain stale data.
        await db.clear_all_citizen_mus()
        mus_tagged, citizens_updated = await cache.sweep_all_mu_memberships()
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "mu_memberships: tagged=%d citizens_updated=%d (%.1fs)",
            mus_tagged, citizens_updated, duration_ms / 1000,
        )
        return mus_tagged, citizens_updated
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("mu_memberships sweep failed")
        return 0, 0


# ── checkpoint helper ─────────────────────────────────────────────────────────


async def _try_checkpoint(db: Database) -> None:
    """Attempt a TRUNCATE WAL checkpoint after a sweep.

    The no-write window immediately after run_once() is the best opportunity
    to reset the WAL write pointer and truncate the file.  If readers are
    still active the checkpoint silently does what it can; non-fatal either way.
    """
    try:
        await db.checkpoint("TRUNCATE")
        logger.info("WAL TRUNCATE checkpoint completed after sweep")
    except Exception:
        logger.warning("WAL TRUNCATE checkpoint failed (non-fatal)", exc_info=True)


# ── main loop ─────────────────────────────────────────────────────────────────


async def run_once(client: APIClient, db: Database, *, country_limit: int = 0) -> None:
    """Execute a single end-to-end sweep.

    Tiers (each gated by an env flag, default-on for cheap ones):
      - country_snapshots   (always)
      - citizens per country (always)
      - weekly_damage       FULL_FETCH_ENABLE_WEEKLY_DAMAGE (default 1)
      - trades              FULL_FETCH_ENABLE_TRADES (default 1)
      - mu_registry         FULL_FETCH_ENABLE_MU_REGISTRY (default 1)
      - mu_memberships      FULL_FETCH_ENABLE_MU_MEMBERSHIPS (default 0 — slow)
      - company_census      FULL_FETCH_ENABLE_COMPANY_CENSUS (default 1)
      - wage_taxes          FULL_FETCH_ENABLE_WAGE_TAXES (default 1; needs census)
      - owner_citizenships  FULL_FETCH_ENABLE_OWNER_CITIZENSHIPS (default 1; needs census)
      - worker_citizenships FULL_FETCH_ENABLE_WORKER_CITIZENSHIPS (default 1; needs census)
      - alliances           FULL_FETCH_ENABLE_ALLIANCES (default 1)
      - region_status       FULL_FETCH_ENABLE_REGION_STATUS (default 1)
      - country_proxy       FULL_FETCH_ENABLE_COUNTRY_PROXY (default 1; no-op without
                             PROXY_API_URL/PROXY_API_KEY configured)
    """
    _written, countries = await fetch_country_snapshots(client, db)
    if not countries:
        return
    if _env_int("FULL_FETCH_ENABLE_ALLIANCES", 1) == 1:
        await fetch_alliance_countries(client, db)
    if _env_int("FULL_FETCH_ENABLE_REGION_STATUS", 1) == 1:
        await fetch_region_status(client, db)
    if _env_int("FULL_FETCH_ENABLE_COUNTRY_PROXY", 1) == 1:
        await fetch_country_proxy_status(db)
    cache = CitizenCache(client, db)
    await fetch_all_citizen_levels(cache, db, countries, limit=country_limit)

    if _env_int("FULL_FETCH_ENABLE_WEEKLY_DAMAGE", 1) == 1:
        await fetch_weekly_damages(client, db)
    if _env_int("FULL_FETCH_ENABLE_TRADES", 1) == 1:
        await fetch_recent_trades(client, db)
    if _env_int("FULL_FETCH_ENABLE_MU_REGISTRY", 1) == 1:
        await fetch_all_mu_names(client, db)
    if _env_int("FULL_FETCH_ENABLE_MU_MEMBERSHIPS", 0) == 1:
        await fetch_mu_memberships(cache, db)
    if _env_int("FULL_FETCH_ENABLE_COMPANY_CENSUS", 1) == 1:
        await fetch_company_census(client, db)
        # Both must run after the census: one depends on the worker→company
        # map it refreshes, the other on the owner IDs in company_owners.
        if _env_int("FULL_FETCH_ENABLE_WAGE_TAXES", 1) == 1:
            await fetch_wage_taxes(client, db, countries)
        if _env_int("FULL_FETCH_ENABLE_OWNER_CITIZENSHIPS", 1) == 1:
            await fetch_missing_owner_citizenships(cache, db)
        if _env_int("FULL_FETCH_ENABLE_WORKER_CITIZENSHIPS", 1) == 1:
            await fetch_missing_worker_citizenships(cache, db)


async def main() -> None:
    db_path = os.getenv("RW_DB_PATH", "database/external.db")
    api_base = os.getenv("RW_API_BASE_URL", "https://api2.warera.io/trpc")
    keys_path = os.getenv("RW_API_KEYS_PATH", "_api_keys.json")
    minute_offset = _env_int("FULL_FETCH_MINUTE_OFFSET", 5)
    run_at_startup = _env_int("FULL_FETCH_RUN_AT_STARTUP", 1) == 1
    country_limit = _env_int("FULL_FETCH_COUNTRY_LIMIT", 0)

    logger.info(
        "full-fetcher start: db=%s base=%s schedule=HH:%02d country_limit=%s",
        db_path, api_base, minute_offset, country_limit or "all",
    )

    db = Database(db_path)
    await db.setup()
    api_keys = _load_api_keys(keys_path)
    client = APIClient(api_base, api_keys=api_keys, source="data-fetcher")
    await client.start()

    stop_event = asyncio.Event()

    def _on_signal(signum, _frame=None):
        logger.info("Received signal %s — stopping", signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except NotImplementedError:  # pragma: no cover (windows)
            signal.signal(sig, _on_signal)

    try:
        if run_at_startup:
            try:
                await run_once(client, db, country_limit=country_limit)
            except Exception:  # noqa: BLE001
                logger.exception("startup sweep failed")
            await _try_checkpoint(db)

        while not stop_event.is_set():
            sleep_s = _seconds_until_next_aligned_run(minute_offset)
            logger.info("full-fetcher: next sweep in %.0fs (at HH:%02d UTC)", sleep_s, minute_offset)
            try:
                # Sleep until the next aligned minute, bail out early on shutdown.
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                pass
            else:
                break  # stop_event was set
            try:
                await run_once(client, db, country_limit=country_limit)
            except Exception:  # noqa: BLE001
                logger.exception("scheduled sweep failed")
            await _try_checkpoint(db)
    finally:
        logger.info("full-fetcher shutting down")
        await client.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
