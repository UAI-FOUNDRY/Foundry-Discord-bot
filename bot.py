import asyncio
import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

# Railway Variables (never commit your .env file or Discord token).
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/data/foundry.db")

IST = ZoneInfo("Asia/Kolkata")
NEWS_INTERVAL_MINUTES = 15
MAX_POSTS_PER_CHECK = 3
DIGEST_HOUR = 20  # 8 PM India time
MAX_DIGEST_ARTICLES = 20

FEEDS = {
    "OpenAI": "https://openai.com/news/rss.xml",
    "Google AI": "https://blog.research.google/feeds/posts/default",
    "Hugging Face": "https://huggingface.co/blog/feed.xml",
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "GitHub": "https://github.blog/feed/",
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("ai-foundry-bot")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def article_id(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def connect_db() -> sqlite3.Connection:
    """Open SQLite and ensure the schema exists on the configured persistent path."""
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_dir, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
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
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    db.commit()
    return db


def get_setting(db: sqlite3.Connection, key: str):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


class FoundryBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        # Fail early if the database path is unavailable.
        db = connect_db()
        db.close()

        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": "AIFoundryDiscordBot/1.0 (+https://github.com/)"},
        )
        await self.tree.sync()
        self.news_loop.start()
        self.digest_loop.start()
        log.info("Slash commands synced; news and digest schedulers started.")

    async def close(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s — AI Foundry bot online.", self.user)

    async def resolve_channel(self, channel_id: int):
        if not channel_id:
            return None
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.error("Could not access channel %s: %s", channel_id, exc)
            return None

    async def on_member_join(self, member: discord.Member):
        channel = await self.resolve_channel(WELCOME_CHANNEL_ID)
        if channel is None:
            log.error("Welcome channel unavailable; check WELCOME_CHANNEL_ID and permissions.")
            return

        embed = discord.Embed(
            title="Welcome to AI Foundry",
            description=(
                "A student-driven community focused on artificial intelligence, "
                "technology, research, and building real-world solutions.\n\n"
                "**Before you get started**\n"
                "Please read the rules in this channel and follow the server guidelines.\n\n"
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
            await channel.send(content=f"Welcome to AI Foundry, {member.mention}!", embed=embed)
        except discord.HTTPException:
            log.exception("Welcome message failed.")

    async def fetch_feed(self, source: str, url: str) -> list[dict]:
        if self.http_session is None:
            return []
        try:
            async with self.http_session.get(url) as response:
                if response.status != 200:
                    log.warning("%s feed returned HTTP %s", source, response.status)
                    return []
                content = await response.read()

            feed = await asyncio.to_thread(feedparser.parse, content)
            if getattr(feed, "bozo", False):
                log.warning("%s feed may be malformed: %s", source, getattr(feed, "bozo_exception", "unknown error"))

            results = []
            for entry in feed.entries[:30]:
                title = str(entry.get("title", "")).strip()
                link = str(entry.get("link", "")).strip()
                if not title or not link.startswith(("https://", "http://")):
                    continue
                results.append({
                    "id": article_id(link),
                    "title": title[:250],
                    "url": link,
                    "source": source,
                    "published": str(entry.get("published", entry.get("updated", "")))[:100],
                })
            if not results:
                log.warning("%s feed returned no usable entries", source)
            return results
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Could not fetch %s: %s", source, exc)
            return []
        except Exception:
            log.exception("Unexpected error while parsing %s feed", source)
            return []

    async def collect_articles(self) -> list[dict]:
        batches = await asyncio.gather(
            *(self.fetch_feed(source, url) for source, url in FEEDS.items()),
            return_exceptions=True,
        )
        articles = []
        for batch in batches:
            if isinstance(batch, Exception):
                log.error("A feed task failed: %s", batch)
            else:
                articles.extend(batch)
        # Deduplicate URLs across feeds while preserving the first occurrence.
        unique = {}
        for item in articles:
            unique.setdefault(item["id"], item)
        return list(unique.values())

    def make_embed(self, article: dict) -> discord.Embed:
        embed = discord.Embed(
            title=article["title"][:256],
            url=article["url"],
            description=f"**Source:** {article['source']}",
            color=discord.Color.blurple(),
        )
        if article.get("published"):
            embed.add_field(name="Published", value=article["published"][:100], inline=False)
        embed.set_footer(text="AI Foundry • Tech Intelligence")
        return embed

    async def run_news_check(self):
        channel = await self.resolve_channel(NEWS_CHANNEL_ID)
        if channel is None:
            log.error("News channel unavailable; check NEWS_CHANNEL_ID and permissions.")
            return

        articles = await self.collect_articles()
        if not articles:
            log.warning("No headlines collected this cycle; will retry next cycle.")
            return

        db = connect_db()
        try:
            # Persist initialization so a Railway restart doesn't skip the next batch.
            initialized = get_setting(db, "initial_scan_complete") == "1"
            if not initialized:
                for item in articles:
                    db.execute(
                        """
                        INSERT OR IGNORE INTO articles
                        (id, title, url, source, published, discovered_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (item["id"], item["title"], item["url"], item["source"], item["published"], utc_now()),
                    )
                set_setting(db, "initial_scan_complete", "1")
                db.commit()
                log.info("Initial scan complete: registered %s existing headlines without posting.", len(articles))
                return

            new_items = []
            for item in articles:
                cursor = db.execute(
                    """
                    INSERT OR IGNORE INTO articles
                    (id, title, url, source, published, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (item["id"], item["title"], item["url"], item["source"], item["published"], utc_now()),
                )
                if cursor.rowcount == 1:
                    new_items.append(item)
            db.commit()

            # Feeds normally list newest first. Post older items first, with a strict per-cycle cap.
            new_items.reverse()
            new_items = new_items[:MAX_POSTS_PER_CHECK]

            for item in new_items:
                try:
                    await channel.send(embed=self.make_embed(item))
                except discord.HTTPException:
                    log.exception("Could not post headline; it will remain unposted in the database.")
                    continue
                db.execute("UPDATE articles SET posted = 1 WHERE id = ?", (item["id"],))
                db.commit()
                log.info("Posted headline: %s", item["title"])
        finally:
            db.close()

    @tasks.loop(minutes=NEWS_INTERVAL_MINUTES)
    async def news_loop(self):
        await self.run_news_check()

    @news_loop.before_loop
    async def before_news_loop(self):
        await self.wait_until_ready()

    @news_loop.error
    async def news_loop_error(self, error: Exception):
        log.error("News loop encountered an error: %s", error, exc_info=error)

    @tasks.loop(minutes=1)
    async def digest_loop(self):
        now = datetime.now(IST)
        if now.hour != DIGEST_HOUR or now.minute != 0:
            return

        today = now.date().isoformat()
        db = connect_db()
        try:
            if get_setting(db, "last_digest_date") == today:
                return

            channel = await self.resolve_channel(NEWS_CHANNEL_ID)
            if channel is None:
                log.error("Digest channel unavailable.")
                return

            rows = db.execute(
                """
                SELECT id, title, url, source
                FROM articles
                WHERE posted = 1 AND digested = 0
                ORDER BY discovered_at ASC
                LIMIT ?
                """,
                (MAX_DIGEST_ARTICLES,),
            ).fetchall()

            if rows:
                lines = []
                for _, title, url, source in rows:
                    line = f"• [{title[:180]}]({url}) — **{source[:60]}**"
                    # Stay comfortably below Discord's embed description limit.
                    if sum(len(existing) + 1 for existing in lines) + len(line) > 3500:
                        break
                    lines.append(line)

                if lines:
                    embed = discord.Embed(
                        title=f"AI Foundry Daily Tech Brief — {now:%d %b %Y}",
                        description="\n".join(lines),
                        color=discord.Color.dark_purple(),
                    )
                    embed.set_footer(text="Daily roundup of headlines • Read the original sources")
                    await channel.send(embed=embed)
                    ids_to_mark = [(row[0],) for row in rows[:len(lines)]]
                    db.executemany("UPDATE articles SET digested = 1 WHERE id = ?", ids_to_mark)

            set_setting(db, "last_digest_date", today)
            db.commit()
            log.info("Daily digest checked for %s; %s article(s) included.", today, len(rows))
        except discord.HTTPException:
            # Don't set the date on a failed send, so the next minute can retry.
            db.rollback()
            log.exception("Daily digest send failed.")
        finally:
            db.close()

    @digest_loop.before_loop
    async def before_digest_loop(self):
        await self.wait_until_ready()

    @digest_loop.error
    async def digest_loop_error(self, error: Exception):
        log.error("Digest loop encountered an error: %s", error, exc_info=error)

    async def post_manual_news(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        items = await self.collect_articles()
        if not items:
            await interaction.followup.send(
                "I couldn't fetch headlines right now. Check the Railway logs.",
                ephemeral=True,
            )
            return
        for item in items[:5]:
            await interaction.followup.send(embed=self.make_embed(item), ephemeral=True)


bot = FoundryBot()


@bot.tree.command(name="ping", description="Check whether Foundry is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)} ms", ephemeral=True)


@bot.tree.command(name="testwelcome", description="Preview the welcome message.")
@app_commands.checks.has_permissions(manage_guild=True)
async def testwelcome(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Welcome to AI Foundry",
        description=(
            "A student-driven community focused on AI, technology, research, "
            "and building real-world solutions.\n\n"
            "**Before you get started**\nPlease read the rules in this channel.\n\n"
            "**Explore the community**\n"
            "• #general-chat\n• #idea-box\n• #project-showcase\n"
            "• #news_updates\n\nLearn. Build. Innovate."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="AI Foundry • Community & Innovation")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@testwelcome.error
async def testwelcome_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        message = "You need Manage Server permission to use this command."
    else:
        log.exception("The /testwelcome command failed", exc_info=error)
        message = "The command failed. Check the bot logs."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="news", description="Preview the latest tech headlines.")
async def news(interaction: discord.Interaction):
    await bot.post_manual_news(interaction)


@news.error
async def news_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("The /news command failed", exc_info=error)
    message = "I couldn't load the headlines. Check the Railway logs."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from environment variables.")
if not WELCOME_CHANNEL_ID or not NEWS_CHANNEL_ID:
    raise RuntimeError("Set WELCOME_CHANNEL_ID and NEWS_CHANNEL_ID in Railway Variables.")


bot.run(TOKEN)
