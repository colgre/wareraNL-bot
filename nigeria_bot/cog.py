"""Verification cog for the Nigerian WarEra Discord bot.

Flow per verification type:
  1. User clicks button → private ticket channel created immediately
  2. Bot asks user to send a screenshot of their WarEra profile
  3. Staff reviews screenshot → runs /approve @user url:<profile_url>
  4. Bot fetches in-game username from URL, gives roles, sets nickname, closes ticket

Commands (staff-only):
  /postverificatie     — Post the verification buttons in the configured channel
  /approve @user url:  — Approve a ticket; fetch username from WarEra URL
  /deny @user [reden]  — Deny and close a ticket
  /setup_ambassades    — Create missing embassy channels for existing ambassador roles
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from nigeria_bot.db import (
    add_pending_ticket_deletion,
    get_all_links,
    get_all_links_full,
    get_link,
    get_pending_ticket_deletions,
    remove_pending_ticket_deletion,
    save_link,
)

logger = logging.getLogger("nigeria_bot.cog")

# ── Configuration ─────────────────────────────────────────────────────────────

GUILD_ID               = 1495375733323989074
VERIFICATION_CHANNEL_ID = 1521084795839447210
EMBASSY_CATEGORY_ID    = 1495693042194317332

NIGERIAN_ROLE_ID        = 1495692212594409482   # real Nigerian players
DUTCH_NIGERIAN_ROLE_IDS = [1495692212594409482, 1503755103037817052]  # Dutch players with Nigerian nationality
VAKANTIEGANGER_ROLE_ID  = DUTCH_NIGERIAN_ROLE_IDS[1]  # "Vakantieganger" — Dutch player currently in Nigeria
DUTCH_ROLE_ID           = 1495692245519699978
VERIFIED_ROLE_ID        = 1521895797757575168   # given to everyone on approval

# In-game country IDs, for the nationality-role sync in nick_sync.
NIGERIA_COUNTRY_ID = "683ddd2c24b5a2e114af15fa"
NL_COUNTRY_ID      = "6813b6d446e731854c7ac7a0"

STAFF_ROLE_IDS = [1495692303367540767, 1495692272728150016, 1495692461375357009, 1495847731963494400]

# Channels where the forbidden-word GIF reactions stay silent (e.g. #gheim).
NO_REACTION_CHANNEL_IDS = {1523076659648008362}

# ── @Roger business-proposal easter egg ───────────────────────────────────────
# Tagging the bot makes it ask "Interested in a business proposal?".  Saying
# yes shortly afterwards gets one of the punchlines below.
#
# How long a proposal stays open for an answer.  Without a window a stray "ja"
# hours later would land the punchline into an unrelated conversation — "ja" is
# far too common a Dutch word to accept indefinitely.
BUSINESS_PROPOSAL_WINDOW_SECONDS = 300

# Anything here counts as saying yes. Matched per whitespace token with
# surrounding punctuation stripped, so "Ja!" and "yes." both land.
BUSINESS_PROPOSAL_YES_WORDS = frozenset({
    "ja", "jazeker", "jawel", "yes", "yeah", "yep", "sure",
})

BUSINESS_PROPOSAL_REPLIES: tuple[str, ...] = (
    "Excellent! I am Prince Roger of Nigeria. I require only your bank details, "
    "your mother's maiden name, and a small processing fee of 500 CC. "
    "Your $4,500,000 USD will arrive within 3-5 business days. 🤝",
    "Wonderful choice! Simply transfer 250 CC to unlock your inheritance of "
    "$12,000,000 USD from a distant uncle you have never heard of. "
    "This is 100% legitimate. I am a certified prince. 👑",
    "Fantastic! Step 1: send me 1000 CC. Step 2: there is no step 2. "
    "Congratulations on your new fortune! 🎉",
    "Splendid! Your funds are held at the Central Bank of Lagos and require "
    "only a modest clearance fee. Please do not tell your government, "
    "your bank, or anyone with common sense. 🤫",
    "Great! I have transferred $9,000,000 USD to your account. It has bounced "
    "17 times. Please send 750 CC to fix the pipes. 🚰",
    "Perfect! You are now Regional Vice-Director of my import/export company. "
    "Your first task: forward this message to 10 friends. Your second task: "
    "pay the onboarding fee. 📈",
    "Marvellous! I only need a photo of your ID, your bank card (both sides), "
    "and the little number on the back. Standard procedure, nothing suspicious. 📸",
    "Superb! The paperwork is complete. There is only the notary fee, the "
    "transfer fee, the anti-fraud fee, and the fee-processing fee. "
    "Then you are rich! 💰",
)

_PUNCT_EDGES = re.compile(r"^\W+|\W+$", re.UNICODE)


def _says_yes(content: str) -> bool:
    """True when *content* contains a bare yes/ja token.

    Punctuation is stripped from each token's edges so "Ja!" and "yes." count,
    while the whole-token rule still keeps it from firing inside longer words
    such as "januari" or "jaloers".
    """
    return any(
        _PUNCT_EDGES.sub("", token) in BUSINESS_PROPOSAL_YES_WORDS
        for token in content.lower().split()
    )


# Word groups that trigger the "not allowed" GIF reply. Plain page URLs
# (tenor.com/view/..., giphy.com/gifs/...) — Discord auto-unfurls these into
# a GIF embed when sent as their own message; hotlinking the direct media
# file via a manually built embed did not render.
# send_text: whether the "Dat woord is niet toegestaan!" line is sent before
# the GIF — False means the GIF is posted on its own.
FORBIDDEN_WORD_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...], bool], ...] = (
    (
        ("eco", "democratie", "revolutie", "coup", "coups"),
        (
            "https://tenor.com/view/jiskefet-debiteuren-crediteuren-korfbal-dat-mag-dus-niet-gif-17582094",
            "https://tenor.com/view/no-party-geen-feest-party-mag-nie-mag-niet-gif-18158017",
            "https://tenor.com/view/get-the-fuck-out-gtfo-get-out-go-away-gif-8055436",
            "https://tenor.com/view/get-out-the-lion-king-comedy-humor-throw-gif-9615975",
            "https://tenor.com/view/damin-toell-get-out-gif-2960077830144535500",
            "https://tenor.com/view/no-babyjake-nope-not-allowed-cant-do-that-gif-21337058",
            "https://tenor.com/view/out-gif-21952548",
            "https://tenor.com/view/get-out-leave-gif-5785321051025239919",
            "https://tenor.com/view/get-out-gif-4007870892487612180",
        ),
        True,
    ),
    (
        ("dictatuur",),
        (
            "https://giphy.com/gifs/reactionseditor-yes-awesome-3ohzdIuqJoo8QdKlnW",
            "https://giphy.com/gifs/dYZuqJLDVsWMLWyIxJ",
            "https://giphy.com/gifs/primevideo-2020-borat-subsequent-moviefilm-Od0QRnzwRBYmDU3eEO",
            "https://giphy.com/gifs/vh1-reactions-love-and-hip-hop-lhhatl-4fYg3bSQuv8K2oZ7LK",
        ),
        False,
    ),
    (
        ("corruptie", "corruption"),
        (
            "https://giphy.com/gifs/money-make-it-rain-MFsqcBSoOKPbjtmvWz",
            "https://giphy.com/gifs/reaction-kj41Ti8GLVs1STX0bH",
            "https://giphy.com/gifs/trump-fifa-african-gGh45VhxyX67eIfnub",
        ),
        False,
    ),
    (
        ("opstand",),
        (
            "https://giphy.com/gifs/netflix-adam-sandler-spaceman-im-not-allowing-that-TL8lZcqnQP0uDnYoGM",
        ),
        True,
    ),
    (
        (
            "gelijkwaardigheid",
            "emancipatie",
            "stemmen",
            "vrijheid",
            "loonsverhoging",
            "referendum",
        ),
        (
            "https://giphy.com/gifs/FlexxedTV-wow-whats-that-XPrN8DrbFIqjbBo8rK",
            # TODO: one gif here was a malformed/truncated URL ("tS0dsIv2h4o2E4tc")
            # — needs the real link before it can be added.
            "https://giphy.com/gifs/cbc-funny-comedy-l1J9DlCFh2h90HrnW",
            "https://giphy.com/gifs/iiTXaJVjiSHew",
            "https://giphy.com/gifs/HelpUsDefend-fish-scam-scammer-YQ2IS26vDsr2gDSCHZ",
        ),
        True,
    ),
    (
        ("debat",),
        (
            # klipy.com rather than tenor/giphy — checked that it serves the
            # same og:image / og:video metadata Discord needs to unfurl it.
            "https://klipy.com/gifs/dojo-dummy",
        ),
        True,
    ),
    (
        ("generaal", "dictator"),
        (
            "https://giphy.com/gifs/GoArmy-respect-salute-gesture-fxsAcheaMi1PnxPJaf",
            "https://giphy.com/gifs/france-mbappe-quipe-de-btpfcvZPLA5w9zQ0Tj",
        ),
        False,
    ),
    (
        ("staking", "vakbond"),
        (
            "https://giphy.com/gifs/netflix-umbrella-academy-hargreeves-4-t4HFdwzbg4x8XNZF8I",
            "https://giphy.com/gifs/no-not-at-all-KxtqqFuP4a31rZOhyq",
        ),
        True,
    ),
    (
        ("minimumloon", "overuren"),
        (
            "https://giphy.com/gifs/sunnyfxx-always-sunny-iasip-its-in-philadelphia-FEfO77A2PKgME2QZgP",
            "https://giphy.com/gifs/cbc-schitts-creek-MBxYX5HEvi0svTHNgV",
        ),
        False,
    ),
    (
        ("kiesrecht",),
        (
            "https://giphy.com/gifs/cbc-funny-comedy-2yA8KXwI7QBykLizf8",
        ),
        True,
    ),
    (
        ("martijn",),
        (
            "https://giphy.com/gifs/stfu-FzyoyDSaAsjHG",
        ),
        True,
    ),
    (
        ("pyramidscheme", "pyramid scheme", "ponzi"),
        (
            "https://giphy.com/gifs/scheme-7wToiGB3M5wME",
        ),
        False,
    ),
    (
        ("419",),
        (
            "https://tenor.com/view/money-rain-make-it-with-gif-10901444",
        ),
        False,
    ),
)

# Resolved from this file's location, not cwd, so it works regardless of the
# working directory the process was launched from.
_PROJECT_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GEAR_IMAGE_PATH = os.path.join(_PROJECT_ROOT, "equipment-item-icons", "gear.png")
_MYTHIC_GEAR_IMAGE_PATH = os.path.join(_PROJECT_ROOT, "nigeria_bot", "mythicgear.png")

AMBASSADOR_ROLES: dict[str, int] = {
    "Netherlands":             1495785962234577037,
    "Angola":                  1495786072305569863,
    "Egypt":                   1495885668600254464,
    "South Korea":             1496121931143975063,
    "Guinea-Bissau":           1496464508565065908,
    "Belgium":                 1496580159497830452,
    "Congo":                   1496823235113910446,
    "Central African Republic": 1496923313916608582,
    "Equatorial Guinea":       1497481619966263426,
    "Benin":                   1497941496140267520,
    "Argentina":               1499477796832149604,
    "Uganda":                  1502885209748672553,
    "Philippines":             1504021543922040862,
    "Morocco":                 1505464818319233137,
    "Tanzania":                1508170151827345479,
    "Mauritania":              1511604948062965850,
    "Colombia":                1513609516917325915,
    "Italy":                   1519206306395979837,
    "Togo":                    1520860784823894119,
}

TICKET_CATEGORY_NAME = "🔐 Verificaties"
WARERA_API_BASE      = "https://api2.warera.io/trpc"
WARERA_API_KEY       = os.environ.get("WARERA_API_KEY", "")

# Delay between consecutive API calls during bulk sync.
# With API key: 0.15 s ≈ 400 req/min. Without: 0.7 s ≈ 85 req/min (under the 100/min anon limit).
_SYNC_DELAY = 0.15 if WARERA_API_KEY else 0.7

logger.info(
    "WARERA_API_KEY %s — using %.2fs inter-request delay for nick sync",
    "loaded" if WARERA_API_KEY else "NOT SET",
    _SYNC_DELAY,
)

_WARERA_URL_RE = re.compile(
    r"https?://(?:app\.)?warera\.io/user/([0-9a-f]{24})", re.IGNORECASE
)

# ── WarEra API ────────────────────────────────────────────────────────────────

async def _fetch_warera_username(
    user_id: str,
    session: aiohttp.ClientSession | None = None,
) -> str | None:
    """Fetch the in-game username for a WarEra user ID. Returns None on failure.

    Sends the API key (x-api-key header) if WARERA_API_KEY is set, which raises
    the rate limit from 100 req/min (anonymous) to the authenticated limit.
    Retries once after a short wait on HTTP 429.
    """
    url = f"{WARERA_API_BASE}/user.getUserLite"
    params = {"input": json.dumps({"userId": user_id})}
    headers = {"x-api-key": WARERA_API_KEY} if WARERA_API_KEY else {}

    async def _do_fetch(sess: aiohttp.ClientSession) -> str | None:
        for attempt in range(2):  # one retry on 429
            try:
                async with sess.get(
                    url, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 429:
                        if attempt == 0:
                            await asyncio.sleep(2)
                            continue
                        body = await resp.text()
                        logger.warning(
                            "_fetch_warera_username: still 429 after retry for %s — %s",
                            user_id, body[:200],
                        )
                        return None
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            "_fetch_warera_username: HTTP %d for %s — %s",
                            resp.status, user_id, body[:200],
                        )
                        return None
                    data = await resp.json()
                    inner = (
                        data.get("result", {}).get("data")
                        or data.get("result")
                        or data
                    )
                    if isinstance(inner, dict):
                        name = inner.get("username") or inner.get("name")
                        if name:
                            return str(name)
                        logger.warning(
                            "_fetch_warera_username: no username field for %s — keys: %s",
                            user_id, list(inner.keys()),
                        )
                        return None
                    logger.warning(
                        "_fetch_warera_username: unexpected response type %s for %s",
                        type(inner).__name__, user_id,
                    )
                    return None
            except Exception as exc:
                logger.warning("_fetch_warera_username: exception for %s: %s", user_id, exc)
                return None
        return None

    if session is not None:
        return await _do_fetch(session)
    async with aiohttp.ClientSession() as sess:
        return await _do_fetch(sess)


async def _fetch_warera_country(
    user_id: str,
    session: aiohttp.ClientSession | None = None,
) -> str | None:
    """Fetch the current in-game country ID for a WarEra user ID. Returns None on failure.

    Same endpoint as _fetch_warera_username (user.getUserLite already
    includes a "country" field, confirmed live) — kept as a separate
    function rather than extending that one so its four existing callers
    are untouched.
    """
    url = f"{WARERA_API_BASE}/user.getUserLite"
    params = {"input": json.dumps({"userId": user_id})}
    headers = {"x-api-key": WARERA_API_KEY} if WARERA_API_KEY else {}

    async def _do_fetch(sess: aiohttp.ClientSession) -> str | None:
        for attempt in range(2):  # one retry on 429
            try:
                async with sess.get(
                    url, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 429:
                        if attempt == 0:
                            await asyncio.sleep(2)
                            continue
                        return None
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    inner = (
                        data.get("result", {}).get("data")
                        or data.get("result")
                        or data
                    )
                    if isinstance(inner, dict):
                        country = inner.get("country")
                        return str(country) if country else None
                    return None
            except Exception as exc:
                logger.warning("_fetch_warera_country: exception for %s: %s", user_id, exc)
                return None
        return None

    if session is not None:
        return await _do_fetch(session)
    async with aiohttp.ClientSession() as sess:
        return await _do_fetch(sess)


async def _sync_nationality_role(
    member: discord.Member, country_id: str, guild: discord.Guild
) -> str | None:
    """Swap DUTCH_ROLE_ID <-> (NIGERIAN_ROLE_ID + VAKANTIEGANGER_ROLE_ID) to
    match a verified member's CURRENT in-game country. Returns a short
    description of the change made, or None if nothing changed.

    Only acts on members already holding one of these two role combos —
    deliberately leaves a "real" Nigerian (NIGERIAN_ROLE_ID without
    VAKANTIEGANGER_ROLE_ID — verified via the plain "nigerian" ticket type)
    untouched even if their in-game country changes: that role marks
    community identity, not a live nationality tracker. Vakantieganger, by
    contrast, exists specifically to mark a Dutch player's *temporary*
    Nigerian residency and should follow them home the moment it ends.
    """
    role_ids = {r.id for r in member.roles}
    has_dutch = DUTCH_ROLE_ID in role_ids
    has_dutch_nigerian = (
        NIGERIAN_ROLE_ID in role_ids and VAKANTIEGANGER_ROLE_ID in role_ids
    )

    if has_dutch and country_id == NIGERIA_COUNTRY_ID:
        to_remove_ids = [DUTCH_ROLE_ID]
        to_add_ids = [NIGERIAN_ROLE_ID, VAKANTIEGANGER_ROLE_ID]
        change = "Nederlander → Nigeriaan (vakantieganger)"
    elif has_dutch_nigerian and country_id == NL_COUNTRY_ID:
        to_remove_ids = [NIGERIAN_ROLE_ID, VAKANTIEGANGER_ROLE_ID]
        to_add_ids = [DUTCH_ROLE_ID]
        change = "Nigeriaan (vakantieganger) → Nederlander"
    else:
        return None

    to_remove = [r for rid in to_remove_ids if (r := guild.get_role(rid))]
    to_add = [r for rid in to_add_ids if (r := guild.get_role(rid))]
    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="Nationaliteitswissel in-game")
        if to_add:
            await member.add_roles(*to_add, reason="Nationaliteitswissel in-game")
    except discord.Forbidden:
        logger.warning(
            "_sync_nationality_role: no permission to update roles for %s", member.id
        )
        return None
    return change


def _extract_warera_id(url: str) -> str | None:
    m = _WARERA_URL_RE.search(url)
    return m.group(1) if m else None

# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_topic(topic: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in (topic or "").split("|"):
        if "=" in part:
            k, _, v = part.partition("=")
            result[k.strip()] = v.strip()
    return result


def _is_staff(member: discord.Member) -> bool:
    return any(r.id in STAFF_ROLE_IDS for r in member.roles)


def _ticket_is_english(ticket_type: str) -> bool:
    """Nigerian and embassy tickets use English; Dutch/Dutch-Nigerian stay Dutch."""
    return ticket_type in ("nigerian", "embassy")


def _mentions_bot_in_text(content: str, bot_user) -> bool:
    """True only when the author literally typed ``@Roger`` in the message.

    Deliberately inspects the raw message text rather than
    ``message.mentions``: Discord adds the bot to ``mentions`` when someone
    merely *replies* to one of its messages, which would otherwise make the
    bot answer itself into every thread it touched.  A reply's ping never puts
    the ``<@id>`` token in the content, so checking the text is the difference
    between "was addressed" and "was referenced".

    Role mentions (``<@&id>``), @everyone and @here are all a different shape
    and so never match here either.  Reactions don't produce a message at all,
    so they can't reach this code path.
    """
    if bot_user is None:
        return False
    return f"<@{bot_user.id}>" in content or f"<@!{bot_user.id}>" in content


async def _get_or_create_ticket_category(guild: discord.Guild) -> discord.CategoryChannel:
    for cat in guild.categories:
        if cat.name == TICKET_CATEGORY_NAME:
            return cat
    return await guild.create_category(
        TICKET_CATEGORY_NAME,
        reason="Verificatie ticket categorie automatisch aangemaakt",
    )


async def _find_ticket_channel(
    guild: discord.Guild, user: discord.Member
) -> discord.TextChannel | None:
    for cat in guild.categories:
        if cat.name != TICKET_CATEGORY_NAME:
            continue
        for channel in cat.text_channels:
            if _parse_topic(channel.topic or "").get("user_id") == str(user.id):
                return channel
    return None


async def _get_or_create_ambassador_role(
    guild: discord.Guild, country: str
) -> discord.Role:
    role_id = AMBASSADOR_ROLES.get(country)
    if role_id:
        role = guild.get_role(role_id)
        if role:
            return role
    role_name = f"Ambassador{country.replace(' ', '')}"
    for role in guild.roles:
        if role.name.lower() == role_name.lower():
            return role
    new_role = await guild.create_role(
        name=role_name,
        mentionable=True,
        reason=f"Embassy role automatically created for {country}",
    )
    AMBASSADOR_ROLES[country] = new_role.id
    logger.info("Created ambassador role %r (id=%d)", role_name, new_role.id)
    return new_role


async def _ensure_embassy_channel(
    guild: discord.Guild, country: str, ambassador_role: discord.Role
) -> discord.TextChannel:
    category = guild.get_channel(EMBASSY_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError(f"Embassy category {EMBASSY_CATEGORY_ID} not found")
    for channel in category.text_channels:
        if channel.overwrites_for(ambassador_role).view_channel:
            return channel
    safe_name = f"embassy-{country.lower().replace(' ', '-')}"
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        ambassador_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
    }
    for role_id in STAFF_ROLE_IDS:
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            )
    channel = await category.create_text_channel(
        name=safe_name, overwrites=overwrites,
        reason=f"Embassy channel automatically created for {country}",
    )
    logger.info("Created embassy channel %r for %r", safe_name, country)
    return channel


async def _close_ticket_later(
    channel: discord.TextChannel,
    hours: int = 8,
    db: aiosqlite.Connection | None = None,
    *,
    delete_at: datetime | None = None,
) -> None:
    """Rename the channel to signal it is closed, then delete it after *hours* hours.

    The wait is a plain asyncio.sleep() living only in this process's memory —
    a restart at any point during the 8h window used to lose the deletion
    forever (channel stays renamed to gesloten-* but nothing ever cleans it
    up). *delete_at* is persisted to *db* so _reconcile_pending_deletions()
    (called from on_ready) can resume it after a restart. Pass *delete_at*
    explicitly when resuming — it skips the rename/persist step, since both
    already happened before the restart.
    """
    if delete_at is None:
        delete_at = datetime.now(timezone.utc) + timedelta(hours=hours)
        try:
            new_name = f"gesloten-{channel.name}"[:100]
            await channel.edit(name=new_name, reason="Ticket gesloten, wordt over 8 uur verwijderd")
        except Exception as exc:
            logger.warning("_close_ticket_later: could not rename channel %s: %s", channel.id, exc)
        if db is not None:
            try:
                await add_pending_ticket_deletion(db, str(channel.id), delete_at.isoformat())
            except Exception:
                logger.warning(
                    "_close_ticket_later: could not persist pending deletion for %s", channel.id
                )

    remaining = (delete_at - datetime.now(timezone.utc)).total_seconds()
    if remaining > 0:
        await asyncio.sleep(remaining)
    try:
        await channel.delete(reason=f"Ticket automatisch verwijderd na {hours} uur")
    except Exception as exc:
        logger.warning("_close_ticket_later: could not delete channel %s: %s", channel.id, exc)
        return  # leave the pending record — the next startup reconciliation retries it
    if db is not None:
        try:
            await remove_pending_ticket_deletion(db, str(channel.id))
        except Exception:
            logger.warning(
                "_close_ticket_later: could not clear pending deletion for %s", channel.id
            )


async def _reconcile_pending_deletions(bot: commands.Bot, db: aiosqlite.Connection) -> None:
    """Resume ticket-deletion timers that were still pending when the bot last stopped.

    Called once from on_ready. Channels already past their delete_at are
    deleted immediately; others are rescheduled for their remaining wait.
    """
    try:
        records = await get_pending_ticket_deletions(db)
    except Exception:
        logger.exception("_reconcile_pending_deletions: failed to read pending deletions")
        return
    if not records:
        return
    logger.info("_reconcile_pending_deletions: resuming %d pending ticket deletion(s)", len(records))
    for channel_id, delete_at_str in records:
        try:
            delete_at = datetime.fromisoformat(delete_at_str)
        except Exception:
            delete_at = datetime.now(timezone.utc)  # malformed record — delete immediately
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            # Channel is already gone (deleted manually, or bot no longer sees
            # its guild) — nothing left to clean up but the stale record.
            try:
                await remove_pending_ticket_deletion(db, channel_id)
            except Exception:
                pass
            continue
        asyncio.create_task(_close_ticket_later(channel, db=db, delete_at=delete_at))


async def _create_ticket(
    interaction: discord.Interaction,
    ticket_type: str,
    warera_url: str,
    country: str = "",
) -> None:
    """Create a private verification ticket with the submitted profile URL visible to staff."""
    guild = interaction.guild
    user  = interaction.user

    safe_user = str(user.id)[-6:]
    if ticket_type == "embassy":
        safe_country = country[:12].lower().replace(" ", "-")
        channel_name = f"emb-{safe_country}-{safe_user}"
    elif ticket_type == "nigerian":
        channel_name = f"ng-verify-{safe_user}"
    elif ticket_type == "dutch_nigerian":
        channel_name = f"nl-ng-verify-{safe_user}"
    else:
        channel_name = f"nl-verify-{safe_user}"

    category = await _get_or_create_ticket_category(guild)

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
    }
    for role_id in STAFF_ROLE_IDS:
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            )

    # Store URL in topic so /approve can read it without staff retyping it
    topic = f"type={ticket_type}|user_id={user.id}|country={country}|warera_url={warera_url}"
    channel = await category.create_text_channel(
        name=channel_name, topic=topic, overwrites=overwrites,
        reason=f"Verificatie ticket voor {user}",
    )

    type_label = {
        "nigerian":       "🇳🇬 Nigerian",
        "dutch_nigerian": "🇳🇱🇳🇬 Nederlander in Nigeria",
        "dutch":          "🇳🇱 Nederlander",
        "embassy":        f"🏛️ Embassy — {country}" if country else "🏛️ Embassy",
    }.get(ticket_type, ticket_type)

    # Info embed for staff — shows the submitted profile URL
    info_embed = discord.Embed(
        title=f"🎫 Verification request — {type_label}",
        description=(
            f"**Discord user:** {user.mention}\n"
            f"**WarEra profile:** {warera_url}\n"
            + (f"**Country:** {country}\n" if country else "")
        ),
        colour=discord.Colour.orange(),
    )
    await channel.send(embed=info_embed)

    # User instruction — English for Nigerian/embassy, Dutch for the rest
    if _ticket_is_english(ticket_type):
        await channel.send(
            f"{user.mention} Thank you! Please send a **screenshot of your WarEra profile** "
            "in this channel to complete your verification.\n\n"
            "📸 **How to find your profile:**\n"
            "Go to WarEra → click your profile picture in the top right → click **Profile**."
        )
        staff_title = "📋 Staff instructions"
        staff_body  = (
            "Check the profile and screenshot of {mention}.\n\n"
            "Use the buttons below to approve or deny this request."
        )
    else:
        await channel.send(
            f"{user.mention} Bedankt! Stuur nu een **screenshot van je WarEra-profiel** "
            "in dit kanaal om je verificatie te voltooien.\n\n"
            "📸 **Hoe vind je je profiel?**\n"
            "Ga naar WarEra → klik op je profielfoto rechtsboven → klik op **Profiel**."
        )
        staff_title = "📋 Staff instructies"
        staff_body  = (
            "Controleer het profiel en screenshot van {mention}.\n\n"
            "Gebruik de knoppen hieronder om dit verzoek goed of af te keuren."
        )

    staff_embed = discord.Embed(
        title=staff_title,
        description=staff_body.format(mention=user.mention),
        colour=discord.Colour.blurple(),
    )
    await channel.send(embed=staff_embed, view=TicketActionView())

    if _ticket_is_english(ticket_type):
        confirm = (
            f"✅ Your verification request has been created in {channel.mention}.\n"
            "Please send a screenshot of your WarEra profile there to complete verification."
        )
    else:
        confirm = (
            f"✅ Je verificatieverzoek is aangemaakt in {channel.mention}.\n"
            "Stuur daar een screenshot van je WarEra-profiel om je verificatie te voltooien."
        )
    await interaction.followup.send(confirm, ephemeral=True)

# ── Retry view (shown after an invalid URL submission) ────────────────────────

class _RetryView(discord.ui.View):
    def __init__(self, ticket_type: str) -> None:
        super().__init__(timeout=120)
        self.ticket_type = ticket_type
        if _ticket_is_english(ticket_type):
            self.retry.label = "↩️ Try again"

    @discord.ui.button(label="↩️ Probeer opnieuw", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.ticket_type == "embassy":
            await interaction.response.send_modal(EmbassyModal())
        else:
            await interaction.response.send_modal(CitizenModal(self.ticket_type))


# ── Modals ────────────────────────────────────────────────────────────────────

class CitizenModal(discord.ui.Modal):
    def __init__(self, ticket_type: str) -> None:
        titles = {
            "nigerian":       "Verification — Nigerian",
            "dutch_nigerian": "Verificatie — Nederlander in Nigeria",
            "dutch":          "Verificatie — Nederlander",
        }
        super().__init__(title=titles.get(ticket_type, "Verificatie"))
        self.ticket_type = ticket_type
        label = "WarEra profile URL" if ticket_type == "nigerian" else "WarEra profiel URL"
        self.warera_url = discord.ui.TextInput(
            label=label,
            placeholder="https://app.warera.io/user/695579c03f0cff6efb694b3a",
            min_length=10,
            max_length=120,
        )
        self.add_item(self.warera_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        url = self.warera_url.value.strip()
        if not _extract_warera_id(url):
            await interaction.response.send_message(
                "❌ **Ongeldige URL.** Zorg dat je URL er precies zo uitziet:\n"
                "`https://app.warera.io/user/695579c03f0cff6efb694b3a`\n\n"
                "Klik op de knop om het opnieuw te proberen.",
                ephemeral=True,
                view=_RetryView(self.ticket_type),
            )
            return
        await interaction.response.defer(ephemeral=True)
        await _create_ticket(interaction, self.ticket_type, warera_url=url)


class EmbassyModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="Embassy request")
        self.country = discord.ui.TextInput(
            label="Country you represent",
            placeholder="E.g. Belgium, Morocco, Germany…",
            min_length=2,
            max_length=60,
        )
        self.warera_url = discord.ui.TextInput(
            label="WarEra profile URL",
            placeholder="https://app.warera.io/user/695579c03f0cff6efb694b3a",
            min_length=10,
            max_length=120,
        )
        self.add_item(self.country)
        self.add_item(self.warera_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        url = self.warera_url.value.strip()
        if not _extract_warera_id(url):
            await interaction.response.send_message(
                "❌ **Invalid URL.** Make sure your URL looks exactly like this:\n"
                "`https://app.warera.io/user/695579c03f0cff6efb694b3a`\n\n"
                "Click the button to try again.",
                ephemeral=True,
                view=_RetryView("embassy"),
            )
            return
        await interaction.response.defer(ephemeral=True)
        await _create_ticket(
            interaction, "embassy",
            warera_url=url,
            country=self.country.value.strip(),
        )

# ── Shared approve logic (used by button and slash command) ──────────────────

async def _execute_approve(
    ticket: discord.TextChannel,
    user: discord.Member,
    approver: discord.Member | discord.User,
    db: aiosqlite.Connection | None = None,
) -> str:
    """Approve a ticket: fetch username, give roles, set nickname, save identity link."""
    ctx          = _parse_topic(ticket.topic or "")
    ticket_type  = ctx.get("type", "")
    country      = ctx.get("country", "")
    warera_url   = ctx.get("warera_url", "")
    guild        = ticket.guild
    english      = _ticket_is_english(ticket_type)

    warera_id = _extract_warera_id(warera_url) if warera_url else None
    if not warera_id:
        if english:
            return "❌ No valid WarEra URL found in the ticket.", ""
        return "❌ Geen geldige WarEra URL gevonden in het ticket.", ""

    username = await _fetch_warera_username(warera_id)
    if not username:
        logger.warning("_execute_approve: could not fetch username for %s", warera_id)

    # Persist the Discord → WarEra ID link for daily nick sync
    if db:
        await save_link(db, str(user.id), warera_id, username=username)

    errors: list[str] = []

    if ticket_type == "nigerian":
        role = guild.get_role(NIGERIAN_ROLE_ID)
        if role:
            try:
                await user.add_roles(role, reason=f"Approved by {approver}")
            except discord.Forbidden:
                errors.append(f"No permission to add role **{role.name}**.")
    elif ticket_type == "dutch_nigerian":
        for role_id in DUTCH_NIGERIAN_ROLE_IDS:
            role = guild.get_role(role_id)
            if role:
                try:
                    await user.add_roles(role, reason=f"Goedgekeurd door {approver}")
                except discord.Forbidden:
                    errors.append(f"Geen toegang om rol **{role.name}** toe te voegen.")
    elif ticket_type == "dutch":
        role = guild.get_role(DUTCH_ROLE_ID)
        if role:
            try:
                await user.add_roles(role, reason=f"Goedgekeurd door {approver}")
            except discord.Forbidden:
                errors.append(f"Geen toegang om rol **{role.name}** toe te voegen.")
    elif ticket_type == "embassy":
        try:
            amb_role = await _get_or_create_ambassador_role(guild, country)
            await user.add_roles(amb_role, reason=f"Approved by {approver}")
            await _ensure_embassy_channel(guild, country, amb_role)
        except Exception as exc:
            errors.append(f"Embassy error: {exc}")
    else:
        return f"❌ Onbekend ticket type: `{ticket_type}`.", ""

    # Give the Verified role to every approved user, regardless of type
    verified_role = guild.get_role(VERIFIED_ROLE_ID)
    if verified_role:
        try:
            reason = f"Verified by {approver}" if english else f"Geverifieerd door {approver}"
            await user.add_roles(verified_role, reason=reason)
        except discord.Forbidden:
            if english:
                errors.append(f"No permission to add role **{verified_role.name}**.")
            else:
                errors.append(f"Geen toegang om rol **{verified_role.name}** toe te voegen.")
    else:
        logger.warning("_execute_approve: Verified role %d not found in guild", VERIFIED_ROLE_ID)

    if username:
        try:
            reason = f"WarEra name set by {approver}" if english else f"WarEra naam ingesteld door {approver}"
            await user.edit(nick=username[:32], reason=reason)
        except discord.Forbidden:
            if english:
                errors.append("No permission to set nickname (higher role?).")
            else:
                errors.append("Geen toegang om nickname in te stellen (hogere rol?).")
    else:
        if english:
            errors.append("Could not fetch WarEra username — nickname unchanged.")
        else:
            errors.append("WarEra gebruikersnaam kon niet worden opgehaald — nickname ongewijzigd.")

    result = f"✅ **{user.mention}** approved." if english else f"✅ **{user.mention}** goedgekeurd."
    if errors:
        result += "\n⚠️ " + " | ".join(errors)
    return result, username


# ── Ticket action buttons (posted inside each ticket channel) ─────────────────

def _ticket_already_closed(channel: discord.TextChannel) -> bool:
    return channel.name.startswith("gesloten-")


class DenyReasonModal(discord.ui.Modal, title="Verificatie afwijzen"):
    reden = discord.ui.TextInput(
        label="Reden (optioneel)",
        placeholder="Bijv. profiel niet gevonden, verkeerd land…",
        required=False,
        max_length=200,
    )

    def __init__(self, button_message: discord.Message, english: bool = False) -> None:
        super().__init__(title="Reject verification" if english else "Verificatie afwijzen")
        self.button_message = button_message
        self.english = english
        if english:
            self.reden.label = "Reason (optional)"
            self.reden.placeholder = "E.g. profile not found, wrong country…"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        # Remove the approve/deny buttons
        try:
            await self.button_message.edit(view=None)
        except Exception:
            pass
        reden = self.reden.value.strip()
        # Post a visible message in the channel for everyone to see
        if self.english:
            await interaction.channel.send(
                f"❌ **Denied** by {interaction.user.mention}"
                + (f"\nReason: {reden}" if reden else "")
                + "\nThis channel will be deleted in 8 hours."
            )
            await interaction.followup.send("✅ Ticket denied.", ephemeral=True)
        else:
            await interaction.channel.send(
                f"❌ **Afgewezen** door {interaction.user.mention}"
                + (f"\nReden: {reden}" if reden else "")
                + "\nDit kanaal wordt over 8 uur verwijderd."
            )
            await interaction.followup.send("✅ Ticket afgewezen.", ephemeral=True)
        db = getattr(interaction.client, "nigeria_db", None)
        asyncio.create_task(_close_ticket_later(interaction.channel, hours=8, db=db))


class TicketActionView(discord.ui.View):
    """Persistent approve / deny buttons posted inside every ticket channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ Goedkeuren",
        style=discord.ButtonStyle.success,
        custom_id="ticket:approve",
    )
    async def approve_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        ctx         = _parse_topic(interaction.channel.topic or "")
        ticket_type = ctx.get("type", "")
        english     = _ticket_is_english(ticket_type)

        if not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            msg = "❌ Only staff can do this." if english else "❌ Alleen staff kan dit doen."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        if _ticket_already_closed(interaction.channel):
            msg = "⚠️ This ticket has already been processed." if english else "⚠️ Dit ticket is al verwerkt."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        user_id = ctx.get("user_id", "")
        try:
            member = await interaction.guild.fetch_member(int(user_id))
        except Exception:
            msg = "❌ User is no longer in the server." if english else "❌ Gebruiker niet meer in de server."
            await interaction.followup.send(msg, ephemeral=True)
            return

        # Remove the buttons immediately so no one can double-click
        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass

        wait_msg = "⏳ Fetching username from WarEra…" if english else "⏳ Gebruikersnaam ophalen van WarEra…"
        await interaction.followup.send(wait_msg, ephemeral=True)
        db = getattr(interaction.client, "nigeria_db", None)
        result, username = await _execute_approve(interaction.channel, member, interaction.user, db=db)

        # Post a visible message in the channel for everyone to see
        if english:
            channel_msg = (
                f"✅ **Approved** by {interaction.user.mention}\n"
                "This channel will be deleted in 8 hours."
            )
        else:
            channel_msg = (
                f"✅ **Goedgekeurd** door {interaction.user.mention}\n"
                "Dit kanaal wordt over 8 uur verwijderd."
            )
        await interaction.channel.send(channel_msg)
        await interaction.followup.send(result, ephemeral=True)
        asyncio.create_task(_close_ticket_later(interaction.channel, hours=8, db=db))

    @discord.ui.button(
        label="❌ Afwijzen",
        style=discord.ButtonStyle.danger,
        custom_id="ticket:deny",
    )
    async def deny_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        ctx         = _parse_topic(interaction.channel.topic or "")
        ticket_type = ctx.get("type", "")
        english     = _ticket_is_english(ticket_type)

        if not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            msg = "❌ Only staff can do this." if english else "❌ Alleen staff kan dit doen."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        if _ticket_already_closed(interaction.channel):
            msg = "⚠️ This ticket has already been processed." if english else "⚠️ Dit ticket is al verwerkt."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.send_modal(
            DenyReasonModal(button_message=interaction.message, english=english)
        )


# ── Persistent views ──────────────────────────────────────────────────────────

class VerificationView(discord.ui.View):
    """Persistent view posted in the verification channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🇳🇬 Nigerian",
        style=discord.ButtonStyle.success,
        custom_id="verify:nigerian",
    )
    async def nigerian(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CitizenModal("nigerian"))

    @discord.ui.button(
        label="🇳🇱🇳🇬 Nederlander in Nigeria",
        style=discord.ButtonStyle.success,
        custom_id="verify:dutch_nigerian",
    )
    async def dutch_nigerian(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CitizenModal("dutch_nigerian"))

    @discord.ui.button(
        label="🇳🇱 Nederlander",
        style=discord.ButtonStyle.primary,
        custom_id="verify:dutch",
    )
    async def dutch(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CitizenModal("dutch"))

    @discord.ui.button(
        label="🏛️ Embassy",
        style=discord.ButtonStyle.secondary,
        custom_id="verify:embassy",
    )
    async def embassy(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(EmbassyModal())

# ── Staff check ───────────────────────────────────────────────────────────────

def _staff_check() -> app_commands.check:
    async def predicate(interaction: discord.Interaction) -> bool:
        # Bot owner always passes
        app_info = await interaction.client.application_info()
        if interaction.user.id == app_info.owner.id:
            return True
        if not isinstance(interaction.user, discord.Member):
            raise app_commands.MissingPermissions(["staff_role"])
        # Members with one of the configured staff roles
        if _is_staff(interaction.user):
            return True
        # Fallback: Manage Guild permission
        if interaction.user.guild_permissions.manage_guild:
            return True
        raise app_commands.MissingPermissions(["staff_role"])
    return app_commands.check(predicate)

# ── Cog ──────────────────────────────────────────────────────────────────────

class VerificationCog(commands.Cog, name="verification"):
    def __init__(self, bot: commands.Bot, db: aiosqlite.Connection) -> None:
        self.bot = bot
        self._db = db
        # {channel_id: monotonic timestamp of the last unanswered proposal}.
        # In-memory on purpose: a restart simply forgets any open proposal,
        # which for an easter egg is better than persisting it.
        self._open_proposals: dict[int, float] = {}
        self.nick_sync.start()

    def cog_unload(self) -> None:
        self.nick_sync.cancel()

    # ── Forbidden-word reaction ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        # Some channels are opted out of the GIF reactions entirely.
        if message.channel.id in NO_REACTION_CHANNEL_IDS:
            return
        # Threads inherit their parent channel's opt-out.
        parent_id = getattr(message.channel, "parent_id", None)
        if parent_id in NO_REACTION_CHANNEL_IDS:
            return

        content = message.content or ""

        # Easter egg: someone actually typed @Roger.
        if _mentions_bot_in_text(content, self.bot.user):
            try:
                await message.channel.send("Interested in a business proposal?")
            except discord.HTTPException as e:
                logger.error(f"Failed to send mention easter egg for {message.id}: {e}")
            else:
                # Only arm the punchline once the question actually went out.
                self._open_proposals[message.channel.id] = time.monotonic()
            return

        # Punchline: anyone saying yes shortly after that question gets the
        # sales pitch. Answering is open to the whole channel rather than just
        # whoever tagged the bot — it reads as a conversation, not a form.
        offered_at = self._open_proposals.get(message.channel.id)
        if offered_at is not None:
            if time.monotonic() - offered_at > BUSINESS_PROPOSAL_WINDOW_SECONDS:
                self._open_proposals.pop(message.channel.id, None)
            elif _says_yes(content):
                # Drop the entry before awaiting, so a second "ja" arriving
                # while this send is in flight can't fire a duplicate pitch.
                self._open_proposals.pop(message.channel.id, None)
                try:
                    await message.channel.send(
                        random.choice(BUSINESS_PROPOSAL_REPLIES)
                    )
                except discord.HTTPException as e:
                    logger.error(
                        f"Failed to send business-proposal reply for {message.id}: {e}"
                    )
                return

        content_lower = content.lower()
        words = content_lower.split()
        for trigger_words, gifs, send_text in FORBIDDEN_WORD_GROUPS:
            # Multi-word triggers (e.g. "pyramid scheme") match as a substring
            # of the raw message; single-word triggers match as a whole token
            # so they don't fire on partial-word matches inside other words.
            if not any(
                tw in content_lower if " " in tw else tw in words
                for tw in trigger_words
            ):
                continue
            try:
                if send_text:
                    await message.channel.send("Dat woord is niet toegestaan!")
                await message.channel.send(random.choice(gifs))
            except discord.HTTPException as e:
                logger.error(f"Failed to send forbidden-word gif for message {message.id}: {e}")
            return

    # ── Daily nickname sync ───────────────────────────────────────────────────

    @tasks.loop(hours=24)
    async def nick_sync(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return
        links = await get_all_links(self._db)
        if not links:
            return

        updated = 0
        nationality_updated = 0
        async with aiohttp.ClientSession() as sess:
            for i, (discord_id, warera_id) in enumerate(links):
                if i > 0:
                    await asyncio.sleep(_SYNC_DELAY)
                member = guild.get_member(int(discord_id))
                if not member:
                    continue
                username = await _fetch_warera_username(warera_id, session=sess)
                if username:
                    await save_link(self._db, discord_id, warera_id, username=username)
                    current = member.nick or member.name
                    if current != username[:32]:
                        try:
                            await member.edit(
                                nick=username[:32],
                                reason="Dagelijkse WarEra gebruikersnaam sync",
                            )
                            updated += 1
                            logger.info("nick_sync: updated %s → %s", discord_id, username)
                        except discord.Forbidden:
                            logger.warning(
                                "nick_sync: no permission to edit nick for %s", discord_id
                            )

                # Nationality role sync — verified members only, and only for
                # those holding a role this tracks (Dutch, or the
                # Nigerian+Vakantieganger "Dutch tourist" combo). See
                # _sync_nationality_role for why a "real" Nigerian is left alone.
                member_role_ids = {r.id for r in member.roles}
                if VERIFIED_ROLE_ID not in member_role_ids:
                    continue
                tracks_nationality = DUTCH_ROLE_ID in member_role_ids or (
                    NIGERIAN_ROLE_ID in member_role_ids
                    and VAKANTIEGANGER_ROLE_ID in member_role_ids
                )
                if not tracks_nationality:
                    continue
                await asyncio.sleep(_SYNC_DELAY)
                country_id = await _fetch_warera_country(warera_id, session=sess)
                if not country_id:
                    continue
                change = await _sync_nationality_role(member, country_id, guild)
                if change:
                    nationality_updated += 1
                    logger.info(
                        "nick_sync: nationality role updated for %s: %s", discord_id, change
                    )
        logger.info(
            "nick_sync: ran for %d members, %d nicks updated, %d nationality roles updated",
            len(links), updated, nationality_updated,
        )

    @nick_sync.before_loop
    async def _before_nick_sync(self) -> None:
        await self.bot.wait_until_ready()

    # ── /postverificatie ──────────────────────────────────────────────────────

    @app_commands.command(
        name="postverificatie",
        description="Post de verificatieknoppen in het verificatiekanaal.",
    )
    @_staff_check()
    async def post_verificatie(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = self.bot.get_channel(VERIFICATION_CHANNEL_ID)
        if channel is None:
            await interaction.followup.send("❌ Verificatiekanaal niet gevonden.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🇳🇬 Verificatie",
            description=(
                "Welcome! Click the button that matches your situation.\n\n"
                "**🇳🇬 Nigerian** — You are a Nigerian citizen in WarEra.\n"
                "**🇳🇱 Nederlander in Nigeria** — You are a Dutch player who lives in Nigeria in WarEra.\n"
                "**🇳🇱 Nederlander** — You are a Dutch citizen in WarEra.\n"
                "**🏛️ Embassy** — You are an ambassador from another country.\n\n"
                "After clicking you will receive a private channel. "
                "Send a screenshot of your WarEra profile there to complete your verification."
            ),
            colour=discord.Colour(0x009A44),
        )
        await channel.send(embed=embed, view=VerificationView())
        await interaction.followup.send(
            f"✅ Verificatieknoppen gepost in {channel.mention}.", ephemeral=True
        )

    # ── /approve ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="approve",
        description="Keur een verificatieverzoek goed (URL wordt automatisch uit het ticket gelezen).",
    )
    @app_commands.describe(user="De Discord gebruiker om goed te keuren")
    @_staff_check()
    async def approve(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True)
        ticket = await _find_ticket_channel(interaction.guild, user)
        if ticket is None:
            await interaction.followup.send(
                f"❌ Geen open ticket gevonden voor {user.mention}.", ephemeral=True
            )
            return
        result, username = await _execute_approve(ticket, user, interaction.user, db=self._db)
        ticket_type = _parse_topic(ticket.topic or "").get("type", "")
        if _ticket_is_english(ticket_type):
            await ticket.send(
                f"✅ **Approved** by {interaction.user.mention}\n"
                "This channel will be deleted in 8 hours."
            )
        else:
            await ticket.send(
                f"✅ **Goedgekeurd** door {interaction.user.mention}\n"
                "Dit kanaal wordt over 8 uur verwijderd."
            )
        await interaction.followup.send(result, ephemeral=True)
        asyncio.create_task(_close_ticket_later(ticket, hours=8, db=self._db))

    # ── /deny ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="deny", description="Wijs een verificatieverzoek af.")
    @app_commands.describe(
        user="De Discord gebruiker om af te wijzen",
        reden="Optionele reden voor afwijzing",
    )
    @_staff_check()
    async def deny(
        self, interaction: discord.Interaction, user: discord.Member, reden: str = ""
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        ticket = await _find_ticket_channel(interaction.guild, user)
        if ticket is None:
            await interaction.followup.send(
                f"❌ Geen open ticket gevonden voor {user.mention}.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ Verificatie van {user.mention} afgewezen.", ephemeral=True
        )
        asyncio.create_task(_close_ticket_later(ticket, hours=8, db=self._db))

    # ── /setup_ambassades ─────────────────────────────────────────────────────

    @app_commands.command(
        name="setup_ambassades",
        description="Maak ontbrekende embassykanalen aan voor bestaande rollen.",
    )
    @_staff_check()
    async def setup_ambassades(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild    = interaction.guild
        category = guild.get_channel(EMBASSY_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send(
                f"❌ Embassycategorie ({EMBASSY_CATEGORY_ID}) niet gevonden.", ephemeral=True
            )
            return

        covered: set[int] = set()
        for channel in category.text_channels:
            for role, ow in channel.overwrites.items():
                if isinstance(role, discord.Role) and ow.view_channel:
                    covered.add(role.id)

        created: list[str] = []
        skipped: list[str] = []

        for country, role_id in AMBASSADOR_ROLES.items():
            role = guild.get_role(role_id)
            if role is None:
                skipped.append(f"{country} — rol niet gevonden")
                continue
            if role_id in covered:
                skipped.append(f"{country} — kanaal bestaat al")
                continue
            safe_name = f"embassy-{country.lower().replace(' ', '-')}"
            overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
            }
            for staff_id in STAFF_ROLE_IDS:
                staff_role = guild.get_role(staff_id)
                if staff_role:
                    overwrites[staff_role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, manage_channels=True
                    )
            try:
                ch = await category.create_text_channel(
                    name=safe_name, overwrites=overwrites,
                    reason="Embassy channel created by /setup_ambassades",
                )
                created.append(f"{country} → {ch.mention}")
            except Exception as exc:
                skipped.append(f"{country} — fout: {exc}")

        lines = [f"**Aangemaakt ({len(created)}):**"]
        lines += [f"• {c}" for c in created] or ["• Niets"]
        lines += ["", f"**Overgeslagen ({len(skipped)}):**"]
        lines += [f"• {s}" for s in skipped] or ["• Geen"]
        await interaction.followup.send("\n".join(lines), ephemeral=True)


    # ── /syncnicks ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="syncnicks",
        description="Forceer een directe synchronisatie van alle WarEra gebruikersnamen.",
    )
    @_staff_check()
    async def syncnicks(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            await interaction.followup.send("❌ Guild niet gevonden.", ephemeral=True)
            return

        links = await get_all_links(self._db)
        if not links:
            await interaction.followup.send("Geen geverifieerde gebruikers gevonden.", ephemeral=True)
            return

        updated = 0
        failed = 0
        skipped = 0

        async with aiohttp.ClientSession() as sess:
            for i, (discord_id, warera_id) in enumerate(links):
                if i > 0:
                    await asyncio.sleep(_SYNC_DELAY)
                member = guild.get_member(int(discord_id))
                if not member:
                    skipped += 1
                    continue
                username = await _fetch_warera_username(warera_id, session=sess)
                if not username:
                    failed += 1
                    continue
                await save_link(self._db, discord_id, warera_id, username=username)
                current = member.nick or member.name
                if current == username[:32]:
                    skipped += 1
                    continue
                try:
                    await member.edit(
                        nick=username[:32],
                        reason="Handmatige WarEra gebruikersnaam sync via /syncnicks",
                    )
                    updated += 1
                    logger.info("syncnicks: updated %s → %s", discord_id, username)
                except discord.Forbidden:
                    failed += 1
        await interaction.followup.send(
            f"✅ Sync voltooid — **{updated}** bijgewerkt, **{skipped}** ongewijzigd, **{failed}** mislukt.",
            ephemeral=True,
        )

    # ── /verificatiestatus ───────────────────────────────────────────────────

    @app_commands.command(
        name="verificatiestatus",
        description="Toon verificatiestatus van de server of een specifiek lid.",
    )
    @app_commands.describe(member="Optioneel: controleer één specifiek lid")
    @_staff_check()
    async def verificatiestatus(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if member:
            link = await get_link(self._db, str(member.id))
            if link:
                warera_id, warera_username = link
                name_str = f"**{warera_username}**" if warera_username else f"`{warera_id}`"
                embed = discord.Embed(
                    title=f"✅ {member.display_name} — geverifieerd",
                    description=(
                        f"**Discord:** {member.mention}\n"
                        f"**WarEra profiel:** {name_str}\n"
                        f"**WarEra ID:** `{warera_id}`"
                    ),
                    colour=discord.Colour.green(),
                )
            else:
                embed = discord.Embed(
                    title=f"❌ {member.display_name} — niet geverifieerd",
                    description=f"{member.mention} heeft geen gekoppeld WarEra-profiel.",
                    colour=discord.Colour.red(),
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Server-wide overview
        all_links = await get_all_links_full(self._db)
        guild = interaction.guild

        # Roles that indicate a verified member
        verified_role_ids = {NIGERIAN_ROLE_ID, DUTCH_ROLE_ID, *DUTCH_NIGERIAN_ROLE_IDS}
        verified_in_db = {row[0] for row in all_links}

        # Members with a verification role but no DB entry (verified via old flow)
        orphaned: list[discord.Member] = []
        for m in guild.members:
            if m.bot:
                continue
            has_role = any(r.id in verified_role_ids for r in m.roles)
            if has_role and str(m.id) not in verified_in_db:
                orphaned.append(m)

        lines: list[str] = [f"**Geverifieerd in DB:** {len(all_links)} leden"]

        if all_links:
            entry_lines: list[str] = []
            for discord_id, warera_id, warera_username, saved_at in all_links[:30]:
                m = guild.get_member(int(discord_id))
                disc_str = m.mention if m else f"`{discord_id}`"
                name_str = warera_username or warera_id
                entry_lines.append(f"• {disc_str} → **{name_str}**")
            lines.append("\n".join(entry_lines))
            if len(all_links) > 30:
                lines.append(f"_…en {len(all_links) - 30} meer_")

        if orphaned:
            lines.append(f"\n**Hebben rol maar geen DB-koppeling ({len(orphaned)}):**")
            lines.append(", ".join(m.mention for m in orphaned[:20]))
            if len(orphaned) > 20:
                lines.append(f"_…en {len(orphaned) - 20} meer_")

        embed = discord.Embed(
            title="📋 Verificatiestatus",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /verifieer ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="verifieer",
        description="Koppel handmatig een Discord-lid aan een WarEra-profiel en geef de juiste rollen.",
    )
    @app_commands.describe(
        user="Het Discord-lid om te verifiëren",
        type="Het type verificatie",
        warera_url="De WarEra-profiel URL (https://app.warera.io/user/…)",
        land="Alleen bij Ambassade: het land dat deze persoon vertegenwoordigt",
    )
    @app_commands.choices(type=[
        app_commands.Choice(name="🇳🇬 Nigerian",                  value="nigerian"),
        app_commands.Choice(name="🇳🇱🇳🇬 Nederlander in Nigeria", value="dutch_nigerian"),
        app_commands.Choice(name="🇳🇱 Nederlander",               value="dutch"),
        app_commands.Choice(name="🏛️ Ambassade",                  value="embassy"),
    ])
    @_staff_check()
    async def verifieer(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        type: str,
        warera_url: str,
        land: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if type == "embassy" and not (land and land.strip()):
            await interaction.followup.send(
                "❌ Kies `land:` — verplicht bij het type Ambassade.",
                ephemeral=True,
            )
            return

        warera_id = _extract_warera_id(warera_url)
        if not warera_id:
            await interaction.followup.send(
                "❌ Ongeldige WarEra URL. Verwacht: `https://app.warera.io/user/<id>`",
                ephemeral=True,
            )
            return

        await interaction.followup.send("⏳ WarEra gebruikersnaam ophalen…", ephemeral=True)

        username = await _fetch_warera_username(warera_id)
        if not username:
            logger.warning("verifieer: could not fetch username for %s", warera_id)

        guild = interaction.guild
        errors: list[str] = []

        # Assign roles
        if type == "nigerian":
            role = guild.get_role(NIGERIAN_ROLE_ID)
            if role:
                try:
                    await user.add_roles(role, reason=f"Handmatig geverifieerd door {interaction.user}")
                except discord.Forbidden:
                    errors.append(f"Geen toegang om rol **{role.name}** toe te voegen.")
        elif type == "dutch_nigerian":
            for role_id in DUTCH_NIGERIAN_ROLE_IDS:
                role = guild.get_role(role_id)
                if role:
                    try:
                        await user.add_roles(role, reason=f"Handmatig geverifieerd door {interaction.user}")
                    except discord.Forbidden:
                        errors.append(f"Geen toegang om rol **{role.name}** toe te voegen.")
        elif type == "dutch":
            role = guild.get_role(DUTCH_ROLE_ID)
            if role:
                try:
                    await user.add_roles(role, reason=f"Handmatig geverifieerd door {interaction.user}")
                except discord.Forbidden:
                    errors.append(f"Geen toegang om rol **{role.name}** toe te voegen.")
        elif type == "embassy":
            country = land.strip()
            try:
                amb_role = await _get_or_create_ambassador_role(guild, country)
                await user.add_roles(amb_role, reason=f"Handmatig geverifieerd door {interaction.user}")
                await _ensure_embassy_channel(guild, country, amb_role)
            except Exception as exc:
                errors.append(f"Ambassade-fout: {exc}")

        # Verified role for everyone
        verified_role = guild.get_role(VERIFIED_ROLE_ID)
        if verified_role:
            try:
                await user.add_roles(verified_role, reason=f"Handmatig geverifieerd door {interaction.user}")
            except discord.Forbidden:
                errors.append(f"Geen toegang om rol **{verified_role.name}** toe te voegen.")

        # Set nickname
        if username:
            try:
                await user.edit(
                    nick=username[:32],
                    reason=f"WarEra naam ingesteld door {interaction.user}",
                )
            except discord.Forbidden:
                errors.append("Geen toegang om nickname in te stellen (hogere rol?).")
        else:
            errors.append("WarEra gebruikersnaam kon niet worden opgehaald — nickname ongewijzigd.")

        # Save link
        await save_link(self._db, str(user.id), warera_id, username=username)

        name_str = f"**{username}**" if username else f"`{warera_id}`"
        result = f"✅ {user.mention} geverifieerd als {name_str}."
        if type == "embassy":
            result += f" (Ambassade — {land.strip()})"
        if errors:
            result += "\n⚠️ " + " | ".join(errors)
        await interaction.followup.send(result, ephemeral=True)

    @app_commands.command(name="gear", description="Stuur de gear-afbeelding.")
    @app_commands.describe(mythic="Stuur de mythic-gear-afbeelding in plaats van de standaard.")
    async def gear(self, interaction: discord.Interaction, mythic: bool = False) -> None:
        image_path = _MYTHIC_GEAR_IMAGE_PATH if mythic else _GEAR_IMAGE_PATH
        filename = "mythicgear.png" if mythic else "gear.png"
        if not os.path.isfile(image_path):
            logger.warning("gear: image not found at %s", image_path)
            await interaction.response.send_message(
                "❌ Afbeelding niet gevonden.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            file=discord.File(image_path, filename=filename)
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Je hebt geen rechten om dit commando te gebruiken.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ Je hebt geen rechten om dit commando te gebruiken.", ephemeral=True
                )
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VerificationCog(bot))
