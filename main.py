import sys
from types import ModuleType

# --- PYTHON 3.13+ COMPATIBILITY PATCH ---
if "audioop" not in sys.modules:
    sys.modules["audioop"] = ModuleType("audioop")

import os
import json
import asyncio
import logging
import discord
from discord import app_commands
import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
from websockets.http import Headers
from dotenv import load_dotenv

# Set up clean, standardized logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ClusterController")

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", 0))
except ValueError:
    ADMIN_USER_ID = 0

SHARED_SECRET = os.getenv("SHARED_SECRET", "change_me_to_something_secure")
PORT = int(os.getenv("PORT", 8765))

# Discord Bot Setup
class ControlBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

bot = ControlBot()
CONNECTED_WORKERS = {}

# --- FIXED HEALTH CHECK FOR RENDER ---
# Distinguishes between Render's HTTP health check and actual WebSocket upgrades.
async def health_check_handler(path, request_headers):
    # If the incoming request has the standard WebSocket upgrade headers,
    # return None to let the connection proceed to the WebSocket server handshake!
    if "upgrade" in request_headers and request_headers["upgrade"].lower() == "websocket":
        return None
    
    # If it's a plain HTTP request (like Render pinging `/` or `/health`), return 200 OK
    if path == "/" or path == "/health":
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
        # Wait for authentication handshake with a 10-second timeout
        auth_payload_raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        
        try:
            auth_data = json.loads(auth_payload_raw)
        except json.JSONDecodeError:
            logger.warning("⚠️ [Security] Handshake rejected: Invalid JSON format sent.")
            await websocket.close(1008, "Invalid JSON handshake")
            return
        
        # Validate Secret Key
        if auth_data.get("secret") != SHARED_SECRET:
            logger.warning("⚠️ [Security] Unauthorized connection attempt blocked.")
            await websocket.close(1008, "Unauthorized")
            return
            
        worker_id = auth_data.get("worker_id")
        if not worker_id:
            logger.warning("⚠️ [Security] Rejected connection: missing 'worker_id'.")
            await websocket.close(1008, "Invalid worker_id")
            return

        CONNECTED_WORKERS[worker_id] = websocket
        logger.info(f"📡 [Connected] Worker '{worker_id}' is online and verified!")
        
        # Keep connection open until client closes it
        await websocket.wait_closed()
        
    except asyncio.TimeoutError:
        logger.warning("⚠️ [Timeout] Incoming connection timed out during handshake.")
        try:
            await websocket.close(1008, "Handshake timeout")
        except Exception:
            pass
    except (ConnectionClosedOK, ConnectionClosed):
        pass
    except Exception as e:
        logger.error(f"❌ [Error] Unexpected websocket exception for '{worker_id or 'Unknown'}': {e}")
    finally:
        if worker_id and worker_id in CONNECTED_WORKERS:
            del CONNECTED_WORKERS[worker_id]
            logger.info(f"🔌 [Disconnected] Worker '{worker_id}' went offline.")

# --- DISCORD SLASH COMMANDS ---
async def send_vps_command(worker_id: str, command: str):
    if worker_id not in CONNECTED_WORKERS:
        return {"status": "error", "message": f"VPS '{worker_id}' is currently offline."}
        
    websocket = CONNECTED_WORKERS[worker_id]
    payload = json.dumps({"command": command})
    
    try:
        await websocket.send(payload)
        response_raw = await asyncio.wait_for(websocket.recv(), timeout=30.0)
        return json.loads(response_raw)
    except asyncio.TimeoutError:
        logger.error(f"⏱️ [Timeout] Worker '{worker_id}' timed out responding to command: '{command}'")
        return {"status": "error", "message": "Worker timed out trying to respond."}
    except (ConnectionClosed, ConnectionClosedOK):
        logger.warning(f"🔌 [Connection Lost] Connection to '{worker_id}' was terminated.")
        return {"status": "error", "message": "Connection to worker was terminated."}
    except Exception as e:
        logger.error(f"❌ [Error] Command delivery failed to '{worker_id}': {e}")
        return {"status": "error", "message": f"Communication failed: {e}"}

async def vps_autocomplete(interaction: discord.Interaction, current: str):
    dead_workers = []
    for worker, ws in list(CONNECTED_WORKERS.items()):
        if ws.closed:
            dead_workers.append(worker)
    for dw in dead_workers:
        CONNECTED_WORKERS.pop(dw, None)
        logger.info(f"🧹 [Auto-Clean] Removed stale socket client: '{dw}'")

    return [
        app_commands.Choice(name=worker, value=worker)
        for worker in CONNECTED_WORKERS.keys()
        if current.lower() in worker.lower()
    ]

@bot.tree.command(name="ping", description="Test if the central bot is alive and responding")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 **Pong!** Bot is online. Latency: `{latency}ms`", ephemeral=True)

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

@bot.tree.command(name="vps", description="Issue clean, start, stop, or log actions to your active instances")
@app_commands.autocomplete(vps_id=vps_autocomplete)
@app_commands.choices(action=[
    app_commands.Choice(name="Start Service", value="start"),
    app_commands.Choice(name="Stop Service", value="stop"),
    app_commands.Choice(name="Check Status", value="status"),
    app_commands.Choice(name="Fetch Logs", value="logs")
])
async def vps_control(interaction: discord.Interaction, vps_id: str, action: app_commands.Choice[str]):
    if interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message("❌ Access Denied: You do not have permissions to manage these servers.", ephemeral=True)
        return

    await interaction.response.defer()
    result = await send_vps_command(vps_id, action.value)
    
    if result.get("status") == "success":
        if action.value == "logs":
            log_output = result.get("output", "No logs present.")
            escaped_backticks = "```"
            await interaction.followup.send(f"📄 **Logs for {vps_id}:**\n{escaped_backticks}\n{log_output[:1900]}\n{escaped_backticks}")
        else:
            await interaction.followup.send(f"✅ **[{vps_id}]** Action '{action.name}' completed: *{result.get('message')}*")
    else:
        await interaction.followup.send(f"❌ **[{vps_id}]** Command failed: {result.get('message')}")

@bot.event
async def on_ready():
    logger.info(f"🤖 Connected to Discord as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        logger.info(f"✨ Successfully synced {len(synced)} slash commands.")
    except Exception as e:
        logger.error(f"⚠️ Slash command synchronization failed: {e}")

# --- SYSTEM INITIALIZATION ---
async def main():
    if not DISCORD_TOKEN or not ADMIN_USER_ID:
        logger.error("❌ Configuration Missing! Please configure your environment variables.")
        return

    async with websockets.serve(
        websocket_handler, 
        "0.0.0.0", 
        PORT, 
        process_request=health_check_handler
    ):
        logger.info(f"🔒 Secure Node Controller listening on port {PORT}...")
        
        async with bot:
            await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopping Controller gracefully.")
