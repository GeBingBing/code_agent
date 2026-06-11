"""Tests for git tool."""

import asyncio
import subprocess

from agent.tools.git_tool import GitTool


def _run(async_fn):
    return asyncio.run(async_fn)


class TestGitTool:
    def test_status_in_repo(self, tmp_path):
        # Initialize a git repo
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        tool = GitTool()
        result = _run(tool.execute(command="status", cwd=str(tmp_path)))
        assert result.success is True
        assert "On branch" in result.content or "nothing to commit" in result.content

    def test_log_empty_repo(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        tool = GitTool()
        result = _run(tool.execute(command="log", cwd=str(tmp_path)))
        # log on empty repo returns error
        assert result.success is False

    def test_diff_no_changes(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        tool = GitTool()
        result = _run(tool.execute(command="diff", cwd=str(tmp_path)))
        assert result.success is True

    def test_blocks_dangerous_flag(self, tmp_path):
        tool = GitTool()
        result = _run(tool.execute(command="push --force"))
        assert result.success is False
        assert "Dangerous flag" in result.error

    def test_blocks_disallowed_subcommand(self, tmp_path):
        tool = GitTool()
        result = _run(tool.execute(command="filter-branch"))
        assert result.success is False
        assert "not allowed" in result.error

    def test_add_and_commit(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=True
        )
        (tmp_path / "a.txt").write_text("hello")

        tool = GitTool()
        r1 = _run(tool.execute(command="add a.txt", cwd=str(tmp_path)))
        assert r1.success is True

        r2 = _run(tool.execute(command="commit -m 'test commit'", cwd=str(tmp_path)))
        assert r2.success is True

        r3 = _run(tool.execute(command="log --oneline", cwd=str(tmp_path)))
        assert r3.success is True
        assert "test commit" in r3.content
