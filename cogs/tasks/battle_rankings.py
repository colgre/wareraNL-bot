"""Background task: daily accumulation of per-battle damage rankings.

Polls ``battle.getBattles`` (finished battles only) once per day at 02:00 UTC,
fetches ``battleRanking.getRanking`` for both sides of every newly seen battle,
and persists the results to ``battle_hits`` / ``processed_battles``.

Why daily at 02:00 UTC?
  The game API retains finished-battle ranking data for at least 3 days.
  Running once per day guarantees we capture every battle well within that window.

Data collected per battle
  • Every player's damage on attacker AND defender side
  • The battle's ``createdAt`` timestamp (used for N-days filtering in /leaderboard)
  • Total attacker + defender damage (stored in processed_battles for fast lookups)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_STARTUP_DELAY_S = 120      # wait 2 min after services-ready before first run
_RUN_HOUR_UTC    = 2        # target hour for the daily sweep
_FETCH_LIMIT     = 100      # battles per getBattles page (API max)
_MAX_PAGES       = 5_000    # hard safety cap (500 000 battles max — effectively unlimited)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _unwrap(resp: object) -> object:
    """Strip tRPC result/data envelopes."""
    if not isinstance(resp, dict):
        return resp
    inner = resp.get("result", {})
    if isinstance(inner, dict):
        return inner.get("data", inner)
    return resp


def _seconds_until_hour(target_hour: int) -> float:
    """Seconds until the next occurrence of *target_hour*:00:00 UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _extract_country_id(raw: object) -> Optional[str]:
    """Resolve a country field that may be a plain string ID or a nested object."""
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        cid = raw.get("_id") or raw.get("id")
        return str(cid) if cid else None
    return None


# ── Cog ──────────────────────────────────────────────────────────────────────


class BattleRankingsTask(TaskCogBase, name="battle_rankings_task"):
    """Daily task that accumulates per-battle player damage into the local DB."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.daily_battle_rankings.start()

    def cog_unload(self) -> None:
        self.daily_battle_rankings.cancel()

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #

    @tasks.loop(hours=24)
    async def daily_battle_rankings(self) -> None:
        if not self._client or not self._db:
            return
        try:
            new_battles, new_hits = await self._run_sweep()
            logger.info(
                "battle_rankings sweep complete: %d new battles, %d player-hits stored",
                new_battles,
                new_hits,
            )
        except Exception:
            logger.exception("battle_rankings sweep: unexpected error")

    @daily_battle_rankings.before_loop
    async def _before_daily(self) -> None:
        await self._wait_for_services()
        # If the DB is empty or data is stale (>1.5 days old), sweep immediately
        # rather than waiting until 02:00 UTC so the leaderboard works right away.
        db = self._db
        if db:
            count = await db.get_battle_hits_count()
            if count == 0:
                logger.info("battle_rankings: DB is empty — running initial sweep now")
                await self._run_sweep()
            else:
                latest = await db.get_latest_processed_at()
                if latest:
                    try:
                        parsed = datetime.fromisoformat(latest.replace("Z", "+00:00"))
                        age_days = (
                            datetime.now(timezone.utc) - parsed
                        ).total_seconds() / 86400
                        if age_days > 1.5:
                            logger.info(
                                "battle_rankings: data is %.1f days stale — running catch-up sweep",
                                age_days,
                            )
                            await self._run_sweep()
                    except (ValueError, TypeError):
                        pass
        # Sleep until the next 02:00 UTC so scheduled sweeps stay on rhythm.
        secs = _seconds_until_hour(_RUN_HOUR_UTC)
        logger.info(
            "battle_rankings: next scheduled sweep in %.0f s (at %02d:00 UTC)",
            secs,
            _RUN_HOUR_UTC,
        )
        await asyncio.sleep(secs)

    # ------------------------------------------------------------------ #
    # Core sweep logic
    # ------------------------------------------------------------------ #

    async def run_sweep_once(self) -> tuple[int, int]:
        """Public entry point for manual triggers (e.g. owner command).

        Returns (new_battles, new_hits).
        """
        return await self._run_sweep()

    async def run_country_id_backfill(self) -> int:
        """Re-fetch battle.getBattles and populate attacker/defender country IDs
        for processed battles that still have NULL country info.

        Returns the number of battles updated.
        """
        db = self._db
        client = self._client
        if not db or not client:
            return 0

        missing_ids = set(await db.get_processed_battles_missing_countries(limit=100_000))
        if not missing_ids:
            return 0

        # Page through battle.getBattles to find our missing ones
        updated = 0
        cursor: Optional[str] = None
        remaining = set(missing_ids)

        for page in range(_MAX_PAGES):
            if not remaining:
                break
            payload: dict = {"isActive": False, "limit": _FETCH_LIMIT}
            if cursor:
                payload["cursor"] = cursor
            try:
                raw = await client.get(
                    "/battle.getBattles",
                    params={"input": json.dumps(payload)},
                )
            except Exception as exc:
                logger.warning("country_id_backfill: getBattles page %d failed: %s", page, exc)
                break

            data = _unwrap(raw)
            page_items: list[dict] = []
            next_cursor: Optional[str] = None
            if isinstance(data, dict):
                page_items = data.get("items", [])
                next_cursor = data.get("nextCursor") or data.get("cursor")
            elif isinstance(data, list):
                page_items = data

            for battle in page_items:
                bid = battle.get("_id")
                if not bid or bid not in remaining:
                    continue
                att_side = battle.get("attacker", {})
                def_side = battle.get("defender", {})
                att_cid = _extract_country_id(
                    att_side.get("country") if isinstance(att_side, dict) else None
                )
                def_cid = _extract_country_id(
                    def_side.get("country") if isinstance(def_side, dict) else None
                )
                if att_cid or def_cid:
                    await db.update_processed_battle_countries(bid, att_cid, def_cid)
                    updated += 1
                remaining.discard(bid)

            if not next_cursor or not page_items:
                break
            cursor = next_cursor

        await db.commit_battle_hits()  # triggers conn.commit — persists all the UPDATE statements
        logger.info("country_id_backfill: updated %d battles", updated)
        return updated

    async def _run_sweep(self) -> tuple[int, int]:
        """Fetch all available finished battles; process any not yet stored."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        client = self._client
        db = self._db

        # ── 1. Collect all available finished battle IDs ──────────────
        all_battles: list[dict] = []
        cursor: Optional[str] = None

        for page in range(_MAX_PAGES):
            payload: dict = {"isActive": False, "limit": _FETCH_LIMIT}
            if cursor:
                payload["cursor"] = cursor

            try:
                raw = await client.get(
                    "/battle.getBattles",
                    params={"input": json.dumps(payload)},
                )
            except Exception as exc:
                logger.warning("battle_rankings: getBattles page %d failed: %s", page, exc)
                break

            data = _unwrap(raw)
            page_items: list[dict] = []
            next_cursor: Optional[str] = None

            if isinstance(data, dict):
                page_items = data.get("items", [])
                next_cursor = data.get("nextCursor") or data.get("cursor")
            elif isinstance(data, list):
                page_items = data

            all_battles.extend(b for b in page_items if isinstance(b, dict))

            if not next_cursor or not page_items:
                break
            cursor = next_cursor

        if not all_battles:
            logger.info("battle_rankings: no finished battles found in API")
            return 0, 0

        logger.info(
            "battle_rankings: fetched %d finished battles from API", len(all_battles)
        )

        # ── 2. Find which ones are new ────────────────────────────────
        all_ids = [b["_id"] for b in all_battles if b.get("_id")]
        unprocessed_ids = set(await db.filter_unprocessed(all_ids))

        new_battles_data = [
            b for b in all_battles if b.get("_id") in unprocessed_ids
        ]

        if not new_battles_data:
            logger.info("battle_rankings: all %d battles already processed", len(all_battles))
            return 0, 0

        logger.info(
            "battle_rankings: %d new battles to process", len(new_battles_data)
        )

        # ── 3. Fetch rankings for each new battle ─────────────────────
        total_new_battles = 0
        total_new_hits = 0

        for battle in new_battles_data:
            bid: str = battle["_id"]
            created_at: str = battle.get("createdAt", now_str)

            attacker_side = battle.get("attacker", {})
            defender_side = battle.get("defender", {})
            att_dmg = float(
                (attacker_side.get("damages") if isinstance(attacker_side, dict) else 0) or 0
            )
            def_dmg = float(
                (defender_side.get("damages") if isinstance(defender_side, dict) else 0) or 0
            )
            att_country_id: Optional[str] = _extract_country_id(
                attacker_side.get("country") if isinstance(attacker_side, dict) else None
            )
            def_country_id: Optional[str] = _extract_country_id(
                defender_side.get("country") if isinstance(defender_side, dict) else None
            )

            hits_added = 0
            for side in ("attacker", "defender"):
                entries = await self._fetch_battle_ranking(bid, side)
                await asyncio.sleep(0.2)
                for entry in entries:
                    uid = entry.get("user")
                    if not isinstance(uid, str) or not uid:
                        continue
                    dmg = float(entry.get("value") or 0)
                    rank = entry.get("rank")
                    await db.insert_battle_hit(
                        battle_id=bid,
                        user_id=uid,
                        side=side,
                        damage=dmg,
                        rank=rank,
                        battle_created_at=created_at,
                        recorded_at=now_str,
                    )
                    hits_added += 1

                # Also fetch MU-level rankings to populate battle_mu_hits
                mu_entries = await self._fetch_battle_ranking(bid, side, rtype="mu")
                await asyncio.sleep(0.2)
                for entry in mu_entries:
                    mu_raw = (
                        entry.get("mu")
                        or entry.get("militaryUnit")
                        or entry.get("muId")
                    )
                    if isinstance(mu_raw, dict):
                        mu_id = mu_raw.get("_id") or mu_raw.get("id")
                        mu_name: Optional[str] = mu_raw.get("name") or mu_raw.get("fullName")
                    elif isinstance(mu_raw, str):
                        mu_id = mu_raw
                        mu_name = entry.get("muName") or entry.get("name")
                    else:
                        continue
                    if not mu_id:
                        continue
                    mu_dmg = float(entry.get("value") or 0)
                    await db.insert_battle_mu_hit(
                        battle_id=bid,
                        mu_id=str(mu_id),
                        side=side,
                        mu_name=mu_name,
                        damage=mu_dmg,
                        battle_created_at=created_at,
                        recorded_at=now_str,
                    )

                # Fetch country-level rankings to populate battle_country_hits
                # (credits damage to each player's home country — matches the game's own ranking)
                country_entries = await self._fetch_battle_ranking(bid, side, rtype="country")
                await asyncio.sleep(0.2)
                for entry in country_entries:
                    cid_raw = (
                        entry.get("country")
                        or entry.get("countryId")
                        or entry.get("_id")
                    )
                    if isinstance(cid_raw, dict):
                        cid_raw = cid_raw.get("_id") or cid_raw.get("id")
                    if not cid_raw or not isinstance(cid_raw, str):
                        continue
                    c_dmg = float(entry.get("value") or 0)
                    await db.insert_battle_country_hit(
                        battle_id=bid,
                        country_id=cid_raw,
                        side=side,
                        damage=c_dmg,
                        battle_created_at=created_at,
                        recorded_at=now_str,
                    )

            await db.commit_battle_hits()
            await db.commit_battle_mu_hits()
            await db.commit_battle_country_hits()
            await db.mark_battle_processed(
                battle_id=bid,
                battle_created_at=created_at,
                attacker_damage=att_dmg,
                defender_damage=def_dmg,
                processed_at=now_str,
                attacker_country_id=att_country_id,
                defender_country_id=def_country_id,
            )

            total_new_battles += 1
            total_new_hits += hits_added
            logger.debug(
                "battle_rankings: battle %s — %d hits (att=%.0f def=%.0f)",
                bid,
                hits_added,
                att_dmg,
                def_dmg,
            )

        return total_new_battles, total_new_hits

    async def _fetch_battle_ranking(
        self, battle_id: str, side: str, rtype: str = "user"
    ) -> list[dict]:
        """Fetch battleRanking.getRanking for one battle+side, return list of entries.

        The list key varies ("items" is what the live API actually returns as
        of 2026-08 — confirmed live; "rankings" was the only key checked here
        before, which silently returned [] every call once the API moved off
        it, with no exception to surface the failure. See the multi-key
        fallback in cogs/tasks/daily_dmg.py / battle_drops.py for the same
        pattern).
        """
        try:
            raw = await self._client.get(
                "/battleRanking.getRanking",
                params={
                    "input": json.dumps(
                        {
                            "battleId": battle_id,
                            "dataType": "damage",
                            "type": rtype,
                            "side": side,
                        }
                    )
                },
            )
        except Exception as exc:
            logger.warning(
                "battle_rankings: ranking fetch failed for battle %s / %s / %s: %s",
                battle_id,
                side,
                rtype,
                exc,
            )
            return []

        data = _unwrap(raw)
        if isinstance(data, dict):
            for key in ("items", "rankings", "ranking", "data", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    return [e for e in v if isinstance(e, dict)]
            return []
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
        return []

    async def run_country_backfill(self) -> tuple[int, int]:
        """Fetch country rankings for all processed battles that lack country hits.

        Returns (battles_processed, country_hits_added).
        """
        db = self._db
        if not db:
            return 0, 0

        battles = await db.get_processed_battles_missing_country_hits()

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        battles_done = 0
        hits_added = 0

        # Pass 1: API-based (works for battles within ~3 days retention window)
        for bid, created_at in battles:
            ca = created_at or now_str
            for side in ("attacker", "defender"):
                entries = await self._fetch_battle_ranking(bid, side, rtype="country")
                for entry in entries:
                    cid_raw = (
                        entry.get("country")
                        or entry.get("countryId")
                        or entry.get("_id")
                    )
                    if isinstance(cid_raw, dict):
                        cid_raw = cid_raw.get("_id") or cid_raw.get("id")
                    if not cid_raw or not isinstance(cid_raw, str):
                        continue
                    c_dmg = float(entry.get("value") or 0)
                    await db.insert_battle_country_hit(
                        battle_id=bid,
                        country_id=cid_raw,
                        side=side,
                        damage=c_dmg,
                        battle_created_at=ca,
                        recorded_at=now_str,
                    )
                    hits_added += 1

            await db.commit_battle_country_hits()
            battles_done += 1

        # Pass 2: DB derivation — fills in any battles still missing country hits
        # by joining battle_hits with citizen_levels (current nationality).
        # Covers all historical battles regardless of API retention window.
        derived = await db.derive_country_hits_from_player_hits()
        hits_added += derived

        logger.info(
            "battle_rankings country backfill: %d battles (API), %d hits total (incl. %d derived)",
            battles_done, hits_added, derived,
        )
        return battles_done, hits_added

    async def run_mu_hits_backfill(self) -> tuple[int, int]:
        """Fetch MU rankings for all processed battles that lack MU hit data.

        Returns (battles_processed, mu_hits_added).
        """
        db = self._db
        if not db:
            return 0, 0

        battles = await db.get_processed_battles_missing_mu_hits()
        if not battles:
            return 0, 0

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        battles_done = 0
        hits_added = 0

        for bid, created_at in battles:
            ca = created_at or now_str
            for side in ("attacker", "defender"):
                entries = await self._fetch_battle_ranking(bid, side, rtype="mu")
                for entry in entries:
                    mu_raw = (
                        entry.get("mu")
                        or entry.get("militaryUnit")
                        or entry.get("muId")
                    )
                    if isinstance(mu_raw, dict):
                        mu_id = mu_raw.get("_id") or mu_raw.get("id")
                        mu_name: Optional[str] = mu_raw.get("name") or mu_raw.get("fullName")
                    elif isinstance(mu_raw, str):
                        mu_id = mu_raw
                        mu_name = entry.get("muName") or entry.get("name")
                    else:
                        continue
                    if not mu_id:
                        continue
                    mu_dmg = float(entry.get("value") or 0)
                    await db.insert_battle_mu_hit(
                        battle_id=bid,
                        mu_id=str(mu_id),
                        side=side,
                        mu_name=mu_name,
                        damage=mu_dmg,
                        battle_created_at=ca,
                        recorded_at=now_str,
                    )
                    hits_added += 1

            await db.commit_battle_mu_hits()
            battles_done += 1

        logger.info(
            "battle_rankings MU backfill: %d battles, %d MU hits added", battles_done, hits_added
        )
        return battles_done, hits_added


async def setup(bot) -> None:
    await bot.add_cog(BattleRankingsTask(bot))
