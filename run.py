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
import logging
import discord
from discord import app_commands
import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
from websockets.http import Headers
from dotenv import load_dotenv
import aiohttp  # <-- Added for making async HTTP requests to Render

# Set up clean, standardized logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ClusterController")

# Silence noisy connection handlers & protocol rejections (scans, health checks, etc.)
logging.getLogger("websockets.server").setLevel(logging.WARNING)
logging.getLogger("websockets.protocol").setLevel(logging.WARNING)

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
RENDER_DEPLOY_HOOK_URL = os.getenv("RENDER_DEPLOY_HOOK_URL")  # <-- Added hook URL environment variable

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

# --- FIXED HEALTH CHECK & WEBPAGE FOR RENDER ---
async def health_check_handler(path, request_headers):
    if "upgrade" in request_headers and request_headers["upgrade"].lower() == "websocket":
        return None
    
    if path == "/" or path == "/health":
        if CONNECTED_WORKERS:
            node_items_html = ""
            for name in sorted(CONNECTED_WORKERS.keys()):
                node_items_html += f"""
                <div class="node-item">
                    <span class="node-name">⚙ {name}</span>
                    <span class="node-status">ONLINE</span>
                </div>
                """
        else:
            node_items_html = '<div class="empty">No workers connected currently.</div>'

        html_content = f"""<!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Cluster Control Console</title>
            <style>
                body {{ background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; margin: 0; padding: 0; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
                .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 30px; max-width: 480px; width: 90%; box-shadow: 0 10px 30px rgba(0,0,0,0.6); text-align: center; }}
                .status-badge {{ display: inline-block; background: rgba(47, 190, 106, 0.15); color: #56d364; padding: 6px 14px; border-radius: 20px; font-weight: bold; font-size: 0.85em; margin-bottom: 20px; border: 1px solid rgba(47, 190, 106, 0.3); letter-spacing: 0.5px; }}
                h1 {{ color: #ffffff; margin: 0 0 12px 0; font-size: 1.75em; font-weight: 600; }}
                p {{ color: #8b949e; font-size: 0.95em; line-height: 1.6; margin: 0 0 24px 0; }}
                .node-list {{ text-align: left; background: #0d1117; padding: 18px; border-radius: 8px; border: 1px solid #21262d; }}
                .node-title {{ font-size: 0.8em; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; font-weight: bold; border-bottom: 1px solid #21262d; padding-bottom: 6px; }}
                .node-item {{ display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #161b22; }}
                .node-item:last-child {{ border-bottom: none; }}
                .node-name {{ color: #58a6ff; font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; font-size: 0.9em; }}
                .node-status {{ color: #56d364; font-size: 0.85em; font-weight: bold; }}
                .empty {{ color: #8b949e; font-style: italic; text-align: center; font-size: 0.9em; padding: 12px 0; }}
                .footer {{ margin-top: 25px; font-size: 0.8em; color: #484f58; }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="status-badge">● CONTROL CENTER ONLINE</div>
                <h1>Cluster Control Console</h1>
                <p>Secure centralized orchestrator for database workers and automated systems.</p>
                <div class="node-list">
                    <div class="node-title">Active VPS Instances ({len(CONNECTED_WORKERS)})</div>
                    {node_items_html}
                </div>
                <div class="footer">Central Controller Bot v2.2 • SSL Encryption Active</div>
            </div>
        </body>
        </html>
        """
        
        headers = Headers([
            ("Content-Type", "text/html"),
            ("Connection", "close")
        ])
        return 200, headers, html_content.encode("utf-8")
    
    return None

# --- WEBSOCKET SERVER LOGIC ---
async def websocket_handler(websocket):
    worker_id = None
    try:
        auth_payload_raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        try:
            auth_data = json.loads(auth_payload_raw)
        except json.JSONDecodeError:
            logger.warning("⚠️ [Security] Handshake rejected: Invalid JSON format sent.")
            await websocket.close(1008, "Invalid JSON handshake")
            return
        
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

# --- NEW REDEPLOY COMMAND ---
@bot.tree.command(name="redeploy", description="Trigger a fresh bot redeployment on Render")
async def redeploy_bot(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message("❌ Access Denied.", ephemeral=True)
        return

    if not RENDER_DEPLOY_HOOK_URL:
        await interaction.response.send_message("❌ configuration Error: `RENDER_DEPLOY_HOOK_URL` is missing from the environment.", ephemeral=True)
        return

    # Defer response since communicating with the external API takes a moment
    await interaction.response.defer(ephemeral=True)

    try:
        async with aiohttp.ClientSession() as session:
            # Render deploy hooks require a POST request
            async with session.post(RENDER_DEPLOY_HOOK_URL) as response:
                if response.status in [200, 201, 204]:
                    await interaction.followup.send("🚀 **Redeploy Triggered Successfully!** Render is spinning up the new deployment now.", ephemeral=True)
                    logger.info(f"🔄 Render redeployment triggered by user ID {interaction.user.id}.")
                else:
                    response_text = await response.text()
                    await interaction.followup.send(f"⚠️ Render API returned status `{response.status}`: {response_text[:100]}", ephemeral=True)
    except Exception as e:
        logger.error(f"❌ Failed to communicate with Render Hook API: {e}")
        await interaction.followup.send(f"❌ Failed to reach Render: `{e}`", ephemeral=True)

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
        process_request=health_check_handler,
        ping_interval=20,
        ping_timeout=20
    ):
        logger.info(f"🔒 Secure Node Controller listening on port {PORT}...")
        
        async with bot:
            await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopping Controller gracefully.")
