"""
General bot commands — /help, /botinfo, /serverinfo, /ping, /invite,
/eight_ball (question), and /feedback.
"""

from __future__ import annotations

import platform
import random
import re
import typing
from datetime import datetime
from typing import TYPE_CHECKING

import discord
import discord.utils
import pytz
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

if TYPE_CHECKING:
    from bot import DiscordBot

# Configuration is provided by the bot at runtime via `bot.config`.


class FeedbackForm(discord.ui.Modal, title="Feedback"):
    """Modal dialog for submitting feedback to the bot owners."""

    feedback = discord.ui.TextInput(
        label="Wat vind je van deze bot?",
        style=discord.TextStyle.long,
        placeholder="Typ je antwoord hier...",
        required=True,
        max_length=256,
    )

    async def on_submit(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.answer = str(self.feedback)
        self.stop()


class General(commands.Cog, name="general"):
    """
    Cog for general-purpose commands like /help, /botinfo,
    /serverinfo, /ping, /invite, and /feedback.
    """

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot
        self.context_menu_user = app_commands.ContextMenu(
            name="Grab ID", callback=self.grab_id
        )
        self.bot.tree.add_command(self.context_menu_user)
        self.context_menu_message = app_commands.ContextMenu(
            name="Remove spoilers", callback=self.remove_spoilers
        )
        self.bot.tree.add_command(self.context_menu_message)
        self.color = int(
            self.bot.config.get("colors", {}).get("primary", "0x154273"), 16
        )
        self.config = getattr(self.bot, "config", {}) or {}

    def _api_status_embed(self, status: dict[str, object]) -> discord.Embed:
        ok = bool(status.get("ok"))
        embed = discord.Embed(
            title="🏓 API Ping",
            description=(
                "PONG — WarEra API reageert."
                if ok
                else "Geen PONG — WarEra API reageert niet."
            ),
            color=(
                self.color
                if ok
                else int(self.config.get("colors", {}).get("warning", "0xF59E42"), 16)
            ),
        )
        embed.add_field(name="Status", value="Online" if ok else "Offline", inline=True)
        embed.add_field(
            name="Latency",
            value=f"{status.get('latency_ms', '—')} ms",
            inline=True,
        )
        last_success = status.get("last_success_at")
        if isinstance(last_success, datetime):
            embed.add_field(
                name="Laatste succes",
                value=f"<t:{int(last_success.timestamp())}:R>",
                inline=True,
            )
        last_failure = status.get("last_failure_at")
        if isinstance(last_failure, datetime):
            embed.add_field(
                name="Laatste fout",
                value=f"<t:{int(last_failure.timestamp())}:R>",
                inline=True,
            )
        error = status.get("last_error")
        if error:
            embed.add_field(
                name="Foutmelding",
                value=f"`{str(error)[:900]}`",
                inline=False,
            )
        base_url = status.get("base_url")
        if base_url:
            embed.set_footer(text=str(base_url))
        return embed

    @commands.command(name="apiping")
    async def apiping(self, ctx: Context) -> None:
        client = getattr(self.bot, "_ext_client", None)
        if not client or not hasattr(client, "ping"):
            embed = discord.Embed(
                title="🏓 API Ping",
                description="Geen API-client beschikbaar in deze bot-sessie.",
                color=int(
                    self.config.get("colors", {}).get("warning", "0xF59E42"), 16
                ),
            )
            await ctx.send(embed=embed)
            return
        status = await client.ping()
        await ctx.send(embed=self._api_status_embed(status))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        Suppress embeds for messages that contain links to app.warera.io.
        Requires MANAGE_MESSAGES permission for the bot in the channel.
        """
        if message.author.bot:
            return

        # Tagging this user (176648226521350144) on the production guild →
        # fixed reply using their current server nickname. Checks raw content
        # for an explicit <@id> tag rather than message.mentions, since
        # mentions also includes the replied-to author on a ping-reply even
        # without an actual @tag in the text.
        if (
            message.guild
            and message.guild.id == 1401530718223335496
            and (
                "<@176648226521350144>" in message.content
                or "<@!176648226521350144>" in message.content
            )
        ):
            member = message.guild.get_member(176648226521350144)
            if member is None:
                try:
                    member = await message.guild.fetch_member(176648226521350144)
                except discord.HTTPException:
                    member = None
            name = member.display_name if member else "Deze gebruiker"
            try:
                await message.channel.send(f"{name} heeft wel betere dingen te doen")
            except discord.HTTPException as e:
                self.bot.logger.error(
                    f"Failed to send unreachable message for message {message.id}: {e}"
                )

        content = message.content or ""
        # self.bot.logger.debug(f"Received message: {content} from {message.author} "
        # f"in {getattr(message.channel, 'id', 'DM')}")
        if "hoezeer" in content.lower():
            self.bot.logger.info(
                f"Hoezeer detected in message {message.id} by {message.author} in {getattr(message.channel, 'id', 'DM')}"
            )
            # add hoezeer reaction to the message
            try:
                emoji = self.bot.get_emoji(1475153734806798665)  # hoezeer emoji ID
                if emoji:
                    await message.add_reaction(emoji)
                else:
                    self.bot.logger.error("Hoezeer emoji not found in the bot's cache.")
            except discord.HTTPException as e:
                self.bot.logger.error(
                    f"Failed to add reaction to message {message.id}: {e}"
                )
        words = content.lower().split()

        # fish / vis → send fishSTEER emoji as message (20% chance)
        if any(w in words for w in ("fish", "vis")) and random.random() < 0.20:
            fish_emoji = discord.utils.get(message.guild.emojis, name="fishSTEER") if message.guild else None
            try:
                await message.channel.send(str(fish_emoji) if fish_emoji else ":fish:")
            except discord.HTTPException as e:
                self.bot.logger.error(
                    f"Failed to send fish message for message {message.id}: {e}"
                )

        # "ja" or "yes" alone → :Yesyes: reaction, 10% chance
        if words == ["ja"] or words == ["yes"]:
            if random.random() < 0.10:
                _e = discord.utils.get(message.guild.emojis, name="Yesyes") if message.guild else None
                if _e:
                    try:
                        await message.add_reaction(_e)
                    except discord.HTTPException:
                        pass

        # "nee", "nein", or "no" alone → :Nono: reaction, 10% chance
        if words == ["no"] or words == ["nee"] or words == ["nein"]:
            if random.random() < 0.10:
                _e = discord.utils.get(message.guild.emojis, name="Nono") if message.guild else None
                if _e:
                    try:
                        await message.add_reaction(_e)
                    except discord.HTTPException:
                        pass

        # cinema / bioscoop → always react :Cinema:
        if any(w in words for w in ("cinema", "bioscoop")):
            _e = discord.utils.get(message.guild.emojis, name="Cinema") if message.guild else None
            if _e:
                try:
                    await message.add_reaction(_e)
                except discord.HTTPException:
                    pass

        # dreiging / let op → always react :194376alarm:
        if any(w in words for w in ("dreiging", "let op")):
            _e = discord.utils.get(message.guild.emojis, name="194376alarm") if message.guild else None
            if _e:
                try:
                    await message.add_reaction(_e)
                except discord.HTTPException:
                    pass

        # kiss / kus → react :catKISS:
        if any(w in words for w in ("kiss", "kus")):
            _e = discord.utils.get(message.guild.emojis, name="catKISS") if message.guild else None
            if _e:
                try:
                    await message.add_reaction(_e)
                except discord.HTTPException:
                    pass

        # ALL CAPS message (3+ chars) → 🔇 reaction
        # Strip both typed shortcodes (:KEKW:) and actual Discord custom emoji
        # (<:KEKW:123456789> or <a:KEKW:123456789> for animated) before checking,
        # so that emoji-only messages don't incorrectly trigger the mute reaction.
        _caps_stripped = re.sub(r'<a?:[A-Za-z0-9_]+:\d+>|:[A-Za-z0-9_]+:', '', content).strip()
        if len(_caps_stripped) >= 50 and content == content.upper() and any(c.isalpha() for c in _caps_stripped):
            try:
                await message.add_reaction("🔇")
            except discord.HTTPException:
                pass

        # belgië / belgie → send GIF, 20% chance
        # Normalise accented variants (ë → e, ï → i) then check for exact word "belgie"
        _belgie_words = content.lower().replace("ë", "e").replace("ï", "i").split()
        if "belgie" in _belgie_words and random.random() < 0.20:
            try:
                await message.channel.send("https://klipy.com/gifs/bumpy-ride-1")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send belgië gif for message {message.id}: {e}")

        # voet / feet → send feet GIF, 10% chance
        if any(w in words for w in ("voet", "feet")) and random.random() < 0.10:
            try:
                await message.channel.send("https://i.imgur.com/KgHokLq.gif")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send voet gif for message {message.id}: {e}")

        # pindakaas → rapper sjors GIF, 20% chance
        if "pindakaas" in words and random.random() < 0.20:
            try:
                await message.channel.send("https://klipy.com/gifs/rapper-sjors-rapper")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send pindakaas gif for message {message.id}: {e}")

        # kaas → ik wil kaas GIF, 20% chance
        if "kaas" in words and random.random() < 0.20:
            try:
                await message.channel.send("https://tenor.com/view/ik-wil-kaas-kaas-ik-ben-ook-een-klant-ook-een-klant-klant-gif-16346845693440188996")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send kaas gif for message {message.id}: {e}")

        # mand → mand GIF, 20% chance
        if "mand" in words and random.random() < 0.20:
            try:
                await message.channel.send("https://tenor.com/view/mand-mand-man-internetgekkie-internetgekkies-nederlands-gif-23384628")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send mand gif for message {message.id}: {e}")

        # hamster → hamster wheel GIF, 20% chance
        if "hamster" in words and random.random() < 0.20:
            try:
                await message.channel.send("https://klipy.com/gifs/hamster-hamster-wheel-1")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send hamster gif for message {message.id}: {e}")

        # fiets / mountainbike → send trauma GIF, 20% chance
        if any(w in words for w in ("fiets", "mountainbike")) and random.random() < 0.20:
            try:
                await message.channel.send("https://i.imgur.com/aEtFHPS.gif")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send fiets gif for message {message.id}: {e}")

        # water → send funny GIF, 10% chance
        if "water" in words and random.random() < 0.10:
            try:
                await message.channel.send("https://tenor.com/view/watur-alcoholist-at5-water-gif-gif-18135185")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send water gif for message {message.id}: {e}")

        # Frysk (Frisian) word detected → "praat Nederlands met me" GIF, always
        # Every word below is spelled differently from its standard-Dutch
        # equivalent (and isn't a common English word either), so this only
        # fires on genuine Frisian, not on normal Dutch/English chat — e.g.
        # "tsiis" (not "kaas"), "hûs" (not "huis"), "wêr" (not "waar").
        # Deliberately excludes short lookalikes that DO collide with real
        # words: "wol" (Dutch "wool"), "net" (Dutch "just/network"), "leaf"
        # (English "leaf"), "giet" (Dutch "gieten" conjugation), "fries"
        # (Dutch for "Frisian" and for French fries), "der" (dêr, stripped —
        # collides with the formal/archaic Dutch genitive article, as in
        # "Koningin der Nederlanden"), "freed" (Dutch/English "freed" —
        # excluded even though "freed" itself is Frisian for Friday), "hân"
        # stripped to "han" (collides with the name "Han").
        _FRYSK_WORDS = {
            "moarn", "jun", "goeiemoarn", "goeiejun", "wolkom", "hjoed",
            "juster", "tankewol", "asjebleaft", "hus", "frou", "wurk",
            "libben", "tige", "mem", "heit", "skoalle", "wetter", "brea",
            "moai", "wer", "hjir", "buter", "griene", "grien",
            "tsiis", "sizze", "gjin", "oprjochte",
            "twa", "trije", "fjouwer", "fiif", "seis", "san",
            "snein", "moandei", "tiisdei", "woansdei", "sneon",
            "simmer", "hjerst", "maitiid", "dei", "wike",
            "holle", "foet", "hynder", "ljip",
            "blau", "giel", "swart", "wyt", "grut", "lyts",
            "gean", "sjen", "prate", "hald", "hja",
        }
        # Normalise diacritics (û→u, ê→e, etc.) so "wêr"/"wer", "hûs"/"hus"
        # both match regardless of how the diacritic-less keyboard user typed it.
        _frysk_normalized = (
            content.lower()
            .replace("û", "u").replace("ê", "e").replace("ô", "o")
            .replace("â", "a").replace("î", "i")
            .split()
        )
        if _FRYSK_WORDS & set(_frysk_normalized):
            try:
                await message.channel.send(
                    "https://tenor.com/view/praat-nederlands-met-me-gif-15078166435843314989"
                )
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send Frysk gif for message {message.id}: {e}")

        # animal word → react with matching emoji, 20% chance each
        _ANIMAL_REACTIONS: list[tuple[set[str], str]] = [
            ({"haan", "haantje", "kip", "chicken"}, "🐔"),
            ({"hond", "dog"}, "🐶"),
            ({"kat", "poes", "cat"}, "🐱"),
            ({"muis", "mouse"}, "🐭"),
            ({"konijn", "rabbit"}, "🐰"),
            ({"varken", "pig"}, "🐷"),
            ({"koe", "cow"}, "🐮"),
            ({"schaap", "sheep"}, "🐑"),
            ({"paard", "horse"}, "🐴"),
            ({"eend", "duck"}, "🦆"),
            ({"geit", "goat"}, "🐐"),
            ({"karper"}, "🐟"),
            ({"kreeft"}, "🦞"),
            ({"lynx"}, "🐱"),
            ({"vogel", "bird"}, "🐦"),
            ({"aap", "monkey"}, "🐒"),
        ]
        word_set = set(words)
        for animal_words, emoji_str in _ANIMAL_REACTIONS:
            if animal_words & word_set and random.random() < 0.20:
                try:
                    await message.add_reaction(emoji_str)
                except discord.HTTPException:
                    pass
                break  # only one animal reaction per message

        # "geef me samenvatting papi" → :Nono: + role mention in message
        if content.strip().lower() == "geef me samenvatting papi":
            _nono = discord.utils.find(lambda e: e.name == "Nono", self.bot.emojis)
            _nono_str = str(_nono) if _nono else ":Nono:"
            try:
                await message.channel.send(f"{_nono_str} moet je bij <@1500230693186179084> zijn")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to handle samenvatting papi for {message.id}: {e}")

        # nigeria → money-rain GIF, 1% chance
        if "nigeria" in words and random.random() < 0.01:
            try:
                await message.channel.send("https://klipy.com/gifs/money-rain-105")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send nigeria gif for message {message.id}: {e}")

        # god → ping lolman, 20% chance
        if "god" in words and random.random() < 0.20:
            try:
                await message.channel.send("<@1255474131281907733> Hij is Hem.")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send god message for message {message.id}: {e}")

        # transparantie → mocking-case reply, 20% chance
        if "transparantie" in words and random.random() < 0.20:
            _patrick = discord.utils.get(message.guild.emojis, name="patrickdumb") if message.guild else None
            _patrick_str = str(_patrick) if _patrick else ":patrickdumb:"
            try:
                await message.channel.send(f"tRanSPaRAntiE {_patrick_str}")
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send transparantie message for message {message.id}: {e}")

        # congres → random meeting/debate GIF, 1% chance
        if "congres" in words and random.random() < 0.01:
            gif = random.choice((
                "https://i.imgur.com/wn0ogiX.gif",
                "https://klipy.com/gifs/meeting-yesterdays-meeting",
                "https://klipy.com/gifs/democracy-palpatine",
                "https://tenor.com/view/court-leap-court-room-jump-attack-gif-17768758501529871397",
            ))
            try:
                await message.channel.send(gif)
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send congres gif for message {message.id}: {e}")

        # slapinet / slaapinet → random GIF, 20% chance
        if any(w in words for w in ("slapinet", "slaapinet")) and random.random() < 0.20:
            gif = random.choice((
                "https://klipy.com/gifs/muppetwiki-muppet-wiki-94",
                "https://klipy.com/gifs/angry-mob-mad",
            ))
            try:
                await message.channel.send(gif)
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send slapinet gif for message {message.id}: {e}")

        # pils → random beer GIF, 20% chance
        if "pils" in words and random.random() < 0.20:
            gif = random.choice((
                "https://giphy.com/gifs/afv-funny-fail-lol-26tP21xUQnOCIIoFi",
                "https://giphy.com/gifs/wJt98DnjJHbJW46KsA",
                "https://tenor.com/view/dog-beer-happy-dog-party-dog-party-puppy-lets-celebrate-gif-27139602",
                "https://tenor.com/view/beer-chug-drinking-drink-thirsty-gif-4362970331505802377",
                "https://tenor.com/view/beer-bottle-beer-cup-beer-meme-gif-25674419",
                "https://tenor.com/view/open-bottle-getting-sprayed-fail-funny-getting-blasted-gif-15850111",
            ))
            try:
                await message.channel.send(gif)
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send pils gif for message {message.id}: {e}")

        # ezel / donkey → random GIF
        if any(w in words for w in ("ezel", "donkey")):
            gif = random.choice((
                "https://klipy.com/gifs/feliz-alegre-2",
                "https://klipy.com/gifs/tongue-out-tickle-8",
                "https://klipy.com/gifs/laughing-funny-167",
                "https://klipy.com/gifs/donkey-flip",
            ))
            try:
                await message.channel.send(gif)
            except discord.HTTPException as e:
                self.bot.logger.error(f"Failed to send ezel gif for message {message.id}: {e}")

        if "app.warera.io" not in content.lower():
            return
        try:
            await message.edit(suppress=True)
            self.bot.logger.info(
                f"Suppressed embeds for message {message.id} in "
                f"{getattr(message.channel, 'id', 'DM')}"
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            self.bot.logger.error(
                f"Failed to suppress embeds for message {message.id}: {e}"
            )

    # Message context menu command
    async def remove_spoilers(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """
        Removes the spoilers from the message.
        This command requires the MESSAGE_CONTENT intent to work properly.

        :param interaction: The application command interaction.
        :param message: The message that is being interacted with.
        """
        spoiler_attachment = None
        for attachment in message.attachments:
            if attachment.is_spoiler():
                spoiler_attachment = attachment
                break
        embed = discord.Embed(
            title="Bericht zonder spoilers",
            description=message.content.replace("||", ""),
            color=self.color,
        )
        if spoiler_attachment is not None:
            embed.set_image(url=attachment.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # User context menu command
    async def grab_id(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        """
        Grabs the ID of the user.

        :param interaction: The application command interaction.
        :param user: The user that is being interacted with.
        """
        embed = discord.Embed(
            description=f"Het ID van {user.mention} is `{user.id}`.",
            color=self.color,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # Emoji per cog name (lowercase) for the help embed headers.
    _COG_EMOJI: dict[str, str] = {
        "general": "ℹ️",
        "owner": "👑",
        "allies": "🤝",
        "welcome": "👋",
        "giveaways": "🎁",
        "embeds": "🖼️",
        "generate_embeds": "🖼️",
        "bonuscog": "🏭",
        "bedrijfswinstcog": "💰",
        "buddysysteemcog": "🤝",
        "ecobuildcog": "🌱",
        "geluk": "🍀",
        "globalluck": "🌍",
        "gevechten": "⚔️",
        "gevechten cog": "⚔️",
        "leaderboardcog": "🏆",
        "monitorcog": "👁️",
        "mu": "🪖",
        "murequest": "🪖",
        "mu_roles": "🪖",
        "mudmgcog": "💥",
        "niveauverdelingcog": "📊",
        "paraatheadcog": "🛡️",
        "peilcog": "🔄",
        "pillenfabriekencog": "💊",
        "pill_reminder": "💊",
        "proxycog": "🔀",
        "samenvattingcog": "📝",
        "schadecog": "⚔️",
        "scrapvaluecog": "♻️",
        "spelerinactiviteitcog": "💤",
        "transactiescog": "💳",
        "users": "👥",
        "weeklydmgcog": "📅",
        "dukaten": "🪙",
        "battles": "⚔️",
        "general_role_selection": "🎭",
        "roles": "🎭",
        "article_scanner": "📰",
        "reddit": "📰",
        "service_coordinator": "⚙️",
    }

    @commands.hybrid_command(
        name="help", description="Toon alle commands die de bot heeft geladen."
    )
    @app_commands.describe(
        privileged="Toon ook commands die speciale rechten vereisen (standaard verborgen)."
    )
    async def help(self, context: Context, privileged: bool = False) -> None:
        """Show all commands that the bot has loaded."""
        if context.interaction:
            await context.interaction.response.defer(ephemeral=False)

        fields: list[tuple[str, str]] = []

        for cog_name in self.bot.cogs:
            # Owner cog: only show to the bot owner, and only with privileged=True
            if cog_name == "owner":
                if not privileged or not await self.bot.is_owner(context.author):
                    continue

            cog = self.bot.get_cog(cog_name.lower())
            if cog is None:
                continue

            # Prefer the app_commands representation of hybrid commands (HybridAppCommand)
            # so that checks set via @app_commands.check() / @has_privileged_role() are
            # correctly detected. Deduplicate by name so hybrid commands don't appear twice.
            seen: set[str] = set()
            cog_commands: list = []
            for cmd in cog.get_app_commands():
                if cmd.name not in seen:
                    cog_commands.append(cmd)
                    seen.add(cmd.name)
            for cmd in cog.get_commands():
                if cmd.name not in seen:
                    cog_commands.append(cmd)
                    seen.add(cmd.name)
            # In privileged mode, also include hidden prefix commands (e.g. !rollen_check)
            if privileged:
                for cmd in cog.__cog_commands__:
                    if getattr(cmd, "hidden", False) and cmd.name not in seen:
                        cog_commands.append(cmd)
                        seen.add(cmd.name)
            data = []
            for command in cog_commands:
                is_priv = self._is_privileged(command)
                # In privileged mode: show ONLY privileged commands.
                # In normal mode: show ONLY non-privileged commands.
                if is_priv != privileged:
                    continue
                description = (command.description or getattr(command, "brief", None) or getattr(command.callback, "__doc__", None) or "").partition("\n")[0]
                # Use ! prefix for hidden prefix-only commands, / for slash/hybrid
                is_hidden_prefix = getattr(command, "hidden", False)
                prefix = "!" if is_hidden_prefix else "/"
                name_str = f"`{prefix}{command.name}`"
                line = f"{name_str} — {description}" if description else name_str
                data.append(line)

            if data:
                emoji = self._COG_EMOJI.get(cog_name.lower(), "▪️")
                header = f"{emoji} {cog_name.capitalize()}"
                # Discord embed field values are capped at 1024 characters.
                # Split into multiple fields if needed.
                chunk: list[str] = []
                chunk_len = 0
                part = 0
                for line in data:
                    # +1 for the newline separator
                    if chunk and chunk_len + len(line) + 1 > 1024:
                        part += 1
                        field_name = f"{header} ({part})" if part > 1 else header
                        fields.append((field_name, "\n".join(chunk)))
                        chunk = []
                        chunk_len = 0
                    chunk.append(line)
                    chunk_len += len(line) + 1
                if chunk:
                    part += 1
                    field_name = f"{header} ({part})" if part > 1 else header
                    fields.append((field_name, "\n".join(chunk)))

        if not fields:
            await context.send("Geen beschikbare commands gevonden.")
            return

        testing: bool = getattr(self.bot, "testing", False)
        rollen_channel_id = 1474454434368061513 if testing else 1456612515902390353
        # Discord limit: max 25 fields per embed
        for idx in range(0, len(fields), 25):
            if privileged:
                description = "Commando's die speciale rechten vereisen."
            else:
                description = (
                    f"Bekijk <#{rollen_channel_id}> voor extra bot-functies zoals bounty pings.\n"
                    "Gebruik `/help privileged:True` om uitsluitend beheercommands te tonen."
                )
            embed = discord.Embed(
                title="🔒 Beheercommands" if privileged else "📖 Help",
                description=description,
                color=self.color,
            )
            for name, value in fields[idx : idx + 25]:
                embed.add_field(name=name, value=value, inline=False)
            await context.send(embed=embed)

    def _is_privileged(self, command) -> bool:
        """Return True if the command requires special permissions.

        Checks (in order):
        1. command.checks — populated by @app_commands.check() and @commands.check()
        2. command.__discord_app_commands_checks__ — set by @app_commands.check() when
           applied directly to a HybridCommand object (not the callback function)
        3. callback.__commands_checks__ — set by @commands.check() on functions that
           are wrapped as app_commands.Command (e.g. @has_mu_privilige on app commands)
        4. callback.__discord_app_commands_checks__ — set by @app_commands.check() on
           the raw callback before it was wrapped into a Command
        5. default_member_permissions — set by @app_commands.default_permissions()
        """
        if getattr(command, "checks", None):
            return True
        # app_commands.check() applied to a HybridCommand stores here
        if getattr(command, "__discord_app_commands_checks__", None):
            return True
        callback = getattr(command, "callback", None)
        if callback and getattr(callback, "__commands_checks__", None):
            return True
        if callback and getattr(callback, "__discord_app_commands_checks__", None):
            return True
        if getattr(command, "default_member_permissions", None) is not None:
            return True
        return False

    @commands.command(name="botinfo")
    async def botinfo(self, context: Context) -> None:
        """
        Get some useful (or not) information about the bot.

        :param context: The hybrid command context.
        """
        embed = discord.Embed(
            description="Rijksoverheid bot voor de Nederlandse WarEra Discord-server.",
            color=self.color,
        )
        embed.set_author(name="Bot-informatie")
        embed.add_field(name="Eigenaar:", value="teunp", inline=True)
        embed.add_field(
            name="Python-versie:", value=f"{platform.python_version()}", inline=True
        )
        embed.add_field(
            name="Prefix:",
            value=f"/ (Slash-commands) of {self.bot.bot_prefix} voor normale commands",
            inline=False,
        )
        embed.set_footer(text=f"Gevraagd door {context.author}")
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="serverinfo",
        description="Laat nuttige informatie over de server zien.",
    )
    async def serverinfo(self, context: Context) -> None:
        """
        Get some useful (or not) information about the server.

        :param context: The hybrid command context.
        """
        roles = [role.name for role in context.guild.roles]
        num_roles = len(roles)
        if num_roles > 50:
            roles = roles[:50]
            roles.append(f">>>> Displaying [50/{num_roles}] Roles")
        roles = ", ".join(roles)

        embed = discord.Embed(
            title="**Servernaam:**", description=f"{context.guild}", color=self.color
        )
        if context.guild.icon is not None:
            embed.set_thumbnail(url=context.guild.icon.url)
        embed.add_field(name="Server-ID", value=context.guild.id)
        embed.add_field(name="Ledenaantal", value=context.guild.member_count)
        embed.add_field(
            name="Tekst/Spraakkanalen", value=f"{len(context.guild.channels)}"
        )
        embed.add_field(name=f"Rollen ({len(context.guild.roles)})", value=roles)
        embed.set_footer(text=f"Aangemaakt op: {context.guild.created_at}")
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="ping",
        description="Controleer of de bot online is.",
    )
    async def ping(self, context: Context) -> None:
        """
        Check if the bot is alive.

        :param context: The hybrid command context.
        """
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"De botvertraging is {round(self.bot.latency * 1000)}ms.",
            color=0xBEBEFE,
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="invite",
        description="Krijg de uitnodigingslink van de bot.",
    )
    async def invite(self, context: Context) -> None:
        """
        Get the invite link of the bot to be able to invite it.

        :param context: The hybrid command context.
        """
        embed = discord.Embed(
            description=f"Nodig me uit door [hier]({self.bot.invite_link}) te klikken.",
            color=self.color,
        )
        try:
            await context.author.send(embed=embed)
            await context.send("Ik heb je een privébericht gestuurd!")
        except discord.Forbidden:
            await context.send(embed=embed)

    # @commands.hybrid_command(
    #     name="server",
    #     description="Get the invite link of the discord server of the bot for some support.",
    # )
    # async def server(self, context: Context) -> None:
    #     """
    #     Get the invite link of the discord server of the bot for some support.

    #     :param context: The hybrid command context.
    #     """
    #     embed = discord.Embed(
    #         description=f"Join the support server for the bot by clicking [here](https://discord.gg/mTBrXyWxAF).",
    #         color=self.color,
    #     )
    #     try:
    #         await context.author.send(embed=embed)
    #         await context.send("I sent you a private message!")
    #     except discord.Forbidden:
    #         await context.send(embed=embed)

    @commands.command(name="8ball")
    async def eight_ball(self, context: Context, *, question: str) -> None:
        """
        Ask any question to the bot.

        :param context: The hybrid command context.
        :param question: The question that should be asked by the user.
        """
        answers = [
            "Het is zeker.",
            "Absoluut.",
            "Je kunt erop rekenen.",
            "Zonder twijfel.",
            "Ja, zeker weten.",
            "Zoals ik het zie, ja.",
            "Hoogstwaarschijnlijk.",
            "Ziet er goed uit.",
            "Ja.",
            "Alle tekenen wijzen op ja.",
            "Antwoord vaag, probeer later opnieuw.",
            "Vraag het later nog eens.",
            "Beter om het nu niet te zeggen.",
            "Kan het nu niet voorspellen.",
            "Concentreer je en stel de vraag opnieuw.",
            "Reken er maar niet op.",
            "Mijn antwoord is nee.",
            "Mijn bronnen zeggen nee.",
            "Vooruitzichten niet zo goed.",
            "Zeer twijfelachtig.",
        ]
        embed = discord.Embed(
            title="**Mijn Antwoord:**",
            description=f"{random.choice(answers)}",
            color=self.color,
        )
        embed.set_footer(text=f"De vraag was: {question}")
        await context.send(embed=embed)

    # @commands.hybrid_command(
    #     name="bitcoin",
    #     description="Get the current price of bitcoin.",
    # )
    # async def bitcoin(self, context: Context) -> None:
    #     """
    #     Get the current price of bitcoin.

    #     :param context: The hybrid command context.
    #     """
    #     # This will prevent your bot from stopping everything when doing a web request -
    #     # see: https://discordpy.readthedocs.io/en/stable/faq.html#how-do-i-make-a-web-request
    #     async with aiohttp.ClientSession() as session:
    #         async with session.get(
    #             "https://api.coindesk.com/v1/bpi/currentprice/BTC.json"
    #         ) as request:
    #             if request.status == 200:
    #                 data = await request.json()
    #                 embed = discord.Embed(
    #                     title="Bitcoin price",
    #                     description=f"The current price is {data['bpi']['USD']['rate']} :dollar:",
    #                     color=self.color,
    #                 )
    #             else:
    #                 embed = discord.Embed(
    #                     title="Error!",
    #                     description="There is something wrong with the API, "
    #                                   "please try again later",
    #                     color=self.color,
    #                 )
    #             await context.send(embed=embed)

    @commands.command(name="feedback")
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def feedback(self, context: Context, *, feedback_text: str) -> None:
        """Submit feedback to the bot owners."""
        await context.send(
            embed=discord.Embed(
                description="Bedankt voor je feedback, de eigenaren zijn op de hoogte gesteld.",
                color=self.color,
            )
        )
        app_owner = (await self.bot.application_info()).owner
        await app_owner.send(
            embed=discord.Embed(
                title="Nieuwe Feedback",
                description=f"{context.author} (<@{context.author.id}>) heeft nieuwe "
                f"feedback ingediend:\n```\n{feedback_text}\n```",
                color=self.color,
            )
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Log when a member leaves the server."""
        self.bot.logger.info(f"{member} has left the server.")
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = member.guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    # Detect whether this was a kick by checking recent audit logs.
                    kicked_entry: typing.Optional[discord.AuditLogEntry] = None
                    try:
                        now = discord.utils.utcnow()
                        async for entry in member.guild.audit_logs(
                            limit=6, action=discord.AuditLogAction.kick
                        ):
                            if getattr(entry.target, "id", None) == member.id:
                                # consider entries within 10 seconds recent enough
                                if (now - entry.created_at).total_seconds() < 10:
                                    kicked_entry = entry
                                    break
                    except Exception:
                        kicked_entry = None

                    if kicked_entry:
                        moderator = kicked_entry.user
                        reason = kicked_entry.reason or "Geen reden opgegeven"
                        log_embed = discord.Embed(
                            title="Gebruiker verwijderd (Kick)",
                            description=(
                                f"**{member.mention} ({member.name}) is gekickt**\n"
                                f"**Door:** {moderator.mention if moderator else moderator}\n"
                                f"**Reden:** {reason}"
                            ),
                            color=discord.Color.red(),
                            timestamp=datetime.now(
                                pytz.timezone("Europe/Amsterdam")
                            ),
                        )
                        if member:
                            log_embed.set_author(
                                name=member.name, icon_url=member.display_avatar.url
                            )
                            log_embed.set_thumbnail(url=member.display_avatar.url)
                        await log_channel.send(embed=log_embed)
                    else:
                        log_embed = discord.Embed(
                            # title="Gebruiker heeft de server verlaten",
                            description=f"**{member.mention if member else 'Unknown'} "
                            f"({member.name if member else 'Unknown'}) heeft de "
                            f"server verlaten**\n",
                            color=discord.Color.red(),
                            timestamp=datetime.now(
                                pytz.timezone("Europe/Amsterdam")
                            ),
                        )
                        if member:
                            log_embed.set_author(
                                name=member.name, icon_url=member.display_avatar.url
                            )
                            log_embed.set_thumbnail(url=member.display_avatar.url)
                        await log_channel.send(embed=log_embed)
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.bot.logger.error(f"Failed to post to log channel: {e}")

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        """Log when a user is banned (with audit-log moderator & reason)."""
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if not log_channel_id:
            return
        log_channel = guild.get_channel(log_channel_id)
        if not log_channel:
            return

        moderator = None
        reason = None
        try:
            async for entry in guild.audit_logs(
                limit=6, action=discord.AuditLogAction.ban
            ):
                if getattr(entry.target, "id", None) == user.id:
                    moderator = entry.user
                    reason = entry.reason
                    break
        except Exception as e:
            self.bot.logger.debug("Could not fetch audit logs for ban: %s", e)

        description = (
            f"**{user} ({getattr(user, 'name', user.id)}) is gebanned**\n"
            f"**Door:** {moderator.mention if moderator else moderator}\n"
            f"**Reden:** {reason or 'Geen reden opgegeven'}"
        )
        embed = discord.Embed(
            title="Gebruiker verbannen",
            description=description,
            color=discord.Color.dark_red(),
            timestamp=datetime.now(pytz.timezone("Europe/Amsterdam")),
        )
        try:
            await log_channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            self.bot.logger.error(f"Failed to post ban log: {e}")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        """Log when a user is unbanned (with audit-log moderator & reason)."""
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if not log_channel_id:
            return
        log_channel = guild.get_channel(log_channel_id)
        if not log_channel:
            return

        moderator = None
        reason = None
        try:
            async for entry in guild.audit_logs(
                limit=6, action=discord.AuditLogAction.unban
            ):
                if getattr(entry.target, "id", None) == user.id:
                    moderator = entry.user
                    reason = entry.reason
                    break
        except Exception as e:
            self.bot.logger.debug("Could not fetch audit logs for unban: %s", e)

        description = (
            f"**{user} ({getattr(user, 'name', user.id)}) is unbanned**\n"
            f"**Door:** {moderator.mention if moderator else moderator}\n"
            f"**Reden:** {reason or 'Geen reden opgegeven'}"
        )
        embed = discord.Embed(
            title="Gebruiker unbanned",
            description=description,
            color=discord.Color.green(),
            timestamp=datetime.now(pytz.timezone("Europe/Amsterdam")),
        )
        try:
            await log_channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            self.bot.logger.error(f"Failed to post unban log: {e}")

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Log when a member is updated, specifically for role changes."""
        self.bot.logger.info(f"{before} has been updated.")
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = before.guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    role_changes = []
                    if before.roles != after.roles:
                        added_roles = [
                            role for role in after.roles if role not in before.roles
                        ]
                        removed_roles = [
                            role for role in before.roles if role not in after.roles
                        ]
                        if added_roles:
                            role_changes.append(
                                f":white_check_mark: {', '.join(role.name for role in added_roles)}"
                            )
                        if removed_roles:
                            role_changes.append(
                                f":no_entry: {', '.join(role.name for role in removed_roles)}"
                            )
                    else:
                        return
                    log_embed = discord.Embed(
                        # title=f"{before.name}",
                        description=f"**:writing_hand: {before.mention if before else 'Unknown'} is bijgewerkt.** \n"
                        f"**Rollen:**\n{chr(10).join(role_changes) if role_changes else 'Geen veranderingen in rollen.'}",
                        color=discord.Color.orange(),
                        timestamp=datetime.now(
                            pytz.timezone("Europe/Amsterdam")
                        ),
                    )
                    log_embed.set_author(
                        name=before.name,
                        icon_url=before.display_avatar.url if before else None,
                    )
                    if before:
                        log_embed.set_thumbnail(url=before.display_avatar.url)
                    await log_channel.send(embed=log_embed)
                except Exception as e:
                    self.bot.logger.error(f"Failed to post to log channel: {e}")

    @commands.command(name="testleave")
    @commands.is_owner()
    async def test_leave(self, context: Context) -> None:
        """Test command to simulate a member leaving the server."""
        await self.on_member_remove(context.author)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Handle app command errors, including cooldown messages."""
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Je kunt dit commando pas weer gebruiken over **{error.retry_after:.0f} seconden**.",
                ephemeral=True,
            )
        else:
            raise error


async def setup(bot) -> None:
    """Add the General cog to the bot."""
    await bot.add_cog(General(bot))
