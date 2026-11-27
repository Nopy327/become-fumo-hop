import argparse
import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from recipients import resolve_recipients
from roblox import (
    PublicServerTracker,
    ServerUnavailable,
    resolve_server,
    instance_target,
)
from settings import ROOT, credentials, load_config
from store import SessionError, Store
from ui import SessionView, safe_name

LOG = logging.getLogger("fumo")
NO_MENTIONS = discord.AllowedMentions.none()


class HopBot(discord.Client):

    def __init__(self, config, guild_id, channel_id, store):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.presences = True

        super().__init__(
            intents=intents,
            allowed_mentions=NO_MENTIONS
        )

        self.config = config
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.store = store

        self.tree = app_commands.CommandTree(self)
        self.http_session = None
        self.server_tracker = None

        self.last_population_check = 0
        self.refresh_lock = asyncio.Lock()

        self.register_commands()

    def register_commands(self):
        guild = discord.Object(id=self.guild_id)

        @self.tree.command(
            name="hop",
            description="Invite everyone to Become Fumo.",
            guild=guild
        )
        async def hop(interaction: discord.Interaction):
            await self.create_hop(interaction, "bf", None)

        @self.tree.command(
            name="add",
            description="Manually add players to the current hop.",
            guild=guild
        )
        async def add(
            interaction: discord.Interaction,
            player1: discord.Member,
            player2: Optional[discord.Member] = None,
            player3: Optional[discord.Member] = None,
            player4: Optional[discord.Member] = None,
            player5: Optional[discord.Member] = None
        ):
            await self.manage_players(
                interaction,
                [player1, player2, player3, player4, player5],
                True
            )

        @self.tree.command(
            name="remove",
            description="Manually remove players from the current hop.",
            guild=guild
        )
        async def remove(
            interaction: discord.Interaction,
            player1: discord.Member,
            player2: Optional[discord.Member] = None,
            player3: Optional[discord.Member] = None,
            player4: Optional[discord.Member] = None,
            player5: Optional[discord.Member] = None
        ):
            await self.manage_players(
                interaction,
                [player1, player2, player3, player4, player5],
                False
            )

        advanced = app_commands.Group(
            name="advanced",
            description="Choose a game and optional private invitees."
        )

        @advanced.command(
            name="hop",
            description="Invite everyone or selected people."
        )
        @app_commands.describe(
            game="bf = Become Fumo; sbf = Scuffed Become Fumo",
            private_users="Optional names, aliases or mentions"
        )
        @app_commands.rename(private_users="private")
        @app_commands.choices(game=[
            app_commands.Choice(name="bf", value="bf"),
            app_commands.Choice(name="sbf", value="sbf")
        ])
        async def advanced_hop(
            interaction: discord.Interaction,
            game: app_commands.Choice[str],
            private_users: Optional[str] = None
        ):
            await self.create_hop(
                interaction,
                game.value,
                private_users
            )

        self.tree.add_command(advanced, guild=guild)

        @self.tree.error
        async def command_error(interaction, error):
            LOG.error("Command error: %s", error)

            await self.private_error(
                interaction,
                "The command failed. Please try again."
            )

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12)
        )

        self.server_tracker = PublicServerTracker(
            self.http_session
        )

        self.store.expire()
        self.store.invalidate_copies()

        await self.tree.sync(
            guild=discord.Object(id=self.guild_id)
        )

        self.maintenance.start()

    async def on_ready(self):
        LOG.info("Logged in as %s", self.user)

    async def private_error(self, interaction, text):
        if interaction.response.is_done():
            await interaction.followup.send(
                text,
                ephemeral=True,
                allowed_mentions=NO_MENTIONS
            )
        else:
            await interaction.response.send_message(
                text,
                ephemeral=True,
                allowed_mentions=NO_MENTIONS
            )

    def active_session(self):
        self.store.expire()

        sessions = [
            session
            for session in self.store.all()
            if not session["locked"]
            and session["recipients"] is None
            and session["guild_id"] == str(self.guild_id)
            and self.store.copies(session["id"])
        ]

        if not sessions:
            return None

        return max(
            sessions,
            key=lambda session: session["created_at"]
        )

    def is_playing(self, member, session):
        if member.bot:
            return False

        game_key = session["game"]
        game_name = session["game_name"].casefold()
        target = instance_target(session.get("join_url"))

        for activity in member.activities:

            if activity.type != discord.ActivityType.playing:
                continue

            name = str(
                getattr(activity, "name", "") or ""
            ).casefold()

            details = str(
                getattr(activity, "details", "") or ""
            ).casefold()

            state = str(
                getattr(activity, "state", "") or ""
            ).casefold()

            activity_text = " ".join(
                [name, details, state]
            )

            if game_key == "bf":
                if "scuffed become fumo" in activity_text:
                    continue

            if not re.search(
                rf"(?<!\w){re.escape(game_name)}(?!\w)",
                activity_text
            ):
                continue

            for attribute in (
                "url",
                "details_url",
                "state_url"
            ):
                url = getattr(activity, attribute, None)

                if not url:
                    continue

                reported = instance_target(url)

                if target and reported and target != reported:
                    break

            else:
                return True

        return False

    def detect_players(self, session):
        guild = self.get_guild(
            int(session["guild_id"])
        )

        if guild is None or guild.unavailable:
            return None

        recipients = session["recipients"]

        return {
            str(member.id): member.display_name
            for member in guild.members
            if self.is_playing(member, session)
            and (
                recipients is None
                or str(member.id) in recipients
            )
        }

    def update_players(self, session):
        detected = self.detect_players(session)

        if detected is None:
            return self.store.get(session["id"])

        return self.store.set_detected(
            session["id"],
            detected
        )

    async def manage_players(
        self,
        interaction,
        members,
        added
    ):
        try:
            if interaction.guild_id != self.guild_id:
                raise SessionError(
                    "Use this command in the configured server."
                )

            session = self.active_session()

            if session is None:
                raise SessionError(
                    "No active hop. Use /hop first."
                )

            is_host = (
                str(interaction.user.id) == session["host_id"]
            )

            is_admin = (
                interaction.user.guild_permissions.administrator
            )

            if not is_host and not is_admin:
                raise SessionError(
                    "Only the hop host or an administrator "
                    "can manage players."
                )

            names = {
                str(member.id): member.display_name
                for member in members
                if member is not None and not member.bot
            }

            if not names:
                raise SessionError("Select at least one player.")

            self.store.set_overrides(
                session["id"],
                names,
                added
            )

            action = "Added" if added else "Removed"

            await interaction.response.send_message(
                f"{action}: "
                + ", ".join(
                    safe_name(name)
                    for name in names.values()
                ),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS
            )

            await self.refresh(session["id"])

        except SessionError as exc:
            await self.private_error(
                interaction,
                str(exc)
            )

    async def target_channel(self, interaction):
        channel = (
            self.get_channel(self.channel_id)
            or await self.fetch_channel(self.channel_id)
        )

        if (
            not isinstance(channel, discord.TextChannel)
            or channel.guild.id != self.guild_id
        ):
            raise SessionError(
                "The configured hop channel is invalid."
            )

        permissions = channel.permissions_for(
            channel.guild.me
        )

        needed = (
            "view_channel",
            "send_messages",
            "embed_links",
            "attach_files",
            "read_message_history",
            "mention_everyone"
        )

        missing = [
            name for name in needed
            if not getattr(permissions, name)
        ]

        if missing:
            raise SessionError(
                "Missing permissions: "
                + ", ".join(missing)
            )

        return channel

    async def create_hop(
        self,
        interaction,
        game_key,
        private_text
    ):
        await interaction.response.defer(
            ephemeral=True,
            thinking=True
        )

        session = None
        cooldown_key = None
        delivered = 0

        try:
            if (
                interaction.guild_id != self.guild_id
                or not isinstance(
                    interaction.user,
                    discord.Member
                )
            ):
                raise SessionError(
                    "Use this command in the configured server."
                )

            is_private = private_text is not None

            if is_private:
                guild = interaction.guild

                if not guild.chunked:
                    await guild.chunk(cache=True)

                recipients = resolve_recipients(
                    private_text,
                    guild.members,
                    self.config["aliases"],
                    interaction.user,
                    self.config["max_private_recipients"]
                )

                key = (
                    f"private:{self.guild_id}:"
                    f"{interaction.user.id}"
                )

                cooldown = self.config[
                    "private_cooldown_seconds"
                ]

                channel = None

            else:
                recipients = None

                channel = await self.target_channel(
                    interaction
                )

                key = f"public:{self.guild_id}"

                cooldown = self.config[
                    "public_cooldown_seconds"
                ]

            self.store.reserve_cooldown(key, cooldown)
            cooldown_key = key

            game = self.config["games"][game_key]

            try:
                url, note = await resolve_server(
                    game,
                    self.http_session
                )

            except ServerUnavailable as exc:
                url = None
                note = str(exc)

            session = self.store.create(
                self.guild_id,
                game_key,
                game["name"],
                interaction.user.id,
                interaction.user.display_name,
                [
                    str(member.id)
                    for member in recipients
                ] if is_private else None,
                url,
                note
            )

            session = self.update_players(session)

            await self.update_population(session)

            session = self.store.get(session["id"])

            if is_private:
                failed = []

                for member in recipients:
                    try:
                        message = await member.send(
                            view=SessionView(self, session),
                            allowed_mentions=NO_MENTIONS
                        )

                        delivered += 1

                        self.store.add_copy(
                            session["id"],
                            message.channel.id,
                            message.id,
                            session["revision"]
                        )

                    except discord.HTTPException:
                        failed.append(
                            safe_name(member.display_name)
                        )

                result = (
                    f"Private invitation delivered to "
                    f"{delivered} of {len(recipients)} people."
                )

                if failed:
                    result += (
                        "\nCould not DM: "
                        + ", ".join(failed)
                    )

            else:
                message = await channel.send(
                    view=SessionView(self, session),
                    allowed_mentions=discord.AllowedMentions(
                        everyone=True,
                        users=False,
                        roles=False,
                        replied_user=False
                    )
                )

                delivered += 1

                self.store.add_copy(
                    session["id"],
                    message.channel.id,
                    message.id,
                    session["revision"]
                )

                for previous in self.store.all():
                    if (
                        previous["id"] != session["id"]
                        and previous["recipients"] is None
                        and previous["guild_id"] == str(
                            self.guild_id
                        )
                        and not previous["locked"]
                        and previous["created_at"]
                        <= session["created_at"]
                    ):
                        self.store.finish(previous["id"])

                result = "Invitation posted."

            if not delivered:
                self.store.delete(session["id"])
                self.store.release_cooldown(cooldown_key)

            if delivered and not is_private:
                await interaction.delete_original_response()

            else:
                await interaction.edit_original_response(
                    content=result,
                    allowed_mentions=NO_MENTIONS
                )

            if delivered:
                await self.refresh(session["id"])

        except (SessionError, discord.HTTPException) as exc:
            if delivered == 0:
                if session:
                    self.store.delete(session["id"])

                if cooldown_key:
                    self.store.release_cooldown(cooldown_key)

            await interaction.edit_original_response(
                content=str(exc),
                allowed_mentions=NO_MENTIONS
            )

    async def refresh(self, sid):
        async with self.refresh_lock:
            session = self.store.get(sid)

            for copy in self.store.copies(sid):
                if copy["revision"] >= session["revision"]:
                    continue

                try:
                    channel = (
                        self.get_channel(
                            int(copy["channel_id"])
                        )
                        or await self.fetch_channel(
                            int(copy["channel_id"])
                        )
                    )

                    message = channel.get_partial_message(
                        int(copy["message_id"])
                    )

                    await message.edit(
                        content=None,
                        embeds=[],
                        attachments=[],
                        view=SessionView(self, session),
                        allowed_mentions=NO_MENTIONS
                    )

                    self.store.mark_copy(
                        copy["message_id"],
                        session["revision"]
                    )

                except discord.NotFound:
                    self.store.remove_copy(
                        copy["message_id"]
                    )

                except discord.HTTPException as exc:
                    LOG.warning(
                        "Card update deferred: %s",
                        exc
                    )

    async def update_population(self, session):
        if self.server_tracker is None:
            return

        try:
            population = await self.server_tracker.population(
                session["join_url"]
            )

        except ServerUnavailable:
            population = {
                "status": "unavailable",
                "players": None,
                "capacity": None
            }

        self.store.set_population(
            session["id"],
            population
        )

    @tasks.loop(seconds=5)
    async def maintenance(self):
        try:
            self.store.expire()

            for session in self.store.all():
                if (
                    not session["locked"]
                    and self.store.copies(session["id"])
                ):
                    self.update_players(session)

            if (
                time.monotonic()
                - self.last_population_check >= 30
            ):
                for session in self.store.all():
                    if (
                        not session["locked"]
                        and self.store.copies(session["id"])
                    ):
                        await self.update_population(session)

                self.last_population_check = time.monotonic()

            for session in self.store.all():
                if any(
                    copy["revision"] < session["revision"]
                    for copy in self.store.copies(session["id"])
                ):
                    await self.refresh(session["id"])

        except Exception:
            LOG.exception("Maintenance failed.")

    @maintenance.before_loop
    async def before_maintenance(self):
        await self.wait_until_ready()

    async def close(self):
        self.maintenance.cancel()

        if self.http_session:
            await self.http_session.close()

        await super().close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "%(name)s: %(message)s"
        )
    )

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--check",
        action="store_true"
    )

    args = parser.parse_args()
    config = load_config()

    if args.check:
        print("Configuration is valid.")
        return

    token, guild_id, channel_id = credentials()

    directory = Path(
        os.getenv("DATA_DIR", str(ROOT / "data"))
    )

    store = Store(
        directory / "attendance.sqlite3"
    )

    bot = HopBot(
        config,
        guild_id,
        channel_id,
        store
    )

    try:
        bot.run(token, log_handler=None)

    finally:
        store.close()


if __name__ == "__main__":
    main()