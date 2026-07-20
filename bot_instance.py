import os
import sys
from types import ModuleType
import discord
from discord.ext import commands

# --- PYTHON 3.13+ COMPATIBILITY PATCH ---
if "audioop" not in sys.modules:
    sys.modules["audioop"] = ModuleType("audioop")

# Define multi-prefixes
prefixes = ["!", "?", "$", ";", "%"]

# Setup Gateway Intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

# Initialize Bot Instance
bot = commands.Bot(
    command_prefix=prefixes,
    intents=intents
)

# Global dictionary to track online WebSocket nodes/workers
CONNECTED_WORKERS = {}
