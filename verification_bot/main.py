"""Verification bot — production-server Nederlander role checker.

This bot sits in the production Discord server (read-only, no slash commands)
and exposes a local HTTP API so the war-guild bot can ask:

    "Does Discord user <id> have the Nederlander role on the production server?"

Endpoints
---------
GET /check/<discord_id>
    Returns {"has_role": true/false, "reason": "..."}
    reason is only present when has_role is false.

GET /member/<discord_id>
    Returns {"has_role": true/false, "nickname": "<display_name or null>"}
    Combined endpoint: role check + server nickname in one call.
    nickname is the member's server-specific nickname (or Discord username as
    fallback), which equals their in-game warera username.

GET /nederlanders
    Returns {"ids": ["123", "456", ...]}
    Full list of Discord user IDs that currently have the Nederlander role.
    Used by the hourly sync on the war-guild bot.

GET /roles/<role_id>
    Returns {"ids": ["123", "456", ...]}
    Full list of Discord user IDs that currently have the given role ID on
    the production server. Generic version of /nederlanders — used to
    mirror cabinet roles (President, ministers, ...) onto the war guild,
    since those are plain 1:1 role mirrors (same Discord account in both
    guilds), unlike Nederlander which also drives citizenship-linked
    behaviour (auto-link, nickname sync).

Authentication
--------------
Both endpoints require:
    Authorization: Bearer <VERIFIER_SECRET>

Environment variables
---------------------
TOKEN_VERIFIER      Discord bot token for this application.
VERIFIER_SECRET     Shared secret; the war-guild bot sends this as a Bearer token.
VERIFIER_GUILD_ID   Production Discord server ID.
VERIFIER_ROLE_ID    ID of the Nederlander role on the production server.
VERIFIER_PORT       HTTP port to listen on (default: 8765).
VERIFIER_BIND       Interface to bind to (default: 127.0.0.1; use 0.0.0.0 in Docker).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets

import aiohttp.web
import discord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("verifier")

# ── Config from environment ───────────────────────────────────────────────────

TOKEN: str = os.environ["TOKEN_VERIFIER"]
SECRET: str = os.environ["VERIFIER_SECRET"]
GUILD_ID: int = int(os.environ["VERIFIER_GUILD_ID"])
ROLE_ID: int = int(os.environ["VERIFIER_ROLE_ID"])
PORT: int = int(os.environ.get("VERIFIER_PORT", "8765"))
BIND: str = os.environ.get("VERIFIER_BIND", "127.0.0.1")

# ── Discord client ────────────────────────────────────────────────────────────

intents = discord.Intents.none()
intents.guilds = True
intents.members = True  # privileged — enable in Discord Developer Portal

client = discord.Client(intents=intents)

# Set when the guild is loaded and members are available.
_guild_ready = asyncio.Event()


@client.event
async def on_ready() -> None:
    logger.info("Verifier bot logged in as %s", client.user)


@client.event
async def on_guild_available(guild: discord.Guild) -> None:
    if guild.id == GUILD_ID:
        logger.info("Production guild available (%d members)", guild.member_count)
        _guild_ready.set()


@client.event
async def on_guild_unavailable(guild: discord.Guild) -> None:
    if guild.id == GUILD_ID:
        logger.warning("Production guild became unavailable")
        _guild_ready.clear()


# ── Auth helper ───────────────────────────────────────────────────────────────

def _authorised(request: aiohttp.web.Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return secrets.compare_digest(auth[7:], SECRET)


# ── HTTP handlers ─────────────────────────────────────────────────────────────

async def handle_check(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Check whether a single Discord user has the Nederlander role."""
    if not _authorised(request):
        return aiohttp.web.Response(status=401)

    try:
        discord_id = int(request.match_info["discord_id"])
    except (ValueError, KeyError):
        return aiohttp.web.Response(status=400)

    if not _guild_ready.is_set():
        return aiohttp.web.json_response({"error": "guild_not_ready"}, status=503)

    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return aiohttp.web.json_response({"error": "guild_not_found"}, status=503)

    member = guild.get_member(discord_id)
    if member is None:
        return aiohttp.web.json_response({"has_role": False, "reason": "not_member"})

    has_role = any(r.id == ROLE_ID for r in member.roles)
    return aiohttp.web.json_response({"has_role": has_role})


async def handle_member(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Return has_role + server nickname for a single Discord member."""
    if not _authorised(request):
        return aiohttp.web.Response(status=401)

    try:
        discord_id = int(request.match_info["discord_id"])
    except (ValueError, KeyError):
        return aiohttp.web.Response(status=400)

    if not _guild_ready.is_set():
        return aiohttp.web.json_response({"error": "guild_not_ready"}, status=503)

    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return aiohttp.web.json_response({"error": "guild_not_found"}, status=503)

    member = guild.get_member(discord_id)
    if member is None:
        return aiohttp.web.json_response({"has_role": False, "nickname": None, "reason": "not_member"})

    has_role = any(r.id == ROLE_ID for r in member.roles)
    return aiohttp.web.json_response({"has_role": has_role, "nickname": member.display_name})


async def handle_nederlanders(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Return all Discord user IDs that currently have the Nederlander role."""
    if not _authorised(request):
        return aiohttp.web.Response(status=401)

    if not _guild_ready.is_set():
        return aiohttp.web.json_response({"error": "guild_not_ready"}, status=503)

    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return aiohttp.web.json_response({"error": "guild_not_found"}, status=503)

    ids = [
        str(m.id)
        for m in guild.members
        if any(r.id == ROLE_ID for r in m.roles)
    ]
    return aiohttp.web.json_response({"ids": ids})


async def handle_role_members(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Return all Discord user IDs that currently have the given role ID."""
    if not _authorised(request):
        return aiohttp.web.Response(status=401)

    try:
        role_id = int(request.match_info["role_id"])
    except (ValueError, KeyError):
        return aiohttp.web.Response(status=400)

    if not _guild_ready.is_set():
        return aiohttp.web.json_response({"error": "guild_not_ready"}, status=503)

    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return aiohttp.web.json_response({"error": "guild_not_found"}, status=503)

    ids = [
        str(m.id)
        for m in guild.members
        if any(r.id == role_id for r in m.roles)
    ]
    return aiohttp.web.json_response({"ids": ids})


# ── Startup ───────────────────────────────────────────────────────────────────

async def start_web_server() -> None:
    app = aiohttp.web.Application()
    app.router.add_get("/check/{discord_id}", handle_check)
    app.router.add_get("/member/{discord_id}", handle_member)
    app.router.add_get("/nederlanders", handle_nederlanders)
    app.router.add_get("/roles/{role_id}", handle_role_members)

    runner = aiohttp.web.AppRunner(app, access_log=None)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, BIND, PORT)
    await site.start()
    logger.info("Verifier HTTP server listening on %s:%d", BIND, PORT)


async def main() -> None:
    async with client:
        await asyncio.gather(
            start_web_server(),
            client.start(TOKEN),
        )


if __name__ == "__main__":
    asyncio.run(main())
