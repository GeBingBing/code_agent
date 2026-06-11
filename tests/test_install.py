"""Tests for package installation tool (install.py)."""

from unittest.mock import patch

import pytest

from agent.tools.base import registry
from agent.tools.install import (
    InstallPackageTool,
    _detect_package_manager,
    _run_install,
)

# ── Package Manager Detection ───────────────────────────────────────


class TestDetectPackageManager:
    """Test auto-detection of package managers."""

    def test_explicit_manager(self):
        assert _detect_package_manager("foo", "pip install") == "pip install"
        assert _detect_package_manager("foo", "brew install") == "brew install"

    def test_pyproject_toml_without_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", str(tmp_path))
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        result = _detect_package_manager("foo", "auto")
        assert result == "pip install"

    def test_pyproject_with_poetry_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", str(tmp_path))
        (tmp_path / "pyproject.toml").write_text("[project]")
        (tmp_path / "poetry.lock").write_text("")
        result = _detect_package_manager("foo", "auto")
        assert result == "poetry add"

    def test_requirements_txt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", str(tmp_path))
        (tmp_path / "requirements.txt").write_text("requests")
        result = _detect_package_manager("foo", "auto")
        assert result == "pip install"

    def test_package_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", str(tmp_path))
        (tmp_path / "package.json").write_text("{}")
        result = _detect_package_manager("foo", "auto")
        assert result == "npm install"

    def test_package_json_with_yarn_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", str(tmp_path))
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "yarn.lock").write_text("")
        result = _detect_package_manager("foo", "auto")
        assert result == "yarn add"

    def test_package_json_with_pnpm_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", str(tmp_path))
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "pnpm-lock.yaml").write_text("")
        result = _detect_package_manager("foo", "auto")
        assert result == "pnpm add"

    def test_gemfile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", str(tmp_path))
        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'")
        result = _detect_package_manager("foo", "auto")
        assert result == "bundle add"

    def test_cargo_toml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", str(tmp_path))
        (tmp_path / "Cargo.toml").write_text("[package]")
        result = _detect_package_manager("foo", "auto")
        assert result == "cargo install"

    def test_fallback_brew(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", str(tmp_path))
        with patch("agent.tools.install.shutil.which", side_effect=lambda x: x == "brew"):
            result = _detect_package_manager("foo", "auto")
            assert result == "brew install"

    def test_fallback_apt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", str(tmp_path))
        with patch("agent.tools.install.shutil.which", side_effect=lambda x: x == "apt"):
            result = _detect_package_manager("foo", "auto")
            assert result == "apt install -y"

    def test_fallback_pip_last_resort(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", str(tmp_path))
        with patch("agent.tools.install.shutil.which", return_value=None):
            result = _detect_package_manager("foo", "auto")
            assert result == "pip install"


# ── Install Command Execution ───────────────────────────────────────


class TestRunInstall:
    """Test install command execution."""

    @pytest.mark.asyncio
    async def test_successful_install(self):
        stdout, stderr, rc = await _run_install(["echo", "installed"], "/tmp", timeout=5)
        assert rc == 0
        assert "installed" in stdout


# ── InstallPackageTool ──────────────────────────────────────────────


class TestInstallPackageTool:
    """Test the InstallPackageTool integration."""

    def test_tool_registered(self):
        tool = registry.get("install_package")
        assert tool is not None
        assert isinstance(tool, InstallPackageTool)

    def test_tool_schema(self):
        tool = registry.get("install_package")
        schema = tool.schema
        func = schema.get("function", schema)
        assert func["name"] == "install_package"
        params = func.get("parameters", {})
        assert "package" in params.get("properties", {})
        assert "manager" in params.get("properties", {})

    @pytest.mark.asyncio
    async def test_execute_with_explicit_manager(self):
        tool = InstallPackageTool()
        with patch("agent.tools.install._run_install") as mock_run:
            mock_run.return_value = ("installed numpy\n", "", 0)
            result = await tool.execute(package="numpy", manager="pip install")
            assert result.success is True
            assert "numpy" in result.content

    @pytest.mark.asyncio
    async def test_execute_failure(self):
        tool = InstallPackageTool()
        with patch("agent.tools.install._run_install") as mock_run:
            mock_run.return_value = ("", "package not found", 1)
            result = await tool.execute(package="nonexistent", manager="pip install")
            assert result.success is False
            assert "package not found" in result.error

    @pytest.mark.asyncio
    async def test_execute_with_extra_args(self):
        tool = InstallPackageTool()
        with patch("agent.tools.install._run_install") as mock_run:
            mock_run.return_value = ("done", "", 0)
            result = await tool.execute(package="requests", manager="pip install", args="--upgrade")
            assert result.success is True
            # Verify extra args were appended
            call_args = mock_run.call_args[0][0]
            assert "--upgrade" in call_args

    @pytest.mark.asyncio
    async def test_execute_normalizes_spaces_in_package_name(self):
        """Package names with spaces are normalized to hyphens."""
        tool = InstallPackageTool()
        with patch("agent.tools.install._run_install") as mock_run:
            mock_run.return_value = ("installed hermes-agent\n", "", 0)
            result = await tool.execute(package="hermes agent")
            assert result.success is True
            # Verify the normalized name was used in the install command
            call_args = mock_run.call_args[0][0]
            assert "hermes-agent" in call_args
            assert "hermes agent" not in call_args
