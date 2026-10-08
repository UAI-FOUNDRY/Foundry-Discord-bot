import os
import json
import re
import html
import hashlib
from datetime import datetime, timezone

import aiohttp
import feedparser
import discord

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from discord.ext import tasks
from discord import app_commands


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# ------------------------------------------------------------
# PUT YOUR DISCORD CHANNEL IDs HERE
# ------------------------------------------------------------

WELCOME_CHANNEL_ID = 1555578807472365569
NEWS_CHANNEL_ID = 1557853433065635951


# ------------------------------------------------------------
# NEWS SETTINGS
# ------------------------------------------------------------

NEWS_INTERVAL_MINUTES = 30

MAX_NEWS_PER_CHECK = 3

RSS_FEEDS = {
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "MIT Technology Review": "https://www.technologyreview.com/feed/",
}


# ============================================================
# DISCORD INTENTS
# ============================================================

intents = discord.Intents.default()

# Required for detecting new members
intents.members = True


# ============================================================
# BOT
# ============================================================

class FoundryBot(discord.Client):

    def __init__(self):
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)

        self.seen_news = self.load_seen_news()

    # --------------------------------------------------------
    # Load previously posted news
    # --------------------------------------------------------

    def load_seen_news(self):

        try:
            with open("seen_news.json", "r", encoding="utf-8") as file:
                return set(json.load(file))

        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    # --------------------------------------------------------
    # Save posted news
    # --------------------------------------------------------

    def save_seen_news(self):

        # Keep only the newest 1000 items
        recent = list(self.seen_news)[-1000:]

        with open("seen_news.json", "w", encoding="utf-8") as file:
            json.dump(recent, file, indent=2)

    # --------------------------------------------------------
    # Bot is ready
    # --------------------------------------------------------

    async def setup_hook(self):

        await self.tree.sync()

        print("Slash commands synced.")

        self.news_loop.start()

    async def on_ready(self):

        print("--------------------------------")
        print(f"Logged in as: {self.user}")
        print(f"Bot ID: {self.user.id}")
        print("Foundry Bot is ONLINE.")
        print("--------------------------------")

    # --------------------------------------------------------
    # NEW MEMBER WELCOME
    # --------------------------------------------------------

    async def on_member_join(self, member):

        channel = self.get_channel(WELCOME_CHANNEL_ID)

        if channel is None:
            print("Welcome channel not found.")
            return

        embed = self.create_welcome_embed(member)

        try:

            await channel.send(
                content=f"👋 Welcome, {member.mention}!",
                embed=embed
            )

            print(f"Welcomed {member}")

        except discord.Forbidden:

            print("Bot does not have permission to send messages.")

        except Exception as error:

            print(f"Welcome error: {error}")

    # --------------------------------------------------------
    # WELCOME EMBED
    # --------------------------------------------------------

    def create_welcome_embed(self, member):

        embed = discord.Embed(

            title="Welcome to AI Foundry 🚀",

            description=(
                "Welcome to **AI Foundry** — a student-driven community "
                "focused on **Artificial Intelligence, technology, research, "
                "and building real-world solutions.**\n\n"

                "Whether you're here to learn, build, collaborate, "
                "share ideas, or simply explore AI, you're in the right place."
            ),

            color=discord.Color.blurple()
        )

        embed.add_field(

            name="📜 Before you get started",

            value=(
                "Please take a moment to read the rules and "
                "guidelines in this channel."
            ),

            inline=False
        )

        embed.add_field(

            name="💬 Join the community",

            value=(
                "Share your thoughts in `#general-chat`, "
                "discuss technology, and connect with other members."
            ),

            inline=False
        )

        embed.add_field(

            name="💡 Have an idea?",

            value=(
                "Drop it in `#idea-box` or contribute a real-world "
                "problem to `#problem-pool`."
            ),

            inline=False
        )

        embed.add_field(

            name="🚀 See what we're building",

            value=(
                "Check `#project-showcase` to explore projects "
                "created by AI Foundry."
            ),

            inline=False
        )

        embed.add_field(

            name="📰 Stay updated",

            value=(
                "Follow `#news_updates` for important AI and "
                "technology industry updates."
            ),

            inline=False
        )

        embed.set_thumbnail(url=member.display_avatar.url)

        embed.set_footer(
            text="AI Foundry • Learn. Build. Innovate."
        )

        return embed

    # ========================================================
    # NEWS SYSTEM
    # ========================================================

    async def fetch_feed(self, source, url):

        try:

            async with aiohttp.ClientSession() as session:

                async with session.get(

                    url,

                    timeout=aiohttp.ClientTimeout(total=15),

                    headers={
                        "User-Agent": "AI-Foundry-News-Bot/1.0"
                    }

                ) as response:

                    if response.status != 200:

                        print(
                            f"{source}: HTTP {response.status}"
                        )

                        return []

                    data = await response.read()

                    feed = feedparser.parse(data)

                    articles = []

                    for entry in feed.entries[:10]:

                        title = entry.get(
                            "title",
                            "Untitled"
                        ).strip()

                        link = entry.get(
                            "link",
                            ""
                        ).strip()

                        description = entry.get(
                            "summary",
                            entry.get("description", "")
                        )

                        published = entry.get(
                            "published",
                            ""
                        )

                        if not title or not link:
                            continue

                        article_id = hashlib.sha256(
                            link.encode("utf-8")
                        ).hexdigest()

                        articles.append({

                            "id": article_id,

                            "source": source,

                            "title": title,

                            "link": link,

                            "description": description,

                            "published": published
                        })

                    return articles

        except Exception as error:

            print(
                f"Error fetching {source}: {error}"
            )

            return []

    # --------------------------------------------------------
    # Clean HTML from descriptions
    # --------------------------------------------------------

    def clean_description(self, text):

        if not text:
            return "No description available."

        text = html.unescape(text)

        soup = BeautifulSoup(
            text,
            "html.parser"
        )

        text = soup.get_text(
            separator=" ",
            strip=True
        )

        text = re.sub(
            r"\s+",
            " ",
            text
        )

        # Discord embed description limit
        if len(text) > 500:

            text = text[:497] + "..."

        return text

    # --------------------------------------------------------
    # Create news embed
    # --------------------------------------------------------

    def create_news_embed(self, article):

        description = self.clean_description(
            article["description"]
        )

        embed = discord.Embed(

            title=article["title"],

            url=article["link"],

            description=description,

            color=discord.Color.blue()
        )

        embed.add_field(

            name="📰 Source",

            value=article["source"],

            inline=True
        )

        embed.add_field(

            name="🏷️ Category",

            value="Technology / AI",

            inline=True
        )

        embed.set_footer(

            text="AI Foundry • Tech Intelligence"
        )

        return embed

    # --------------------------------------------------------
    # Get news
    # --------------------------------------------------------

    async def get_latest_news(self):

        all_articles = []

        for source, url in RSS_FEEDS.items():

            articles = await self.fetch_feed(
                source,
                url
            )

            all_articles.extend(articles)

        return all_articles

    # --------------------------------------------------------
    # Automatic news loop
    # --------------------------------------------------------

    @tasks.loop(minutes=NEWS_INTERVAL_MINUTES)
    async def news_loop(self):

        print("Checking for new technology news...")

        channel = self.get_channel(
            NEWS_CHANNEL_ID
        )

        if channel is None:

            print(
                "News channel not found."
            )

            return

        articles = await self.get_latest_news()

        new_articles = []

        for article in articles:

            if article["id"] not in self.seen_news:

                new_articles.append(article)

        # ----------------------------------------------------
        # First run protection
        # ----------------------------------------------------

        if not self.seen_news:

            print(
                "First news scan. "
                "Marking existing articles as seen."
            )

            for article in articles:

                self.seen_news.add(
                    article["id"]
                )

            self.save_seen_news()

            return

        # ----------------------------------------------------
        # Send only a few news articles
        # ----------------------------------------------------

        new_articles = new_articles[
            :MAX_NEWS_PER_CHECK
        ]

        for article in new_articles:

            try:

                embed = self.create_news_embed(
                    article
                )

                await channel.send(
                    embed=embed
                )

                self.seen_news.add(
                    article["id"]
                )

            except Exception as error:

                print(
                    f"Error sending news: {error}"
                )

        self.save_seen_news()

    # --------------------------------------------------------
    # Wait until bot is ready before news loop
    # --------------------------------------------------------

    @news_loop.before_loop
    async def before_news_loop(self):

        await self.wait_until_ready()


# ============================================================
# CREATE BOT
# ============================================================

bot = FoundryBot()


# ============================================================
# /PING
# ============================================================

@bot.tree.command(
    name="ping",
    description="Check if Foundry Bot is online."
)
async def ping(interaction: discord.Interaction):

    latency = round(
        bot.latency * 1000
    )

    await interaction.response.send_message(
        f"🏓 Pong! **{latency}ms**"
    )


# ============================================================
# /TESTWELCOME
# ============================================================

@bot.tree.command(
    name="testwelcome",
    description="Test the AI Foundry welcome message."
)
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def testwelcome(
    interaction: discord.Interaction
):

    embed = bot.create_welcome_embed(
        interaction.user
    )

    await interaction.response.send_message(
        content=(
            f"👋 Welcome, "
            f"{interaction.user.mention}!"
        ),
        embed=embed
    )


# ============================================================
# /NEWS
# ============================================================

@bot.tree.command(
    name="news",
    description="Fetch the latest technology news."
)
async def news(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    articles = await bot.get_latest_news()

    if not articles:

        await interaction.followup.send(
            "❌ I couldn't retrieve the latest news right now."
        )

        return

    articles = articles[:5]

    for article in articles:

        embed = bot.create_news_embed(
            article
        )

        await interaction.followup.send(
            embed=embed
        )


# ============================================================
# ERROR HANDLING
# ============================================================

@ping.error
async def ping_error(
    interaction,
    error
):

    print(
        f"Ping command error: {error}"
    )


@testwelcome.error
async def testwelcome_error(
    interaction,
    error
):

    if isinstance(
        error,
        app_commands.errors.MissingPermissions
    ):

        await interaction.response.send_message(
            "❌ You need **Manage Server** permission "
            "to use this command.",
            ephemeral=True
        )

    else:

        print(
            f"Welcome command error: {error}"
        )


# ============================================================
# START BOT
# ============================================================

if not TOKEN:

    raise RuntimeError(
        "DISCORD_TOKEN is missing from your .env file."
    )


bot.run(TOKEN)