"""Background task: daily poll for battle loot drops, stored in battle_drops table.

For every battle with non-zero damage, fetches the user rankings for both sides
and stores any lootItem entries.  Uses INSERT OR IGNORE (keyed on lootItem._id)
so re-running the same battle is a safe no-op.

Runs hourly, with a 20-hour cooldown so it effectively fires once per day.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_STARTUP_DELAY_S = 120   # 2 min — let citizen cache populate first
_COOLDOWN_HOURS = 20     # minimum hours between full polls
_PAGES_PER_POLL = 5      # ≈ 50 most recent battles per poll run


def _unwrap(resp: object) -> object:
    if not isinstance(resp, dict):
        return resp
    d = resp.get("result", resp)
    if isinstance(d, dict):
        return d.get("data", d)
    return d


class BattleDropsTasks(TaskCogBase, name="battle_drops_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.battle_drops_refresh.start()

    def cog_unload(self) -> None:
        self.battle_drops_refresh.cancel()

    @tasks.loop(hours=1)
    async def battle_drops_refresh(self) -> None:
        if not self._client or not self._db:
            return
        try:
            await self._run_poll()
        except Exception:
            logger.exception("battle_drops_refresh: unexpected error")

    @battle_drops_refresh.before_loop
    async def before_battle_drops_refresh(self) -> None:
        await self._wait_for_services()
        logger.info("battle_drops_refresh: waiting %ds before first run", _STARTUP_DELAY_S)
        await asyncio.sleep(_STARTUP_DELAY_S)

    async def run_once(self) -> int:
        """Public entry point for manual triggers. Returns number of new drops inserted."""
        return await self._run_poll(force=True)

    # ------------------------------------------------------------------ #

    async def _run_poll(self, force: bool = False) -> int:
        now_utc = datetime.now(timezone.utc)
        now_str = now_utc.isoformat()

        # Cooldown check
        if not force:
            last_run = await self._db.get_poll_state("battle_drops_last_run")
            if last_run:
                elapsed_h = (now_utc - datetime.fromisoformat(last_run)).total_seconds() / 3600
                if elapsed_h < _COOLDOWN_HOURS:
                    logger.debug(
                        "battle_drops_refresh: skipping — %.1fh since last run (need %dh)",
                        elapsed_h, _COOLDOWN_HOURS,
                    )
                    return 0

        logger.info("battle_drops_refresh: starting poll (%d pages)", _PAGES_PER_POLL)
        inserted_total = 0
        battles_processed = 0

        cursor: Optional[str] = None
        for _page in range(_PAGES_PER_POLL):
            inp: dict = {}
            if cursor:
                inp["cursor"] = cursor

            raw = await self._client.get(
                "/battle.getBattles", params={"input": json.dumps(inp)}
            )
            d = _unwrap(raw)
            if not isinstance(d, dict):
                break

            items: list[dict] = d.get("items", [])
            cursor = d.get("nextCursor")

            for battle in items:
                bid = str(battle.get("_id", ""))
                if not bid:
                    continue
                atk_dmg = battle.get("attacker", {}).get("damages", 0) or 0
                def_dmg = battle.get("defender", {}).get("damages", 0) or 0
                if atk_dmg == 0 and def_dmg == 0:
                    continue  # empty battle, no loot possible

                created_at = battle.get("createdAt")
                n = await self._process_battle(bid, created_at, now_str)
                inserted_total += n
                battles_processed += 1

            if not cursor:
                break

        logger.info(
            "battle_drops_refresh: done — %d battles checked, %d new drops",
            battles_processed, inserted_total,
        )
        await self._db.set_poll_state("battle_drops_last_run", now_str)
        return inserted_total

    async def _process_battle(
        self, battle_id: str, battle_created_at: Optional[str], recorded_at: str
    ) -> int:
        """Fetch rankings for both sides and insert any loot drops. Returns inserted count."""
        inserted = 0
        for side in ("attacker", "defender"):
            raw = await self._client.get(
                "/battleRanking.getRanking",
                params={"input": json.dumps({
                    "battleId": battle_id,
                    "dataType": "damage",
                    "type": "user",
                    "side": side,
                })},
            )
            d = _unwrap(raw)
            rankings: list[dict] = []
            if isinstance(d, dict):
                # "items" is what the live API actually returns (confirmed
                # live 2026-08) — "rankings" alone silently returned [] here
                # for every call once the API moved off it, with no
                # exception to surface the failure.
                for key in ("items", "rankings", "ranking", "data", "results"):
                    v = d.get(key)
                    if isinstance(v, list):
                        rankings = v
                        break

            for entry in rankings:
                loot = entry.get("lootItem")
                if not loot:
                    continue

                item_id = str(loot.get("_id", ""))
                if not item_id:
                    continue

                user_id = entry.get("user")
                if not isinstance(user_id, str) or not user_id:
                    continue

                item_code = str(loot.get("code", ""))
                skills = loot.get("skills")
                item_skills_json = json.dumps(skills) if skills else None
                dropped_at = loot.get("lastAcquisitionAt") or battle_created_at

                ok: bool = await self._db.insert_battle_drop(
                    item_id=item_id,
                    battle_id=battle_id,
                    user_id=user_id,
                    side=side,
                    rank=entry.get("rank"),
                    damage_dealt=entry.get("value"),
                    item_code=item_code,
                    item_skills_json=item_skills_json,
                    dropped_at=dropped_at,
                    recorded_at=recorded_at,
                )
                if ok:
                    inserted += 1

        if inserted:
            await self._db.commit_battle_drops()

        return inserted


async def setup(bot) -> None:
    await bot.add_cog(BattleDropsTasks(bot))
