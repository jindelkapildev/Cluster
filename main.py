import sys
from types import ModuleType

# --- PYTHON 3.13+ COMPATIBILITY PATCH ---
# Modern Python versions (3.13, 3.14) removed the 'audioop' module, causing discord.py to crash on import.
# Since this bot only handles text/slash commands and doesn't use audio/voice channels,
# we securely mock 'audioop' at runtime before importing discord.
if "audioop" not in sys.modules:
    sys.modules["audioop"] = ModuleType("audioop")

import os
import json
import asyncio
import discord
from discord import app_commands
import websockets
from websockets.http import Headers
from dotenv import load_dotenv

# Load secret tokens from our secure .env file
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Safely convert ADMIN_USER_ID to an integer
try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", 0))
except ValueError:
    ADMIN_USER_ID = 0

SHARED_SECRET = os.getenv("SHARED_SECRET", "change_me_to_something_secure")

# Render automatically provides a PORT environment variable.
# If not present, it defaults to 8765.
PORT = int(os.getenv("PORT", 8765))

# Discord Bot Setup
class ControlBot(discord.Client):
    def __init__(self):
        # We need default intents. Presence, Members, and Message Content must be enabled in the developer portal.
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

bot = ControlBot()

# Registry to track live VPS connections
# Key: worker_id (string), Value: websockets.WebSocketServerProtocol
CONNECTED_WORKERS = {}

# --- HEALTH CHECK FOR RENDER ---
# Render Web Services require an HTTP response to verify the app is healthy.
# This interceptor responds with "OK" to HTTP requests, while letting WebSockets pass through.
async def health_check_handler(connection, path, request_headers):
    if path == "/" or path == "/health":
        # Return a standard HTTP 200 OK response
        headers = Headers([
            ("Content-Type", "text/plain"),
            ("Connection", "close")
        ])
        return 200, headers, b"OK - Central Controller is Online"
    return None

# --- WEBSOCKET SERVER LOGIC ---
async def websocket_handler(websocket):
    worker_id = None
    try:
        # 1. Wait for authentication handshake as the very first message
        auth_payload_raw = await websocket.recv()
        auth_data = json.loads(auth_payload_raw)
        
        # Validate Secret Key
        if auth_data.get("secret") != SHARED_SECRET:
            print("[Security] Blocked connection attempt: Invalid Secret Token.")
            await websocket.close(1008, "Unauthorized")
            return
            
        worker_id = auth_data.get("worker_id")
        if not worker_id:
            print("[Security] Blocked connection attempt: Missing worker_id.")
            await websocket.close(1008, "Invalid worker_id")
            return

        # Register connection
        CONNECTED_WORKERS[worker_id] = websocket
        print(f"📡 [Connected] Worker '{worker_id}' is online and verified!")
        
        # Keep connection open until the worker drops off
        await websocket.wait_closed()
        
    except Exception as e:
        print(f"[Error] Connection error with worker '{worker_id}': {e}")
    finally:
        # Clean up registration when connection is closed
        if worker_id and worker_id in CONNECTED_WORKERS:
            del CONNECTED_WORKERS[worker_id]
            print(f"❌ [Disconnected] Worker '{worker_id}' went offline.")

# --- DISCORD SLASH COMMANDS ---

# Helper function to forward commands to the specific VPS client
async def send_vps_command(worker_id: str, command: str):
    if worker_id not in CONNECTED_WORKERS:
        return {"status": "error", "message": f"VPS '{worker_id}' is offline."}
        
    websocket = CONNECTED_WORKERS[worker_id]
    payload = json.dumps({"command": command})
    
    try:
        await websocket.send(payload)
        response_raw = await websocket.recv()
        return json.loads(response_raw)
    except Exception as e:
        return {"status": "error", "message": f"Communication failed: {e}"}

# Autocomplete provider so Discord lists only currently active/online VPS connections
async def vps_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=worker, value=worker)
        for worker in CONNECTED_WORKERS.keys()
        if current.lower() in worker.lower()
    ]

# 1. TEST COMMAND: Ping
@bot.tree.command(name="ping", description="Test if the central bot is alive and responding")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 **Pong!** Bot is online. Latency: `{latency}ms`", ephemeral=True)

# 2. TEST COMMAND: List Connected Workers
@bot.tree.command(name="list-nodes", description="Show all currently connected VPS worker nodes")
async def list_nodes(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message("❌ Access Denied.", ephemeral=True)
        return

    if not CONNECTED_WORKERS:
        await interaction.response.send_message("📡 No workers are currently connected to the controller.", ephemeral=True)
        return

    active_nodes = "\n".join([f"• `{worker}` (Connected)" for worker in CONNECTED_WORKERS.keys()])
    await interaction.response.send_message(f"📡 **Active Database Workers ({len(CONNECTED_WORKERS)}):**\n{active_nodes}", ephemeral=True)

# 3. CONTROLLER COMMAND: Manage Services
@bot.tree.command(name="vps", description="Issue clean, start, stop, or log actions to your active instances")
@app_commands.autocomplete(vps_id=vps_autocomplete)
@app_commands.choices(action=[
    app_commands.Choice(name="Start Service", value="start"),
    app_commands.Choice(name="Stop Service", value="stop"),
    app_commands.Choice(name="Check Status", value="status"),
    app_commands.Choice(name="Fetch Logs", value="logs")
])
async def vps_control(interaction: discord.Interaction, vps_id: str, action: app_commands.Choice[str]):
    # Security Validation: Only the designated Admin User can command these nodes
    if interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message("❌ Access Denied: You do not have permissions to manage these servers.", ephemeral=True)
        return

    # Acknowledge command and keep the connection alive (gives worker time to respond)
    await interaction.response.defer()

    # Dispatch to the specific websocket
    result = await send_vps_command(vps_id, action.value)
    
    if result.get("status") == "success":
        if action.value == "logs":
            log_output = result.get("output", "No logs present.")
            # Format and send long log outputs inside code blocks safely
            escaped_backticks = "```"
            await interaction.followup.send(f"📄 **Logs for {vps_id}:**\n{escaped_backticks}\n{log_output[:1900]}\n{escaped_backticks}")
        else:
            await interaction.followup.send(f"✅ **[{vps_id}]** Action '{action.name}' completed: *{result.get('message')}*")
    else:
        await interaction.followup.send(f"❌ **[{vps_id}]** Command failed: {result.get('message')}")

@bot.event
async def on_ready():
    print(f"🤖 Connected to Discord as {bot.user} (ID: {bot.user.id})")
    try:
        # Sync slash commands globally
        synced = await bot.tree.sync()
        print(f"✨ Successfully synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"⚠️ Slash command synchronization failed: {e}")

# --- SYSTEM INITIALIZATION ---
async def main():
    if not DISCORD_TOKEN or not ADMIN_USER_ID:
        print("❌ Configuration Missing! Please configure your environment variables with your Discord token and Admin ID.")
        return

    # Fire up the inbound WebSocket server on Render's designated PORT
    # Incorporates the HTTP health_check_handler to satisfy Render's web portal checks
    async with websockets.serve(
        websocket_handler, 
        "0.0.0.0", 
        PORT, 
        process_request=health_check_handler
    ):
        print(f"🔒 Secure Node Controller listening on port {PORT}...")
        
        # Run Discord Client alongside websocket task loops
        async with bot:
            await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopping Controller gracefully.")
