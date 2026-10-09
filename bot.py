
import os
import asyncio
import sqlite3
import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
import feedparser

from dotenv import load_dotenv
from discord import app_commands
from discord.ext import tasks


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", "0"))

DB_PATH = os.getenv("DB_PATH", "/data/foundry.db")
IST = ZoneInfo("Asia/Kolkata")

NEWS_INTERVAL_MINUTES = 15
MAX_POSTS_PER_CHECK = 3
DIGEST_HOUR = 20  # 8 PM India time

FEEDS = {
    "OpenAI": "https://openai.com/news/rss.xml",
    "Google AI": "https://blog.research.google/feeds/posts/default",
    "Hugging Face": "https://huggingface.co/blog/feed.xml",
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "GitHub": "https://github.blog/feed/",
}


def connect_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            source TEXT NOT NULL,
            published TEXT,
            discovered_at TEXT NOT NULL,
            posted INTEGER NOT NULL DEFAULT 0,
            digested INTEGER NOT NULL DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    db.commit()
    return db


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def article_id(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class FoundryBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.first_scan_done = False

    async def setup_hook(self):
        connect_db().close()
        await self.tree.sync()
        self.news_loop.start()
        self.digest_loop.start()
        print("Slash commands synced. News and digest schedulers started.")

    async def on_ready(self):
        print(f"Logged in as {self.user} — AI Foundry bot online.")

    async def on_member_join(self, member):
        channel = self.get_channel(WELCOME_CHANNEL_ID)
        if channel is None:
            print("Welcome channel not found; check WELCOME_CHANNEL_ID.")
            return

        embed = discord.Embed(
            title="Welcome to AI Foundry",
            description=(
                "A student-driven community focused on artificial "
                "intelligence, technology, research, and building "
                "real-world solutions.\n\n"
                "**Before you get started**\n"
                "Please read the rules in this channel and follow the "
                "server guidelines.\n\n"
                "**Explore the community**\n"
                "• Join conversations in #general-chat\n"
                "• Share ideas in #idea-box\n"
                "• Explore #project-showcase\n"
                "• Follow #news_updates for technology headlines\n\n"
                "Learn. Build. Innovate."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="AI Foundry • Community & Innovation")

        try:
            await channel.send(
                content=f"Welcome to AI Foundry, {member.mention}!",
                embed=embed,
            )
        except discord.HTTPException as exc:
            print(f"Welcome message failed: {exc}")

    async def fetch_feed(self, session, source, url):
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    print(f"{source} feed returned HTTP {response.status}")
                    return []

                content = await response.read()

            feed = await asyncio.to_thread(feedparser.parse, content)
            results = []

            for entry in feed.entries[:20]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                if not title or not link.startswith(("https://", "http://")):
                    continue

                results.append({
                    "id": article_id(link),
                    "title": title[:250],
                    "url": link,
                    "source": source,
                    "published": entry.get("published", ""),
                })

            return results

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(f"Could not fetch {source}: {exc}")
            return []

    async def collect_articles(self):
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {"User-Agent": "AIFoundryDiscordBot/1.0"}

        async with aiohttp.ClientSession(
            timeout=timeout, headers=headers
        ) as session:
            batches = await asyncio.gather(
                *[
                    self.fetch_feed(session, source, url)
                    for source, url in FEEDS.items()
                ]
            )

        return [item for batch in batches for item in batch]

    def make_embed(self, article):
        embed = discord.Embed(
            title=article["title"],
            url=article["url"],
            description=f"**Source:** {article['source']}",
            color=discord.Color.blurple(),
        )
        if article.get("published"):
            embed.add_field(
                name="Published",
                value=article["published"][:100],
                inline=False,
            )
        embed.set_footer(text="AI Foundry • Tech Intelligence")
        return embed

    async def run_news_check(self):
        channel = self.get_channel(NEWS_CHANNEL_ID)
        if channel is None:
            print("News channel not found; check NEWS_CHANNEL_ID.")
            return

        articles = await self.collect_articles()
        db = connect_db()

        try:
            # On first run, register existing stories without flooding Discord.
            if not self.first_scan_done:
                for item in articles:
                    db.execute(
                        """INSERT OR IGNORE INTO articles
                        (id, title, url, source, published, discovered_at)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            item["id"], item["title"], item["url"],
                            item["source"], item["published"], utc_now(),
                        ),
                    )
                db.commit()
                self.first_scan_done = True
                print(f"Initial scan complete: {len(articles)} articles checked.")
                return

            new_items = []
            for item in articles:
                cursor = db.execute(
                    """INSERT OR IGNORE INTO articles
                    (id, title, url, source, published, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        item["id"], item["title"], item["url"],
                        item["source"], item["published"], utc_now(),
                    ),
                )
                if cursor.rowcount == 1:
                    new_items.append(item)

            db.commit()

            # Post oldest first when a feed returns multiple new items.
            new_items = list(reversed(new_items))[:MAX_POSTS_PER_CHECK]

            for item in new_items:
                try:
                    await channel.send(embed=self.make_embed(item))
                    db.execute(
                        "UPDATE articles SET posted = 1 WHERE id = ?",
                        (item["id"],),
                    )
                    db.commit()
                    print(f"Posted headline: {item['title']}")
                except discord.HTTPException as exc:
                    print(f"Could not post headline: {exc}")

        finally:
            db.close()

    @tasks.loop(minutes=NEWS_INTERVAL_MINUTES)
    async def news_loop(self):
        await self.run_news_check()

    @news_loop.before_loop
    async def before_news_loop(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def digest_loop(self):
        now = datetime.now(IST)
        if now.hour != DIGEST_HOUR or now.minute != 0:
            return

        today = now.date().isoformat()
        db = connect_db()

        try:
            row = db.execute(
                "SELECT value FROM settings WHERE key = 'last_digest_date'"
            ).fetchone()
            if row and row[0] == today:
                return

            channel = self.get_channel(NEWS_CHANNEL_ID)
            if channel is None:
                print("Digest channel not found.")
                return

            rows = db.execute("""
                SELECT id, title, url, source
                FROM articles
                WHERE posted = 1 AND digested = 0
                ORDER BY discovered_at ASC
                LIMIT 20
            """).fetchall()

            if rows:
                embed = discord.Embed(
                    title=f"AI Foundry Daily Tech Brief — {now:%d %b %Y}",
                    description="\n".join(
                        f"• [{title}]({url}) — **{source}**"
                        for _, title, url, source in rows
                    )[:4000],
                    color=discord.Color.dark_purple(),
                )
                embed.set_footer(
                    text="A daily roundup of headlines • Read the original sources"
                )

                await channel.send(embed=embed)
                db.executemany(
                    "UPDATE articles SET digested = 1 WHERE id = ?",
                    [(row[0],) for row in rows],
                )

            db.execute(
                """INSERT INTO settings(key, value)
                VALUES ('last_digest_date', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (today,),
            )
            db.commit()
            print(f"Daily digest checked for {today}.")

        except discord.HTTPException as exc:
            print(f"Daily digest failed: {exc}")
        finally:
            db.close()

    @digest_loop.before_loop
    async def before_digest_loop(self):
        await self.wait_until_ready()

    async def post_manual_news(self, interaction):
        await interaction.response.defer(ephemeral=True)
        items = await self.collect_articles()
        if not items:
            await interaction.followup.send(
                "I couldn't fetch headlines right now. Check the Railway logs.",
                ephemeral=True,
            )
            return

        # This command previews the newest feed entries; it doesn't repost them
        # to the public channel or mark them as published.
        for item in items[:5]:
            await interaction.followup.send(
                embed=self.make_embed(item), ephemeral=True
            )


bot = FoundryBot()


@bot.tree.command(name="ping", description="Check whether Foundry is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Pong! {round(bot.latency * 1000)} ms", ephemeral=True
    )


@bot.tree.command(name="testwelcome", description="Preview the welcome message.")
@app_commands.checks.has_permissions(manage_guild=True)
async def testwelcome(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Welcome to AI Foundry",
        description=(
            "A student-driven community focused on AI, technology, research, "
            "and building real-world solutions.\n\n"
            "**Before you get started**\n"
            "Please read the rules in this channel.\n\n"
            "**Explore the community**\n"
            "• #general-chat\n• #idea-box\n• #project-showcase\n"
            "• #news_updates\n\nLearn. Build. Innovate."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="AI Foundry • Community & Innovation")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@testwelcome.error
async def testwelcome_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    message = (
        "You need Manage Server permission to use this command."
        if isinstance(error, app_commands.errors.MissingPermissions)
        else "The command failed. Check the bot logs."
    )
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="news", description="Preview the latest tech headlines.")
async def news(interaction: discord.Interaction):
    await bot.post_manual_news(interaction)


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from environment variables.")
if not WELCOME_CHANNEL_ID or not NEWS_CHANNEL_ID:
    raise RuntimeError(
        "Set WELCOME_CHANNEL_ID and NEWS_CHANNEL_ID in Railway Variables."
    )

bot.run(TOKEN)
