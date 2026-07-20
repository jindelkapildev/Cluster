import os
import json
import asyncio
import logging
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from bot_instance import CONNECTED_WORKERS

logger = logging.getLogger("ClusterController")

try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", 0))
except ValueError:
    ADMIN_USER_ID = 0

RENDER_DEPLOY_HOOK_URL = os.getenv("RENDER_DEPLOY_HOOK_URL")

class VPSControlCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def send_vps_command(self, worker_id: str, command: str):
        if worker_id not in CONNECTED_WORKERS:
            return {"status": "error", "message": f"VPS '{worker_id}' is currently offline."}
            
        websocket = CONNECTED_WORKERS[worker_id]
        payload = json.dumps({"command": command})
        
        try:
            await websocket.send(payload)
            response_raw = await asyncio.wait_for(websocket.recv(), timeout=30.0)
            return json.loads(response_raw)
        except asyncio.TimeoutError:
            return {"status": "error", "message": "Worker timed out responding."}
        except (ConnectionClosed, ConnectionClosedOK):
            return {"status": "error", "message": "Connection to worker was terminated."}
        except Exception as e:
            return {"status": "error", "message": f"Communication failed: {e}"}

    async def vps_autocomplete(self, interaction: discord.Interaction, current: str):
        dead_workers = [worker for worker, ws in list(CONNECTED_WORKERS.items()) if ws.closed]
        for dw in dead_workers:
            CONNECTED_WORKERS.pop(dw, None)

        return [
            app_commands.Choice(name=worker, value=worker)
            for worker in CONNECTED_WORKERS.keys()
            if current.lower() in worker.lower()
        ]

    @app_commands.command(name="ping", description="Test if the central bot is alive")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 **Pong!** Bot is online. Latency: `{latency}ms`", ephemeral=True)

    @app_commands.command(name="list-nodes", description="Show all connected VPS worker nodes")
    async def list_nodes(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID:
            await interaction.response.send_message("❌ Access Denied.", ephemeral=True)
            return

        if not CONNECTED_WORKERS:
            await interaction.response.send_message("📡 No workers are currently connected.", ephemeral=True)
            return

        active_nodes = "\n".join([f"• `{worker}` (Connected)" for worker in CONNECTED_WORKERS.keys()])
        await interaction.response.send_message(f"📡 **Active Workers ({len(CONNECTED_WORKERS)}):**\n{active_nodes}", ephemeral=True)

    @app_commands.command(name="vps", description="Manage active VPS instances")
    @app_commands.autocomplete(vps_id=vps_autocomplete)
    @app_commands.choices(action=[
        app_commands.Choice(name="Start Service", value="start"),
        app_commands.Choice(name="Stop Service", value="stop"),
        app_commands.Choice(name="Check Status", value="status"),
        app_commands.Choice(name="Fetch Logs", value="logs")
    ])
    async def vps_control(self, interaction: discord.Interaction, vps_id: str, action: app_commands.Choice[str]):
        if interaction.user.id != ADMIN_USER_ID:
            await interaction.response.send_message("❌ Access Denied.", ephemeral=True)
            return

        await interaction.response.defer()
        result = await self.send_vps_command(vps_id, action.value)
        
        if result.get("status") == "success":
            if action.value == "logs":
                log_output = result.get("output", "No logs present.")
                await interaction.followup.send(f"📄 **Logs for {vps_id}:**\n```\n{log_output[:1900]}\n```")
            else:
                await interaction.followup.send(f"✅ **[{vps_id}]** '{action.name}' completed: *{result.get('message')}*")
        else:
            await interaction.followup.send(f"❌ **[{vps_id}]** Command failed: {result.get('message')}")

    @app_commands.command(name="redeploy", description="Trigger fresh redeploy on Render")
    async def redeploy(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID:
            await interaction.response.send_message("❌ Access Denied.", ephemeral=True)
            return

        if not RENDER_DEPLOY_HOOK_URL:
            await interaction.response.send_message("❌ `RENDER_DEPLOY_HOOK_URL` is missing.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(RENDER_DEPLOY_HOOK_URL) as response:
                    if response.status in [200, 201, 204]:
                        await interaction.followup.send("🚀 **Redeploy Triggered Successfully!**", ephemeral=True)
                    else:
                        text = await response.text()
                        await interaction.followup.send(f"⚠️ Render API returned status `{response.status}`: {text[:100]}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to reach Render: `{e}`", ephemeral=True)

async def setup(bot):
    await bot.add_cog(VPSControlCog(bot))
