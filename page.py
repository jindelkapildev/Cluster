import os
import json
import asyncio
import logging
import websockets
from websockets.http import Headers
from bot_instance import CONNECTED_WORKERS

logger = logging.getLogger("ClusterController")

# Silence noisy websocket protocol logs
logging.getLogger("websockets.server").setLevel(logging.WARNING)
logging.getLogger("websockets.protocol").setLevel(logging.WARNING)

SHARED_SECRET = os.getenv("SHARED_SECRET", "change_me_to_something_secure")
PORT = int(os.getenv("PORT", 8765))

async def health_check_handler(path, request_headers):
    """Fixes UptimeRobot 400 Bad Request error by responding cleanly to HTTP traffic."""
    # Allow WebSocket upgrade requests to pass through
    if "upgrade" in request_headers and request_headers["upgrade"].lower() == "websocket":
        return None

    # Handle standard HTTP GET pings from UptimeRobot, Render, or Web Browsers
    if path in ["/", "/health"]:
        if CONNECTED_WORKERS:
            node_items_html = "".join([
                f'<div class="node-item"><span class="node-name">⚙ {name}</span><span class="node-status">ONLINE</span></div>'
                for name in sorted(CONNECTED_WORKERS.keys())
            ])
        else:
            node_items_html = '<div class="empty">No workers connected currently.</div>'

        html_content = f"""<!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Cluster Control Console</title>
            <style>
                body {{ background-color: #0d1117; color: #c9d1d9; font-family: sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
                .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 30px; max-width: 480px; width: 90%; text-align: center; }}
                .status-badge {{ background: rgba(47, 190, 106, 0.15); color: #56d364; padding: 6px 14px; border-radius: 20px; font-weight: bold; font-size: 0.85em; display: inline-block; margin-bottom: 20px; }}
                h1 {{ color: #ffffff; margin-bottom: 12px; font-size: 1.75em; }}
                p {{ color: #8b949e; margin-bottom: 24px; }}
                .node-list {{ text-align: left; background: #0d1117; padding: 18px; border-radius: 8px; border: 1px solid #21262d; }}
                .node-item {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #161b22; }}
                .node-name {{ color: #58a6ff; font-family: monospace; }}
                .node-status {{ color: #56d364; font-weight: bold; }}
                .empty {{ color: #8b949e; font-style: italic; text-align: center; }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="status-badge">● CONTROL CENTER ONLINE</div>
                <h1>Cluster Control Console</h1>
                <p>Centralized orchestrator for automated systems.</p>
                <div class="node-list">
                    {node_items_html}
                </div>
            </div>
        </body>
        </html>"""

        headers = Headers([
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(html_content.encode("utf-8")))),
            ("Connection", "close")
        ])
        return 200, headers, html_content.encode("utf-8")

    return 404, Headers([("Connection", "close")]), b"404 Not Found"

async def websocket_handler(websocket):
    worker_id = None
    try:
        auth_payload_raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        try:
            auth_data = json.loads(auth_payload_raw)
        except json.JSONDecodeError:
            await websocket.close(1008, "Invalid JSON handshake")
            return
        
        if auth_data.get("secret") != SHARED_SECRET:
            await websocket.close(1008, "Unauthorized")
            return
            
        worker_id = auth_data.get("worker_id")
        if not worker_id:
            await websocket.close(1008, "Invalid worker_id")
            return

        CONNECTED_WORKERS[worker_id] = websocket
        logger.info(f"📡 [Connected] Worker '{worker_id}' is online!")
        await websocket.wait_closed()
        
    except (websockets.exceptions.ConnectionClosedOK, websockets.exceptions.ConnectionClosed):
        pass
    except Exception as e:
        logger.error(f"❌ Websocket error for '{worker_id or 'Unknown'}': {e}")
    finally:
        if worker_id and worker_id in CONNECTED_WORKERS:
            del CONNECTED_WORKERS[worker_id]
            logger.info(f"🔌 Worker '{worker_id}' disconnected.")

async def run_web_server():
    """Starts the WebSocket & HTTP Health Check Server."""
    async with websockets.serve(
        websocket_handler, 
        "0.0.0.0", 
        PORT, 
        process_request=health_check_handler,
        ping_interval=20,
        ping_timeout=20
    ):
        logger.info(f"🔒 Health Check & Websocket Server running on port {PORT}...")
        # Keep server running forever alongside Discord bot
        await asyncio.Future()
