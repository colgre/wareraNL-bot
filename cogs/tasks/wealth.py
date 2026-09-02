"""Background task: fetch wealth for all citizens and store in DB.

Runs every 24 hours. Calls ``user.getUserById`` for every citizen we know
about (via tRPC HTTP batching — see ``APIClient.batch_get``, ~100 citizens
per HTTP request rather than one request each) and reads the total AND the
per-category breakdown from each response's ``stats.wealth`` — confirmed
live to be ``{companies, items, money, equipments, weapons, total}``, with
the five categories always summing to ``total``. This *is* visible via our
API keys (an earlier attempt at this looked at a top-level ``wealth`` field
that's genuinely always null for other users — the real field lives one
level down, under ``stats``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timezone
from typing import Optional

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# Give the citizen_refresh task enough time to populate citizen_levels first.
_STARTUP_DELAY_S = 240  # 4 minutes


# ── Response parsing helpers ──────────────────────────────────────────────────

def _unwrap(resp: object) -> object:
    """Strip tRPC result/data envelopes — batch_get() already does this for
    the normal (batched) path, but its per-item fallback path may not, so
    every response is run through this regardless of which path it took."""
    if not isinstance(resp, dict):
        return resp
    for key in ("result", "data"):
        v = resp.get(key)
        if isinstance(v, dict):
            return v.get("data", v)
    return resp


def _extract_wealth(user_doc: object) -> Optional[dict]:
    """Pull {companies, items, money, equipments, weapons, total} out of one
    user.getUserById response — it lives at stats.wealth, NOT top-level
    (confirmed live; a top-level "wealth" field also exists but is always
    null for anyone other than the account's own authenticated session)."""
    data = _unwrap(user_doc)
    if not isinstance(data, dict):
        return None
    stats = data.get("stats")
    if not isinstance(stats, dict):
        return None
    wealth = stats.get("wealth")
    return wealth if isinstance(wealth, dict) else None


# ── Task cog ──────────────────────────────────────────────────────────────────

class WealthTasks(TaskCogBase, name="wealth_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.wealth_refresh.start()

    def cog_unload(self) -> None:
        self.wealth_refresh.cancel()

    @tasks.loop(time=time(4, 0, tzinfo=timezone.utc))
    async def wealth_refresh(self) -> None:
        if not self._client or not self._db:
            return
        try:
            await self._run_wealth_refresh()
        except Exception:
            logger.exception("wealth_refresh: unexpected error")

    @wealth_refresh.before_loop
    async def before_wealth_refresh(self) -> None:
        await self._wait_for_services()
        logger.info("wealth_refresh: waiting %ds before first run", _STARTUP_DELAY_S)
        await asyncio.sleep(_STARTUP_DELAY_S)

    async def run_wealth_refresh_once(self) -> dict:
        """Public entry point for manual triggers (e.g. /peil wealth).

        Returns a stats dict with at least a ``'saved'`` key.
        """
        return await self._run_wealth_refresh()

    # ------------------------------------------------------------------ #

    async def _run_wealth_refresh(self) -> dict:
        logger.info("wealth_refresh: starting")

        # ── 1. Get ALL citizens from DB (all countries) ─────────────────
        # get_all_citizens_for_tips_scan returns [(user_id, country_id, citizen_name)]
        all_citizens = await self._db.get_all_citizens_for_tips_scan()
        if not all_citizens:
            logger.warning("wealth_refresh: no citizens in DB")
            return {"saved": 0}

        logger.info("wealth_refresh: fetching wealth for %d citizens across all countries", len(all_citizens))

        # ── 2. user.getUserById for every citizen, tRPC-batched (~100 per
        # HTTP request — see APIClient.batch_get) rather than one request
        # each. A small sleep between chunks (not the default 0) since this
        # is thousands of citizens, not the dozens batch_get is more usually
        # called with elsewhere — 429 backoff alone would still recover, but
        # pacing it avoids leaning on that every single day.
        # This is a citizen-scale sweep (tens of thousands of calls, batched)
        # just like citizen_refresh's own country sweep — serialized against
        # it and the other heavy sweeps (luck, global_luck, ...) via the
        # same shared lock so they don't all hammer the discord bot's small
        # API key pool at once.
        try:
            async with self._heavy_api_lock:
                raw_results = await self._client.batch_get(
                    "/user.getUserById",
                    [{"userId": uid} for uid, _cid, _name in all_citizens],
                    batch_size=100,
                    chunk_sleep=0.3,
                )
        except Exception as exc:
            logger.warning("wealth_refresh: user.getUserById batch fetch failed: %s", exc)
            return {"saved": 0}

        now_str = datetime.now(timezone.utc).isoformat()
        today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # ── 3. Write DB rows (cheap — no more API calls from here on), a
        # bounded number concurrently so this doesn't serialize thousands
        # of tiny SQLite writes one at a time.
        _BATCH = 20
        sem = asyncio.Semaphore(_BATCH)
        saved = 0
        missing_wealth = 0

        async def _process_citizen(
            user_id: str, country_id: str, citizen_name: Optional[str], raw: object,
        ) -> None:
            nonlocal saved, missing_wealth
            async with sem:
                user_doc = _unwrap(raw)
                api_name = user_doc.get("username") if isinstance(user_doc, dict) else None
                resolved_name = api_name or citizen_name
                wealth = _extract_wealth(raw)
                if wealth is None:
                    # Couldn't fetch this one this run (missing/banned/API
                    # error on this citizen specifically) — leave their
                    # existing row alone rather than overwriting it with 0s.
                    missing_wealth += 1
                    return

                companies = float(wealth.get("companies") or 0.0)
                items = float(wealth.get("items") or 0.0)
                money = float(wealth.get("money") or 0.0)
                equipments = float(wealth.get("equipments") or 0.0)
                weapons = float(wealth.get("weapons") or 0.0)
                total = wealth.get("total")
                wealth_total = float(total) if isinstance(total, (int, float)) else (
                    companies + items + money + equipments + weapons
                )

                await self._db.upsert_citizen_wealth(
                    user_id=user_id,
                    country_id=country_id,
                    citizen_name=resolved_name,
                    wealth_active=wealth_total,
                    wealth_inactive=0.0,
                    updated_at=now_str,
                    wealth_companies=companies,
                    wealth_items=items,
                    wealth_money=money,
                    wealth_equipments=equipments,
                    wealth_weapons=weapons,
                )
                await self._db.insert_wealth_snapshot(
                    user_id=user_id,
                    country_id=country_id,
                    citizen_name=resolved_name,
                    wealth_total=wealth_total,
                    snapshot_date=today_date,
                    wealth_companies=companies,
                    wealth_items=items,
                    wealth_money=money,
                    wealth_equipments=equipments,
                    wealth_weapons=weapons,
                )
                saved += 1

        await asyncio.gather(*[
            _process_citizen(uid, cid, name, raw)
            for (uid, cid, name), raw in zip(all_citizens, raw_results)
        ])

        await self._db.flush_citizen_wealth()
        await self._db.flush_wealth_history()
        await self._db.set_poll_state("wealth_ranking_total", str(saved))
        await self._db.set_poll_state("wealth_ranking_last_run", now_str)
        logger.info(
            "wealth_refresh: done — %d citizens saved, %d skipped (no wealth data this run)",
            saved, missing_wealth,
        )
        return {"saved": saved, "missing": missing_wealth}


async def setup(bot) -> None:
    await bot.add_cog(WealthTasks(bot))
