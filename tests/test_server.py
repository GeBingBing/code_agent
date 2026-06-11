"""Tests for Local Server (agent/server.py)."""

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

# Set testing mode to relax workspace restrictions
os.environ["CODING_AGENT_TESTING"] = "1"
os.environ["AGENT_MODE"] = "bypass"
os.environ["AGENT_SERVER_PORT"] = "18793"  # Use different port for testing


class MockStreamResponse:
    """Simulate OpenAI streaming response."""
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        return iter(self.chunks)


class MockChunk:
    def __init__(self, content: str):
        self.choices = [type('Choice', (), {'delta': type('Delta', (), {'content': content})()})()]


class TestServerAPI:
    """Test the FastAPI server endpoints."""

    @pytest.fixture
    def mock_engine_stream(self, monkeypatch):
        """Mock AgentEngine.run_stream to return test events."""
        async def mock_stream(self, task):
            yield {"type": "step_start", "step": 1, "max_steps": 20}
            yield {"type": "thinking", "content": ""}
            yield {"type": "content", "content": "Hello "}
            yield {"type": "content", "content": "world!"}
            yield {"type": "content_end", "content": "Hello world!"}
            yield {"type": "final", "content": "Hello world!"}

        # Patch before importing server
        monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
        monkeypatch.setenv("DEFAULT_MODEL", "mock")
        monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
        monkeypatch.setenv("AGENT_SERVER_PORT", "18793")

        from agent.core.engine import AgentEngine
        monkeypatch.setattr(AgentEngine, "run_stream", mock_stream)

    @pytest.fixture
    def client(self, mock_engine_stream):
        """Create test client with mocked engine."""
        from agent.server import app
        return TestClient(app)

    def test_health_endpoint(self, client):
        """GET /health should return status."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["server"] == "coding-agent"

    def test_root_endpoint(self, client):
        """GET / should return server info."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["server"] == "coding-agent"
        assert "endpoints" in data

    def test_completion_stream(self, client):
        """GET /completion/stream should return SSE."""
        response = client.get(
            "/completion/stream",
            params={"task": "test task"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

        # Read SSE events
        chunks = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                chunks.append(data)
                if data.get("type") in ("done", "error"):
                    break

        assert len(chunks) >= 1
        # Should have step_start and content events
        types = [c.get("type") for c in chunks]
        assert "step_start" in types

    def test_completion_stream_with_empty_task(self, client):
        """GET /completion/stream without task should fail."""
        response = client.get("/completion/stream")
        assert response.status_code == 422  # Validation error

    def test_completion_stream_sse_format(self, client):
        """SSE events should be properly formatted."""
        response = client.get(
            "/completion/stream",
            params={"task": "hello"}
        )
        lines = list(response.iter_lines())

        # Each line should be "data: {...}"
        for line in lines:
            if line.startswith("data: "):
                data = json.loads(line[6:])
                assert isinstance(data, dict)
                assert "type" in data


class TestServerConfig:
    """Test server configuration."""

    def test_server_defaults(self):
        """Server should have sensible defaults for port and key."""
        from agent.server import SERVER_PORT, SERVER_KEY
        # Default values from .env or fallback
        assert isinstance(SERVER_PORT, int)
        assert SERVER_PORT > 0
        assert isinstance(SERVER_KEY, str)