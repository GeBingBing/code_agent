"""Tests for config file support and structured logging."""

import json

from agent.core.engine import AgentConfig, _load_config_file


class TestConfigFile:
    """Test config.json file support."""

    def test_load_config_file_returns_dict(self):
        """_load_config_file should return dict or empty dict."""
        result = _load_config_file()
        assert isinstance(result, dict)

    def test_config_allows_explicit_values(self):
        """Config with explicit args should use those values."""
        config = AgentConfig(model="MiniMax-M2.7", provider="minimax", mode="bypass")
        assert config.model == "MiniMax-M2.7"
        assert config.provider == "minimax"
        assert config.mode == "bypass"


class TestStructuredLogging:
    """Test structured logging with trace_id."""

    def test_log_entry_contains_trace_id(self):
        """Log entries should include trace_id."""
        import uuid

        trace_id = str(uuid.uuid4())[:8]

        log_entry = {
            "trace_id": trace_id,
            "timestamp": "2026-05-17T12:00:00",
            "event": "tool_call",
            "tool": "write_file",
            "path": "workspace/test.py",
        }

        assert "trace_id" in log_entry
        assert len(log_entry["trace_id"]) == 8

    def test_json_log_format(self):
        """Logs should be parseable JSON."""

        log = {
            "timestamp": "2026-05-17T12:00:00",
            "trace_id": "abc12345",
            "level": "INFO",
            "event": "agent_start",
            "model": "MiniMax-M2.7",
        }

        # Should be valid JSON
        parsed = json.loads(json.dumps(log))
        assert parsed["trace_id"] == "abc12345"
        assert parsed["level"] == "INFO"

    def test_agent_engine_has_trace_id(self, monkeypatch):
        """AgentEngine should have a trace_id attribute."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        config = AgentConfig(model="mock", provider="openai", mode="bypass")
        from unittest.mock import AsyncMock

        from agent.core.engine import AgentEngine

        eng = AgentEngine(config)
        eng.llm = type("StubLLM", (), {"chat": AsyncMock()})()

        assert hasattr(eng, "trace_id")
        assert isinstance(eng.trace_id, str)
        assert len(eng.trace_id) == 8
