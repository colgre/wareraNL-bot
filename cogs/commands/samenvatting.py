"""
/samenvatting uren:<int> — Vat de berichten in dit kanaal samen met AI.

Haalt berichten op uit het huidige kanaal van de afgelopen X uur,
stuurt ze naar de OpenAI API en geeft een Nederlandse samenvatting terug.

Vereist: OPENAI_API_KEY in .env
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

# Maximum number of messages to feed to the model per call.
# gpt-4.1-mini has a 1M-token context window; even 500 long Discord
# messages rarely exceed 50k tokens, so 500 is a safe ceiling.
_MAX_MESSAGES = 500

# System prompts per detail level
_SYSTEM_PROMPTS: dict[str, str] = {
    "globaal": (
        "Je bent een assistent die Discord-gesprekken samenvat. "
        "Geef een korte samenvatting in 2-3 zinnen in het Nederlands. "
        "Noem alleen de belangrijkste onderwerpen die besproken zijn."
    ),
    "normaal": (
        "Je bent een assistent die Discord-gesprekken samenvat. "
        "De gebruiker geeft je een reeks berichten uit een Discord-kanaal. "
        "Schrijf een heldere, beknopte samenvatting in het Nederlands. "
        "Groepeer gerelateerde onderwerpen samen. "
        "Noem de gebruikersnamen alleen als ze relevant zijn voor de context. "
        "Gebruik geen markdown-opmaak met # koppen — gewone alinea's zijn prima."
    ),
    "gedetailleerd": (
        "Je bent een assistent die Discord-gesprekken samenvat. "
        "De gebruiker geeft je een reeks berichten uit een Discord-kanaal. "
        "Schrijf een uitgebreide, gedetailleerde samenvatting in het Nederlands. "
        "Bespreek alle onderwerpen die aan bod kwamen met voldoende context. "
        "Groepeer gerelateerde onderwerpen samen met duidelijke alinea's. "
        "Noem relevante gebruikersnamen en citeer standpunten waar nuttig. "
        "Gebruik geen markdown-opmaak met # koppen — gewone alinea's zijn prima."
    ),
}
_MAX_TOKENS_PER_LEVEL: dict[str, int] = {
    "globaal": 200,
    "normaal": 1024,
    "gedetailleerd": 3000,
}


class SamenvattingCog(commands.Cog, name="samenvatting"):
    def __init__(self, bot: "DiscordBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="samenvatting",
        description="Maak een AI-samenvatting van de berichten in dit kanaal.",
    )
    @app_commands.describe(
        uren="Berichten van de afgelopen X uur samenvatten (bijv. 6)",
        detailniveau="Hoe uitgebreid de samenvatting moet zijn (standaard: normaal)",
    )
    @app_commands.choices(detailniveau=[
        app_commands.Choice(name="Globaal (2-3 zinnen)", value="globaal"),
        app_commands.Choice(name="Normaal", value="normaal"),
        app_commands.Choice(name="Gedetailleerd", value="gedetailleerd"),
    ])
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.channel_id)
    async def samenvatting(self, interaction: discord.Interaction, uren: int, detailniveau: str = "normaal") -> None:
        allowed_role_ids: list[int] = self.bot.config.get("roles", {}).get(
            "samenvatting_allowed", []
        )
        member = interaction.user
        if allowed_role_ids and isinstance(member, discord.Member):
            # Server admins always pass
            is_admin = member.guild_permissions.administrator
            # Specific user always passes
            is_breakerh = member.name == "breakerh"
            # Privileged bot roles also pass
            privileged_keys = {"officier", "government", "commandant", "president", "vice_president"}
            privileged_ids = {
                self.bot.config.get("roles", {}).get(k)
                for k in privileged_keys
            }
            member_role_ids = {r.id for r in member.roles}
            has_allowed = bool(member_role_ids.intersection(allowed_role_ids))
            has_privileged = bool(member_role_ids.intersection(privileged_ids - {None}))
            if not (is_admin or is_breakerh or has_allowed or has_privileged):
                await interaction.response.send_message(
                    "Je hebt niet de juiste rol om dit commando te gebruiken.",
                    ephemeral=True,
                )
                return

        if uren < 1 or uren > 168:
            await interaction.response.send_message(
                "Kies een waarde tussen 1 en 168 uur (1 week).", ephemeral=True
            )
            return

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            await interaction.response.send_message(
                "⚠️ `OPENAI_API_KEY` is niet ingesteld. Vraag de beheerder dit toe te voegen aan de `.env`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send(
                "Dit commando werkt alleen in tekst-kanalen en threads.", ephemeral=True
            )
            return

        # Fetch messages — no limit so we get everything in the time window,
        # then cap at _MAX_MESSAGES (keeping the most recent ones).
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=uren)
        messages: list[discord.Message] = []
        truncated = False
        try:
            async for msg in channel.history(limit=None, after=cutoff, oldest_first=True):
                if msg.author.bot:
                    continue
                if msg.content.strip():
                    messages.append(msg)
        except discord.Forbidden:
            await interaction.followup.send(
                "De bot heeft geen toestemming om berichten in dit kanaal te lezen.", ephemeral=True
            )
            return

        if len(messages) > _MAX_MESSAGES:
            messages = messages[-_MAX_MESSAGES:]
            truncated = True

        if not messages:
            await interaction.followup.send(
                f"Geen berichten gevonden in de afgelopen **{uren} uur**."
            )
            return

        # Build transcript
        lines: list[str] = []
        for msg in messages:
            ts = msg.created_at.strftime("%H:%M")
            lines.append(f"[{ts}] {msg.author.display_name}: {msg.content}")
        transcript = "\n".join(lines)

        # Call OpenAI
        try:
            summary = await self._summarise(
                transcript, uren, len(messages), api_key, detailniveau
            )
        except Exception as exc:
            logger.error("Samenvatting: OpenAI call failed: %s", exc)
            await interaction.followup.send(
                f"Er ging iets mis bij het genereren van de samenvatting: `{exc}`",
                ephemeral=True,
            )
            return

        # Build embed(s)
        label = f"{uren} uur" if uren != 1 else "1 uur"
        detail_label = {"globaal": "globaal", "normaal": "normaal", "gedetailleerd": "gedetailleerd"}.get(detailniveau, detailniveau)
        truncated_note = f" • ⚠️ afgekapt op {_MAX_MESSAGES}" if truncated else ""
        footer = f"{len(messages)} berichten{truncated_note} • model: gpt-4.1-mini • {detail_label}"

        # Split long summaries (gedetailleerd can exceed embed description limit)
        _EMBED_MAX = 4000
        chunks = []
        if len(summary) <= _EMBED_MAX:
            chunks = [summary]
        else:
            # Split on paragraph boundaries where possible
            current = ""
            for paragraph in summary.split("\n\n"):
                segment = ("\n\n" if current else "") + paragraph
                if len(current) + len(segment) > _EMBED_MAX:
                    chunks.append(current)
                    current = paragraph
                else:
                    current += segment
            if current:
                chunks.append(current)

        for i, chunk in enumerate(chunks):
            title = f"📋 Samenvatting — afgelopen {label}"
            if len(chunks) > 1:
                title += f" ({i + 1}/{len(chunks)})"
            embed = discord.Embed(
                title=title,
                description=chunk,
                colour=discord.Colour.blurple(),
            )
            if i == len(chunks) - 1:
                embed.set_footer(text=footer)
            if i == 0:
                await interaction.followup.send(embed=embed)
            else:
                assert interaction.channel is not None
                await interaction.channel.send(embed=embed)  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _summarise(
        self, transcript: str, uren: int, n_messages: int, api_key: str, detailniveau: str = "normaal"
    ) -> str:
        """Call the OpenAI Chat Completions API and return the summary text."""
        import httpx

        system_prompt = _SYSTEM_PROMPTS.get(detailniveau, _SYSTEM_PROMPTS["normaal"])
        max_tokens = _MAX_TOKENS_PER_LEVEL.get(detailniveau, 1024)

        user_prompt = (
            f"Hieronder staan {n_messages} berichten uit een Discord-kanaal van de afgelopen {uren} uur. "
            f"Schrijf een samenvatting:\n\n{transcript}"
        )

        payload = {
            "model": "gpt-4.1-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.4,
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        return data["choices"][0]["message"]["content"].strip()

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    @samenvatting.error
    async def _on_cooldown(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"Wacht nog {error.retry_after:.0f} seconden voordat je dit opnieuw gebruikt.",
                ephemeral=True,
            )
        else:
            raise error


async def setup(bot: "DiscordBot") -> None:
    await bot.add_cog(SamenvattingCog(bot))
