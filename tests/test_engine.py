"""Integration tests: tool registry and engine config."""

import os

import pytest

from agent.core.engine import AgentConfig, AgentEngine
from agent.tools.base import registry


class TestToolRegistry:
    """Verify all expected tools are registered."""

    EXPECTED_TOOLS = {
        "read_file", "write_file", "list_files",
        "apply_diff", "insert_after_line", "replace_lines",
        "execute_command",
        "create_skill", "list_skills", "search_skills",
        "spawn_sub_agent",
        "sandbox_execute", "snapshot", "rollback",
        "code_search",
    }

    def test_all_tools_registered(self):
        registered = set(registry._tools.keys())
        missing = self.EXPECTED_TOOLS - registered
        assert not missing, f"Missing tools: {missing}"

    def test_tool_schemas_valid(self):
        for schema in registry.schemas:
            assert "type" in schema
            assert schema["type"] == "function"
            assert "function" in schema
            assert "name" in schema["function"]


class TestAgentConfig:
    """Test configuration loading from environment."""

    def test_explicit_values(self):
        config = AgentConfig(model="qwen-plus", provider="dashscope", mode="plan")
        assert config.model == "qwen-plus"
        assert config.provider == "dashscope"
        assert config.mode == "plan"

    def test_default_literals(self):
        config = AgentConfig()
        # Dataclass defaults are evaluated at import time from env;
        # we just verify the instance is created with *some* values.
        assert config.model is not None
        assert config.provider is not None
        assert config.mode is not None


class TestAgentEngine:
    """Test engine initialization."""

    def test_init_without_config(self):
        engine = AgentEngine()
        assert engine.config is not None
        assert engine.memory is not None
        assert engine.skills is not None
        assert engine.permissions is not None

    def test_init_with_custom_config(self):
        config = AgentConfig(mode="plan")
        engine = AgentEngine(config)
        assert engine.config.mode == "plan"


class TestEnvContext:
    """Test _get_env_context() — system-reminder contents.

    Verifies that the engine correctly detects project markers in cwd
    so the LLM doesn't have to glob to know what kind of project it's in.
    """

    def test_get_env_context_returns_required_keys(self, tmp_path, monkeypatch):
        """Required keys always present in env context."""
        from agent.core.engine import AgentEngine, AgentConfig, WORKSPACE
        # Redirect WORKSPACE to tmp_path
        monkeypatch.setattr("agent.core.engine.WORKSPACE", tmp_path)
        engine = AgentEngine()
        ctx = engine._get_env_context()
        assert "cwd" in ctx
        assert "git_status" in ctx
        assert "plan_progress" in ctx
        assert "mode" in ctx
        assert "project_dir" in ctx
        assert "project_hint" in ctx

    def test_project_hint_detects_python(self, tmp_path, monkeypatch):
        """project_hint identifies a Python project (pyproject.toml or requirements.txt)."""
        from agent.core.engine import AgentEngine
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
        monkeypatch.setattr("agent.core.engine.WORKSPACE", tmp_path)
        engine = AgentEngine()
        ctx = engine._get_env_context()
        assert "pyproject.toml" in ctx["project_hint"]
        assert "python" in ctx["project_hint"]

    def test_project_hint_detects_node(self, tmp_path, monkeypatch):
        """project_hint identifies a Node project (package.json)."""
        from agent.core.engine import AgentEngine
        (tmp_path / "package.json").write_text('{"name": "foo"}')
        monkeypatch.setattr("agent.core.engine.WORKSPACE", tmp_path)
        engine = AgentEngine()
        ctx = engine._get_env_context()
        assert "package.json" in ctx["project_hint"]
        assert "node" in ctx["project_hint"]

    def test_project_hint_detects_multiple(self, tmp_path, monkeypatch):
        """project_hint includes all matching markers."""
        from agent.core.engine import AgentEngine
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "README.md").write_text("")
        (tmp_path / "Makefile").write_text("")
        monkeypatch.setattr("agent.core.engine.WORKSPACE", tmp_path)
        engine = AgentEngine()
        ctx = engine._get_env_context()
        for marker in ("pyproject.toml", "README.md", "Makefile"):
            assert marker in ctx["project_hint"]

    def test_project_hint_empty_for_empty_dir(self, tmp_path, monkeypatch):
        """project_hint is empty string when no markers found."""
        from agent.core.engine import AgentEngine
        monkeypatch.setattr("agent.core.engine.WORKSPACE", tmp_path)
        engine = AgentEngine()
        ctx = engine._get_env_context()
        assert ctx["project_hint"] == ""

    def test_project_hint_does_not_crash_on_permission_error(self, tmp_path, monkeypatch):
        """Even if reading cwd raises, project_hint stays empty (no crash)."""
        from agent.core.engine import AgentEngine
        monkeypatch.setattr("agent.core.engine.WORKSPACE", tmp_path)
        # Patch the engine's internal markers check to raise — not Path.exists
        # globally, which would also break memory loading.
        from agent.core import engine as engine_mod
        orig_check = getattr(engine_mod, "_check_project_markers", None)
        # Wrap the iteration by patching the engine's method
        engine = AgentEngine()
        # Force a permission error by making Path.exists raise for our specific path
        from pathlib import Path
        original_exists = Path.exists
        def selective_exists(self):
            if str(self) == str(tmp_path):
                raise OSError("permission denied")
            return original_exists(self)
        monkeypatch.setattr(Path, "exists", selective_exists)
        ctx = engine._get_env_context()
        # Empty, not crashed
        assert ctx["project_hint"] == ""

    def test_start_command_hint_in_context(self, tmp_path, monkeypatch):
        """start_command_hint key is always present (empty if not detected)."""
        from agent.core.engine import AgentEngine
        monkeypatch.setattr("agent.core.engine.WORKSPACE", tmp_path)
        engine = AgentEngine()
        ctx = engine._get_env_context()
        assert "start_command_hint" in ctx
        assert isinstance(ctx["start_command_hint"], str)


class TestDetectStartCommand:
    """Test _detect_start_command() — pre-computed start command hints."""

    def test_empty_dir_returns_empty(self, tmp_path):
        from agent.core.engine import AgentEngine
        assert AgentEngine._detect_start_command(tmp_path) == ""

    def test_node_with_start_script(self, tmp_path):
        import json
        from agent.core.engine import AgentEngine
        (tmp_path / "package.json").write_text(json.dumps({
            "name": "test", "scripts": {"start": "node index.js"}
        }))
        hint = AgentEngine._detect_start_command(tmp_path)
        assert "npm" in hint
        assert "start" in hint

    def test_node_falls_back_to_dev(self, tmp_path):
        import json
        from agent.core.engine import AgentEngine
        (tmp_path / "package.json").write_text(json.dumps({
            "name": "test", "scripts": {"dev": "vite"}
        }))
        hint = AgentEngine._detect_start_command(tmp_path)
        # dev is the only script — used as fallback
        assert "dev" in hint

    def test_python_with_main_py(self, tmp_path):
        from agent.core.engine import AgentEngine
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "main.py").write_text("print('hi')")
        hint = AgentEngine._detect_start_command(tmp_path)
        assert "main.py" in hint

    def test_python_with_app_py(self, tmp_path):
        from agent.core.engine import AgentEngine
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "app.py").write_text("print('hi')")
        hint = AgentEngine._detect_start_command(tmp_path)
        assert "app.py" in hint

    def test_makefile_first_target(self, tmp_path):
        from agent.core.engine import AgentEngine
        (tmp_path / "Makefile").write_text("run:\n\tpython main.py\n\ntest:\n\tpytest\n")
        hint = AgentEngine._detect_start_command(tmp_path)
        assert "make" in hint
        assert "run" in hint

    def test_go_project(self, tmp_path):
        from agent.core.engine import AgentEngine
        (tmp_path / "go.mod").write_text("module test\n\ngo 1.21\n")
        hint = AgentEngine._detect_start_command(tmp_path)
        assert "go run" in hint

    def test_rust_project(self, tmp_path):
        from agent.core.engine import AgentEngine
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
        hint = AgentEngine._detect_start_command(tmp_path)
        assert "cargo run" in hint

    def test_invalid_json_does_not_crash(self, tmp_path):
        """Malformed package.json returns empty string instead of raising."""
        from agent.core.engine import AgentEngine
        (tmp_path / "package.json").write_text("{not valid json")
        # Should not raise
        hint = AgentEngine._detect_start_command(tmp_path)
        # Returns empty or some safe fallback
        assert isinstance(hint, str)
