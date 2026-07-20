import os
import asyncio
import logging
from dotenv import load_dotenv
import discord

from bot_instance import bot
from page import run_web_server

# Set up standard logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("Main")

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

async def dynamic_setup_hook():
    """Runs before the bot connects. Dynamically loads cogs and launches page.py server."""
    logger.info("-----------------------------------------------------")
    logger.info("[System] Scanning 'cogs' folder for extensions...")
    
    if os.path.exists("./cogs"):
        for filename in os.listdir("./cogs"):
            if filename.endswith(".py") and not filename.startswith("__"):
                cog_name = f"cogs.{filename[:-3]}"
                try:
                    await bot.load_extension(cog_name)
                    logger.info(f"📦 Successfully loaded cog: {cog_name}")
                except Exception as e:
                    logger.error(f"❌ Failed to load {cog_name}: {e}")
    logger.info("-----------------------------------------------------")
    
    # Launch page.py background web/health-check server
    logger.info("[System] Launching page.py web server...")
    bot.loop.create_task(run_web_server())

bot.setup_hook = dynamic_setup_hook

@bot.event
async def on_ready():
    logger.info(f"✅ {bot.user.name} is online!")
    
    # Sync slash commands globally
    try:
        synced = await bot.tree.sync()
        logger.info(f"✨ Successfully synced {len(synced)} slash commands.")
    except Exception as e:
        logger.error(f"⚠️ Slash command sync failed: {e}")

    # Set initial bot activity
    await bot.change_presence(
        status=discord.Status.idle, 
        activity=discord.Game(name="Watching for updates... 👀")
    )

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.critical("[Error] DISCORD_TOKEN is missing from environment variables!")
    else:
        bot.run(DISCORD_TOKEN)
