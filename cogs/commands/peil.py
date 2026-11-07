"""
This module defines the PeilCog, which provides the /peil command to trigger on-demand refreshes of various cached game data subsystems in the WarEraNL bot.
- /peil [burgers/events/weerstand/alles] [land]
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase, country_autocomplete
from services.country_utils import country_id as cid_of
from services.country_utils import find_country
from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")


class PeilCog(CommandCogBase, name="peil"):
    """Cog for the /peil command, allowing privileged users to trigger on-demand refreshes of various cached game data subsystems."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # /peil                                                                #
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(
        name="peil",
        description="Ververs cache of peil game-data: burgers, mus, events of weerstand.",
    )
    @app_commands.describe(
        onderdeel="Wat wil je peilen?",
        land="Land (voor 'burgers': welk land; voor 'mus': filter op land). Leeg = alles.",
    )
    @app_commands.choices(
        onderdeel=[
            app_commands.Choice(name="burgers", value="burgers"),
            app_commands.Choice(name="mus", value="mus"),
            app_commands.Choice(name="events", value="events"),
            app_commands.Choice(name="weerstand", value="weerstand"),
            app_commands.Choice(name="weekschade", value="weekschade"),
            app_commands.Choice(name="geluk", value="geluk"),
            app_commands.Choice(name="globalluck", value="globalluck"),
            app_commands.Choice(name="slagveld", value="slagveld"),
            app_commands.Choice(name="dagschade", value="dagschade"),
            app_commands.Choice(name="dagschade-backfill", value="dagschade-backfill"),
            app_commands.Choice(name="artikelen", value="artikelen"),
            app_commands.Choice(name="wealth", value="wealth"),
            app_commands.Choice(name="backfill", value="backfill"),
            app_commands.Choice(name="eco_donaties", value="eco_donaties"),
            app_commands.Choice(name="alles", value="alles"),
        ]
    )
    @app_commands.autocomplete(land=country_autocomplete)
    @has_privileged_role()
    async def peil(
        self,
        ctx: Context,
        onderdeel: str,
        land: str | None = None,
    ):
        """Trigger an on-demand data refresh for the chosen subsystem.

        • burgers   — ververs citizen-level cache (NL of opgegeven land, of alle)
        • mus       — ververs MU-lidmaatschappen voor NL
        • events    — voer een event-poll uit (herpost meest recente per categorie)
        • weerstand — voer een verzetspeiling uit
        • alles     — voer alle peilingen uit
        """
        if not self._client or not self._db:
            if getattr(self.bot, "_force_api_offline", False):
                await ctx.send("❌ Api offline.", ephemeral=True)
            else:
                await ctx.send("❌ Diensten niet geïnitialiseerd.", ephemeral=True)
            return

        if hasattr(ctx, "defer"):
            await ctx.defer(ephemeral=True)

        if onderdeel in ("burgers", "alles"):
            await self._peil_burgers(ctx, land)
        if onderdeel in ("mus", "alles"):
            await self._peil_mus(ctx, land)
        if onderdeel in ("events", "alles"):
            await self._peil_events(ctx)
        if onderdeel in ("weerstand", "alles"):
            await self._peil_weerstand(ctx)
        if onderdeel in ("weekschade", "alles"):
            await self._peil_weekschade(ctx)
        if onderdeel in ("geluk", "alles"):
            await self._peil_geluk(ctx)
        if onderdeel in ("globalluck", "alles"):
            await self._peil_globalluck(ctx)
        if onderdeel in ("slagveld", "alles"):
            await self._peil_slagveld(ctx)
        if onderdeel in ("dagschade", "alles"):
            await self._peil_dagschade(ctx)
        if onderdeel == "dagschade-backfill":
            await self._peil_dagschade_backfill(ctx, land)
        if onderdeel in ("wealth", "alles"):
            await self._peil_wealth(ctx)
        if onderdeel == "artikelen":
            await self._peil_artikelen(ctx, land)
        if onderdeel == "backfill":
            await self._peil_backfill(ctx)
        if onderdeel == "eco_donaties":
            await self._peil_eco_donaties(ctx)

    # ------------------------------------------------------------------ #
    # Burgers subsystem                                                    #
    # ------------------------------------------------------------------ #

    async def _peil_burgers(self, ctx: Context, land: str | None) -> None:
        citizen_cache = getattr(self.bot, "_ext_citizen_cache", None)
        if not citizen_cache:
            await ctx.send("❌ Citizen cache niet beschikbaar.", ephemeral=True)
            return

        country_list = await self._fetch_country_list(ctx)
        if country_list is None:
            return

        if land:
            target = find_country(land, country_list)
            if target is None:
                await ctx.send(f"Land `{land}` niet gevonden.", ephemeral=True)
                return
            countries = [target]
        else:
            countries = country_list

        n = len(countries)
        label = f"**{countries[0].get('name', land)}**" if n == 1 else f"**{n}** landen"
        status_msg = await ctx.send(
            f"Burgersniveau-verversing gestart voor {label}…", ephemeral=True
        )

        t_start = time.monotonic()
        total_recorded = 0
        failed: list[str] = []
        for i, c in enumerate(countries, 1):
            cid = cid_of(c)
            name = c.get("name", cid)
            if n > 1:
                await status_msg.edit(
                    content=f"Refreshing citizen levels… ({i}/{n}) **{name}**"
                )
            try:
                recorded = await citizen_cache.refresh_country(
                    cid,
                    name,
                    progress_msg=status_msg if n == 1 else None,
                )
                total_recorded += recorded
                logger.info("peil burgers: %s — %d levels cached", name, recorded)
            except Exception:
                logger.exception("peil burgers: error for %s", name)
                failed.append(name)

        elapsed = time.monotonic() - t_start
        elapsed_str = (
            f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            if elapsed >= 60
            else f"{elapsed:.1f}s"
        )
        if n == 1:
            summary = (
                f"Citizen level cache verversing klaar voor **{countries[0].get('name', land)}** "
                f"— {total_recorded} levels opgeslagen. ⏱ {elapsed_str}"
            )
        else:
            summary = (
                f"Citizen level cache verversing klaar voor **{n}** landen "
                f"— {total_recorded} levels opgeslagen. ⏱ {elapsed_str}"
            )
        if failed:
            summary += f"\nMislukt: {', '.join(failed)}"
        await status_msg.edit(content=summary)

    # ------------------------------------------------------------------ #
    # MUs subsystem                                                        #
    # ------------------------------------------------------------------ #

    async def _peil_mus(self, ctx: Context, land: str | None = None) -> None:
        citizen_cache = getattr(self.bot, "_ext_citizen_cache", None)
        if not citizen_cache:
            await ctx.send("❌ Citizen cache niet beschikbaar.", ephemeral=True)
            return

        # Resolve land → country_id filter
        filter_country_id: str | None = None
        filter_country_name: str | None = None
        if land:
            country_list = await self._fetch_country_list(ctx)
            if country_list is None:
                return
            target = find_country(land, country_list)
            if target is None:
                await ctx.send(f"Land `{land}` niet gevonden.", ephemeral=True)
                return
            filter_country_id = cid_of(target)
            filter_country_name = target.get("name", land)

        status_msg = await ctx.send("🔄 MU-namen vernieuwen…", ephemeral=True)
        try:
            mu_tasks = self.bot.get_cog("mu_tasks")
            if mu_tasks:
                await mu_tasks.refresh_mu_info()
                await mu_tasks.refresh_all_mu_names()

            total_mus = (await self._db.get_all_known_mu_ids(country_id=filter_country_id)) if self._db else []
            total_count = len(total_mus)

            scope_label = f" ({filter_country_name})" if filter_country_name else ""
            await status_msg.edit(
                content=f"🔄 MU sweep gestart — {total_count} MUs laden{scope_label}…"
            )

            last_edit_idx = 0

            async def _progress(done: int, total: int, mu_name: str) -> None:
                nonlocal last_edit_idx
                # Only edit every 25 MUs to avoid Discord rate limits
                if done - last_edit_idx >= 25 or done == total:
                    last_edit_idx = done
                    pct = int(done / total * 100) if total else 0
                    try:
                        await status_msg.edit(
                            content=(
                                f"🔄 MU sweep bezig… {done}/{total} ({pct}%)"
                                f"\nLaatste: **{mu_name}**"
                            )
                        )
                    except Exception:
                        pass

            t_start = time.monotonic()
            mus_tagged, citizens_updated = await citizen_cache.sweep_all_mu_memberships(
                progress_callback=_progress,
                country_id=filter_country_id,
            )
            elapsed = time.monotonic() - t_start
            elapsed_str = (
                f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                if elapsed >= 60
                else f"{elapsed:.1f}s"
            )
            scope_suffix = f" voor **{filter_country_name}**" if filter_country_name else ""
            await status_msg.edit(
                content=(
                    f"✅ MU sweep klaar{scope_suffix} — {mus_tagged}/{total_count} MUs getagt met land, "
                    f"{citizens_updated} burgers bijgewerkt. ⏱ {elapsed_str}"
                )
            )
            logger.info(
                "peil mus: sweep done — %d MUs tagged, %d citizens updated in %s",
                mus_tagged, citizens_updated, elapsed_str,
            )
        except Exception as exc:
            logger.exception("peil mus: sweep failed")
            await status_msg.edit(content=f"❌ MU sweep mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Events subsystem                                                     #
    # ------------------------------------------------------------------ #

    async def _peil_events(self, ctx: Context) -> None:
        event_cog = self.bot.get_cog("event_tasks")
        if not event_cog:
            await ctx.send("❌ Event task cog niet geladen.", ephemeral=True)
            return
        # Clear init keys so the catch-up block fires and re-posts latest per category.
        try:
            await self._db._conn.execute(
                "DELETE FROM poll_state WHERE key LIKE 'event_cat_init_%'"
            )
            await self._db._conn.commit()
        except Exception as exc:
            await ctx.send(f"❌ Kon init-sleutels niet wissen: {exc}", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 Event-peiling gestart…", ephemeral=True)
        try:
            await event_cog.run_event_poll()
            await status_msg.edit(content="✅ Event-peiling voltooid.")
        except Exception as exc:
            logger.exception("peil events: error")
            await status_msg.edit(content=f"❌ Event-peiling mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Weerstand subsystem                                                  #
    # ------------------------------------------------------------------ #

    async def _peil_weerstand(self, ctx: Context) -> None:
        resistance_cog = self.bot.get_cog("resistance")
        if not resistance_cog:
            await ctx.send("❌ Resistance cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 Verzetspeiling gestart…", ephemeral=True)
        try:
            embed = await resistance_cog.build_resistance_embed()
            if embed is None:
                await status_msg.edit(content="ℹ️ Geen bezette buitenlandse regio's gevonden.")
            else:
                await status_msg.edit(content="✅ Verzetspeiling voltooid.")
        except Exception as exc:
            logger.exception("peil weerstand: error")
            await status_msg.edit(content=f"❌ Verzetspeiling mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Weekschade subsystem                                                 #
    # ------------------------------------------------------------------ #

    async def _peil_weekschade(self, ctx: Context) -> None:
        damage_cog = self.bot.get_cog("damage_tasks")
        if not damage_cog:
            await ctx.send("❌ Damage task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 Wekelijkse schade ophalen…", ephemeral=True)
        try:
            updated, zeroed = await damage_cog.run_damage_refresh_once()
            await status_msg.edit(
                content=f"✅ Weekschade verversing klaar — {updated} met schade, {zeroed} op nul."
            )
        except Exception as exc:
            logger.exception("peil weekschade: error")
            await status_msg.edit(content=f"❌ Weekschade verversing mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # NL Luck subsystem                                                    #
    # ------------------------------------------------------------------ #

    async def _peil_geluk(self, ctx: Context) -> None:
        luck_cog = self.bot.get_cog("luck_tasks")
        if not luck_cog:
            await ctx.send("❌ Luck task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 NL geluksranking verversen…", ephemeral=True)
        try:
            await luck_cog.run_luck_refresh()
            total_str = await self._db.get_poll_state("luck_ranking_total")
            total = int(total_str or 0)
            try:
                await status_msg.edit(
                    content=f"✅ NL geluksranking klaar — {total:,} spelers gescoord."
                )
            except discord.HTTPException:
                logger.warning(
                    "peil geluk: interaction token expired, result saved to DB"
                )
        except Exception as exc:
            logger.exception("peil geluk: error")
            try:
                await status_msg.edit(content=f"❌ NL geluksranking mislukt: {exc}")
            except discord.HTTPException:
                logger.warning(
                    "peil geluk: interaction token expired while reporting error"
                )

    # ------------------------------------------------------------------ #
    # Slagveld subsystem                                                   #
    # ------------------------------------------------------------------ #

    async def _peil_slagveld(self, ctx: Context) -> None:
        task_cog = self.bot.get_cog("battle_rankings_task")
        if not task_cog:
            await ctx.send("❌ Battle rankings task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 Slagveld sweep gestart…", ephemeral=True)
        try:
            new_battles, new_hits = await task_cog.run_sweep_once()
            await status_msg.edit(
                content=f"✅ Slagveld sweep klaar — {new_battles} nieuwe gevechten, {new_hits} nieuwe treffers."
            )
        except Exception as exc:
            logger.exception("peil slagveld: error")
            await status_msg.edit(content=f"❌ Slagveld sweep mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Dagelijkse schade subsystem                                          #
    # ------------------------------------------------------------------ #

    async def _peil_dagschade(self, ctx: Context) -> None:
        task_cog = self.bot.get_cog("daily_dmg_task")
        if not task_cog:
            await ctx.send("❌ Daily damage task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 Dagschade sweep gestart…", ephemeral=True)
        try:
            new_battles, new_hits = await task_cog.run_sweep_once()
            await status_msg.edit(
                content=f"✅ Dagschade sweep klaar — {new_battles} nieuwe gevechten, {new_hits} NL treffers opgeslagen."
            )
        except Exception as exc:
            logger.exception("peil dagschade: error")
            await status_msg.edit(content=f"❌ Dagschade sweep mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Dagschade backfill subsystem                                        #
    # ------------------------------------------------------------------ #

    async def _peil_dagschade_backfill(self, ctx: Context, land: str | None) -> None:
        """Backfill daily damage for the last N days (land arg = number of days, default 30)."""
        task_cog = self.bot.get_cog("daily_dmg_task")
        if not task_cog:
            await ctx.send("❌ Daily damage task cog niet geladen.", ephemeral=True)
            return

        # Parse optional days argument from the 'land' parameter
        days_back = 30
        if land:
            try:
                days_back = max(1, min(int(land), 365))
            except ValueError:
                await ctx.send(
                    f"❌ Ongeldige waarde `{land}`. Geef een aantal dagen op (bijv. `30`).",
                    ephemeral=True,
                )
                return

        status_msg = await ctx.send(
            f"🔄 Dagschade backfill gestart voor de laatste **{days_back} dagen**… "
            f"(dit kan lang duren)",
            ephemeral=True,
        )
        try:
            new_battles, new_hits = await task_cog.run_backfill(days_back)
            await status_msg.edit(
                content=(
                    f"✅ Dagschade backfill klaar — {new_battles} gevechten verwerkt, "
                    f"{new_hits} NL treffers opgeslagen (laatste {days_back} dagen)."
                )
            )
        except Exception as exc:
            logger.exception("peil dagschade-backfill: error")
            await status_msg.edit(content=f"❌ Dagschade backfill mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Artikel tips subsystem                                               #
    # ------------------------------------------------------------------ #

    async def _peil_artikelen(self, ctx: Context, land: str | None) -> None:
        citizen_cache = getattr(self.bot, "_ext_citizen_cache", None)
        if not citizen_cache:
            await ctx.send("❌ Citizen cache niet beschikbaar.", ephemeral=True)
            return

        # Resolve optional country filter
        country_id_filter: str | None = None
        label = "alle landen"
        if land:
            country_list = await self._fetch_country_list(ctx)
            if country_list is None:
                return
            target = find_country(land, country_list)
            if target is None:
                await ctx.send(f"Land `{land}` niet gevonden.", ephemeral=True)
                return
            country_id_filter = cid_of(target)
            label = f"**{target.get('name', land)}**"

        status_msg = await ctx.send(
            f"🔄 Artikel tip scan gestart voor {label}… (dit kan lang duren)",
            ephemeral=True,
        )

        last_edit_idx = 0

        async def _progress(done: int, total: int, citizen_name: str) -> None:
            nonlocal last_edit_idx
            if done - last_edit_idx >= 50 or done == total:
                last_edit_idx = done
                pct = int(done / total * 100) if total else 0
                try:
                    await status_msg.edit(
                        content=(
                            f"🔄 Artikel tip scan… {done}/{total} ({pct}%)"
                            f"\nLaatste: **{citizen_name}**"
                        )
                    )
                except Exception:
                    pass

        try:
            t_start = time.monotonic()
            citizens_scanned, tips_stored = await citizen_cache.sweep_article_tips(
                country_id=country_id_filter,
                progress_callback=_progress,
            )
            elapsed = time.monotonic() - t_start
            elapsed_str = (
                f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                if elapsed >= 60
                else f"{elapsed:.1f}s"
            )
            result_msg = (
                f"✅ Artikel tip scan klaar — {citizens_scanned} burgers gescand, "
                f"{tips_stored} nieuwe tip-records opgeslagen. ⏱ {elapsed_str}"
            )
            logger.info(
                "peil artikelen: %d citizens scanned, %d tips stored in %s",
                citizens_scanned, tips_stored, elapsed_str,
            )
            try:
                await status_msg.edit(content=result_msg)
            except discord.HTTPException:
                logger.warning("peil artikelen: interaction token expired, result saved to DB")
        except Exception as exc:
            logger.exception("peil artikelen: error")
            try:
                await status_msg.edit(content=f"❌ Artikel tip scan mislukt: {exc}")
            except discord.HTTPException:
                logger.warning("peil artikelen: interaction token expired while reporting error")

    # ------------------------------------------------------------------ #
    # Backfill subsystem                                                   #
    # ------------------------------------------------------------------ #

    async def _peil_backfill(self, ctx: Context) -> None:
        task_cog = self.bot.get_cog("battle_rankings_task")
        if not task_cog:
            await ctx.send("❌ Battle rankings task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send(
            "🔄 Backfill gestart (3 stappen: land-IDs, landtreffers, MU-treffers)…",
            ephemeral=True,
        )
        try:
            # Step 1: backfill missing attacker/defender country IDs
            await status_msg.edit(content="🔄 Stap 1/3: Battle land-IDs bijvullen…")
            updated = await task_cog.run_country_id_backfill()
            # Step 2: backfill country-level damage hits
            await status_msg.edit(
                content=f"✅ Stap 1/3 klaar: {updated} gevechten bijgewerkt.\n🔄 Stap 2/3: Landtreffers bijvullen…"
            )
            battles_done, hits_added = await task_cog.run_country_backfill()
            # Step 3: backfill MU hits
            await status_msg.edit(
                content=(
                    f"✅ Stap 2/3 klaar: {battles_done} gevechten, {hits_added} landtreffers.\n"
                    "🔄 Stap 3/3: MU-treffers bijvullen… (dit kan lang duren)"
                )
            )
            mu_battles, mu_hits = await task_cog.run_mu_hits_backfill()
            await status_msg.edit(
                content=(
                    f"✅ Backfill klaar:\n"
                    f"• Land-IDs: {updated} gevechten bijgewerkt\n"
                    f"• Landtreffers: {battles_done} gevechten, {hits_added} treffers\n"
                    f"• MU-treffers: {mu_battles} gevechten, {mu_hits} treffers"
                )
            )
        except Exception as exc:
            logger.exception("peil backfill: error")
            await status_msg.edit(content=f"❌ Backfill mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Wealth subsystem                                                     #
    # ------------------------------------------------------------------ #

    async def _peil_wealth(self, ctx: Context) -> None:
        wealth_cog = self.bot.get_cog("wealth_tasks")
        if not wealth_cog:
            await ctx.send("❌ Wealth task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send(
            "🔄 Wealth ranking ophalen… (dit kan een tijdje duren)", ephemeral=True
        )
        try:
            result = await wealth_cog.run_wealth_refresh_once()
            saved = result.get("saved", 0)
            done_text = f"✅ Wealth ranking verversing klaar — {saved} spelers opgeslagen."
            try:
                await status_msg.edit(content=done_text)
            except discord.HTTPException:
                # Interaction token expired (>15 min); post to channel instead
                logger.info("peil wealth: token expired, posting result to channel")
                try:
                    await ctx.channel.send(done_text)
                except Exception:
                    logger.warning("peil wealth: could not post result to channel either")
        except Exception as exc:
            logger.exception("peil wealth: error")
            err_text = f"❌ Wealth ranking mislukt: {exc}"
            try:
                await status_msg.edit(content=err_text)
            except discord.HTTPException:
                try:
                    await ctx.channel.send(err_text)
                except Exception:
                    logger.warning("peil wealth: could not post error to channel")

    # ------------------------------------------------------------------ #
    # Global luck subsystem                                                #
    # ------------------------------------------------------------------ #

    async def _peil_globalluck(self, ctx: Context) -> None:
        cog = self.bot.get_cog("global_luck_tasks")
        if not cog:
            await ctx.send("❌ Global luck task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send(
            "🔄 Globale geluksweep gestart… (dit kan lang duren)", ephemeral=True
        )
        try:
            await cog.run_global_luck_refresh()
            total_str = await self._db.get_poll_state("global_luck_ranking_total")
            total = int(total_str or 0)
            try:
                await status_msg.edit(
                    content=f"✅ Globale gelukranking klaar — {total:,} spelers gescoord."
                )
            except discord.HTTPException:
                logger.warning(
                    "peil globalluck: interaction token expired, result saved to DB"
                )
        except Exception as exc:
            logger.exception("peil globalluck: error")
            try:
                await status_msg.edit(content=f"❌ Globale geluksweep mislukt: {exc}")
            except discord.HTTPException:
                logger.warning(
                    "peil globalluck: interaction token expired while reporting error"
                )


    # ------------------------------------------------------------------ #
    # Eco donations subsystem                                              #
    # ------------------------------------------------------------------ #

    async def _peil_eco_donaties(self, ctx: Context) -> None:
        cog = self.bot.get_cog("eco_donations_poller")
        if not cog:
            await ctx.send("❌ Eco-donatie poller cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send(
            "🔄 Eco-donaties ophalen… (eerste keer kan lang duren)", ephemeral=True
        )
        try:
            inserted = await cog.run_once()
            done_text = f"✅ Eco-donaties gesynchroniseerd — {inserted} nieuwe transacties opgeslagen."
            try:
                await status_msg.edit(content=done_text)
            except discord.HTTPException:
                try:
                    await ctx.channel.send(done_text)
                except Exception:
                    logger.warning("peil eco_donaties: could not post result")
        except Exception as exc:
            logger.exception("peil eco_donaties: error")
            err_text = f"❌ Eco-donaties sync mislukt: {exc}"
            try:
                await status_msg.edit(content=err_text)
            except discord.HTTPException:
                try:
                    await ctx.channel.send(err_text)
                except Exception:
                    logger.warning("peil eco_donaties: could not post error")


async def setup(bot) -> None:
    """Add the PeilCog to the bot."""
    await bot.add_cog(PeilCog(bot))
