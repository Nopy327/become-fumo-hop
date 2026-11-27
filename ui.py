import time

import discord


def safe_name(name):
    return discord.utils.escape_markdown(
        discord.utils.escape_mentions(str(name))
    )[:100]


def population_text(session):
    population = session.get("population") or {}
    status = population.get("status")

    if status == "live" and population.get("players") is not None:
        players = population["players"]
        capacity = population.get("capacity")

        count = (
            f"{players} / {capacity}"
            if capacity else str(players)
        )

        return f"-# IN-GAME RIGHT NOW  ·  {count} players"

    if status == "offline":
        return "-# IN-GAME RIGHT NOW  ·  Server offline"

    return None


def avatar_url(bot, session):
    user_id = int(session["host_id"])
    guild = bot.get_guild(int(session["guild_id"]))

    member = guild.get_member(user_id) if guild else None
    user = member or bot.get_user(user_id)

    if user:
        return str(user.display_avatar.url)

    return (
        "https://cdn.discordapp.com/embed/avatars/"
        f"{(user_id >> 22) % 6}.png"
    )


class SessionView(discord.ui.LayoutView):

    def __init__(self, bot, session):
        super().__init__(timeout=None)

        self.bot = bot
        self.sid = session["id"]

        appearance = bot.config["appearance"]

        closed = (
            session["locked"]
            or time.time() >= session["expires_at"]
        )

        private = session["recipients"] is not None

        players = session["attendees"]
        count = len(players)

        # Public notification
        if not private:
            self.add_item(
                discord.ui.TextDisplay("@everyone")
            )

        # Main card
        card = discord.ui.Container(
            accent_color=int(
                appearance["color"].lstrip("#"), 16
            )
        )

        title = appearance["title"].format(
            game=session["game_name"]
        )[:256]

        state_text = "CLOSED" if closed else "OPEN"

        # Title and host profile picture
        card.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    f"# {title}\n-# {state_text}"
                ),
                accessory=discord.ui.Thumbnail(
                    avatar_url(bot, session),
                    description="Host profile picture"
                )
            )
        )

        # Roblox server population
        server_line = population_text(session)

        if server_line:
            card.add_item(
                discord.ui.TextDisplay(server_line)
            )

        # Automatically detected and manually managed players
        names = [
            safe_name(name)
            for name in players.values()
        ]

        rows = [
            " · ".join(names[i:i + 4])
            for i in range(0, len(names), 4)
        ]

        count_line = f"{count:02d} playing from Discord"

        attendee_text = (
            count_line + "\n" + "\n".join(rows)
            if rows
            else count_line + "\nNo players detected."
        )

        card.add_item(
            discord.ui.TextDisplay(attendee_text)
        )

        self.add_item(card)