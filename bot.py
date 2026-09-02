"""
The main bot file. This is where the bot is instantiated and run, and where the main events are handled.
"""

import argparse
import asyncio
import json
import logging
import os
import platform
import random
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import discord
from discord.ext import commands, tasks
from discord.ext.commands import Context
from dotenv import load_dotenv

from database import DatabaseManager

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # Add this line for member join/leave events
intents.presences = True

# Disable unused intents to optimize performance
intents.dm_messages = False
intents.dm_reactions = False
intents.dm_typing = False
intents.bans = False
intents.integrations = False
intents.invites = False
intents.webhooks = False
intents.emojis_and_stickers = True
intents.guild_scheduled_events = False
intents.guild_typing = False
intents.presences = False

# Setup both of the loggers


class LoggingFormatter(logging.Formatter):
    """Custom logging formatter with colors and timestamps."""

    # Colors
    black = "\x1b[30m"
    red = "\x1b[31m"
    green = "\x1b[32m"
    yellow = "\x1b[33m"
    blue = "\x1b[34m"
    gray = "\x1b[38m"
    # Styles
    reset = "\x1b[0m"
    bold = "\x1b[1m"

    COLORS = {
        logging.DEBUG: gray + bold,
        logging.INFO: blue + bold,
        logging.WARNING: yellow + bold,
        logging.ERROR: red,
        logging.CRITICAL: red + bold,
    }

    def format(self, record):
        log_color = self.COLORS[record.levelno]
        format = "(black){asctime}(reset) (levelcolor){levelname:<8}(reset) (green){name}(reset) {message}"
        format = format.replace("(black)", self.black + self.bold)
        format = format.replace("(reset)", self.reset)
        format = format.replace("(levelcolor)", log_color)
        format = format.replace("(green)", self.green + self.bold)
        formatter = logging.Formatter(format, "%Y-%m-%d %H:%M:%S", style="{")
        return formatter.format(record)


logger = logging.getLogger("discord_bot")
logger.setLevel(logging.DEBUG)
logger.propagate = False

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Avoid duplicate handlers if this module is imported more than once.
if not logger.handlers:
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(LoggingFormatter())

    # File handler (always resolves to <repo>/logs/discord.log)
    file_handler = logging.FileHandler(
        filename=LOG_DIR / "discord.log", encoding="utf-8", mode="a"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler_formatter = logging.Formatter(
        "[{asctime}] [{levelname:<8}] {name}: {message}",
        "%Y-%m-%d %H:%M:%S",
        style="{",
    )
    file_handler.setFormatter(file_handler_formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)


class DiscordBot(commands.Bot):
    def __init__(self, config_path: str | Path | None = None) -> None:
        # Load config early so owner_ids can be passed to the superclass.
        _early_config: dict = {}
        try:
            _cfg_path = Path(config_path) if config_path else Path("config/config.json")
            with _cfg_path.open("r", encoding="utf-8") as _f:
                _early_config = json.load(_f)
        except Exception:
            pass
        _owner_ids: set[int] = {
            int(i) for i in _early_config.get("owner_ids", []) if str(i).isdigit()
        }
        super().__init__(
            command_prefix=commands.when_mentioned_or(os.getenv("PREFIX")),
            intents=intents,
            help_command=None,
            owner_ids=_owner_ids or None,
        )
        # This creates custom bot variables so that we can access these variables in cogs more easily.

        # For example, The logger is available using the following code:
        # - self.logger # In this class
        # - bot.logger # In this file
        # - self.bot.logger # In cogs
        self.logger = logger
        self.database = None
        self.bot_prefix = os.getenv("PREFIX")
        self.invite_link = os.getenv("INVITE_LINK")
        self.config = self.load_config(config_path)
        self.start_time = discord.utils.utcnow()
        self.testing = False

    def load_config(self, config_path: str | Path | None = None) -> dict:
        """Load configuration from given JSON path (relative paths supported).

        If `config_path` is None the default `config.json` in the project root is used.
        """
        if config_path:
            cfg = Path(config_path)
        else:
            cfg = Path("config/config.json")

        try:
            with cfg.open("r", encoding="utf-8") as f:
                config = json.load(f)
                self.logger.info(f"Configuration loaded from {cfg}")
                return config
        except Exception as e:
            self.logger.error(f"Failed to load config {cfg}: {e}")
            return {
                "colors": {
                    "primary": "0x154273",
                    "success": "0x57F287",
                    "error": "0xE02B2B",
                    "warning": "0xF59E42",
                }
            }

    async def init_db(self) -> None:
        ext_db_path = self.config.get("external_db_path", "database/external.db")
        async with aiosqlite.connect(ext_db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=60000")
            schema_sql = (Path("database") / "schema.sql").read_text(encoding="utf-8")
            for statement in schema_sql.split(";"):
                stmt = statement.strip()
                if stmt:
                    try:
                        await db.execute(stmt)
                    except Exception:
                        pass
            # Idempotent column migrations for DBs created before schema update
            for _sql in [
                "ALTER TABLE resistance_state ADD COLUMN resistance_max REAL DEFAULT 100.0",
                "ALTER TABLE citizen_luck ADD COLUMN rarity_json TEXT",
                "ALTER TABLE division_mu_overrides ADD COLUMN mu_id TEXT",
            ]:
                try:
                    await db.execute(_sql)
                except Exception:
                    pass  # Column already exists

            # identity_links: migrate off the old discord_user_id-only primary
            # key to a composite (discord_user_id, guild_id) key — see
            # schema.sql's comment on this table for why. SQLite can't ALTER a
            # primary key in place, so this rebuilds the table when (and only
            # when) the old single-column key is still in use; every row
            # already has a globally-unique discord_user_id at this point (that
            # was the old constraint), so the rebuild can never collide.
            try:
                cur = await db.execute("PRAGMA table_info(identity_links)")
                cols = await cur.fetchall()
                pk_cols = {c[1] for c in cols if c[5]}  # c[5] is the pk column ordinal (0 = not part of PK)
                if pk_cols == {"discord_user_id"}:
                    await db.executescript(
                        """
                        CREATE TABLE identity_links_new (
                            discord_user_id        TEXT NOT NULL,
                            guild_id                TEXT NOT NULL,
                            in_game_user_id         TEXT NOT NULL,
                            nationality              TEXT NOT NULL,
                            request_type             TEXT NOT NULL,
                            embassy_country          TEXT,
                            approved_by_discord_id   TEXT NOT NULL,
                            approved_at              TEXT NOT NULL,
                            updated_at               TEXT NOT NULL,
                            PRIMARY KEY (discord_user_id, guild_id)
                        );
                        INSERT INTO identity_links_new
                            SELECT discord_user_id, guild_id, in_game_user_id, nationality,
                                   request_type, embassy_country, approved_by_discord_id,
                                   approved_at, updated_at
                            FROM identity_links;
                        DROP TABLE identity_links;
                        ALTER TABLE identity_links_new RENAME TO identity_links;
                        CREATE INDEX IF NOT EXISTS idx_identity_links_ingame
                            ON identity_links(in_game_user_id);
                        """
                    )
                    self.logger.info(
                        "init_db: migrated identity_links to a composite "
                        "(discord_user_id, guild_id) primary key"
                    )
            except Exception:
                self.logger.exception("init_db: identity_links primary-key migration failed")

            await db.commit()

    async def _write_command_catalogue(self) -> None:
        """Write all registered commands to website/data/commands.json.

        The website reads this file to display the command reference.
        Called once during setup_hook after all cogs are loaded.
        """
        import discord.app_commands as _app

        catalogue: dict[str, dict] = {}  # label -> {slash: [], prefix: []}

        _cog_labels: dict[str, str] = {
            "poller": "📊 Statistieken & Polls",
            "production_tasks": "📊 Statistieken & Polls",
            "citizen_tasks": "📊 Statistieken & Polls",
            "event_tasks": "📊 Statistieken & Polls",
            "luck_tasks": "📊 Statistieken & Polls",
            "resistance": "📊 Statistieken & Polls",
            "service_coordinator": "📊 Statistieken & Polls",
            "bonus": "📊 Statistieken & Polls",
            "paraatheid": "📊 Statistieken & Polls",
            "niveauverdeling": "📊 Statistieken & Polls",
            "peil": "📊 Statistieken & Polls",
            "debug": "🔑 Beheer (owner)",
            "mus": "🪖 MU Beheer",
            "mu": "🪖 MU Info",
            "general": "⚙️ Algemeen",
            "owner": "🔑 Beheer (owner)",
            "welcome": "👋 Welkom & Verificatie",
            "moderation": "🛡️ Moderatie",
            "battles": "⚔️ Gevechten",
            "roles": "🎭 Rollen",
            "help": "📚 Help",
        }

        def _label(cog_name: str | None) -> str:
            if not cog_name:
                return "🔧 Overig"
            return _cog_labels.get(
                cog_name.lower(), f"🔧 {cog_name.replace('_', ' ').title()}"
            )

        def _collect_slash(cmd, cog_label: str) -> None:
            if isinstance(cmd, _app.Group):
                for child in cmd.commands:
                    _collect_slash(child, cog_label)
                return
            if isinstance(cmd, _app.ContextMenu):
                return  # context menus have no description, skip them
            params = []
            for p in getattr(cmd, "parameters", []):
                params.append(
                    {
                        "name": p.name,
                        "required": p.required,
                        "description": p.description or "",
                    }
                )
            parts, parent = [cmd.name], getattr(cmd, "parent", None)
            while parent and hasattr(parent, "name"):
                parts.insert(0, parent.name)
                parent = getattr(parent, "parent", None)
            entry = {
                "name": "/" + " ".join(parts),
                "description": cmd.description or "",
                "params": params,
                "type": "slash",
            }
            catalogue.setdefault(cog_label, {"slash": [], "prefix": []})[
                "slash"
            ].append(entry)

        for cmd in self.tree.get_commands():
            cog_name: str | None = None
            for cog in self.cogs.values():
                if cmd.name in [c.name for c in cog.get_app_commands()]:
                    cog_name = type(cog).__name__
                    break
            _collect_slash(cmd, _label(cog_name))

        for cmd in self.commands:
            if cmd.hidden:
                continue
            if getattr(cmd, "app_command", None) is not None:
                continue  # hybrid command; already listed as slash above
            cog_name = type(cmd.cog).__name__ if cmd.cog else None
            lbl = _label(cog_name)
            desc = (cmd.help or cmd.brief or "").strip().split("\n")[0]
            params = [
                {"name": p, "required": True, "description": ""}
                for p in cmd.clean_params
            ]
            entry = {
                "name": f"!{cmd.qualified_name}",
                "description": desc,
                "params": params,
                "type": "prefix",
            }
            catalogue.setdefault(lbl, {"slash": [], "prefix": []})["prefix"].append(
                entry
            )

        result = [
            {"category": lbl, "slash": v["slash"], "prefix": v["prefix"]}
            for lbl, v in sorted(catalogue.items())
        ]

        out_dir = Path("website/data")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "commands.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        self.logger.info(
            f"Command catalogue written to {out_path} ({len(result)} categories)"
        )

    async def load_cogs(self) -> None:
        """
        The code in this function is executed whenever the bot will start.
        Recursively loads all .py files from cogs/ and its subdirectories.
        """
        cogs_path = Path("cogs")

        for root, dirs, files in os.walk(str(cogs_path)):
            # Skip __pycache__ directories
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for file in files:
                if file.endswith(".py"):
                    # Skip private/base files (starting with _)
                    if file.startswith("_"):
                        continue
                    # Calculate relative path from cogs directory
                    relative_path = os.path.relpath(
                        os.path.join(root, file), str(cogs_path)
                    )
                    # Convert file path to module path (e.g., standard_messages/beginner_handleiding.py -> standard_messages.beginner_handleiding)
                    extension = relative_path.replace(os.sep, ".")[:-3]
                    await self.load_extension(f"cogs.{extension}")
                    self.logger.info(f"Loaded extension '{extension}'")
                    # except Exception as e:
                    #     exception = f"{type(e).__name__}: {e}"
                    #     self.logger.error(
                    #         f"Failed to load extension {extension}\n{exception}"
                    #     )

    @tasks.loop(minutes=1.0)
    async def status_task(self) -> None:
        """
        Setup the game status task of the bot.
        """
        statuses = [
            "Werelddominantie aan het voorbereiden...",
            "Regiment Wielrijders aan het verzamelen...",
            "Tulpen aan het handelen...",
            "Polders aan het inpolderen...",
        ]
        await self.change_presence(activity=discord.Game(random.choice(statuses)))

    @status_task.before_loop
    async def before_status_task(self) -> None:
        """
        Before starting the status changing task, we make sure the bot is ready
        """
        await self.wait_until_ready()

    async def setup_hook(self) -> None:
        """
        This will just be executed when the bot starts the first time.
        """
        self.logger.info(f"Logged in as {self.user.name}")
        self.logger.info(f"discord.py API version: {discord.__version__}")
        self.logger.info(f"Python version: {platform.python_version()}")
        self.logger.info(
            f"Running on: {platform.system()} {platform.release()} ({os.name})"
        )
        self.logger.info("-------------------")
        # Ensure slash-command exceptions are always routed through our logger.
        self.tree.on_error = self.on_app_command_error

        await self.init_db()
        await self.load_cogs()
        await self._write_command_catalogue()
        self.status_task.start()
        self.database = DatabaseManager(
            connection=await aiosqlite.connect("database/database.db")
        )
        if self.testing:
            asyncio.create_task(_run_terminal_loop(self))

        guild_id: int = int(self.config.get("guild_id") or 0)
        if guild_id:
            guild = discord.Object(id=guild_id)
            # Sync guild-scoped commands (e.g. owner cog) to the guild.
            # Do NOT use copy_global_to here — combining 91 global + 12 guild-only
            # commands would exceed Discord's 100-command guild limit (103 total).
            # Global commands are synced separately below; Discord counts each pool
            # independently so 91 global + 12 guild-only is within limits.
            await self.tree.sync(guild=guild)
            self.logger.info("Guild-scoped commands synced to guild %d", guild_id)
        else:
            self.logger.warning("guild_id not set in config — skipping guild slash-command sync")
        # Sync global commands globally (visible in all guilds + DMs).
        await self.tree.sync()
        self._last_sync_at = datetime.now(timezone.utc)
        self._last_sync_scope = f"guild:{guild_id} + global" if guild_id else "global"
        self.logger.info("Global slash commands synced")

    async def on_disconnect(self) -> None:
        """
        Event handler when the bot disconnects from Discord.
        """
        self.logger.warning("Bot disconnected from Discord")

    async def on_resumed(self) -> None:
        """
        Event handler when the bot reconnects to Discord.
        """
        self.logger.info("Bot reconnected to Discord")

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        """
        Event handler for general errors that occur in events.
        """
        self.logger.error(f"An error occurred in {event_method}", exc_info=True)

    # ...existing code...
    async def on_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        """Handle errors from application (slash) commands."""

        # always print full traceback to stderr (visible in terminal) and to logger
        traceback.print_exception(
            type(error), error, error.__traceback__, limit=None, file=sys.stderr
        )
        self.logger.error(
            f"An error occurred in app command {getattr(interaction, 'command', None)}: {error}",
            exc_info=(type(error), error, error.__traceback__),
        )
        # try to notify the user if possible (avoid raising another exception)
        try:
            if interaction.response.is_done():
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
            # ensure any follow-up failure is also visible
            traceback.print_exc(file=sys.stderr)
            self.logger.error(
                "Failed to notify user about app command error", exc_info=True
            )

    # ...existing code...

    async def on_message(self, message: discord.Message) -> None:
        """
        The code in this event is executed every time someone sends a message, with or without the prefix

        :param message: The message that was sent.
        """
        if message.author == self.user or message.author.bot:
            return
        await self.process_commands(message)

    async def on_command_completion(self, context: Context) -> None:
        """
        The code in this event is executed every time a normal command has been *successfully* executed.

        :param context: The context of the command that has been executed.
        """
        full_command_name = context.command.qualified_name
        split = full_command_name.split(" ")
        executed_command = str(split[0])
        if context.guild is not None:
            self.logger.info(
                f"Executed {executed_command} command in {context.guild.name} (ID: {context.guild.id}) by {context.author} (ID: {context.author.id})"
            )
        else:
            self.logger.info(
                f"Executed {executed_command} command by {context.author} (ID: {context.author.id}) in DMs"
            )

    async def on_command_error(self, context: Context, error) -> None:
        """
        The code in this event is executed every time a normal valid command catches an error.

        :param context: The context of the normal command that failed executing.
        :param error: The error that has been faced.
        """
        if isinstance(error, commands.CommandOnCooldown):
            minutes, seconds = divmod(error.retry_after, 60)
            hours, minutes = divmod(minutes, 60)
            hours = hours % 24
            embed = discord.Embed(
                description=f"**Please slow down** - You can use this command again in {f'{round(hours)} hours' if round(hours) > 0 else ''} {f'{round(minutes)} minutes' if round(minutes) > 0 else ''} {f'{round(seconds)} seconds' if round(seconds) > 0 else ''}.",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.NotOwner):
            embed = discord.Embed(
                description="You are not the owner of the bot!", color=0xE02B2B
            )
            await context.send(embed=embed)
            if context.guild:
                self.logger.warning(
                    f"{context.author} (ID: {context.author.id}) tried to execute an owner only command in the guild {context.guild.name} (ID: {context.guild.id}), but the user is not an owner of the bot."
                )
            else:
                self.logger.warning(
                    f"{context.author} (ID: {context.author.id}) tried to execute an owner only command in the bot's DMs, but the user is not an owner of the bot."
                )
        elif isinstance(error, commands.MissingPermissions):
            embed = discord.Embed(
                description="You are missing the permission(s) `"
                + ", ".join(error.missing_permissions)
                + "` to execute this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.BotMissingPermissions):
            embed = discord.Embed(
                description="I am missing the permission(s) `"
                + ", ".join(error.missing_permissions)
                + "` to fully perform this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.MissingRequiredArgument):
            embed = discord.Embed(
                title="Error!",
                # We need to capitalize because the command arguments have no capital letter in the code and they are the first word in the error message.
                description=str(error).capitalize(),
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        else:
            raise error


# `bot` will be instantiated in __main__ after parsing CLI args to select config/token


# ------------------------------------------------------------------ #
# Terminal command runner (--testing mode)                            #
# ------------------------------------------------------------------ #


class _TerminalMessage:
    """Returned by _TerminalContext.send(); supports .edit() for status messages."""

    async def edit(self, *, content=None, **kwargs):
        if content:
            print(content, flush=True)


class _TerminalTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class _TerminalContext:
    """Minimal duck-typed Context for invoking commands from stdin in --testing mode."""

    def __init__(self, bot):
        self.bot = bot
        self.guild = bot.guilds[0] if bot.guilds else None

        class _Author:
            name = "Terminal"
            bot = False

        _Author.id = bot.owner_id or 0
        self.author = _Author()
        self.message = type("_M", (), {"content": "", "attachments": []})()

    async def send(self, content=None, *, embed=None, **kwargs):
        if content:
            print(content, flush=True)
        if embed:
            if getattr(embed, "title", None):
                print(f"[{embed.title}]", flush=True)
            if getattr(embed, "description", None):
                print(embed.description, flush=True)
            for field in getattr(embed, "fields", []):
                print(f"  {field.name}: {field.value}", flush=True)
        return _TerminalMessage()

    async def reply(self, *args, **kwargs):
        return await self.send(*args, **kwargs)

    def typing(self):
        return _TerminalTyping()

    @property
    def channel(self):
        return self


async def _run_terminal_loop(bot) -> None:
    """Read lines from stdin and invoke prefix commands directly (--testing only)."""
    import inspect
    import shlex

    await bot.wait_until_ready()
    prefix = os.getenv("PREFIX", "!")
    print(f"[Terminal] Ready. Type commands (e.g. {prefix}leaders)", flush=True)

    loop = asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except Exception:
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            line = prefix + line[1:]
        elif not line.startswith(prefix):
            line = prefix + line
        rest = line[len(prefix) :]
        try:
            parts = shlex.split(rest)
        except ValueError as e:
            print(f"[Terminal] Parse error: {e}", flush=True)
            continue
        if not parts:
            continue
        cmd_name, *raw_args = parts
        cmd = bot.get_command(cmd_name)
        if cmd is None:
            print(f"[Terminal] Unknown command: {cmd_name!r}", flush=True)
            continue

        ctx = _TerminalContext(bot)
        params = list(cmd.clean_params.values())
        call_kwargs = {}
        pos_i = 0
        for param in params:
            if param.kind is inspect.Parameter.KEYWORD_ONLY:
                joined = " ".join(raw_args[pos_i:])
                call_kwargs[param.name] = (
                    joined
                    if joined
                    else (
                        None
                        if param.default is inspect.Parameter.empty
                        else param.default
                    )
                )
                pos_i = len(raw_args)
            elif param.kind is inspect.Parameter.VAR_POSITIONAL:
                break
            else:
                if pos_i < len(raw_args):
                    call_kwargs[param.name] = raw_args[pos_i]
                    pos_i += 1
                elif param.default is not inspect.Parameter.empty:
                    call_kwargs[param.name] = param.default

        try:
            cog = cmd.cog
            if cog:
                await cmd.callback(cog, ctx, **call_kwargs)
            else:
                await cmd.callback(ctx, **call_kwargs)
        except Exception:
            import traceback as _tb

            _tb.print_exc()


# Main loop with reconnection logic
async def main():
    """
    Main function with automatic reconnection handling.
    """
    async with bot:
        while True:
            try:
                # Pick token env var based on whether we run with testing config
                token_name = os.getenv("BOT_TOKEN_ENV", "TOKEN")
                await bot.start(os.getenv(token_name))
            except Exception as e:
                bot.logger.error(f"Bot crashed with error: {e}", exc_info=True)
                bot.logger.info("Attempting to reconnect in 15 seconds...")
                await asyncio.sleep(15)
            finally:
                if not bot.is_closed():
                    await bot.close()


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description="Run the WarEraNL Discord bot")
    parser.add_argument(
        "--testing",
        action="store_true",
        help="Run using testing_config.json and TOKEN_TEST env var",
    )
    parser.add_argument(
        "--config", type=str, help="Path to config JSON to use (overrides --testing)"
    )
    parser.add_argument(
        "--token-env",
        type=str,
        help="Environment variable name that contains the bot token (overrides default)",
    )
    args = parser.parse_args()

    # Determine config path
    if args.config:
        config_path = args.config
        # If the path doesn't exist as-is, try config/<name> as a convenience fallback
        # so `--config testing_config.json` works the same as `--config config/testing_config.json`
        if not Path(config_path).exists():
            candidate = Path("config") / Path(config_path).name
            if candidate.exists():
                config_path = str(candidate)
    else:
        config_path = (
            "config/testing_config.json" if args.testing else "config/config.json"
        )

    # Auto-detect testing mode from config file when --testing flag wasn't given
    if not args.testing:
        try:
            with open(config_path) as _f:
                import json as _json

                if _json.load(_f).get("test"):
                    args.testing = True
        except Exception:
            pass

    if args.testing:
        load_dotenv(".env_test", override=True)
    else:
        load_dotenv()

    # Determine token env var name
    if args.token_env:
        os.environ["BOT_TOKEN_ENV"] = args.token_env
    else:
        # default behaviour: use TOKEN_TEST when testing, otherwise TOKEN
        os.environ["BOT_TOKEN_ENV"] = "TOKEN_TEST" if args.testing else "TOKEN"

    # instantiate bot with chosen config
    bot = DiscordBot(config_path=config_path)
    bot.testing = args.testing
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        bot.logger.info("Bot stopped by user")
