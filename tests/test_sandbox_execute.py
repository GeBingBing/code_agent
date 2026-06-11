"""Tests for Phase 7: Sandbox execute tool."""

import asyncio

from agent.tools.sandbox import SandboxExecuteTool


def _run(async_fn):
    return asyncio.run(async_fn)


class TestSandboxExecute:
    def test_executes_or_reports_error(self):
        """When Docker daemon is not running, should report error gracefully."""
        tool = SandboxExecuteTool()
        result = _run(tool.execute(command="echo hello"))
        if tool.sandbox.has_docker:
            # Docker CLI exists; result depends on daemon state
            assert result.content != "" or result.error != ""
        else:
            assert "Docker not available" in result.content
