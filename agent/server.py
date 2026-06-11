"""Local Server - FastAPI service for VS Code extension integration."""

import asyncio
import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from agent.core.config import config
from agent.core.engine import AgentEngine, AgentConfig

# Server configuration
SERVER_PORT = config.get("server_port")
SERVER_KEY = config.get("server_key")

app = FastAPI(title="Coding Agent Local Server")

# CORS - allow VS Code extensions and localhost
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"(vscode:|file:|https?://localhost:\d+|https?://127\.0\.0\.1:\d+)",
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_agent_config() -> AgentConfig:
    """Build AgentConfig from unified config."""
    return AgentConfig(
        model=config.get("model"),
        provider=config.get("provider"),
        mode=config.get("mode"),
        verbose=False,
        max_steps=config.get("max_steps"),
        max_tool_retries=config.get("max_tool_retries"),
        mcp_enabled=config.get("mcp_enabled"),
        mcp_config_path=config.get("mcp_config_path"),
    )


def verify_key(request: Request) -> bool:
    """Verify API key if configured."""
    if not SERVER_KEY:
        return True  # No key configured
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:] == SERVER_KEY
    return False


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "server": "coding-agent", "version": "0.1.0"}


@app.get("/completion/stream")
async def completion_stream(
    request: Request,
    task: str = Query(..., description="Task description"),
):
    """SSE stream of completion results."""
    if not verify_key(request):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    config = get_agent_config()
    engine = AgentEngine(config)

    async def event_generator():
        try:
            async for event in engine.run_stream(task):
                # Check if client disconnected
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(event)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            await engine.shutdown()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/completion")
async def completion(request: Request, task: str = Query(...)):
    """Non-streaming completion (for simple requests)."""
    if not verify_key(request):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    config = get_agent_config()
    engine = AgentEngine(config)
    try:
        result = await engine.run(task)
        return {"result": result}
    finally:
        await engine.shutdown()


@app.get("/")
async def root():
    """Server info."""
    return {
        "server": "coding-agent",
        "version": "0.1.0",
        "endpoints": [
            "GET /health",
            "GET /completion/stream?task=...",
            "POST /completion?task=...",
        ],
    }


def run_server(port: int = SERVER_PORT):
    """Run the server with uvicorn."""
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    run_server()