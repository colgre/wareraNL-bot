"""
Member verification and welcome flow.

Commands and listeners:
  !postwelcome              — (admin) post the welcome message with verification buttons
  on_member_join            — automatically prompts new members to verify
  /nickname (user, nickname) — change a member's server nickname
    /approve (in_game_id, reason) — approve a pending verification request
  /deny (reason)            — deny a pending verification request
    /embassyapprove (country, in_game_id) — approve an embassy membership request
"""

import asyncio
import datetime
import json
import logging
import re
import secrets
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from cogs.commands._base import country_autocomplete
from services.country_utils import _COUNTRY_ALIASES
from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")

# Dutch display labels for the fixed-slot minister "title" values returned by
# /government.getByCountryId. Duplicated (rather than imported) from the
# same small mapping in war_guild_divisions.py / government_role_sync.py —
# this file is production-only and shouldn't depend on the war-guild cog.
_GOV_TITLE_DISPLAY: dict[str, str] = {
    "minOfDefense": "Minister van Defensie",
    "minOfEconomy": "Minister van Economie",
    "minOfForeignAffairs": "Minister van Buitenlandse Zaken",
    "minOfInterior": "Minister van Binnenlandse Zaken",
    "minOfCommunication": "Minister van Communicatie",
    "chiefOfStaff": "Stafchef",
}

# Verification codes are typed by hand into a game text field, so the
# alphabet drops visually ambiguous characters (0/O, 1/I/L).
_VERIFICATION_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_VERIFICATION_CODE_TTL_SECONDS = 30 * 60


def _generate_verification_code(length: int = 8) -> str:
    """Generate a short random code for the in-game bio ownership check."""
    return "".join(secrets.choice(_VERIFICATION_CODE_ALPHABET) for _ in range(length))


class WelcomeView(discord.ui.View):
    """
    Persistent view containing the three verification buttons.

    Using timeout=None and custom_id makes these buttons persist
    across bot restarts - they'll still work after the bot reconnects.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Nederlander",
        style=discord.ButtonStyle.success,
        custom_id="welcome_citizen",
        emoji="🇳🇱",
    )
    async def citizen_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        """Handle citizen verification request."""
        await interaction.response.send_modal(VerificationQuestionnaireModal("citizen"))

    @discord.ui.button(
        label="Belgian",
        style=discord.ButtonStyle.success,
        custom_id="welcome_belgian",
        emoji="🇧🇪",
    )
    async def belgian_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Handle Belgian verification request."""
        await interaction.response.send_modal(VerificationQuestionnaireModal("belgian"))

    @discord.ui.button(
        label="Foreigner",
        style=discord.ButtonStyle.primary,
        custom_id="welcome_foreigner",
        emoji="🌍",
    )
    async def foreigner_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Handle foreigner verification request."""
        await interaction.response.send_modal(
            VerificationQuestionnaireModal("foreigner")
        )

    @discord.ui.button(
        label="Embassy Request",
        style=discord.ButtonStyle.danger,
        custom_id="welcome_embassy",
        emoji="🚨",
    )
    async def embassy_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Handle embassy request."""
        await interaction.response.send_modal(VerificationQuestionnaireModal("embassy"))

    @discord.ui.button(
        label="Admin Contact",
        style=discord.ButtonStyle.secondary,
        custom_id="welcome_admin_contact",
        emoji="📩",
    )
    async def admin_contact_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Handle admin contact request."""
        await interaction.response.send_modal(AdminContactModal())


class AutoVerifyView(discord.ui.View):
    """Persistent single-button view for the in-game bio-code ownership check.

    One fixed custom_id shared by every ticket — the callback reads
    ``interaction.channel`` to find which ticket/code it belongs to, so this
    survives bot restarts as long as it's registered once via
    ``bot.add_view`` (done in Welcome.__init__), same technique as
    WelcomeView above.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Ik heb mijn bio aangepast ✅",
        style=discord.ButtonStyle.success,
        custom_id="autoverify_check_bio",
    )
    async def check_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        cog = interaction.client.get_cog("welcome")
        if cog is None:
            await interaction.response.send_message(
                "Interne fout: verificatiemodule niet geladen.", ephemeral=True
            )
            return
        await cog.handle_auto_verify_click(interaction)


class EmbassyDecisionView(discord.ui.View):
    """Persistent approve/deny buttons shown after an embassy ticket passes
    the automatic in-game ownership check (see AutoVerifyView)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Toevoegen aan ambassade",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="embassy_decision_approve",
    )
    async def approve_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        cog = interaction.client.get_cog("welcome")
        if cog is None:
            await interaction.response.send_message(
                "Interne fout: verificatiemodule niet geladen.", ephemeral=True
            )
            return
        await cog.handle_embassy_decision_click(interaction, approve=True)

    @discord.ui.button(
        label="Afwijzen",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="embassy_decision_deny",
    )
    async def deny_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        cog = interaction.client.get_cog("welcome")
        if cog is None:
            await interaction.response.send_message(
                "Interne fout: verificatiemodule niet geladen.", ephemeral=True
            )
            return
        await cog.handle_embassy_decision_click(interaction, approve=False)


class AdminContactModal(discord.ui.Modal, title="Contact Admins"):
    """Simple modal for sending a question or suggestion to admins."""

    message = discord.ui.TextInput(
        label="Vraag of suggestie",
        style=discord.TextStyle.paragraph,
        placeholder="Stel je vraag of geef je suggestie hier...",
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await create_verification_channel(
                interaction,
                "admin_contact",
                questionnaire_answers={"Vraag / Suggestie": str(self.message).strip()},
            )
        except Exception:
            logger.exception(
                "Unexpected error in create_verification_channel for %s (admin_contact)",
                interaction.user,
            )
            await interaction.followup.send(
                "Er is een onverwachte fout opgetreden. Probeer het opnieuw of neem contact op met een moderator.",
                ephemeral=True,
            )


class VerificationQuestionnaireModal(discord.ui.Modal):
    """Questionnaire shown to users before opening a verification ticket."""

    def __init__(self, request_type: str):
        self.request_type = request_type
        is_english = request_type in {"belgian", "foreigner", "embassy"}
        super().__init__(
            title=(
                "Verification Questionnaire"
                if is_english
                else "Verificatie Vragenlijst"
            )
        )

        self.warera_name = discord.ui.TextInput(
            label="WarEra username" if is_english else "WarEra gebruikersnaam",
            placeholder=(
                "Enter your in-game name" if is_english else "Vul je in-game naam in"
            ),
            required=True,
            max_length=64,
        )
        self.profile_link = discord.ui.TextInput(
            label=(
                "URL to your in-game profile or your user ID"
                if is_english
                else "Profiel-URL of gebruikers-ID"
            ),
            style=discord.TextStyle.paragraph,
            placeholder=(
                "Paste your profile URL or user ID"
                if is_english
                else "Plak je profiellink of gebruikers-ID"
            ),
            required=True,
            max_length=500,
        )
        self.extra_info = discord.ui.TextInput(
            label="Additional info" if is_english else "Aanvullende info",
            style=discord.TextStyle.paragraph,
            placeholder=(
                "Optional: extra context for the moderators"
                if is_english
                else "Optioneel: extra context voor de moderators"
            ),
            required=False,
            max_length=500,
        )

        self.add_item(self.warera_name)
        self.add_item(self.profile_link)

        self.embassy_country = None
        if self.request_type == "embassy":
            self.embassy_country = discord.ui.TextInput(
                label="Country",
                placeholder="Which country is this embassy request for?",
                required=True,
                max_length=64,
            )
            self.add_item(self.embassy_country)

        self.add_item(self.extra_info)

    async def on_submit(self, interaction: discord.Interaction):
        is_english = self.request_type in {"belgian", "foreigner", "embassy"}

        raw_profile_value = str(self.profile_link).strip().strip("<>")
        profile_value_for_admins = raw_profile_value
        if raw_profile_value and "://" not in raw_profile_value:
            profile_value_for_admins = f"https://app.warera.io/user/{raw_profile_value}"

        questionnaire_answers = {
            ("WarEra username" if is_english else "WarEra gebruikersnaam"): str(
                self.warera_name
            ).strip(),
            (
                "URL to your in-game profile or your user ID"
                if is_english
                else "Profiel-URL of gebruikers-ID"
            ): profile_value_for_admins,
        }
        canonical_country: str | None = None
        if self.embassy_country:
            raw_country = str(self.embassy_country).strip()
            cog = interaction.client.get_cog("welcome")
            resolved = await cog._resolve_country(raw_country) if cog else None
            if not resolved:
                await interaction.response.send_message(
                    f"⚠️ '{raw_country}' is not a recognized country name. Please use "
                    "the exact country name (e.g. 'France', 'Germany') and try again.",
                    ephemeral=True,
                )
                return
            _country_id, canonical_country = resolved
            questionnaire_answers["Country"] = canonical_country

        extra = str(self.extra_info).strip()
        if extra:
            questionnaire_answers[
                "Additional info" if is_english else "Aanvullende info"
            ] = extra

        try:
            await create_verification_channel(
                interaction,
                self.request_type,
                questionnaire_answers=questionnaire_answers,
                profile_input=raw_profile_value,
                embassy_country=canonical_country,
            )
        except Exception:
            logger.exception(
                "Unexpected error in create_verification_channel for %s (%s)",
                interaction.user,
                self.request_type,
            )
            msg = "Er is een onverwachte fout opgetreden. Probeer het opnieuw of neem contact op met een moderator."
            await interaction.followup.send(msg, ephemeral=True)


_welcome_db_fallback = None  # Database | None


async def _get_shared_db(client):
    """Return the bot's shared external DB, lazily creating one as fallback.

    Never sets client._ext_db — that is the coordinator's job.  If a standalone
    fallback was opened before services were ready, it is closed and discarded
    the first time the shared connection becomes available.
    """
    global _welcome_db_fallback
    shared = getattr(client, "_ext_db", None)
    if shared is not None:
        if _welcome_db_fallback is not None:
            try:
                await _welcome_db_fallback.close()
            except Exception:
                pass
            _welcome_db_fallback = None
        return shared
    if _welcome_db_fallback is None:
        from services.db import Database
        config = getattr(client, "config", {}) or {}
        db_path = config.get("external_db_path", "database/external.db")
        _welcome_db_fallback = Database(db_path)
        await _welcome_db_fallback.setup()
    return _welcome_db_fallback


async def _start_auto_verification(
    client,
    *,
    channel: discord.TextChannel,
    request_type: str,
    discord_user_id: int,
    in_game_id: str,
    country: str | None,
) -> None:
    """Issue a one-time code and post the automatic-verification
    instructions + button for a citizen/belgian/embassy ticket.

    Failures here are logged by the caller and simply leave the ticket on
    the existing manual flow (screenshot + moderator /approve or
    /embassyapprove) — this is a fast-path on top of it, never a
    replacement.
    """
    db = await _get_shared_db(client)
    code = _generate_verification_code()
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = now + datetime.timedelta(seconds=_VERIFICATION_CODE_TTL_SECONDS)
    await db.upsert_pending_verification_code(
        channel_id=str(channel.id),
        guild_id=str(channel.guild.id),
        discord_user_id=str(discord_user_id),
        request_type=request_type,
        in_game_user_id=in_game_id,
        code=code,
        created_at=now.isoformat(),
        expires_at=expires_at.isoformat(),
        country=country,
    )

    minutes = _VERIFICATION_CODE_TTL_SECONDS // 60
    profile_link = f"https://app.warera.io/user/{in_game_id}"
    if request_type == "citizen":
        description = (
            "Je kan dit verzoek automatisch laten goedkeuren door te bewijzen dat "
            f"je echt de eigenaar bent van [dit WarEra-profiel]({profile_link}):\n\n"
            "1. Ga op WarEra naar je profiel → **Profile** → **Edit**.\n"
            "2. Zet je **Bio/beschrijving** tijdelijk op exact deze code:\n"
            f"```{code}```\n"
            "3. Klik hieronder op de knop.\n\n"
            "Je kan je bio hierna gewoon weer terugzetten naar wat het was. "
            f"Deze code is {minutes} minuten geldig — lukt het niet, dan "
            "beoordeelt een moderator je verzoek handmatig."
        )
    else:
        description = (
            "You can get this request approved automatically by proving you "
            f"actually own [this WarEra profile]({profile_link}):\n\n"
            "1. Go to your WarEra profile → **Profile** → **Edit**.\n"
            "2. Temporarily set your **Bio/description** to exactly this code:\n"
            f"```{code}```\n"
            "3. Click the button below.\n\n"
            "You can change your bio back afterwards. "
            f"This code is valid for {minutes} minutes — if it doesn't work, "
            "a moderator will review your request manually."
        )
    embed = discord.Embed(
        title="⚡ Automatic verification" if request_type != "citizen" else "⚡ Automatische verificatie",
        description=description,
        color=discord.Color.blurple(),
    )
    await channel.send(embed=embed, view=AutoVerifyView())


async def _resolve_mofa_line(client, guild, config) -> str:
    """Build the "contact the MoFA" line for embassy tickets.

    Resolved live from whoever currently holds the *Minister van Buitenlandse
    Zaken* role, so the ticket never points at a former minister after a
    cabinet change.  For each holder we try to include their WarEra profile
    link, resolved in order of reliability:

      1. ``identity_links`` — the mapping written when they were verified.
      2. Their Discord display name matched against ``citizen_levels``
         (the bot keeps nicknames in sync with in-game usernames).

    Falls back to the ``users.mofa`` entry in config.json when the role has no
    members (or is missing), so this can never end up mentioning nobody.
    """
    role_id = (config.get("roles") or {}).get("minister_foreign_affairs")
    role = guild.get_role(int(role_id)) if role_id and guild else None
    holders = [m for m in role.members if not m.bot] if role else []

    role_mention = role.mention if role else "MoFA"

    if not holders:
        # Nobody currently holds the role — use the configured fallback.
        info = (config.get("users") or {}).get("mofa") or {}
        did, gid = info.get("discord_id"), info.get("in_game_id")
        mention = f"<@{did}>" if did else role_mention
        line = f"Hello, please send a message with your Discord name to our MoFA {mention} on WarEra "
        line += "to complete your verification request."
        if gid:
            line += f"\nLink: https://app.warera.io/user/{gid}"
        return line

    db = None
    try:
        db = await _get_shared_db(client)
    except Exception:
        logger.warning("MoFA lookup: shared DB unavailable", exc_info=True)

    async def _warera_id(member) -> str | None:
        if not db:
            return None
        try:
            link = await db.get_identity_link_by_discord(str(member.id))
            if link and link.get("in_game_user_id"):
                return str(link["in_game_user_id"])
        except Exception:
            logger.debug("MoFA lookup: identity_links failed for %s", member.id)
        # Fall back to matching their display name against known citizens.
        for name in filter(None, (member.nick, member.display_name, member.name)):
            try:
                matches = await db.search_citizen_names(name, limit=5)
            except Exception:
                break
            for cname, uid in matches:
                if cname.lower() == name.lower():
                    return str(uid)
        return None

    mentions = " ".join(m.mention for m in holders)
    line = (
        f"Hello, please send a message with your Discord name to our MoFA "
        f"{role_mention} {mentions} on WarEra to complete your verification request."
    )
    for member in holders:
        gid = await _warera_id(member)
        if gid:
            label = member.display_name
            line += f"\nLink ({label}): https://app.warera.io/user/{gid}"
    return line


async def create_verification_channel(
    interaction: discord.Interaction,
    request_type: str,
    questionnaire_answers: dict[str, str] | None = None,
    profile_input: str | None = None,
    embassy_country: str | None = None,
) -> None:
    """
    Create a private verification ticket channel for the user.

    Args:
        interaction: The button interaction from the user
        request_type: One of "citizen", "foreigner", or "embassy"

    The channel is only visible to:
    - The requesting user
    - The bot itself
    - The relevant moderator roles (Border Control or Embassy handlers)
    """
    user = interaction.user
    guild = interaction.guild
    config = getattr(interaction.client, "config", {}) or {}
    logger.info(
        "Creating verification channel for %s (%s) in guild %s",
        user.name,
        request_type,
        guild.name,
    )

    # Defer immediately — channel creation + message sends can exceed Discord's 3-second
    # interaction response deadline, which shows "something went wrong" to the user.
    await interaction.response.defer(ephemeral=True)

    # check if the user already has the requested role to prevent duplicate requests
    role_id = None
    if request_type == "citizen":
        role_id = config.get("roles", {}).get("nederlander")
    elif request_type == "belgian":
        role_id = config.get("roles", {}).get("belgian")
    elif request_type == "foreigner":
        role_id = config.get("roles", {}).get("foreigner")

    if role_id:
        role = guild.get_role(role_id)
        if role and role in user.roles:
            await interaction.followup.send(
                f"You already have the {role.name} role and cannot create a new request.",
                ephemeral=True,
            )
            return

    # Also check actual existing channels (covers bot restarts and manual channel cleanup)
    channels_cfg = config.get("channels", {})
    verification_cat_id = channels_cfg.get("verification")
    _raw_verification_ch = (
        guild.get_channel(verification_cat_id) if verification_cat_id else None
    )
    verification_category = (
        _raw_verification_ch
        if isinstance(_raw_verification_ch, discord.CategoryChannel)
        else None
    )
    if _raw_verification_ch and not verification_category:
        logger.warning(
            "channels.verification (%s) is not a CategoryChannel (got %s); "
            "falling back to scanning all guild text channels",
            verification_cat_id,
            type(_raw_verification_ch).__name__,
        )
    channels_to_check = (
        verification_category.channels if verification_category else guild.text_channels
    )

    username_slug = user.name.lower().replace(" ", "-")
    known_prefixes = ("citizen-", "belgian-", "foreigner-", "embassy-", "admin-")
    # Only block if there's already a ticket of the *same* type.
    type_prefix = f"{request_type}-"
    existing_channel = None
    for channel in channels_to_check:
        topic = channel.topic or ""
        name = channel.name.lower()
        if not name.startswith(type_prefix):
            continue
        # Prefer exact user-id match in topic; fallback to username pattern in channel name
        if f"User ID: {user.id}" in topic or (
            name.endswith(f"-{username_slug}") and name.startswith(known_prefixes)
        ):
            existing_channel = channel
            break

    if existing_channel:
        await interaction.followup.send(
            f"Je hebt al een open ticket: {existing_channel.mention}. "
            "Los dit eerst op voordat je een nieuw ticket aanmaakt.",
            ephemeral=True,
        )
        return

    # Generate unique ticket ID (stored in central config if present)
    ticket_id = None
    try:
        if "ticket_counter" in config:
            config["ticket_counter"] = int(config.get("ticket_counter", 0)) + 1
            ticket_id = config["ticket_counter"]
    except Exception:
        ticket_id = None

    if ticket_id is None:
        # fallback: use timestamp
        ticket_id = int(datetime.datetime.utcnow().timestamp())

    # Configure channel properties based on request type
    roles_cfg = config.get("roles", {})
    if request_type == "citizen":
        channel_name = f"citizen-{ticket_id}-{user.name}"
        role_ids = [roles_cfg.get("border_control")]
        embed_color = discord.Color.green()
        request_title = "Verificatieverzoek Nederlanderschap"
    elif request_type == "belgian":
        channel_name = f"belgian-{ticket_id}-{user.name}"
        role_ids = [roles_cfg.get("border_control")]
        embed_color = discord.Color.green()
        request_title = "Belgian Citizenship Verification Request"
    elif request_type == "foreigner":
        channel_name = f"foreigner-{ticket_id}-{user.name}"
        role_ids = [roles_cfg.get("border_control")]
        embed_color = discord.Color.blue()
        request_title = "Foreigner Verification Request"
    elif request_type == "admin_contact":
        channel_name = f"admin-{ticket_id}-{user.name}"
        role_ids = [roles_cfg.get("admin")]
        embed_color = discord.Color.og_blurple()
        request_title = "Admin Contact"
    else:  # embassy
        channel_name = f"embassy-{ticket_id}-{user.name}"
        # Embassy requests notify multiple high-level roles
        role_ids = [
            roles_cfg.get("minister_foreign_affairs"),
            roles_cfg.get("president"),
            roles_cfg.get("vice_president"),
        ]
        embed_color = discord.Color.red()
        request_title = "Emergency Embassy Request"

    # Sanitize channel name (Discord requires lowercase, no spaces, max 100 chars)
    channel_name = channel_name.lower().replace(" ", "-")[:100]

    # Get the category to create the channel in (if configured)
    # Re-use the already-resolved verification_category (guaranteed CategoryChannel or None)
    category = verification_category

    # Set up channel permissions
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            embed_links=True,
        ),
    }

    # Grant access to the relevant moderator roles
    for role_id in role_ids:
        if role_id:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    use_application_commands=True,
                )

    # Check if bot has permission to create channels in the category
    if category:
        bot_permissions = category.permissions_for(guild.me)
        if not bot_permissions.manage_channels:
            if request_type == "embassy":
                await interaction.followup.send(
                    f"I don't have permission to create channels"
                    f" in the **{category.name}** category.\n\n"
                    "**Solution:** Go to channel settings > Permissions > "
                    "Add the bot role with 'Manage Channels' enabled.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"Ik heb geen toestemming om kanalen aan te maken"
                    f" in de **{category.name}** categorie.\n\n"
                    "**Oplossing:** Ga naar kanaalinstellingen > Rechten > "
                    "Voeg de botrol toe met 'Kanalen beheren' ingeschakeld.",
                    ephemeral=True,
                )
            return

    # Create the ticket channel
    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=(
                f"Verification request by {user.name} | Type: {request_type}"
                f" | ID: {ticket_id} | User ID: {user.id}"
            ),
        )
    except discord.Forbidden as e:
        if request_type == "embassy":
            error_msg = (
                "I don't have permission to create channels.\n\n"
                "**Possible solutions:**\n"
                "• Make sure the bot has 'Manage Channels' permission server-wide\n"
            )
            if category:
                error_msg += (
                    f"• Add the bot to the **{category.name}** category "
                    "with 'Manage Channels' permission\n"
                )
            error_msg += f"\n**Error:** {e}"
        else:
            error_msg = (
                "Ik heb geen toestemming om kanalen aan te maken.\n\n"
                "**Mogelijke oplossingen:**\n"
                "• Zorg dat de bot 'Kanalen beheren' toestemming heeft op de hele server\n"
            )
            if category:
                error_msg += (
                    f"• Voeg de bot toe aan de **{category.name}** categorie "
                    "met 'Kanalen beheren' toestemming\n"
                )
            error_msg += f"\n**Fout:** {e}"
        await interaction.followup.send(error_msg, ephemeral=True)
        return

    # Log the ticket for /ticketstats reporting
    try:
        db = await _get_shared_db(interaction.client)
        await db.log_ticket_created(
            guild_id=str(guild.id),
            channel_id=str(channel.id),
            request_type=request_type,
            discord_user_id=str(user.id),
            created_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
    except Exception:
        logger.exception("Failed to log ticket creation for %s (%s)", user, request_type)

    # Build list of role mentions to ping
    role_mentions = []
    for role_id in role_ids:
        if role_id:
            role = guild.get_role(role_id)
            if role:
                role_mentions.append(role.mention)

    # Create the ticket embed with request details
    # Embassy tickets are staffed by (potentially non-Dutch-speaking) foreign
    # diplomats, so keep this one in English; the other request types keep
    # their existing Dutch labels.
    if request_type == "embassy":
        embed = discord.Embed(
            title=f"📋 {request_title}",
            description=(
                f"**User:** {user.mention}\n"
                f"**Type:** {request_type.title()}\n"
                f"**Ticket ID:** #{ticket_id}"
            ),
            color=embed_color,
            timestamp=datetime.datetime.now(datetime.UTC),
        )
    else:
        embed = discord.Embed(
            title=f"📋 {request_title}",
            description=(
                f"**Gebruiker:** {user.mention}\n"
                f"**Type:** {request_type.title()}\n"
                f"**Ticket ID:** #{ticket_id}"
            ),
            color=embed_color,
            timestamp=datetime.datetime.now(datetime.UTC),
        )
    embed.set_thumbnail(url=user.display_avatar.url)
    if request_type == "embassy":
        embed.add_field(
            name="Instructions for Moderators",
            value=(
                "Use `/embassyapprove` to approve this request\n"
                "Use `/deny` to reject this request"
            ),
            inline=False,
        )
    elif request_type != "admin_contact":
        embed.add_field(
            name="Instructies voor Moderators",
            value=(
                "Gebruik `/approve` om dit verzoek goed te keuren\n"
                "Gebruik `/deny` om dit verzoek af te wijzen"
            ),
            inline=False,
        )
    embed.set_footer(text=f"User ID: {user.id}")

    # Send the ticket message, pinging relevant moderators
    mention_text = " ".join(role_mentions) if role_mentions else ""
    await channel.send(content=mention_text, embed=embed)

    if questionnaire_answers:
        questionnaire_embed = discord.Embed(
            title="🧾 Submitted Questionnaire" if request_type == "embassy" else "🧾 Ingevulde Vragenlijst",
            color=embed_color,
            timestamp=datetime.datetime.now(datetime.UTC),
        )
        for label, value in questionnaire_answers.items():
            questionnaire_embed.add_field(
                name=label,
                value=value[:1024] if value else "-",
                inline=False,
            )
        await channel.send(embed=questionnaire_embed)

    if request_type == "citizen":
        instruction_text = (
            "Hallo! Je kan dit verzoek automatisch laten goedkeuren — zie het "
            "bericht hieronder. Lukt dat niet, stuur dan een screenshot van je "
            "WarEra profiel en wacht op een moderator."
        )
    elif request_type == "belgian":
        instruction_text = (
            "Hello! You can get this request approved automatically — see the "
            "message below. If that doesn't work, send a screenshot of your "
            "WarEra profile and wait for a moderator."
        )
    elif request_type == "admin_contact":
        instruction_text = (
            "Hallo! Een admin zal zo snel mogelijk reageren op je bericht. "
            "Je kunt hier alvast meer context geven als dat nodig is."
        )
    elif request_type == "embassy":
        instruction_text = await _resolve_mofa_line(
            interaction.client, guild, config
        )
    else:
        instruction_text = (
            "Hello, please send a screenshot of your WarEra profile "
            "to complete your verification request."
        )

    instructions_embed = discord.Embed(
        description=instruction_text,
        color=embed_color,
    )
    await channel.send(content=user.mention, embed=instructions_embed)

    # Kick off the automatic in-game ownership check for the request types
    # it applies to. If the profile input doesn't parse to an ID, or
    # anything else goes wrong here, the ticket just stays on the existing
    # manual flow above — this is a fast-path on top of it, not a
    # replacement.
    if request_type in {"citizen", "belgian", "embassy"} and profile_input:
        try:
            parsed_in_game_id = Welcome.normalize_ingame_id(profile_input)
        except ValueError:
            parsed_in_game_id = None
        if parsed_in_game_id:
            try:
                await _start_auto_verification(
                    interaction.client,
                    channel=channel,
                    request_type=request_type,
                    discord_user_id=user.id,
                    in_game_id=parsed_in_game_id,
                    country=embassy_country,
                )
            except Exception:
                logger.exception(
                    "Failed to start auto-verification for %s (%s)", user, request_type
                )

    # Confirm to the user (only they can see this response)
    if request_type == "admin_contact":
        await interaction.followup.send(
            f"Je bericht is verstuurd naar de admins: {channel.mention}\n"
            "Een admin zal zo snel mogelijk reageren.",
            ephemeral=True,
        )
    elif request_type == "citizen":
        await interaction.followup.send(
            f"Je verificatiekanaal is aangemaakt: {channel.mention}\n"
            "Wacht op een moderator om je verzoek te beoordelen.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"Your verification channel has been created: {channel.mention}\n"
            "Please wait for a moderator to review your request.",
            ephemeral=True,
        )


class Welcome(commands.Cog, name="welcome"):
    """Cog for welcome messages and verification system."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.bot.logger.info("Welcome cog initialized")
        # Add the persistent view when the cog is loaded
        self.bot.add_view(WelcomeView(bot))
        self.bot.add_view(AutoVerifyView())
        self.bot.add_view(EmbassyDecisionView())
        # Use the central bot configuration
        self.config = getattr(self.bot, "config", {}) or {}
        # Per-(guild, country) locks to prevent duplicate embassy channel creation
        self._embassy_locks: dict[str, asyncio.Lock] = {}
        self._approval_db = None

    def cog_load(self) -> None:
        """On (re)load, reschedule deletion for any approved ticket channels that survived a restart."""
        asyncio.create_task(self._reschedule_pending_deletions())

    async def _reschedule_pending_deletions(self) -> None:
        """Re-schedule deletion for approved ticket channels that survived a restart."""
        await self.bot.wait_until_ready()
        db = await self._get_approval_db()
        records = await db.get_pending_ticket_deletions()
        now = discord.utils.utcnow()
        for rec in records:
            guild = self.bot.get_guild(int(rec["guild_id"]))
            if guild is None:
                continue
            channel = guild.get_channel(int(rec["channel_id"]))
            if channel is None:
                # Channel already gone — clean up the DB record
                await db.remove_pending_ticket_deletion(rec["channel_id"])
                continue
            try:
                delete_at = datetime.datetime.fromisoformat(rec["delete_at"])
                if delete_at.tzinfo is None:
                    delete_at = delete_at.replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                delete_at = now  # malformed timestamp — delete immediately
            remaining = max(0.0, (delete_at - now).total_seconds())
            asyncio.create_task(
                self._delete_channel_after(
                    channel,
                    remaining,
                    "Ticket verlopen (bot herstart)",
                    db=db,
                )
            )
            self.bot.logger.info(
                "Welcome: rescheduled deletion for %s in %.0fs",
                channel.name,
                remaining,
            )

    async def _delete_channel_after(
        self,
        channel: discord.TextChannel,
        delay: float,
        reason: str,
        *,
        db=None,
    ) -> None:
        """Sleep for *delay* seconds then delete *channel*.

        Failures are logged rather than swallowed: a missing Manage Channels
        permission on one ticket category (embassy tickets live in a different
        category from verification tickets) otherwise looks exactly like the
        deletion never having been scheduled, with nothing in the logs to tell
        the two apart.
        """
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await channel.delete(reason=reason)
            self.bot.logger.info(
                "Welcome: deleted ticket channel %s (%s) — %s",
                channel.name, channel.id, reason,
            )
        except discord.NotFound:
            self.bot.logger.info(
                "Welcome: ticket channel %s already gone, nothing to delete",
                channel.id,
            )
        except discord.Forbidden:
            self.bot.logger.error(
                "Welcome: NOT allowed to delete ticket channel %s (%s) — "
                "the bot is missing Manage Channels on that category",
                channel.name, channel.id,
            )
        except discord.HTTPException as exc:
            self.bot.logger.error(
                "Welcome: failed to delete ticket channel %s (%s): %s",
                channel.name, channel.id, exc,
            )
        if db is not None:
            try:
                await db.remove_pending_ticket_deletion(str(channel.id))
            except Exception:
                self.bot.logger.warning(
                    "Welcome: could not clear pending deletion row for %s",
                    channel.id, exc_info=True,
                )
            try:
                await db.delete_pending_verification_code(str(channel.id))
            except Exception:
                pass

    @property
    def _client(self):
        return getattr(self.bot, "_ext_client", None)

    async def _get_approval_db(self):
        """Return shared external DB, closing any standalone fallback when services become ready."""
        shared = getattr(self.bot, "_ext_db", None)
        if shared is not None:
            if self._approval_db is not None:
                try:
                    await self._approval_db.close()
                except Exception:
                    pass
                self._approval_db = None
            return shared
        if self._approval_db is None:
            from services.db import Database
            db_path = self.config.get("external_db_path", "database/external.db")
            self._approval_db = Database(db_path)
            await self._approval_db.setup()
        return self._approval_db

    def _build_citizen_welcome_embed(
        self, member: discord.Member, *, referrer_hint: str = "2sa"
    ) -> discord.Embed:
        """Build the "welcome to NL" embed sent on citizen approval.

        Shared by /approve and the automatic bio-code fast-path, so both
        feel identical to the new citizen.
        """
        cfg_channels = self.bot.config.get("channels", {})
        handleiding_ch = cfg_channels.get("handleiding")
        roles_ch = cfg_channels.get("roles_claim")
        support_ch = cfg_channels.get("vragen")

        parts = [f"Welkom {member.mention} in WarEra Nederland!\n\n"]
        if handleiding_ch:
            parts.append(f"Om je op weg te helpen, bekijk onze <#{handleiding_ch}>")
        if roles_ch:
            parts.append(f" en claim je rollen in <#{roles_ch}>")
        if support_ch:
            parts.append(f". Voor vragen kun terecht in <#{support_ch}>")
        parts.append(".\n\nHier een paar kleine tips om je op weg te helpen:\n")
        tips = [
            (
                "Wij werken met ping-rollen in deze server, jij kan kiezen waarvoor je "
                "een ping (mededeling) wilt ontvangen. Die kan je kiezen in "
                + (f"<#{roles_ch}>." if roles_ch else "de rollenkanaal.")
            ),
            (
                "We hebben redelijk wat kanalen in deze server, via 'Browse Channels' "
                "bovenin de kanalenlijst kan je selectief kanalen aan-/uitzetten."
            ),
            (
                "In het kanaal " + (f"<#{handleiding_ch}>" if handleiding_ch else "beginner-handleiding")
                + " staat een link naar onze wikipedia, hier kan je ook een "
                "beginnershandleiding vinden."
            ),
            (
                "We hebben een hele behulpzame community: kom je ergens niet uit, of "
                "heb je vragen? Stel ze in "
                + (f"<#{support_ch}>" if support_ch else "vragen-en-antwoorden")
                + " of in de in-game chat."
            ),
        ]
        parts.append("\n".join(f"{i}. {tip}" for i, tip in enumerate(tips, 1)))
        parts.append(
            f"\n\nAls laatste: je kan op je profiel klikken op je profielfoto, dan op "
            f"**Profile**, en dan **Referrals** — daar kan je een referrer opgeven. Vul "
            f"hier het liefst een **Nederlander** in (bijvoorbeeld *{referrer_hint}*), "
            f"dan krijgen jij en de referrer muntjes."
        )

        welcome_embed = discord.Embed(
            title="Welkom Nederlander! 🇳🇱",
            description="".join(parts),
            color=discord.Color.gold(),
        )
        welcome_embed.set_thumbnail(url=member.display_avatar.url)
        welcome_embed.set_footer(text="Dit kanaal zal worden verwijderd over 8 uur.")
        return welcome_embed

    async def _store_identity_link(
        self,
        *,
        guild_id: str,
        member: discord.Member,
        in_game_id: str,
        request_type: str,
        nationality: str,
        approved_by_discord_id: str,
        embassy_country: str | None = None,
    ) -> None:
        """Persist Discord ↔ in-game identity mapping for approved users."""
        db = await self._get_approval_db()
        approved_at = datetime.datetime.now(datetime.UTC).isoformat()
        await db.upsert_identity_link(
            discord_user_id=str(member.id),
            guild_id=str(guild_id),
            in_game_user_id=in_game_id,
            nationality=nationality,
            request_type=request_type,
            embassy_country=embassy_country,
            approved_by_discord_id=str(approved_by_discord_id),
            approved_at=approved_at,
        )

    async def _validate_identity_link_target(
        self,
        *,
        guild_id: str,
        member: discord.Member,
        in_game_id: str,
    ) -> None:
        """Ensure new mapping does not conflict with existing records."""
        db = await self._get_approval_db()
        guild_id = str(guild_id)
        discord_id = str(member.id)

        existing_for_discord = await db.get_identity_link_by_discord(
            discord_user_id=discord_id,
            guild_id=guild_id,
        )
        if (
            existing_for_discord
            and existing_for_discord.get("in_game_user_id") != in_game_id
        ):
            raise ValueError(
                "Deze Discord gebruiker is al gekoppeld aan een ander in-game ID. "
                "Werk de mapping eerst handmatig bij om fouten te voorkomen."
            )

        existing_for_ingame = await db.get_identity_links_by_ingame(
            in_game_user_id=in_game_id,
            guild_id=guild_id,
        )
        conflicting_discord = next(
            (
                link.get("discord_user_id")
                for link in existing_for_ingame
                if link.get("discord_user_id") != discord_id
            ),
            None,
        )
        if conflicting_discord:
            raise ValueError(
                "Dit in-game ID is al gekoppeld aan een andere Discord gebruiker: "
                f"<@{conflicting_discord}> (`{conflicting_discord}`)."
            )

    async def _fetch_full_warera_user(self, in_game_id: str) -> dict | None:
        """Fetch the full ``user.getUserById`` payload for an in-game ID.

        Returns None on any failure (bad ID, API down) — every caller treats
        "could not verify right now" as a soft failure, never raises.
        """
        client = self._client
        if client is None:
            return None
        try:
            raw = await client.get(
                "/user.getUserById",
                params={"input": json.dumps({"userId": in_game_id})},
            )
        except Exception:
            return None
        try:
            data = raw["result"]["data"]
        except (KeyError, TypeError):
            data = raw
        return data if isinstance(data, dict) else None

    async def _resolve_country(self, raw_name: str) -> tuple[str, str] | None:
        """Resolve a country's (id, canonical display name) from an English
        name or a known Dutch alias, case-insensitive. Returns None if it
        doesn't match any known country.
        """
        db = await self._get_approval_db()
        try:
            name_map = await db.get_country_name_map()
        except Exception:
            return None
        target = raw_name.strip().lower()
        target = _COUNTRY_ALIASES.get(target, target)
        for country_id, name in name_map.items():
            if name.strip().lower() == target:
                return country_id, name
        return None

    async def _resolve_country_id_by_name(self, country_name: str) -> str | None:
        """Resolve a country's WarEra ID from its (English) name, case-insensitive."""
        result = await self._resolve_country(country_name)
        return result[0] if result else None

    async def _lookup_government_title(
        self, country_id: str, in_game_user_id: str
    ) -> str | None:
        """Return a Dutch display label if *in_game_user_id* holds a government
        seat in *country_id*, else None. Best-effort — never raises.
        """
        client = self._client
        if not client or not country_id:
            return None
        try:
            raw = await client.get(
                "/government.getByCountryId",
                params={"input": json.dumps({"countryId": country_id})},
            )
        except Exception:
            return None
        try:
            data = raw["result"]["data"]
        except (KeyError, TypeError):
            data = raw
        if not isinstance(data, dict):
            return None

        if data.get("president") == in_game_user_id:
            return "President"
        if data.get("vicePresident") == in_game_user_id:
            return "Vice-President"

        ministers = data.get("ministers")
        if isinstance(ministers, list):
            for entry in ministers:
                if not isinstance(entry, dict) or entry.get("user") != in_game_user_id:
                    continue
                title = entry.get("title")
                if title == "advisor":
                    custom = entry.get("customTitle")
                    return f"Adviseur ({custom})" if custom else "Adviseur"
                return _GOV_TITLE_DISPLAY.get(title, str(title) if title else "Onbekende functie")
        return None

    def _schedule_ticket_deletion(
        self,
        channel: discord.TextChannel,
        guild_id: int,
        reason: str,
        delay: int = 8 * 3600,
    ) -> None:
        """Delete *channel* after *delay* seconds; persists the schedule first
        so a bot restart in the meantime can resume it (see
        ``_reschedule_pending_deletions``).
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        delete_at = now + datetime.timedelta(seconds=delay)

        async def _persist_and_delete() -> None:
            db = None
            try:
                db = await self._get_approval_db()
                await db.add_pending_ticket_deletion(
                    channel_id=str(channel.id),
                    guild_id=str(guild_id),
                    approved_at=now.isoformat(),
                    delete_at=delete_at.isoformat(),
                )
            except Exception as e:
                self.bot.logger.warning(
                    "Could not record pending ticket deletion for %s: %s", channel.id, e
                )
            await self._delete_channel_after(channel, delay, reason, db=db)

        asyncio.create_task(_persist_and_delete())

    async def _finalize_citizen_approval(
        self,
        *,
        guild: discord.Guild,
        channel: discord.TextChannel,
        member: discord.Member,
        in_game_id: str,
        request_type: str,  # "citizen" | "belgian"
        approver_label: str,
        approver_avatar_url: str | None,
        reason: str,
    ) -> dict:
        """Grant the nationality role, store the identity link, notify the
        ticket + log channel, and schedule ticket deletion.

        Mirrors what ``/approve`` does for citizen/belgian tickets, so the
        automatic bio-code fast-path produces an identical end result. Does
        NOT raise — returns ``{"ok": bool, "error": str | None}``.
        """
        is_nl = request_type == "citizen"
        role_key = {"citizen": "nederlander", "belgian": "belgian"}.get(request_type)
        role_to_give = guild.get_role(self.config["roles"][role_key]) if role_key else None

        if role_to_give:
            try:
                await member.add_roles(role_to_give)
            except (discord.Forbidden, discord.HTTPException) as e:
                error = (
                    f"Kon de {role_to_give.name} rol niet toekennen: {e}"
                    if is_nl else
                    f"Could not assign the {role_to_give.name} role: {e}"
                )
                return {"ok": False, "error": error}

        old_role = guild.get_role(self.config["roles"]["bezoeker"])
        if old_role:
            try:
                await member.remove_roles(old_role)
            except (discord.Forbidden, discord.HTTPException) as e:
                self.bot.logger.error(
                    "auto-verify: could not remove %s from %s: %s",
                    old_role.name, member.name, e,
                )

        db_saved = True
        try:
            await self._store_identity_link(
                guild_id=str(guild.id),
                member=member,
                in_game_id=in_game_id,
                request_type=request_type,
                nationality=role_key or request_type,
                approved_by_discord_id=str(self.bot.user.id) if self.bot.user else "0",
            )
        except Exception as e:
            db_saved = False
            self.bot.logger.error("Failed to persist identity link in auto-verify: %s", e)

        if is_nl:
            welcome_embed = self._build_citizen_welcome_embed(member)
            await channel.send(content=member.mention, embed=welcome_embed)
        else:
            user_embed = discord.Embed(
                title="✅ Request Approved!",
                description=f"Your {request_type} verification request has been approved automatically!",
                color=discord.Color.green(),
            )
            if role_to_give:
                user_embed.add_field(
                    name="Role Granted", value=role_to_give.mention, inline=False
                )
            user_embed.set_footer(text="This channel will be deleted in 8 hours.")
            await channel.send(content=member.mention, embed=user_embed)

        log_channel_id = self.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    log_embed = discord.Embed(
                        title="✅ Verificatie Automatisch Goedgekeurd",
                        description=(
                            f"**Gebruiker:** {member.mention} ({member.name})\n"
                            f"**Type:** {request_type.title()}\n"
                            f"**Reden:** {reason}"
                        ),
                        color=discord.Color.green(),
                        timestamp=datetime.datetime.now(datetime.UTC),
                    )
                    log_embed.set_thumbnail(url=member.display_avatar.url)
                    if approver_avatar_url:
                        log_embed.set_footer(
                            text=f"Goedgekeurd door {approver_label}",
                            icon_url=approver_avatar_url,
                        )
                    else:
                        log_embed.set_footer(text=f"Goedgekeurd door {approver_label}")
                    if role_to_give:
                        log_embed.add_field(
                            name="Rol Toegewezen", value=role_to_give.mention, inline=True
                        )
                    if not db_saved:
                        log_embed.add_field(
                            name="⚠️ Database",
                            value="Identity mapping kon niet worden opgeslagen.",
                            inline=False,
                        )
                    await log_channel.send(embed=log_embed)
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.bot.logger.error(
                        "Failed to post to log channel (auto-verify): %s", e
                    )

        self._schedule_ticket_deletion(channel, guild.id, reason)
        return {"ok": True, "error": None}

    async def _finalize_embassy_approval(
        self,
        *,
        guild: discord.Guild,
        channel: discord.TextChannel,
        member: discord.Member,
        in_game_id: str,
        country: str,
        approver_label: str,
        approver_avatar_url: str | None,
    ) -> dict:
        """Grant the foreign-diplomat role, create/grant the country's embassy
        channel, store the identity link, and schedule ticket deletion.

        Mirrors what ``/embassyapprove`` does, used by the "Toevoegen aan
        ambassade" button on auto-verified embassy tickets. Does NOT raise —
        returns ``{"ok": bool, "error": str | None}``.
        """
        minister_role = guild.get_role(self.config["roles"]["government"])
        president_role = guild.get_role(self.config["roles"]["president"])
        vice_president_role = guild.get_role(self.config["roles"]["vice_president"])
        if any(r is None for r in (minister_role, president_role, vice_president_role)):
            return {
                "ok": False,
                "error": "Government/president/vice-president roles are not configured correctly.",
            }

        embassy_role_id = self.config.get("roles", {}).get("buitenlandse_diplomaat")
        embassy_role = guild.get_role(embassy_role_id) if embassy_role_id else None
        if not embassy_role:
            return {"ok": False, "error": "Embassy role not found in the server configuration."}

        try:
            await member.add_roles(embassy_role)
        except (discord.Forbidden, discord.HTTPException) as e:
            return {"ok": False, "error": f"Could not assign the {embassy_role.name} role: {e}"}

        old_role = guild.get_role(self.config["roles"]["bezoeker"])
        if old_role:
            try:
                await member.remove_roles(old_role)
            except (discord.Forbidden, discord.HTTPException) as e:
                self.bot.logger.error(
                    "Could not remove bezoeker role from %s: %s", member.name, e
                )

        lock_key = f"{guild.id}:{country.lower()}"
        async with self._embassy_locks.setdefault(lock_key, asyncio.Lock()):
            embassy_channel = self._find_existing_embassy_channel(guild, country)
            if not embassy_channel:
                channel_name = next(
                    name
                    for name in self._embassy_channel_candidates(country)
                    if name.endswith("-embassy")
                )
                category = None
                embassy_categories = self._resolve_embassy_categories(guild)
                if embassy_categories:
                    target_index = self._embassy_category_bucket_index(country)
                    category = embassy_categories[
                        min(target_index, len(embassy_categories) - 1)
                    ]
                if category is None:
                    verification_cat_id = self.config.get("channels", {}).get("verification")
                    if verification_cat_id:
                        candidate = guild.get_channel(verification_cat_id)
                        if isinstance(candidate, discord.CategoryChannel):
                            category = candidate

                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    minister_role: discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True
                    ),
                    president_role: discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True
                    ),
                    vice_president_role: discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True
                    ),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, manage_channels=True,
                        manage_messages=True, embed_links=True,
                    ),
                }
                try:
                    embassy_channel = await guild.create_text_channel(
                        name=channel_name,
                        category=category,
                        overwrites=overwrites,
                        topic=f"Embassy channel for {country}",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    return {"ok": False, "error": f"Could not create the embassy channel: {e}"}

        try:
            await embassy_channel.set_permissions(
                member, view_channel=True, send_messages=True, read_message_history=True,
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            self.bot.logger.error(
                "Could not grant %s access to %s: %s", member.name, embassy_channel.name, e
            )

        db_saved = True
        try:
            await self._store_identity_link(
                guild_id=str(guild.id),
                member=member,
                in_game_id=in_game_id,
                request_type="embassy",
                nationality=country.strip().lower(),
                approved_by_discord_id=str(self.bot.user.id) if self.bot.user else "0",
                embassy_country=country.strip(),
            )
        except Exception as e:
            db_saved = False
            self.bot.logger.error(
                "Failed to persist identity link in embassy auto-approve: %s", e
            )

        confirmation_embed = discord.Embed(title=f"Welcome to {country.title()} Embassy! 🇳🇱")
        await embassy_channel.send(
            content=f"{member.mention} {minister_role.mention}", embed=confirmation_embed
        )

        approved_embed = discord.Embed(
            title="✅ Embassy Request Approved",
            description=f"You now have access to {embassy_channel.mention}.",
            color=discord.Color.green(),
        )
        approved_embed.set_footer(text="This ticket channel will be deleted in 8 hours.")
        await channel.send(content=member.mention, embed=approved_embed)

        log_channel_id = self.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    log_embed = discord.Embed(
                        title="✅ Embassy Request Approved",
                        description=(
                            f"**User:** {member.mention} ({member.name})\n"
                            f"**Country:** {country.title()}\n"
                        ),
                        color=discord.Color.green(),
                        timestamp=datetime.datetime.now(datetime.UTC),
                    )
                    log_embed.set_thumbnail(url=member.display_avatar.url)
                    if approver_avatar_url:
                        log_embed.set_footer(
                            text=f"Approved by {approver_label}", icon_url=approver_avatar_url
                        )
                    else:
                        log_embed.set_footer(text=f"Approved by {approver_label}")
                    if not db_saved:
                        log_embed.add_field(
                            name="⚠️ Database",
                            value="Identity mapping kon niet worden opgeslagen.",
                            inline=False,
                        )
                    await log_channel.send(embed=log_embed)
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.bot.logger.error(
                        "Failed to post to log channel (embassy auto-approve): %s", e
                    )

        self._schedule_ticket_deletion(
            channel, guild.id, f"Embassy request approved by {approver_label}"
        )
        return {"ok": True, "error": None}

    async def handle_auto_verify_click(self, interaction: discord.Interaction) -> None:
        """Handle a click on AutoVerifyView's "Ik heb mijn bio aangepast" button.

        Checks the claimed WarEra account's bio against the code issued for
        this ticket. On match: citizen/belgian tickets are approved
        immediately (same result as /approve); embassy tickets instead post
        a verified-identity + government-role summary with Approve/Deny
        buttons for a moderator (see handle_embassy_decision_click).
        """
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        guild = interaction.guild
        db = await self._get_approval_db()

        row = await db.get_pending_verification_code(str(channel.id))
        if not row:
            await interaction.followup.send(
                "No pending automatic verification request found in this channel.",
                ephemeral=True,
            )
            return

        request_type = row["request_type"]
        is_nl = request_type == "citizen"

        try:
            expires_at = datetime.datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            expires_at = datetime.datetime.now(datetime.timezone.utc)
        if datetime.datetime.now(datetime.timezone.utc) > expires_at:
            await db.delete_pending_verification_code(str(channel.id))
            msg = (
                "⏰ Deze verificatiecode is verlopen. Vraag een moderator om je "
                "verzoek handmatig te controleren."
                if is_nl else
                "⏰ This verification code has expired. Ask a moderator to review "
                "your request manually."
            )
            await interaction.followup.send(msg, ephemeral=True)
            return

        attempts = await db.increment_verification_attempts(str(channel.id))
        if attempts > 20:
            msg = (
                "Te veel pogingen. Wacht even of vraag een moderator om hulp."
                if is_nl else
                "Too many attempts. Wait a moment or ask a moderator for help."
            )
            await interaction.followup.send(msg, ephemeral=True)
            return

        in_game_id = row["in_game_user_id"]
        user_data = await self._fetch_full_warera_user(in_game_id)
        if not user_data:
            msg = (
                "⚠️ Kon je WarEra-profiel niet ophalen. Controleer of het ingevoerde "
                "ID/profiel klopt en probeer het opnieuw."
                if is_nl else
                "⚠️ Could not fetch your WarEra profile. Check that the ID/profile "
                "you entered is correct and try again."
            )
            await interaction.followup.send(msg, ephemeral=True)
            return

        current_bio = str((user_data.get("infos") or {}).get("description") or "").strip()
        if current_bio.lower() != str(row["code"]).strip().lower():
            if is_nl:
                msg = (
                    "❌ Code niet gevonden in je bio.\n"
                    f"Verwachte code: `{row['code']}`\n"
                    f"Huidige bio: `{current_bio or '(leeg)'}`\n"
                    "Zorg dat je bio **exact** de code bevat en probeer het opnieuw."
                )
            else:
                msg = (
                    "❌ Code not found in your bio.\n"
                    f"Expected code: `{row['code']}`\n"
                    f"Current bio: `{current_bio or '(empty)'}`\n"
                    "Make sure your bio contains **exactly** the code and try again."
                )
            await interaction.followup.send(msg, ephemeral=True)
            return

        member = guild.get_member(int(row["discord_user_id"]))
        if not member:
            await db.delete_pending_verification_code(str(channel.id))
            msg = "De gebruiker is niet meer op de server." if is_nl else "The user is no longer on the server."
            await interaction.followup.send(msg, ephemeral=True)
            return

        username = user_data.get("username") or member.display_name

        if request_type in {"citizen", "belgian"}:
            target_name = "Netherlands" if is_nl else "Belgium"
            if is_nl:
                target_country_id = self.config.get("nl_country_id")
            else:
                target_country_id = await self._resolve_country_id_by_name(target_name)

            actual_country_id = user_data.get("country")
            if not target_country_id or actual_country_id != target_country_id:
                name_map = {}
                try:
                    name_map = await db.get_country_name_map()
                except Exception:
                    pass
                actual_name = name_map.get(
                    actual_country_id, actual_country_id or ("onbekend" if is_nl else "unknown")
                )
                await db.delete_pending_verification_code(str(channel.id))
                if is_nl:
                    mismatch_embed = discord.Embed(
                        title="⚠️ Eigendom bevestigd, land komt niet overeen",
                        description=(
                            f"{member.mention} heeft bewezen **{username}** te zijn, maar dit "
                            f"account staat geregistreerd in **{actual_name}**, niet "
                            f"{target_name}.\n\nEen moderator moet dit handmatig beoordelen "
                            "met `/approve`."
                        ),
                        color=discord.Color.orange(),
                    )
                    followup_msg = (
                        "✅ Eigendom bevestigd, maar het land komt niet overeen — zie het "
                        "bericht in het kanaal. Een moderator zal je verzoek beoordelen."
                    )
                else:
                    mismatch_embed = discord.Embed(
                        title="⚠️ Ownership confirmed, country doesn't match",
                        description=(
                            f"{member.mention} has proven to be **{username}**, but this "
                            f"account is registered in **{actual_name}**, not "
                            f"{target_name}.\n\nA moderator needs to review this manually "
                            "with `/approve`."
                        ),
                        color=discord.Color.orange(),
                    )
                    followup_msg = (
                        "✅ Ownership confirmed, but the country doesn't match — see the "
                        "message in the channel. A moderator will review your request."
                    )
                await channel.send(embed=mismatch_embed)
                await interaction.followup.send(followup_msg, ephemeral=True)
                return

            try:
                await self._validate_identity_link_target(
                    guild_id=str(guild.id), member=member, in_game_id=in_game_id
                )
            except ValueError as e:
                await db.delete_pending_verification_code(str(channel.id))
                await interaction.followup.send(str(e), ephemeral=True)
                return

            try:
                await member.edit(nick=username)
            except (discord.Forbidden, discord.HTTPException) as e:
                self.bot.logger.warning(
                    "auto-verify: could not set nickname for %s: %s", member, e
                )

            await db.delete_pending_verification_code(str(channel.id))
            result = await self._finalize_citizen_approval(
                guild=guild,
                channel=channel,
                member=member,
                in_game_id=in_game_id,
                request_type=request_type,
                approver_label="🤖 Automatische verificatie (in-game bio-code)",
                approver_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
                reason="Automatische in-game verificatie (bio-code match)",
            )
            if result["ok"]:
                msg = (
                    "✅ Geverifieerd! Je verzoek is automatisch goedgekeurd."
                    if is_nl else
                    "✅ Verified! Your request has been approved automatically."
                )
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.followup.send(f"⚠️ {result['error']}", ephemeral=True)
            return

        # Embassy: ownership confirmed, but never auto-approved — show the
        # claimed country's government role (if any) and hand off to a
        # moderator via the approve/deny buttons. The pending-code row is
        # deliberately kept (not deleted) so handle_embassy_decision_click
        # can still read in_game_id/country once a moderator clicks. The
        # claimed country was already validated/canonicalized against our
        # country list when the ticket was created (see
        # VerificationQuestionnaireModal.on_submit), so this always
        # resolves cleanly.
        country = row.get("country") or "Unknown"
        gov_country_id = await self._resolve_country_id_by_name(country)
        gov_title = None
        if gov_country_id:
            gov_title = await self._lookup_government_title(gov_country_id, in_game_id)

        if gov_country_id is None:
            gov_line = "Country not found in our database."
        elif gov_title:
            gov_line = f"**{gov_title}** in {country}"
        else:
            gov_line = f"No government role found in {country}."

        verified_embed = discord.Embed(
            title="✅ In-game identity verified",
            description=(
                f"{member.mention} is verified as **{username}** "
                f"([profile](https://app.warera.io/user/{in_game_id})).\n\n"
                f"**Claimed country:** {country}\n"
                f"**Government role:** {gov_line}"
            ),
            color=discord.Color.blurple(),
        )
        await channel.send(embed=verified_embed, view=EmbassyDecisionView())
        await interaction.followup.send(
            "✅ You're verified! A moderator will now review your request.",
            ephemeral=True,
        )

    async def handle_embassy_decision_click(
        self, interaction: discord.Interaction, *, approve: bool
    ) -> None:
        """Handle a click on EmbassyDecisionView's approve/deny button."""
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        guild = interaction.guild

        if approve:
            mod_roles = [
                self.config["roles"]["government"],
                self.config["roles"]["president"],
                self.config["roles"]["vice_president"],
            ]
        else:
            mod_roles = [
                self.config["roles"]["border_control"],
                self.config["roles"]["minister_foreign_affairs"],
                self.config["roles"]["president"],
                self.config["roles"]["vice_president"],
            ]
        user_role_ids = [r.id for r in interaction.user.roles]
        has_permission = any(r for r in mod_roles if r in user_role_ids)
        if not has_permission and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(
                "You don't have permission to use this.", ephemeral=True
            )
            return

        db = await self._get_approval_db()
        row = await db.get_pending_verification_code(str(channel.id))
        if not row or row.get("request_type") != "embassy":
            await interaction.followup.send(
                "Could not find the matching verification data anymore. Use "
                "`/embassyapprove` or `/deny` manually.",
                ephemeral=True,
            )
            return

        member = guild.get_member(int(row["discord_user_id"]))
        if not member:
            await db.delete_pending_verification_code(str(channel.id))
            await interaction.followup.send(
                "The user is no longer on the server.", ephemeral=True
            )
            return

        try:
            if interaction.message:
                await interaction.message.edit(view=None)
        except (discord.Forbidden, discord.HTTPException):
            pass

        if approve:
            try:
                await self._validate_identity_link_target(
                    guild_id=str(guild.id),
                    member=member,
                    in_game_id=row["in_game_user_id"],
                )
            except ValueError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return

            result = await self._finalize_embassy_approval(
                guild=guild,
                channel=channel,
                member=member,
                in_game_id=row["in_game_user_id"],
                country=row.get("country") or "Unknown",
                approver_label=interaction.user.name,
                approver_avatar_url=interaction.user.display_avatar.url,
            )
            await db.delete_pending_verification_code(str(channel.id))
            if result["ok"]:
                await interaction.followup.send("✅ Approved.", ephemeral=True)
            else:
                await interaction.followup.send(f"⚠️ {result['error']}", ephemeral=True)
            return

        # Deny
        user_embed = discord.Embed(
            title="❌ Request Denied",
            description="Your embassy verification request has been denied.",
            color=discord.Color.red(),
        )
        user_embed.set_footer(text="This channel will be deleted in 8 hours.")
        await channel.send(content=member.mention, embed=user_embed)

        log_channel_id = self.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    log_embed = discord.Embed(
                        title="❌ Embassy Request Denied",
                        description=f"**User:** {member.mention} ({member.name})\n",
                        color=discord.Color.red(),
                        timestamp=datetime.datetime.now(datetime.UTC),
                    )
                    log_embed.set_footer(
                        text=f"Denied by {interaction.user.name}",
                        icon_url=interaction.user.display_avatar.url,
                    )
                    await log_channel.send(embed=log_embed)
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.bot.logger.error(
                        "Failed to post to log channel (embassy deny button): %s", e
                    )

        await db.delete_pending_verification_code(str(channel.id))
        self._schedule_ticket_deletion(
            channel, guild.id, f"Embassy request denied by {interaction.user.name}"
        )
        await interaction.followup.send("Request denied.", ephemeral=True)

    def _resolve_embassy_categories(
        self, guild: discord.Guild
    ) -> list[discord.CategoryChannel]:
        """Resolve configured embassy categories in priority order."""
        channels_cfg = self.bot.config.get("channels", {}) if self.bot.config else {}

        raw_ids: list[int] = []

        # Prefer explicit ordered list when available.
        explicit_list = channels_cfg.get("embassy_categories")
        if isinstance(explicit_list, list) and explicit_list:
            raw_ids.extend(explicit_list)
        else:
            # Backward-compatible single key.
            primary = channels_cfg.get("embassy_category")
            if primary:
                raw_ids.append(primary)

            # Support numbered keys such as embassy_category_2, embassy_category_3, etc.
            numbered: list[tuple[int, int]] = []
            for key, value in channels_cfg.items():
                if not key.startswith("embassy_category_"):
                    continue
                suffix = key.removeprefix("embassy_category_")
                try:
                    order = int(suffix)
                    category_id = int(value)
                except (TypeError, ValueError):
                    continue
                numbered.append((order, category_id))
            numbered.sort(key=lambda item: item[0])
            raw_ids.extend(category_id for _order, category_id in numbered)

        categories: list[discord.CategoryChannel] = []
        seen: set[int] = set()
        for raw_id in raw_ids:
            try:
                category_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if category_id in seen or category_id <= 0:
                continue
            seen.add(category_id)

            category = guild.get_channel(category_id)
            if isinstance(category, discord.CategoryChannel):
                categories.append(category)

        return categories

    @staticmethod
    def _embassy_category_bucket_index(country: str) -> int:
        """Map country to one of three buckets: A-I, J-R, S-Z."""
        match = re.search(r"[a-z]", country.lower())
        if not match:
            return 2
        first = match.group(0)
        if first <= "i":
            return 0
        if first <= "r":
            return 1
        return 2

    @staticmethod
    def normalize_ingame_id(in_game_id: str) -> str:
        """Normalize and validate in-game ID or WarEra profile URL input."""
        raw_value = str(in_game_id).strip()
        if not raw_value:
            raise ValueError("In-game ID cannot be empty.")

        # Accept direct profile links like: https://app.warera.io/user/{id}
        match = re.match(
            r"^https?://app\.warera\.io/user/([^/?#]+)(?:[/?#].*)?$",
            raw_value,
            flags=re.IGNORECASE,
        )
        if match:
            normalized = match.group(1).strip()
        else:
            normalized = raw_value
            if "://" in raw_value:
                raise ValueError(
                    "Invalid WarEra profile URL. "
                    "Use `https://app.warera.io/user/{id}` or provide the raw in-game ID."
                )

        if not normalized:
            raise ValueError("Could not extract an in-game ID from the provided input.")
        if len(normalized) > 64:
            raise ValueError("In-game ID is too long (max 64 characters).")
        return normalized

    @staticmethod
    def _embassy_channel_candidates(country: str) -> set[str]:
        """Return possible embassy channel names for a country."""
        slug = re.sub(r"[^a-z0-9-]+", "-", country.lower().strip())
        slug = re.sub(r"-+", "-", slug).strip("-")
        return {f"{slug}-embassy", f"{slug}-ambassade"}

    def _find_existing_embassy_channel(
        self, guild: discord.Guild, country: str
    ) -> discord.TextChannel | None:
        """Find an existing embassy channel across configured embassy categories."""
        candidate_names = self._embassy_channel_candidates(country)

        # First search configured embassy categories in priority order.
        for category in self._resolve_embassy_categories(guild):
            for text_channel in category.text_channels:
                if text_channel.name in candidate_names:
                    return text_channel

        # Backward-compatible fallback: search globally visible text channels.
        for text_channel in guild.text_channels:
            if text_channel.name in candidate_names:
                return text_channel

        return None

    # def cog_load(self) -> None:
    #     """Start the scheduled tasks when the cog is loaded."""
    #     self.daily_bezoeker_ping.start()

    # def cog_unload(self) -> None:
    #     """Cancel scheduled tasks when the cog is unloaded."""
    #     self.daily_bezoeker_ping.cancel()

    @commands.command(
        name="postwelcome",
        description="Post the welcome message with verification buttons (admin only)",
    )
    @commands.has_permissions(administrator=True)
    async def post_welcome(self, ctx: commands.Context):
        # Create the welcome embed
        embed = discord.Embed(
            title="🇳🇱 Welcome to Nederland!",
            description=self.config.get("welcome_message", "Welcome!"),
            color=discord.Color.gold(),
            timestamp=datetime.datetime.now(datetime.UTC),
        )
        embed.set_thumbnail(
            url="https://3.bp.blogspot.com/-x8PxTZ-frT8/VzhaiN0qnTI/AAAAAAAAskA/BFXeRJND8YU3oUxBBqq6Ny9ITeWpq5BuACKgB/s1600/NEDERLAND.%2BWAPEN%2B%25281%2529.png"
        )
        # embed.set_author(name=member.name, icon_url=member.display_avatar.url)
        # embed.set_footer(text=f"Member #{self.bot.guild.member_count}")

        # Send welcome message with verification buttons
        channel_id = self.bot.config.get("channels", {}).get("welcome_buttons")
        if not channel_id:
            await ctx.send("Welcome channel ID not configured in bot config.")
            return

        channel = ctx.guild.get_channel(channel_id)
        if not channel:
            await ctx.send(
                "Welcome channel not found. Please check the channel ID in bot config."
            )
            return

        await channel.send(embed=embed, view=WelcomeView(self.bot))

    # @tasks.loop(time=datetime.time(19, 0))  # Runs daily at 19:00
    # async def daily_bezoeker_ping(self):
    #     """Send a daily ping to the bezoeker role in the welcome channel."""
    #     try:
    #         # Get the welcome channel id from bot config
    #         welcome_channel_id = self.bot.config.get("channels", {}).get("welcome_buttons")
    #         if not welcome_channel_id:
    #             self.bot.logger.warning("Welcome channel ID not configured")
    #             return

    #         # Find the welcome channel across all guilds the bot is in
    #         for guild in self.bot.guilds:
    #             channel = guild.get_channel(welcome_channel_id)
    #             if channel:
    #                 # Get the bezoeker role
    #                 bezoeker_role_id = self.bot.config.get("roles", {}).get("bezoeker")
    #                 if not bezoeker_role_id:
    #                     self.bot.logger.warning("Bezoeker role ID not configured")
    #                     return

    #                 role = guild.get_role(bezoeker_role_id)
    #                 if not role:
    #                     self.bot.logger.warning(f"Bezoeker role not found in guild {guild.name}")
    #                     return

    #                 # Send the ping
    #                 await channel.send(f"{role.mention} please use one of the above buttons to claim your role.")
    #                 self.bot.logger.info(f"Sent daily bezoeker ping in {guild.name}")
    #                 return

    #         self.bot.logger.warning(f"Welcome channel {welcome_channel_id} not found in any guild")
    #     except Exception as e:
    #         self.bot.logger.error(f"Error sending daily bezoeker ping: {e}")

    # @daily_bezoeker_ping.before_loop
    # async def before_daily_ping(self):
    #     """Ensure the bot is ready before starting the scheduled task."""
    #     await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """
        Send a welcome message when a new member joins the server.

        The message includes the configured welcome text and the
        three verification buttons (Citizen, Foreigner, Embassy).
        """

        # Skip if no welcome channel is configured
        welcome_channel_id = self.bot.config.get("channels", {}).get("welcome_buttons")
        if not welcome_channel_id:
            return

        channel = member.guild.get_channel(welcome_channel_id)
        if not channel:
            return

        default_role_id = self.bot.config.get("roles", {}).get("bezoeker")
        if default_role_id:
            role = member.guild.get_role(default_role_id)
            if role:
                await member.add_roles(role)

        embed = discord.Embed(
            title="🇳🇱 Welcome to Nederland!",
            description=f"Welcome {member.mention}! We're glad to have you here.\n\nPlease head "
            f"over to <#{welcome_channel_id}> and click one of the buttons to verify your status "
            f"and gain access to the rest of the server!",
            color=int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16),
        )
        # optionally send to a dedicated welcome/announcement channel if configured
        extra_welcome = self.bot.config.get("channels", {}).get("welcome_message")
        if extra_welcome:
            ch = member.guild.get_channel(extra_welcome)
            if ch:
                await ch.send(embed=embed)

        # # Create the welcome embed
        # embed = discord.Embed(
        #     title="🇳🇱 Welcome to Nederland!",
        #     description=self.config.get("welcome_message", "Welcome!"),
        #     color=discord.Color.gold(),
        #     timestamp=datetime.datetime.now(datetime.UTC)
        # )
        # embed.set_thumbnail(url=member.display_avatar.url)
        # embed.set_author(name=member.name, icon_url=member.display_avatar.url)
        # embed.set_footer(text=f"Member #{member.guild.member_count}")

        # # Send welcome message with verification buttons
        # await channel.send(content=member.mention, embed=embed, view=WelcomeView(self.bot))

    @app_commands.command(
        name="nickname", description="Stel de bijnaam van een gebruiker in op de server"
    )
    @app_commands.describe(
        user="De gebruiker van wie je de bijnaam wilt wijzigen",
        nickname="De nieuwe bijnaam",
    )
    @commands.has_permissions(manage_nicknames=True)
    async def nickname(
        self, interaction: discord.Interaction, user: discord.Member, nickname: str
    ):
        """
        Change a user's nickname in the server.

        :param interaction: The interaction that triggered the command.
        :param user: The member whose nickname is to be changed.
        :param nickname: The new nickname to set.
        """
        try:
            await user.edit(
                nick=nickname, reason=f"Nickname changed by {interaction.user.name}"
            )
            await interaction.response.send_message(
                f"Bijnaam van {user.mention} is succesvol gewijzigd naar **{nickname}**.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Ik heb geen toestemming om de bijnaam van deze gebruiker te wijzigen.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Bijnaam wijzigen mislukt: {e}", ephemeral=True
            )

        # Log to the government log channel
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    log_embed = discord.Embed(
                        title="Nickname aangepast",
                        description=f"**User:** {user.mention} ({user.name})\n",
                        color=discord.Color.green(),
                        timestamp=datetime.datetime.now(datetime.UTC),
                    )
                    log_embed.set_thumbnail(url=user.display_avatar.url)
                    log_embed.set_footer(
                        text=f"Veranderd door {interaction.user.name}",
                        icon_url=interaction.user.display_avatar.url,
                    )
                    await log_channel.send(embed=log_embed)
                    _log_posted = True
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.bot.logger.error(f"Failed to post to log channel: {e}")

    @app_commands.command(
        name="ticketstats",
        description="Toon hoeveel verificatietickets per type zijn aangemaakt sinds een datum",
    )
    @app_commands.describe(
        start_date="Begindatum in JJJJ-MM-DD formaat (bijv. 2026-01-01)",
    )
    @has_privileged_role()
    async def ticketstats(self, interaction: discord.Interaction, start_date: str):
        """Show ticket creation counts per type since *start_date*."""
        try:
            requested_start = datetime.datetime.strptime(start_date, "%Y-%m-%d").replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            await interaction.response.send_message(
                "Ongeldige datum. Gebruik het formaat JJJJ-MM-DD, bijvoorbeeld 2026-01-01.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        db = await _get_shared_db(interaction.client)
        guild_id = str(interaction.guild.id)

        earliest = await db.get_earliest_ticket_log_at(guild_id)
        if earliest is None:
            await interaction.followup.send(
                "Er is nog geen ticketdata gelogd. Vanaf nu worden nieuwe tickets "
                "automatisch geregistreerd — probeer dit commando over enige tijd opnieuw.",
                ephemeral=True,
            )
            return

        earliest_dt = datetime.datetime.fromisoformat(earliest)
        if earliest_dt.tzinfo is None:
            earliest_dt = earliest_dt.replace(tzinfo=datetime.timezone.utc)

        effective_start = max(requested_start, earliest_dt)
        counts = await db.get_ticket_counts_since(guild_id, effective_start.isoformat())

        type_labels = {
            "citizen": "🇳🇱 Nederlander",
            "belgian": "🇧🇪 Belgian",
            "foreigner": "🌍 Foreigner",
            "embassy": "🚨 Embassy",
            "admin_contact": "📩 Admin Contact",
        }

        lines = []
        total = 0
        for key, label in type_labels.items():
            count = counts.get(key, 0)
            total += count
            lines.append(f"{label}: **{count}**")
        # Include any other/unexpected types as well
        for key, count in counts.items():
            if key not in type_labels:
                total += count
                lines.append(f"{key}: **{count}**")

        embed = discord.Embed(
            title="📊 Ticket statistieken",
            description="\n".join(lines) + f"\n\n**Totaal: {total}**",
            color=discord.Color.blurple(),
            timestamp=datetime.datetime.now(datetime.UTC),
        )

        if requested_start < earliest_dt:
            embed.set_footer(
                text=(
                    f"We loggen ticketdata pas vanaf {earliest_dt.strftime('%Y-%m-%d')}. "
                    "De cijfers hierboven zijn vanaf die datum."
                )
            )
        else:
            embed.set_footer(text=f"Vanaf {requested_start.strftime('%Y-%m-%d')}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _get_nickname(self, in_game_id: str) -> str | None:
        # Ensure the shared API client is available. The ServiceCoordinator cog
        # initializes `bot._ext_client` asynchronously; wait for that if present.
        client = self._client
        if client is None:
            ready_event = getattr(self.bot, "_ext_services_ready", None)
            if ready_event is not None:
                try:
                    await asyncio.wait_for(ready_event.wait(), timeout=5.0)
                except Exception:
                    # timed out or other error; continue to check client below
                    pass
                client = self._client

        if client is None:
            raise RuntimeError("API client is not available. Cannot fetch username.")

        try:
            params = {"input": json.dumps({"userId": in_game_id})}
            user_info: dict = await client.get("/user.getUserLite", params=params)
            # Defensive extraction in case the API returns unexpected shapes
            nickname = user_info.get("result", {}).get("data", {}).get("username")
            if not nickname:
                raise ValueError("username not found in API response")
            return nickname
        except Exception as e:
            self.bot.logger.error(
                f"Error fetching username for in-game ID {in_game_id}: {e}"
            )
            raise ValueError(
                "Failed to fetch username from API for the provided in-game ID."
            )

    @app_commands.command(
        name="approve", description="Keur een verificatieverzoek goed"
    )
    @app_commands.describe(
        in_game_id="In-game ID of profiel-URL (https://app.warera.io/user/{id})",
        reason="Interne reden voor goedkeuring (niet zichtbaar voor de gebruiker)",
        nickname="[Optioneel]: Gebruikersnaam van de speler",
    )
    @has_privileged_role()
    async def approve(
        self,
        interaction: discord.Interaction,
        in_game_id: str,
        nickname: str | None = None,
        reason: str = "Geen reden opgegeven",
    ):
        """
        Approve a verification request in the current ticket channel.
        """
        # defer to avoid timing out
        await interaction.response.defer(ephemeral=True)

        channel = interaction.channel
        try:
            in_game_id = self.normalize_ingame_id(in_game_id)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        if nickname is None:
            try:
                nickname = await self._get_nickname(in_game_id)
            except Exception as e:
                await interaction.followup.send(
                    f"Failed to retrieve username for in-game ID: {e}", ephemeral=True
                )
                return

        # Verify this is a ticket channel
        if not channel.name.startswith(("citizen-", "foreigner-", "belgian-")):
            await interaction.followup.send(
                "This command can only be used in verification channels.",
                ephemeral=True,
            )
            return

        # Check if the user has permission to moderate
        mod_roles = [
            self.config["roles"]["border_control"],
            self.config["roles"]["minister_foreign_affairs"],
            self.config["roles"]["president"],
            self.config["roles"]["vice_president"],
        ]

        user_role_ids = [role.id for role in interaction.user.roles]
        has_permission = any(
            role_id in user_role_ids for role_id in mod_roles if role_id
        )

        if not has_permission and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        # Extract user ID from channel topic
        topic = channel.topic or ""
        user_id = None
        for part in topic.split("|"):
            if "User ID:" in part:
                try:
                    user_id = int(part.split(":")[-1].strip())
                except ValueError:
                    logger.debug("Could not parse user ID from topic part: %r", part)

        if not user_id:
            await interaction.followup.send(
                "Could not find the user for this request. Please check manually.",
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(user_id)
        if not member:
            await interaction.followup.send(
                "The user is no longer in the server.", ephemeral=True
            )
            return

        try:
            await member.edit(nick=nickname)
        except Exception as e:
            await interaction.followup.send(
                f"Failed to edit member nickname: {e}", ephemeral=True
            )
            self.bot.logger.error(f"Failed to edit member nickname: {e}")
            return

        try:
            await self._validate_identity_link_target(
                guild_id=str(interaction.guild.id),
                member=member,
                in_game_id=in_game_id,
            )
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            self.bot.logger.error(
                "Failed identity validation in /approve: %s", e, exc_info=True
            )
            await interaction.followup.send(
                "Kon identity mapping niet valideren door een interne fout.",
                ephemeral=True,
            )
            return

        # Determine which role to grant based on request type
        request_type = channel.name.split("-")[0]
        role_to_give = None

        if request_type == "citizen":
            role_to_give = interaction.guild.get_role(
                self.config["roles"]["nederlander"]
            )
        elif request_type == "belgian":
            role_to_give = interaction.guild.get_role(self.config["roles"]["belgian"])
        elif request_type == "foreigner":
            role_to_give = interaction.guild.get_role(self.config["roles"]["foreigner"])

        # Attempt to assign the role
        if role_to_give:
            try:
                await member.add_roles(role_to_give)
                self.bot.logger.info(
                    f"Assigned role {role_to_give.name} to "
                    f"{member.name} for {request_type} verification"
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    f"I don't have permission to assign the {role_to_give.name} role. "
                    "Make sure my bot role is **higher** than "
                    f"this role in Server Settings > Roles.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as e:
                await interaction.followup.send(
                    f"Failed to assign role: {e}", ephemeral=True
                )
                return
            except Exception as e:
                await interaction.followup.send(
                    f"An unexpected error occurred while assigning the role: {e}",
                    ephemeral=True,
                )
                return

        # Remove old role
        old_role_id = self.config["roles"]["bezoeker"]
        old_role = interaction.guild.get_role(old_role_id)
        if old_role:
            try:
                await member.remove_roles(old_role)
            except discord.Forbidden:
                self.bot.logger.error(
                    f"Could not remove role {old_role.name} from "
                    f"{member.name} due to permission issues."
                )
            except discord.HTTPException as e:
                self.bot.logger.error(
                    f"Failed to remove role {old_role.name} from {member.name}: {e}"
                )

        db_saved = True
        try:
            nationality = {
                "citizen": "nederlander",
                "belgian": "belgian",
                "foreigner": "foreigner",
            }.get(request_type, request_type)
            await self._store_identity_link(
                guild_id=str(interaction.guild.id),
                member=member,
                in_game_id=in_game_id,
                request_type=request_type,
                nationality=nationality,
                approved_by_discord_id=str(interaction.user.id),
            )
        except Exception as e:
            db_saved = False
            self.bot.logger.error("Failed to persist identity link in /approve: %s", e)

        # Notify the user of approval
        if not request_type == "citizen":
            user_embed = discord.Embed(
                title="✅ Request Approved!",
                description=f"Your {request_type} verification request has been approved!",
                color=discord.Color.green(),
            )
            if role_to_give:
                user_embed.add_field(
                    name="Role Granted", value=role_to_give.mention, inline=False
                )

            user_embed.set_footer(text="Dit kanaal zal worden verwijderd over 8 uur.")

            await channel.send(content=member.mention, embed=user_embed)

        # Log to the government log channel
        log_posted = False
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    log_embed = discord.Embed(
                        title="✅ Verificatie Goedgekeurd",
                        description=(
                            f"**Gebruiker:** {member.mention} ({member.name})\n"
                            f"**Type:** {request_type.title()}\n"
                            f"**Reden:** {reason}"
                        ),
                        color=discord.Color.green(),
                        timestamp=datetime.datetime.now(datetime.UTC),
                    )
                    log_embed.set_thumbnail(url=member.display_avatar.url)
                    log_embed.set_footer(
                        text=f"Goedgekeurd door {interaction.user.name}",
                        icon_url=interaction.user.display_avatar.url,
                    )
                    if role_to_give:
                        log_embed.add_field(
                            name="Rol Toegewezen",
                            value=role_to_give.mention,
                            inline=True,
                        )
                    await log_channel.send(embed=log_embed)
                    log_posted = True
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.bot.logger.error(f"Failed to post to log channel: {e}")

        # Confirm to the moderator
        mod_embed = discord.Embed(
            title="📝 Goedkeuring Geregistreerd",
            description=(
                f"**Gebruiker:** {member.mention}\n"
                f"**Type:** {request_type}\n"
                f"**In-game ID:** `{in_game_id}`\n"
                f"**Reden:** {reason}"
            ),
            color=discord.Color.green(),
        )
        mod_embed.set_footer(text=f"Goedgekeurd door {interaction.user.name}")

        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if not log_posted and log_channel_id:
            mod_embed.add_field(
                name="⚠️ Waarschuwing",
                value="Kon niet in het logkanaal posten",
                inline=False,
            )
        if not db_saved:
            mod_embed.add_field(
                name="⚠️ Database",
                value="Rol is toegekend, maar identity mapping kon niet worden opgeslagen.",
                inline=False,
            )

        await interaction.followup.send(embed=mod_embed, ephemeral=True)

        if request_type == "citizen":
            welcome_embed = self._build_citizen_welcome_embed(
                member, referrer_hint=interaction.user.nick or "2sa"
            )
            self.bot.logger.info(
                f"Sending welcome message to {member.name} in {interaction.guild.name}"
            )
            await channel.send(content=member.mention, embed=welcome_embed)

        # Delete the ticket channel after a delay — use create_task so the deletion
        # survives even if the interaction coroutine finishes.  We also persist the
        # deletion schedule to the DB so that a bot restart can reschedule it.
        delay = 8 * 3600
        _now = datetime.datetime.now(datetime.timezone.utc)
        _delete_at = _now + datetime.timedelta(seconds=delay)
        try:
            _db = await self._get_approval_db()
            await _db.add_pending_ticket_deletion(
                channel_id=str(channel.id),
                guild_id=str(interaction.guild_id),
                approved_at=_now.isoformat(),
                delete_at=_delete_at.isoformat(),
            )
        except Exception as _e:
            self.bot.logger.warning("approve: could not record pending deletion: %s", _e)
            _db = None
        asyncio.create_task(
            self._delete_channel_after(
                channel,
                delay,
                f"Verificatie goedgekeurd door {interaction.user.name}",
                db=_db,
            )
        )

    @app_commands.command(name="deny", description="Wijs een verificatieverzoek af")
    @app_commands.describe(
        reason="Interne reden voor afwijzing (niet zichtbaar voor de gebruiker)"
    )
    @has_privileged_role()
    async def deny(
        self, interaction: discord.Interaction, reason: str = "Geen reden opgegeven"
    ):
        """
        Deny a verification request in the current ticket channel.
        """

        channel = interaction.channel

        # Verify this is a ticket channel
        if not channel.name.startswith(
            ("citizen-", "foreigner-", "embassy-", "belgian-")
        ):
            await interaction.response.send_message(
                "Dit commando kan alleen worden gebruikt in verificatiekanalen.",
                ephemeral=True,
            )
            return

        # Check if the user has permission to moderate
        mod_roles = [
            self.config["roles"]["border_control"],
            self.config["roles"]["minister_foreign_affairs"],
            self.config["roles"]["president"],
            self.config["roles"]["vice_president"],
        ]

        user_role_ids = [role.id for role in interaction.user.roles]
        has_permission = any(
            role_id in user_role_ids for role_id in mod_roles if role_id
        )

        if not has_permission and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Je hebt geen toestemming om dit commando te gebruiken.", ephemeral=True
            )
            return

        # Extract user ID from channel topic
        topic = channel.topic or ""
        user_id = None
        for part in topic.split("|"):
            if "User ID:" in part:
                try:
                    user_id = int(part.split(":")[-1].strip())
                except ValueError:
                    logger.debug("Could not parse user ID from topic part: %r", part)

        member = interaction.guild.get_member(user_id) if user_id else None
        request_type = channel.name.split("-")[0]

        # Notify the user of denial
        user_embed = discord.Embed(
            title="❌ Request Denied",
            description=f"Your {request_type} verification request has been denied.",
            color=discord.Color.red(),
        )
        user_embed.set_footer(text="This channel will be deleted in 8 hours.")

        if member:
            await channel.send(content=member.mention, embed=user_embed)
        else:
            await channel.send(embed=user_embed)

        # Log to the government log channel
        log_posted = False
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    log_embed = discord.Embed(
                        title="❌ Verificatie Afgewezen",
                        description=(
                            f"**Gebruiker:** {member.mention if member else 'Onbekend'} "
                            f"({member.name if member else 'Onbekend'})\n"
                            f"**Type:** {request_type.title()}\n"
                            f"**Reden:** {reason}"
                        ),
                        color=discord.Color.red(),
                        timestamp=datetime.datetime.now(datetime.UTC),
                    )
                    if member:
                        log_embed.set_thumbnail(url=member.display_avatar.url)
                    log_embed.set_footer(
                        text=f"Afgewezen door {interaction.user.name}",
                        icon_url=interaction.user.display_avatar.url,
                    )
                    await log_channel.send(embed=log_embed)
                    log_posted = True
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.bot.logger.error(f"Failed to post to log channel: {e}")

        # Confirm to the moderator
        mod_embed = discord.Embed(
            title="📝 Afwijzing Geregistreerd",
            description=f"**Gebruiker:** {member.mention if member else 'Onbekend'}\n"
            f"**Type:** {request_type}\n"
            f"**Reden:** {reason}",
            color=discord.Color.red(),
        )
        mod_embed.set_footer(text=f"Afgewezen door {interaction.user.name}")

        if not log_posted and log_channel_id:
            mod_embed.add_field(
                name="⚠️ Waarschuwing",
                value="Kon niet in het logkanaal posten",
                inline=False,
            )

        await interaction.response.send_message(embed=mod_embed, ephemeral=True)

        # Delete the ticket channel after 8 hours — persisted to DB for restart recovery.
        _deny_delay = 8 * 3600
        _deny_now = datetime.datetime.now(datetime.timezone.utc)
        _deny_delete_at = _deny_now + datetime.timedelta(seconds=_deny_delay)
        try:
            _deny_db = await self._get_approval_db()
            await _deny_db.add_pending_ticket_deletion(
                channel_id=str(channel.id),
                guild_id=str(interaction.guild_id),
                approved_at=_deny_now.isoformat(),
                delete_at=_deny_delete_at.isoformat(),
            )
        except Exception as _deny_e:
            self.bot.logger.warning("deny: could not record pending deletion: %s", _deny_e)
            _deny_db = None
        asyncio.create_task(
            self._delete_channel_after(
                channel,
                _deny_delay,
                f"Verificatie afgewezen door {interaction.user.name}",
                db=_deny_db,
            )
        )

    @app_commands.command(
        name="embassyapprove", description="Approve an embassy request"
    )
    @app_commands.describe(
        country="Country of the embassy request",
        in_game_id="In-game ID or profile URL (https://app.warera.io/user/{id})",
    )
    @app_commands.autocomplete(country=country_autocomplete)
    @has_privileged_role()
    async def embassy_approve(
        self,
        interaction: discord.Interaction,
        country: str,
        in_game_id: str,
    ):
        """
        Approve an embassy request and assign the corresponding role.

        This command is similar to /approve but also assigns the specific embassy role.
        """

        try:
            # avoid "The application did not respond" (Discord requires a response within 3s)
            await interaction.response.defer(ephemeral=True)
            try:
                in_game_id = self.normalize_ingame_id(in_game_id)
            except ValueError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return
            # quick trace so you can see the command started
            self.bot.logger.info(
                f"embassy_approve started by {interaction.user} for country={country}"
            )

            # helper to reply whether we've already deferred
            async def reply(content=None, **kwargs):
                if interaction.response.is_done():
                    await interaction.followup.send(content, **kwargs)
                else:
                    await interaction.response.send_message(content, **kwargs)

            channel = interaction.channel
            guild = interaction.guild

            minister_role = interaction.guild.get_role(
                self.config["roles"]["government"]
            )
            president_role = interaction.guild.get_role(
                self.config["roles"]["president"]
            )
            vice_president_role = interaction.guild.get_role(
                self.config["roles"]["vice_president"]
            )

            if any(role is None for role in [minister_role, president_role, vice_president_role]):
                await reply(
                    "One or more required roles (government, president, vice president) are not configured correctly.",
                    ephemeral=True,
                )
                self.bot.logger.error(
                    "Required roles missing in config: government=%s, president=%s, vice_president=%s",
                    minister_role,
                    president_role,
                    vice_president_role,
                )
                return

            # Check if the user has permission to moderate
            mod_roles = [
                self.config["roles"]["government"],
                self.config["roles"]["president"],
                self.config["roles"]["vice_president"],
            ]

            user_role_ids = [role.id for role in interaction.user.roles]
            has_permission = any(
                role_id in user_role_ids for role_id in mod_roles if role_id
            )

            if (
                not has_permission
                and not interaction.user.guild_permissions.administrator
            ):
                await interaction.followup.send(
                    "You don't have permission to use this command.", ephemeral=True
                )
                return

            self.bot.logger.debug(
                f"looking for user ID in channel topic: {channel.topic}"
            )
            # Extract user ID from channel topic
            topic = channel.topic or ""
            user_id = None
            for part in topic.split("|"):
                if "User ID:" in part:
                    try:
                        user_id = int(part.split(":")[-1].strip())
                    except ValueError:
                        self.bot.logger.debug(
                            "Could not parse user ID from topic part: %r", part
                        )

            if not user_id:
                await interaction.followup.send(
                    "Could not find the user for this request. Please check manually.",
                    ephemeral=True,
                )
                return

            member = interaction.guild.get_member(user_id)
            if not member:
                await interaction.followup.send(
                    "The user is no longer on the server.", ephemeral=True
                )
                return

            try:
                await self._validate_identity_link_target(
                    guild_id=str(interaction.guild.id),
                    member=member,
                    in_game_id=in_game_id,
                )
            except ValueError as e:
                await reply(str(e), ephemeral=True)
                return

            # Attempt to assign the embassy role based on country
            self.bot.logger.debug(f"Assigning embassy role for country: {country}")
            embassy_role_id = self.bot.config.get("roles", {}).get(
                "buitenlandse_diplomaat"
            )
            embassy_role = (
                interaction.guild.get_role(embassy_role_id) if embassy_role_id else None
            )

            if not embassy_role:
                await reply(
                    "Embassy role not found in the server. Please check the bot configuration.",
                    ephemeral=True,
                )
                self.bot.logger.error("Embassy role not found with ID: %s", embassy_role_id)
                return

            try:
                await member.add_roles(embassy_role)
            except discord.Forbidden:
                await interaction.followup.send(
                    f"I don't have permission to assign the {embassy_role.name} role. "
                    f"Make sure my bot role is **higher** than this role "
                    f"in Server Settings > Roles.",
                    ephemeral=True,
                )
                return

            # remove visitor role
            old_role_id = self.config["roles"]["bezoeker"]
            old_role = interaction.guild.get_role(old_role_id)
            if old_role:
                try:
                    await member.remove_roles(old_role)
                except discord.Forbidden:
                    self.bot.logger.error(
                        f"Could not remove role {old_role.name} from {member.name} "
                        f"due to permission issues."
                    )
                except discord.HTTPException as e:
                    self.bot.logger.error(
                        f"Failed to remove role {old_role.name} from {member.name}: {e}"
                    )

            # Check if the embassy channel exists — guarded by a per-country lock to
            # prevent a race condition when two moderators approve simultaneously.
            lock_key = f"{interaction.guild_id}:{country.lower()}"
            async with self._embassy_locks.setdefault(lock_key, asyncio.Lock()):
                self.bot.logger.debug(
                    f"Checking for existing embassy channel for country: {country}"
                )
                embassy_channel = self._find_existing_embassy_channel(
                    interaction.guild, country
                )

                if not embassy_channel:
                    # Create the embassy channel
                    self.bot.logger.debug(
                        f"Creating embassy channel for country: {country}"
                    )
                    channel_name = next(
                        name
                        for name in self._embassy_channel_candidates(country)
                        if name.endswith("-embassy")
                    )
                    # Route by country alphabet bucket using configured order:
                    # A-I => index 0, J-R => index 1, S-Z => index 2.
                    category = None
                    embassy_categories = self._resolve_embassy_categories(
                        interaction.guild
                    )
                    if embassy_categories:
                        target_index = self._embassy_category_bucket_index(country)
                        category = embassy_categories[
                            min(target_index, len(embassy_categories) - 1)
                        ]

                    if category is None:
                        # Fallback to verification category for older configs.
                        verification_cat_id = self.bot.config.get("channels", {}).get(
                            "verification"
                        )
                        if verification_cat_id:
                            candidate = interaction.guild.get_channel(
                                verification_cat_id
                            )
                            if isinstance(candidate, discord.CategoryChannel):
                                category = candidate

                    # Set up channel permissions
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(
                            view_channel=False
                        ),
                        minister_role: discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                        ),
                        president_role: discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                        ),
                        vice_president_role: discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                        ),
                        guild.me: discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            manage_channels=True,
                            manage_messages=True,
                            embed_links=True,
                        ),
                    }

                    try:
                        channel = await guild.create_text_channel(
                            name=channel_name,
                            category=category,
                            overwrites=overwrites,
                            topic=f"Embassy channel for {country}",
                        )
                        embassy_channel = channel
                    except discord.Forbidden as e:
                        error_msg = (
                            "I don't have permission to create channels.\n\n"
                            "**Possible solutions:**\n"
                            "• Make sure the bot has 'Manage Channels' permission "
                            "server-wide\n"
                        )
                        if category:
                            error_msg += (
                                f"• Add the bot to the **{category.name}** category "
                                "with 'Manage Channels' permission\n"
                            )
                        error_msg += f"\n**Error:** {e}"
                        await interaction.followup.send(error_msg, ephemeral=True)
                        return
                    except discord.HTTPException as e:
                        await interaction.followup.send(
                            "Could not create the embassy channel. Check that the configured categories aren't full and that the bot has sufficient permissions.\n"
                            f"**Error:** {e}",
                            ephemeral=True,
                        )
                        return

            if embassy_channel:
                self.bot.logger.debug(
                    "Failed to edit member nickname: {e}"
                    f"Setting permissions for member {member} in "
                    f"embassy channel {embassy_channel.name}"
                )
                # try:
                await embassy_channel.set_permissions(
                    member,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )
                # except discord.Forbidden:
                #     self.bot.logger.error(
                #         f"Could not set permissions for {member.name} in {embassy_channel.name} due to permission issues."
                #     )
                # except discord.HTTPException as e:
                #     self.bot.logger.error(
                #         f"Failed to set permissions for {member.name} in {embassy_channel.name}: {e}"
                #     )
            self.bot.logger.debug(
                f"Successfully approved embassy request for {member.name} and assigned role {embassy_role.name}"
            )

            db_saved = True
            try:
                await self._store_identity_link(
                    guild_id=str(interaction.guild.id),
                    member=member,
                    in_game_id=in_game_id,
                    request_type="embassy",
                    nationality=country.strip().lower(),
                    approved_by_discord_id=str(interaction.user.id),
                    embassy_country=country.strip(),
                )
            except Exception as e:
                db_saved = False
                self.bot.logger.error(
                    "Failed to persist identity link in /embassyapprove: %s", e
                )

            confirmation_embed = discord.Embed(
                title=f"Welcome to {country.title()} Embassy! 🇳🇱",
            )
            # send confirmation in embassy channel
            await embassy_channel.send(
                content=f"{member.mention} {minister_role.mention}",
                embed=confirmation_embed,
            )

            # Also post a public confirmation in the TICKET channel itself
            # (embassy-<id>-<user> — this command runs in it, "channel" is
            # it). Without this, approval was only ever visible to the
            # moderator (ephemeral response_text below) and in the shared
            # country embassy channel — never in the ticket that's about to
            # close, so the applicant had no visible sign their request had
            # actually been approved before the channel disappeared.
            approved_embed = discord.Embed(
                title="✅ Embassy Request Approved",
                description=f"You now have access to {embassy_channel.mention}.",
                color=discord.Color.green(),
            )
            approved_embed.set_footer(text="This ticket channel will be deleted in 8 hours.")
            await channel.send(content=member.mention, embed=approved_embed)

            response_text = (
                f"Successfully approved embassy request for {member.mention} and assigned role {embassy_role.mention}. "
                f"Access to the embassy channel {embassy_channel.mention} has been granted."
            )
            if not db_saved:
                response_text += (
                    "\n⚠️ Identity mapping could not be saved to the database."
                )
            # Same notice /approve and /deny give, so a moderator can see the
            # 8-hour deletion was actually scheduled. The confirmation embed is
            # deliberately not used for this — it goes to the embassy channel,
            # which is not the channel being deleted.
            response_text += "\nThis ticket channel will be deleted in 8 hours."
            await reply(response_text)

            # Log to the government log channel
            log_channel_id = self.bot.config.get("channels", {}).get("logs")
            if log_channel_id:
                log_channel = interaction.guild.get_channel(log_channel_id)
                if log_channel:
                    try:
                        log_embed = discord.Embed(
                            title="✅ Embassy Request Approved",
                            description=f"**User:** {member.mention} ({member.name})\n"
                            f"**Country:** {country.title()}\n",
                            color=discord.Color.green(),
                            timestamp=datetime.datetime.now(datetime.UTC),
                        )
                        log_embed.set_thumbnail(url=member.display_avatar.url)
                        log_embed.set_footer(
                            text=f"Approved by {interaction.user.name}",
                            icon_url=interaction.user.display_avatar.url,
                        )
                        await log_channel.send(embed=log_embed)
                        _log_posted = True
                    except (discord.Forbidden, discord.HTTPException) as e:
                        self.bot.logger.error(f"Failed to post to log channel: {e}")

            # Delete the ticket channel after a delay — persist so restarts survive
            _emb_delay = 8 * 3600
            _emb_now = datetime.datetime.now(datetime.timezone.utc)
            _emb_delete_at = _emb_now + datetime.timedelta(seconds=_emb_delay)
            try:
                _emb_db = await self._get_approval_db()
                await _emb_db.add_pending_ticket_deletion(
                    channel_id=str(interaction.channel.id),
                    guild_id=str(interaction.guild_id),
                    approved_at=_emb_now.isoformat(),
                    delete_at=_emb_delete_at.isoformat(),
                )
            except Exception as _emb_e:
                self.bot.logger.warning("embassyapprove: could not record pending deletion: %s", _emb_e)
                _emb_db = None
            asyncio.create_task(
                self._delete_channel_after(
                    interaction.channel,
                    _emb_delay,
                    f"Embassy request approved by {interaction.user.name}",
                    db=_emb_db,
                )
            )

        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__)
            self.bot.logger.error("Unhandled error in embassy_approve", exc_info=True)
            try:
                if (
                    interaction
                    and hasattr(interaction, "response")
                    and interaction.response.is_done()
                ):
                    await interaction.followup.send(
                        "An internal error occurred while running this command.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        "An internal error occurred while running this command.",
                        ephemeral=True,
                    )
            except Exception:
                traceback.print_exc()
            return

    @commands.command(name="testwelcome")
    @commands.is_owner()
    async def testwelcome(self, context: commands.Context):
        """Simulate a member join for testing"""
        await self.on_member_join(context.author)


async def setup(bot) -> None:
    """Add the Welcome cog to the bot."""
    await bot.add_cog(Welcome(bot))
