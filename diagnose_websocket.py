#!/usr/bin/env python3
"""Diagnose WebSocket connection issues with both Authorization and query parameter methods."""
import asyncio
import json
import logging
import os
import sys

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("diagnose")

from omni.config import Config
from omni.upstream import sanitize_workspace_id

async def test_authorization_header():
    """Test with Authorization header (old method)."""
    import websockets

    cfg = Config.from_env()
    log.info("Testing Authorization header method...")
    log.info(f"  API Key: {cfg.api_key[:10] if cfg.api_key else 'NOT SET'}...")
    log.info(f"  Workspace ID: {cfg.workspace_id or 'NOT SET'}")
    log.info(f"  Model: {cfg.realtime_model}")

    workspace_id = sanitize_workspace_id(cfg.workspace_id) if cfg.workspace_id else ""
    if workspace_id:
        url = f"wss://{workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model={cfg.realtime_model}"
    else:
        url = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={cfg.realtime_model}"

    log.info(f"  URL: {url}")

    try:
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        log.debug(f"  Headers: {headers}")
        ws = await asyncio.wait_for(
            websockets.connect(url, additional_headers=headers, open_timeout=10.0),
            timeout=15.0
        )
        log.info("✅ Authorization header method: CONNECTED OK")
        await ws.close()
        return True
    except Exception as exc:
        log.error(f"❌ Authorization header method: {type(exc).__name__}: {exc}")
        return False

async def test_query_parameter():
    """Test with query parameter (new method)."""
    import websockets

    cfg = Config.from_env()
    log.info("Testing query parameter method...")
    log.info(f"  API Key: {cfg.api_key[:10] if cfg.api_key else 'NOT SET'}...")
    log.info(f"  Workspace ID: {cfg.workspace_id or 'NOT SET'}")
    log.info(f"  Model: {cfg.realtime_model}")

    workspace_id = sanitize_workspace_id(cfg.workspace_id) if cfg.workspace_id else ""
    if workspace_id:
        url = f"wss://{workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model={cfg.realtime_model}"
    else:
        url = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={cfg.realtime_model}"

    if cfg.api_key:
        url += f"&key={cfg.api_key}"

    log.info(f"  URL: {url}")

    try:
        ws = await asyncio.wait_for(
            websockets.connect(url, open_timeout=10.0),
            timeout=15.0
        )
        log.info("✅ Query parameter method: CONNECTED OK")
        await ws.close()
        return True
    except Exception as exc:
        log.error(f"❌ Query parameter method: {type(exc).__name__}: {exc}")
        return False

async def main():
    log.info("=== WebSocket Connection Diagnostic ===")
    log.info(f"Python: {sys.version}")

    try:
        import websockets
        log.info(f"websockets version: {websockets.__version__}")
    except Exception as e:
        log.warning(f"Could not get websockets version: {e}")

    log.info(f"HTTPS_PROXY: {os.environ.get('HTTPS_PROXY', 'NOT SET')}")
    log.info("")

    auth_ok = await test_authorization_header()
    log.info("")
    query_ok = await test_query_parameter()
    log.info("")

    log.info("=== Summary ===")
    log.info(f"Authorization header: {'✅ OK' if auth_ok else '❌ FAILED'}")
    log.info(f"Query parameter: {'✅ OK' if query_ok else '❌ FAILED'}")

    if auth_ok and not query_ok:
        log.warning("⚠️ Authorization header works, but query parameter fails!")
        log.warning("Current code uses query parameter — should revert to Authorization header.")
    elif query_ok and not auth_ok:
        log.warning("⚠️ Query parameter works, but Authorization header fails.")
        log.warning("Current code is correct.")
    elif auth_ok and query_ok:
        log.info("Both methods work — issue may be elsewhere.")
    else:
        log.error("Both methods failed — check API key, workspace ID, and network configuration.")

if __name__ == "__main__":
    asyncio.run(main())
