"""Handles fetching and caching citizen level data from the WarEra API."""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from services.api_client import APIClient
from services.db import Database

logger = logging.getLogger("discord_bot")


class CitizenCache:
    """Fetches citizen level data from the API and persists it to the DB cache."""

    def __init__(self, client: APIClient, db: Database) -> None:
        self._client = client
        self._db = db

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    async def refresh_country(
        self, country_id: str, country_name: str, *, progress_msg=None
    ) -> int:
        """Fetch every citizen's level for a country and write to DB cache.

        Uses tRPC HTTP batching (30 users per request) with automatic fallback
        to individual calls if the server doesn't support batching.

        Returns the number of citizens whose level was successfully recorded.
        """
        user_ids = await self._fetch_user_ids(country_id)
        if not user_ids:
            return 0

        # Do NOT delete first — use upsert then prune so MU data written by
        # the parallel mu_refresh task is preserved across citizen refreshes.
        updated_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        batch_size = 100
        inputs = [{"userId": uid} for uid in user_ids]
        total_batches = (len(user_ids) + batch_size - 1) // batch_size

        # user.getUserLite dropped its "skills" field (API change observed
        # 2026-11); user.getUserById still returns the full per-skill
        # breakdown, so skill_mode can be computed correctly.
        results = await self._client.batch_get(
            "/user.getUserById",
            inputs,
            batch_size=batch_size,
            chunk_sleep=0.15,
        )

        recorded = 0
        pill_tracking_rows: list[tuple] = []
        combat_state_rows: list[tuple] = []

        # self._db._conn is ONE connection shared by every concurrent coroutine
        # in the bot process (every cog/task).  Do NOT wrap this loop in an
        # explicit BEGIN/COMMIT transaction: each `await` below yields control
        # back to the event loop, letting unrelated tasks (the WAL checkpoint
        # loop, event_poll, ethics_watcher, ...) submit their own statements on
        # this same connection in between — those interleave *inside* the open
        # transaction, and anything that touches transaction state (a
        # checkpoint, another COMMIT) can leave it invalid before we reach our
        # own COMMIT ("cannot commit - no transaction is active").  Under
        # autocommit (isolation_level=None, set in DatabaseBase.setup), every
        # individual execute() below is already its own atomic unit dispatched
        # whole to aiosqlite's single worker thread, so it can't be split by
        # another task — that per-statement safety is what actually matters
        # here, not batching multiple statements into one transaction.
        for i, (uid, obj) in enumerate(zip(user_ids, results)):
            lvl = self._extract_level(obj)
            mode = self._extract_skill_mode(obj)
            reset_at = self._extract_last_skills_reset_at(obj)
            last_login = self._extract_last_login_at(obj)
            name = self._extract_name(obj)
            mu_id, mu_name = self._extract_mu_info(obj)
            avatar_url = self._extract_avatar_url(obj)
            is_active = self._extract_is_active(obj)
            is_banned = self._extract_is_banned(obj)
            if lvl is not None:
                await self._db.upsert_citizen_level(
                    uid,
                    country_id,
                    lvl,
                    updated_at,
                    skill_mode=mode,
                    last_skills_reset_at=reset_at,
                    citizen_name=name,
                    last_login_at=last_login,
                    mu_id=mu_id,
                    mu_name=mu_name,
                    avatar_url=avatar_url,
                    is_active=is_active,
                    is_banned=is_banned,
                )
                recorded += 1

            # Collect pill tracking data (only store non-null buffEndAt to preserve debuff detection)
            if isinstance(obj, dict):
                _buffs = obj.get("buffs") or {}
                _buff_end_at = _buffs.get("buffEndAt")
                _exp_ts: int | None = None
                if _buff_end_at:
                    try:
                        _exp_ts = int(datetime.fromisoformat(_buff_end_at.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        pass
                pill_tracking_rows.append((uid, country_id, _exp_ts, updated_at))

                # Combat state (health/hunger) for /damage-projection — same
                # getUserById response, just persisting two fields that were
                # previously read and discarded.
                health_cur, health_max, hunger_cur, hunger_max = self._extract_combat_state(obj)
                if health_cur is not None or hunger_cur is not None:
                    combat_state_rows.append(
                        (uid, country_id, health_cur, health_max, hunger_cur, hunger_max)
                    )

            if (i + 1) % (batch_size * 2) == 0 and progress_msg:
                batch_done = (i + 1) // batch_size
                try:
                    await progress_msg.edit(
                        content=(
                            f"Refreshing **{country_name}**: "
                            f"batch {batch_done}/{total_batches} "
                            f"({i + 1}/{len(user_ids)} citizens)…"
                        )
                    )
                except Exception:
                    pass
        if pill_tracking_rows:
            try:
                await self._db.bulk_upsert_pill_tracking(pill_tracking_rows)
            except Exception:
                logger.warning("refresh_country: failed to persist pill tracking for %s", country_id)
        if combat_state_rows:
            try:
                await self._db.bulk_upsert_combat_state(combat_state_rows, updated_at)
            except Exception:
                logger.warning("refresh_country: failed to persist combat state for %s", country_id)
        # Remove any citizens not seen in this refresh (they left the country)
        pruned = await self._db.prune_stale_citizens(country_id, updated_at)
        if pruned:
            logger.debug(
                "refresh_country: pruned %d stale citizens for %s", pruned, country_id
            )
        return recorded

    async def refresh_specific_users(self, user_ids: list[str]) -> int:
        """Fetch and persist an explicit list of user IDs via getUserById.

        Exists for citizens who fall out of :meth:`refresh_country`'s per-country
        sweep entirely: ``user.getUsersByCountry`` was confirmed live to omit
        inactive players (``isActive: false``) from its paginated results, even
        though such a player's profile (and companies) are still fully live and
        fetchable by ID. A country-by-country sweep can therefore never see
        them no matter how often it runs — the gap has to be closed by asking
        for these specific IDs directly, which is what this method is for.

        Callers are expected to already know which IDs need backfilling (e.g.
        company owners with no ``citizen_levels`` row yet — see
        ``full_fetcher.fetch_missing_owner_citizenships``); this method does no
        discovery of its own and does not prune anything, since it only ever
        touches the exact rows it was asked about.

        This is also the only path that ever writes ``is_active = False``:
        the per-country sweep in :meth:`refresh_country` can't, because
        ``user.getUsersByCountry`` never returns inactive players to begin
        with. Once written, :meth:`Database.prune_stale_citizens` leaves
        inactive rows alone, so — unlike active citizens — an inactive owner
        only needs to be fetched here once, not every sweep.

        Returns the number of citizens successfully recorded.
        """
        if not user_ids:
            return 0

        updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        inputs = [{"userId": uid} for uid in user_ids]
        # See refresh_country: getUserLite no longer returns "skills", so
        # skill_mode needs the full-profile endpoint here too.
        results = await self._client.batch_get(
            "/user.getUserById", inputs, batch_size=100, chunk_sleep=0.15,
        )

        recorded = 0
        pill_tracking_rows: list[tuple] = []
        combat_state_rows: list[tuple] = []

        for uid, obj in zip(user_ids, results):
            if not isinstance(obj, dict):
                continue
            country_id = obj.get("country")
            if not country_id:
                continue  # can't satisfy citizen_levels' NOT NULL country_id
            country_id = str(country_id)

            lvl = self._extract_level(obj)
            if lvl is not None:
                await self._db.upsert_citizen_level(
                    uid,
                    country_id,
                    lvl,
                    updated_at,
                    skill_mode=self._extract_skill_mode(obj),
                    last_skills_reset_at=self._extract_last_skills_reset_at(obj),
                    citizen_name=self._extract_name(obj),
                    last_login_at=self._extract_last_login_at(obj),
                    avatar_url=self._extract_avatar_url(obj),
                    is_active=self._extract_is_active(obj),
                    is_banned=self._extract_is_banned(obj),
                )
                recorded += 1

            buffs = obj.get("buffs") or {}
            buff_end_at = buffs.get("buffEndAt")
            exp_ts: int | None = None
            if buff_end_at:
                try:
                    exp_ts = int(
                        datetime.fromisoformat(buff_end_at.replace("Z", "+00:00")).timestamp()
                    )
                except Exception:
                    pass
            pill_tracking_rows.append((uid, country_id, exp_ts, updated_at))

            health_cur, health_max, hunger_cur, hunger_max = self._extract_combat_state(obj)
            if health_cur is not None or hunger_cur is not None:
                combat_state_rows.append(
                    (uid, country_id, health_cur, health_max, hunger_cur, hunger_max)
                )

        if pill_tracking_rows:
            try:
                await self._db.bulk_upsert_pill_tracking(pill_tracking_rows)
            except Exception:
                logger.warning("refresh_specific_users: failed to persist pill tracking")
        if combat_state_rows:
            try:
                await self._db.bulk_upsert_combat_state(combat_state_rows, updated_at)
            except Exception:
                logger.warning("refresh_specific_users: failed to persist combat state")

        return recorded

    async def fetch_mu_players_live(
        self, mu_query: str
    ) -> tuple[str | None, list[dict]]:
        """Fetch MU members live from the API for any MU present in known_mus.

        Used as a fallback when the MU has no entries in citizen_levels (e.g.
        foreign MUs). Returns (mu_name, players) in the same format as
        get_mu_readiness_players.
        """
        mu_id, mu_name = await self._db.get_known_mu_by_name(mu_query)
        if not mu_id:
            return None, []

        member_ids, live_name, _avatar = await self._fetch_mu_members_and_name(mu_id)
        effective_name = live_name or mu_name
        if not member_ids:
            return effective_name, []

        # user.getUserLite dropped its "skills" field (API change observed
        # 2026-11); user.getUserById still returns the full per-skill
        # breakdown, so this path — which needs skill_mode — uses that
        # endpoint instead. It's a heavier response, but MU rosters are at
        # most a few hundred players, matching the batch sizes already used
        # for getUserById elsewhere (wealth.py, luck.py, war_sync.py).
        inputs = [{"userId": uid} for uid in member_ids]
        results = await self._client.batch_get(
            "/user.getUserById", inputs, batch_size=100
        )

        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        now_utc_str = now.isoformat()
        debuff_duration = 15.5 * 3600  # seconds
        players: list[dict] = []
        tracking_rows: list[tuple] = []
        no_pill_entries: list[tuple[int, str]] = []  # (player_list_index, user_id)
        for uid, obj in zip(member_ids, results):
            if not isinstance(obj, dict):
                tracking_rows.append((uid, "", None, now_utc_str))
                continue
            level = self._extract_level(obj)
            mode = self._extract_skill_mode(obj) or "eco"
            reset_at = self._extract_last_skills_reset_at(obj)
            name = self._extract_name(obj)
            is_active = self._extract_is_active(obj)
            days_ago: float | None = None
            can_reset = True
            if reset_at and mode != "war":
                try:
                    ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                    days_ago = (now - ts).total_seconds() / 86400
                    can_reset = days_ago >= 7
                except Exception:
                    pass

            # Health and hunger current values
            skills = obj.get("skills") or {}
            health_cur: float | None = None
            health_max: float | None = None
            hunger_cur: float | None = None
            hunger_max: float | None = None
            _h = skills.get("health")
            if isinstance(_h, dict):
                health_cur = _h.get("currentBarValue")
                health_max = _h.get("total")
            _hu = skills.get("hunger")
            if isinstance(_hu, dict):
                hunger_cur = _hu.get("currentBarValue")
                hunger_max = _hu.get("total")

            # Pill / debuff status
            buffs = obj.get("buffs") or {}
            buff_codes = buffs.get("buffCodes") or []
            buff_end_at = buffs.get("buffEndAt")
            pill_expires_ts: float | None = None
            if "cocain" in buff_codes:
                pill_icon = "🟢"
                if buff_end_at:
                    try:
                        pill_expires_ts = datetime.fromisoformat(buff_end_at.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        pass
            elif buff_end_at:
                try:
                    end_ts = datetime.fromisoformat(buff_end_at.replace("Z", "+00:00")).timestamp()
                    debuff_end = end_ts + debuff_duration
                    if debuff_end > now_ts:
                        pill_icon = "🔴"
                        pill_expires_ts = debuff_end
                    else:
                        pill_icon = "⚪"
                except Exception:
                    pill_icon = "⚪"
            else:
                pill_icon = "⚪"

            if pill_icon == "⚪":
                no_pill_entries.append((len(players), uid))
            players.append(
                {
                    "citizen_name": name or "?",
                    "level": level,
                    "skill_mode": mode,
                    "days_ago": days_ago,
                    "can_reset": can_reset,
                    "health_cur": health_cur,
                    "health_max": health_max,
                    "hunger_cur": hunger_cur,
                    "hunger_max": hunger_max,
                    "pill_icon": pill_icon,
                    "pill_expires_ts": pill_expires_ts,
                    "is_active": is_active,
                }
            )
            # Build tracking row (uses raw buff_end_at from API)
            exp: int | None = None
            if buff_end_at:
                try:
                    exp = int(datetime.fromisoformat(buff_end_at.replace("Z", "+00:00")).timestamp())
                except Exception:
                    pass
            tracking_rows.append((uid, "", exp, now_utc_str))

        # DB fallback: live API may not return buffEndAt once the buff has expired.
        # Check stored buff_expires_at to detect players still in the 16h debuff window.
        if no_pill_entries and self._db:
            try:
                check_uids = [uid for _, uid in no_pill_entries]
                tracking_map = await self._db.get_pill_tracking_bulk(check_uids)
                for player_idx, uid in no_pill_entries:
                    exp_db = tracking_map.get(uid)
                    if exp_db is not None and exp_db < now_ts and exp_db + debuff_duration > now_ts:
                        players[player_idx]["pill_icon"] = "🔴"
                        players[player_idx]["pill_expires_ts"] = float(exp_db) + debuff_duration
            except Exception:
                logger.warning("fetch_mu_players_live: DB debuff fallback failed")

        # Persist pill tracking data to DB so non-Dutch MU members are also tracked.
        if self._db and tracking_rows:
            try:
                await self._db.bulk_upsert_pill_tracking(tracking_rows)
            except Exception:
                logger.warning("fetch_mu_players_live: failed to persist pill tracking")

        players.sort(key=lambda p: -(p["level"] or 0))
        return effective_name, players

    async def refresh_mu_memberships(self, country_id: str, mu_entries: list[tuple[str, str]]) -> int:
        """Fetch MU member lists from the API and write mu_id/mu_name to citizen_levels.

        Accepts a list of (mu_id, mu_name) pairs. For each MU, calls /mu.getById to
        get the current member list, then bulk-updates the DB.

        Only citizens already present in citizen_levels are updated (via user_id match).
        Uses a targeted clear so citizens assigned to MUs outside the given list keep
        their MU assignment (set from their profile during refresh_country).

        Returns the number of citizen rows updated.
        """
        if not mu_entries:
            logger.warning("refresh_mu_memberships: empty mu_entries list")
            return 0

        # Clear only citizens currently assigned to one of the known MU IDs.
        # This preserves assignments for citizens in foreign MUs (set from their
        # profile by refresh_country) while allowing stale NL MU entries to be
        # cleaned up correctly.
        known_mu_ids = [mu_id for mu_id, _ in mu_entries]
        await self._db.clear_citizen_mus_for_known_mu_ids(country_id, known_mu_ids)

        updated = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for mu_id, mu_name in mu_entries:
            member_ids, live_name, mu_avatar = await self._fetch_mu_members_and_name(mu_id)
            effective_name = live_name or mu_name
            # Tag this MU as belonging to country_id in the known_mus registry
            await self._db.upsert_known_mu(mu_id, effective_name, now_iso, country_id, avatar_url=mu_avatar)
            for uid in member_ids:
                await self._db.update_citizen_mu(uid, mu_id, effective_name, country_id)
                updated += 1
            await self._db.flush_citizen_levels()
            logger.debug(
                "refresh_mu_memberships: %s → %d members",
                effective_name,
                len(member_ids),
            )

        return updated

    async def sweep_all_mu_memberships(
        self,
        progress_callback=None,
        country_id: str | None = None,
    ) -> tuple[int, int]:
        """Sweep every MU in known_mus: fetch its members, infer home country, update DB.

        If *country_id* is given, only MUs already tagged with that country are swept.

        For each MU:
        1. Call /mu.getById to get the current member list.
        2. Query citizen_levels to find the country_id for as many members as possible.
        3. Tag known_mus.country_id with the majority country found.
        4. Write mu_id / mu_name back to citizen_levels for every matched member.

        *progress_callback*, when supplied, is called as
        ``await progress_callback(done, total, mu_name)`` after each MU.

        Returns ``(mus_tagged, citizens_updated)``.
        """
        from collections import Counter

        mu_rows = await self._db.get_all_known_mu_ids(country_id=country_id)
        total = len(mu_rows)
        now_iso = datetime.now(timezone.utc).isoformat()

        mus_tagged = 0
        citizens_updated = 0

        for idx, (mu_id, mu_name, existing_country_id) in enumerate(mu_rows):
            member_ids, live_name, mu_avatar = await self._fetch_mu_members_and_name(mu_id)
            effective_name = live_name or mu_name

            inferred_country_id: str | None = existing_country_id

            if member_ids:
                country_map = await self._db.get_country_ids_for_users(member_ids)
                if country_map:
                    counter: Counter[str] = Counter(country_map.values())
                    inferred_country_id = counter.most_common(1)[0][0]

                # Update citizen_levels.mu_id for every known member
                for uid in member_ids:
                    await self._db.update_citizen_mu(uid, mu_id, effective_name, inferred_country_id)
                    citizens_updated += 1
                await self._db.flush_citizen_levels()

            await self._db.upsert_known_mu(mu_id, effective_name, now_iso, inferred_country_id, avatar_url=mu_avatar)
            if inferred_country_id:
                mus_tagged += 1

            logger.debug(
                "sweep_all_mu_memberships: [%d/%d] %s → country=%s members=%d",
                idx + 1,
                total,
                effective_name,
                inferred_country_id,
                len(member_ids),
            )

            if progress_callback:
                await progress_callback(idx + 1, total, effective_name)

        await self._db.flush_known_mus()
        return mus_tagged, citizens_updated

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    async def _fetch_mu_members_and_name(
        self, mu_id: str
    ) -> tuple[list[str], str | None, str | None]:
        """Call /mu.getById and return (member user IDs, MU name, avatarUrl)."""
        user_ids: list[str] = []
        mu_name: str | None = None
        avatar_url: str | None = None
        # mu.getById returns the full MU including its members list
        try:
            resp = await self._client.get(
                "/mu.getById",
                params={"input": json.dumps({"muId": mu_id})},
            )
        except Exception as exc:
            logger.warning(
                "_fetch_mu_members_and_name(%s): request failed: %s", mu_id, exc
            )
            return user_ids, mu_name

        # Navigate the response to find the members list
        data = resp
        if isinstance(resp, dict):
            for key in ("result", "data"):
                v = resp.get(key)
                if isinstance(v, dict):
                    data = v.get("data", v)
                    break

        members: list = []
        if isinstance(data, dict):
            raw_name = data.get("name") or data.get("title")
            if isinstance(raw_name, str) and raw_name:
                mu_name = raw_name
            raw_avatar = data.get("avatarUrl")
            if isinstance(raw_avatar, str) and raw_avatar:
                avatar_url = raw_avatar
            for key in ("members", "citizenIds", "userIds", "users"):
                v = data.get(key)
                if isinstance(v, list):
                    members = v
                    break

        for entry in members:
            if isinstance(entry, str):
                user_ids.append(entry)
            elif isinstance(entry, dict):
                uid = (
                    entry.get("userId")
                    or entry.get("_id")
                    or entry.get("id")
                    or entry.get("citizenId")
                )
                if uid:
                    user_ids.append(str(uid))

        return user_ids, mu_name, avatar_url

    async def _fetch_user_ids(self, country_id: str) -> list[str]:
        """Paginate /user.getUsersByCountry and return all user IDs."""
        user_ids: list[str] = []
        cursor = None
        while True:
            params: dict = {"countryId": country_id, "limit": 100}
            if cursor:
                params["cursor"] = cursor

            resp = await self._client.get(
                "/user.getUsersByCountry",
                params={"input": json.dumps(params)},
            )

            data_obj = resp
            if isinstance(resp, dict):
                for key in ("result", "data"):
                    v = resp.get(key)
                    if isinstance(v, dict):
                        data_obj = v.get("data", v)
                        break

            users: list = []
            next_cursor = None
            if isinstance(data_obj, list):
                users = data_obj
            elif isinstance(data_obj, dict):
                for key in ("items", "users", "data", "result"):
                    v = data_obj.get(key)
                    if isinstance(v, list):
                        users = v
                        break
                next_cursor = (
                    data_obj.get("nextCursor")
                    or data_obj.get("cursor")
                    or data_obj.get("next")
                )

            for user in users:
                if isinstance(user, dict):
                    uid = user.get("_id") or user.get("id") or user.get("userId")
                    if uid:
                        user_ids.append(str(uid))

            if not next_cursor or not users:
                break
            cursor = next_cursor
        return user_ids

    @staticmethod
    def _extract_level(obj: Any) -> int | None:
        """Pull the level integer out of a getUserLite result dict."""
        if not isinstance(obj, dict):
            return None
        try:
            return int(obj["leveling"]["level"])
        except (KeyError, TypeError, ValueError):
            pass
        if obj.get("level") is not None:
            try:
                return int(obj["level"])
            except (ValueError, TypeError):
                pass
        try:
            return int(obj["rankings"]["userLevel"]["value"])
        except (KeyError, TypeError, ValueError):
            pass
        return None

    @staticmethod
    def _extract_skill_mode(obj: Any) -> str | None:
        """Classify a player as 'eco' or 'war' based on where they spent skill points.

        Handles three known API shapes for the ``skills`` field:
        - dict-of-dicts : {"attack": {"level": 5}, "entrepreneurship": {"level": 3}}
        - flat dict      : {"attack": 5, "entrepreneurship": 3}
        - list-of-dicts  : [{"name": "attack", "level": 5}, ...]

        Also checks root-level ``skillMode`` / ``mode`` fields as a direct fallback.

        Points spent in skill at level L = L*(L+1)//2.
        Eco skills : entrepreneurship, energy, production, companies,
                     management, lootChance.
        War skills : attack, health, hunger, criticalChance, criticalDamages,
                     armor, precision, dodge.
        Ties go to eco.

        lootChance counts as eco (despite living in the fighting skill
        section) to catch the "lootbuild" meta: heavy loot-chance investment
        with only light actual combat/eco spend, which otherwise got
        misclassified as war even though these players function like eco
        players for readiness purposes (they don't fight).

        Returns None when no skill data is available.
        """
        if not isinstance(obj, dict):
            return None

        # ── Direct mode field (cheapest, try first) ──────────────────────
        for key in ("skillMode", "skill_mode", "mode"):
            v = obj.get(key)
            if isinstance(v, str) and v.lower() in ("eco", "war"):
                return v.lower()

        skills = obj.get("skills")
        if skills is None:
            return None

        eco_names = {
            "entrepreneurship",
            "energy",
            "production",
            "companies",
            "management",
            "lootChance",
        }
        war_names = {
            "attack",
            "health",
            "hunger",
            "criticalChance",
            "criticalDamages",
            "armor",
            "precision",
            "dodge",
        }
        eco_pts = 0
        war_pts = 0

        # ── Check for direct mode inside the skills object ────────────────
        if isinstance(skills, dict):
            for key in ("mode", "skillMode", "currentMode"):
                v = skills.get(key)
                if isinstance(v, str) and v.lower() in ("eco", "war"):
                    return v.lower()

        # ── Normalise to iterable of (name, level) ───────────────────────
        pairs: list[tuple[str, int]] = []
        if isinstance(skills, dict):
            for name, sdata in skills.items():
                if isinstance(sdata, dict):
                    lv = int(sdata.get("level") or 0)
                elif isinstance(sdata, (int, float)):
                    lv = int(sdata)
                else:
                    continue
                pairs.append((name, lv))
        elif isinstance(skills, list):
            for entry in skills:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name") or entry.get("skill") or entry.get("type")
                lv = int(entry.get("level") or entry.get("value") or 0)
                if name:
                    pairs.append((str(name), lv))
        else:
            return None

        if not pairs:
            return None

        for name, lv in pairs:
            pts = lv * (lv + 1) // 2
            if name in eco_names:
                eco_pts += pts
            elif name in war_names:
                war_pts += pts

        # If we found skill data but all levels are 0 (brand-new player), return None
        # rather than defaulting to eco which would be misleading.
        if eco_pts == 0 and war_pts == 0:
            return None

        return "eco" if eco_pts >= war_pts else "war"

    @staticmethod
    def _extract_combat_state(
        obj: Any,
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """Pull (health_cur, health_max, hunger_cur, hunger_max) from getUserLite.

        ``currentBarValue`` is the live amount, ``total`` the max (already the
        convention used by :meth:`fetch_mu_players_live` below). Returns all
        None when the skills object is absent or malformed.
        """
        if not isinstance(obj, dict):
            return None, None, None, None
        skills = obj.get("skills")
        if not isinstance(skills, dict):
            return None, None, None, None

        def _bar(entry: Any) -> tuple[float | None, float | None]:
            if not isinstance(entry, dict):
                return None, None
            cur = entry.get("currentBarValue")
            mx = entry.get("total")
            try:
                cur = float(cur) if cur is not None else None
            except (TypeError, ValueError):
                cur = None
            try:
                mx = float(mx) if mx is not None else None
            except (TypeError, ValueError):
                mx = None
            return cur, mx

        health_cur, health_max = _bar(skills.get("health"))
        hunger_cur, hunger_max = _bar(skills.get("hunger"))
        return health_cur, health_max, hunger_cur, hunger_max

    @staticmethod
    def _extract_avatar_url(obj: Any) -> str | None:
        """Pull the avatarUrl from a getUserLite result."""
        if not isinstance(obj, dict):
            return None
        return obj.get("avatarUrl") or None

    @staticmethod
    def _extract_is_active(obj: Any) -> bool:
        """Pull ``isActive`` from a getUserLite result.

        Confirmed live to always be present (boolean) on both listing-endpoint
        and by-ID responses. Defaults to True when missing or malformed so a
        future API change degrades to "never excluded" rather than silently
        marking everyone inactive.
        """
        if not isinstance(obj, dict):
            return True
        val = obj.get("isActive")
        return val if isinstance(val, bool) else True

    @staticmethod
    def _extract_is_banned(obj: Any) -> bool:
        """Pull ``infos.isBanned`` from a getUserLite result.

        Confirmed live present on both listing-endpoint and by-ID responses
        (a banned player can also be ``isActive: false`` at the same time).
        Defaults to False when missing or malformed — a schema change should
        degrade to "never excluded", never to "everyone banned".
        """
        if not isinstance(obj, dict):
            return False
        infos = obj.get("infos")
        if isinstance(infos, dict):
            val = infos.get("isBanned")
            if isinstance(val, bool):
                return val
        return False

    @staticmethod
    def _extract_name(obj: Any) -> str | None:
        """Pull the player name from a getUserLite result."""
        if not isinstance(obj, dict):
            return None
        for key in ("name", "username", "displayName", "nick"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
        for sub in ("profile", "user"):
            sub_obj = obj.get(sub)
            if isinstance(sub_obj, dict):
                for key in ("name", "username", "displayName"):
                    val = sub_obj.get(key)
                    if isinstance(val, str) and val:
                        return val
        return None

    @staticmethod
    def _extract_last_skills_reset_at(obj: Any) -> str | None:
        """Pull the lastSkillsResetAt ISO timestamp from a getUserLite result.

        The value lives inside the nested ``dates`` object and is absent when
        the player has never reset their skills.
        """
        if not isinstance(obj, dict):
            return None
        dates = obj.get("dates")
        if not isinstance(dates, dict):
            return None
        val = dates.get("lastSkillsResetAt")
        if isinstance(val, str) and val:
            return val
        return None

    @staticmethod
    def _extract_mu_info(obj: Any) -> tuple[str | None, str | None]:
        """Extract (mu_id, mu_name) from a getUserLite result dict.

        Tries common nested and flat field names so we're resilient to API
        changes. Returns (None, None) when no MU info is present.
        """
        if not isinstance(obj, dict):
            return None, None
        # Nested MU object under various keys
        for mu_key in ("mu", "militaryUnit", "regiment", "unit", "militaryUnits"):
            mu_obj = obj.get(mu_key)
            if isinstance(mu_obj, dict):
                mu_id = mu_obj.get("_id") or mu_obj.get("id") or mu_obj.get("muId")
                mu_name = (
                    mu_obj.get("name") or mu_obj.get("title") or mu_obj.get("muName")
                )
                if mu_id:
                    return str(mu_id), str(mu_name) if mu_name else None
        # Flat fields at root level
        mu_id_flat = (
            obj.get("muId") or obj.get("militaryUnitId") or obj.get("regimentId")
        )
        mu_name_flat = (
            obj.get("muName") or obj.get("militaryUnitName") or obj.get("regimentName")
        )
        if mu_id_flat:
            return str(mu_id_flat), str(mu_name_flat) if mu_name_flat else None
        return None, None

    @staticmethod
    def _extract_last_login_at(obj: Any) -> str | None:
        """Pull the last-login ISO timestamp from a getUserLite result.

        Tries several candidate field names in the ``dates`` sub-object and at
        the root level, since the exact key varies across API versions.
        """
        if not isinstance(obj, dict):
            return None
        # Try nested ``dates`` object first (same location as lastSkillsResetAt)
        dates = obj.get("dates")
        if isinstance(dates, dict):
            for key in (
                "lastConnectionAt",
                "lastLoginAt",
                "lastSeenAt",
                "lastOnlineAt",
                "lastActiveAt",
                "lastLogin",
            ):
                val = dates.get(key)
                if isinstance(val, str) and val:
                    return val
        # Fall back to root-level keys
        for key in (
            "lastConnectionAt",
            "lastLoginAt",
            "lastSeenAt",
            "lastOnlineAt",
            "lastActiveAt",
            "lastLogin",
        ):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
        return None

    # ------------------------------------------------------------------ #
    # Article tips sweep                                                   #
    # ------------------------------------------------------------------ #

    async def sweep_article_tips(
        self,
        country_id: str | None = None,
        progress_callback=None,
    ) -> tuple[int, int]:
        """Scan known citizens' transactions for outgoing article tips and store them.

        Incremental behaviour:
        - Citizens who have stored tips are re-scanned from their latest known tip
          onwards (using MAX(tip_at) as a cutoff — transactions older than this are
          skipped during pagination).
        - Citizens who have never tipped get a full scan the first time; afterwards
          their scan timestamp is recorded and they are skipped for RESCAN_DAYS so
          subsequent sweeps don't re-hit the API for every zero-tip citizen.

        *progress_callback* is called as
        ``await progress_callback(done, total, citizen_name)`` after each citizen.

        Returns ``(citizens_scanned, tips_stored)``.
        """
        from datetime import datetime, timedelta, timezone

        RESCAN_DAYS = 7  # skip zero-tip citizens re-scanned within this many days

        if country_id:
            rows = await self._db.get_citizens_in_country(country_id)
        else:
            rows = await self._db.get_all_citizens_for_tips_scan()

        total = len(rows)
        now_iso = datetime.now(timezone.utc).isoformat()
        rescan_cutoff = (datetime.now(timezone.utc) - timedelta(days=RESCAN_DAYS)).isoformat()

        citizens_scanned = 0
        tips_stored = 0

        for idx, (user_id, c_country_id, citizen_name) in enumerate(rows):
            cutoff = await self._db.get_latest_tip_at_for_user(user_id)

            # If this citizen has never tipped, check if we scanned them recently.
            # If so, skip them to avoid redundant full API scans every sweep.
            if cutoff is None:
                last_scan = await self._db.get_last_scanned_at(user_id)
                if last_scan and last_scan >= rescan_cutoff:
                    # scanned recently, no tips found then — skip
                    if progress_callback:
                        await progress_callback(idx + 1, total, citizen_name or user_id)
                    continue

            page_tips = await self._fetch_tip_transactions(
                user_id, c_country_id, citizen_name, now_iso, cutoff=cutoff
            )
            for tip_record in page_tips:
                inserted = await self._db.upsert_article_tip(*tip_record)
                if inserted:
                    tips_stored += 1

            # Record that this citizen was scanned so zero-tip citizens are skipped
            # for RESCAN_DAYS on the next sweep.
            await self._db.upsert_scan_timestamp(user_id, now_iso)
            await self._db.flush_article_tips()
            citizens_scanned += 1

            logger.debug(
                "sweep_article_tips: [%d/%d] %s → %d tips",
                idx + 1, total, citizen_name or user_id, len(page_tips),
            )

            if progress_callback:
                await progress_callback(idx + 1, total, citizen_name or user_id)

        # Bulk-stamp any citizen not explicitly tracked yet (e.g. everyone scanned
        # by a previous run of the old code that had no timestamp tracking).
        # OR IGNORE means already-tracked citizens keep their existing timestamp.
        await self._db.bulk_init_scan_timestamps(now_iso)

        return citizens_scanned, tips_stored

    async def _fetch_tip_transactions(
        self,
        user_id: str,
        country_id: str | None,
        citizen_name: str | None,
        recorded_at: str,
        *,
        cutoff: str | None = None,
    ) -> list[tuple]:
        """Fetch outgoing articleTip transactions for a single user.

        Each returned tuple is: (user_id, country_id, citizen_name, amount, tip_at, recorded_at)

        If *cutoff* is given (ISO timestamp of the latest tip already stored for
        this user), pagination stops as soon as we encounter a transaction with
        ``createdAt <= cutoff`` so that we only fetch *new* records.
        """
        records: list[tuple] = []
        cursor: str | None = None
        page_size = 100
        max_pages = 200  # safety cap: 20 000 tip transactions per citizen

        for _ in range(max_pages):
            payload: dict = {
                "userId": user_id,
                "transactionType": "articleTip",
                "limit": page_size,
            }
            if cursor:
                payload["cursor"] = cursor

            try:
                raw = await self._client.post(
                    "/transaction.getPaginatedTransactions",
                    json=payload,
                )
            except Exception as exc:
                logger.warning(
                    "_fetch_tip_transactions(%s): request failed: %s", user_id, exc
                )
                break

            # Unwrap tRPC envelope
            if isinstance(raw, list):
                items = raw
                cursor = None
            elif isinstance(raw, dict):
                data = raw.get("result", {}).get("data", raw)
                if not isinstance(data, dict):
                    data = {}
                items = (
                    data.get("items")
                    or data.get("transactions")
                    or data.get("results")
                    or []
                )
                cursor = data.get("nextCursor") or data.get("cursor")
            else:
                break

            for tx in items:
                if not isinstance(tx, dict):
                    continue
                tx_type = tx.get("type") or tx.get("transactionType") or ""
                if tx_type != "articleTip":
                    continue
                money = tx.get("money")
                if money is None:
                    continue
                money_f = float(money)
                # Only outgoing tips (tipper's perspective → money is negative)
                if money_f >= 0:
                    continue
                amount = abs(money_f)
                tip_at = (
                    tx.get("createdAt")
                    or tx.get("date")
                    or tx.get("timestamp")
                    or recorded_at
                )
                # Incremental scan: stop once we reach already-processed transactions.
                # Transactions are returned newest-first, so the first tx older than
                # the cutoff means all remaining are already stored.
                if cutoff and tip_at and tip_at <= cutoff:
                    cursor = None  # signal to stop pagination
                    break
                records.append(
                    (user_id, country_id, citizen_name, amount, tip_at, recorded_at)
                )

            if not cursor or not items:
                break

        return records
