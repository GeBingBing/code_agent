"""Integration test: verify all registered tools can be invoked in ReAct loop."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from agent.core.engine import AgentConfig, AgentEngine
from agent.tools import *  # noqa: F401,F403 - registers all tools
from agent.tools.base import registry

WORKSPACE = Path(__file__).parent.parent / "workspace"


@dataclass
class MockFunction:
    name: str
    arguments: str


@dataclass
class MockToolCall:
    id: str
    function: MockFunction


@dataclass
class MockMessage:
    content: str = ""
    tool_calls: list = None


class MockLLMClient:
    def __init__(self, steps):
        self.steps = steps
        self.idx = 0

    async def chat(self, messages, tools=None, stream=False, **kwargs):
        if self.idx >= len(self.steps):
            return "Done."
        step = self.steps[self.idx]
        self.idx += 1
        return step


class TestAllToolsIntegration:
    """Invoke every registered tool at least once through the engine."""

    def test_all_tools(self, tmp_path, monkeypatch):
        """Sequentially call each tool and verify they execute without error."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        tool_results = {}

        async def run_test():
            config = AgentConfig(model="mock", provider="mock", mode="bypass")
            agent = AgentEngine(config)
            agent.llm = MockLLMClient([])  # dummy, we'll call tools directly

            # 1. write_file
            tool = registry.get("write_file")
            r = await tool.execute(path=str(tmp_path / "test.txt"), content="hello")
            tool_results["write_file"] = r.success
            assert r.success

            # 2. read_file
            tool = registry.get("read_file")
            r = await tool.execute(path=str(tmp_path / "test.txt"))
            tool_results["read_file"] = r.success
            assert r.success
            assert "hello" in r.content

            # 3. list_files
            tool = registry.get("list_files")
            r = await tool.execute(path=str(tmp_path))
            tool_results["list_files"] = r.success
            assert r.success
            assert "test.txt" in r.content

            # 4. execute_command
            tool = registry.get("execute_command")
            r = await tool.execute(command="echo integration_test")
            tool_results["execute_command"] = r.success
            assert r.success
            assert "integration_test" in r.content

            # 5. apply_diff
            (tmp_path / "diff.txt").write_text("old content\n")
            tool = registry.get("apply_diff")
            r = await tool.execute(
                path=str(tmp_path / "diff.txt"), search="old content", replace="new content"
            )
            tool_results["apply_diff"] = r.success
            assert r.success

            # 6. insert_after_line
            (tmp_path / "insert.txt").write_text("line1\nline2\n")
            tool = registry.get("insert_after_line")
            r = await tool.execute(path=str(tmp_path / "insert.txt"), line=1, content="inserted")
            tool_results["insert_after_line"] = r.success
            assert r.success

            # 7. replace_lines
            (tmp_path / "replace.txt").write_text("a\nb\nc\n")
            tool = registry.get("replace_lines")
            r = await tool.execute(
                path=str(tmp_path / "replace.txt"), start=2, end=2, content="REPLACED"
            )
            tool_results["replace_lines"] = r.success
            assert r.success

            # 8. code_search
            (tmp_path / "search.py").write_text("class SearchClass:\n    def find(self): pass\n")
            tool = registry.get("code_search")
            r = await tool.execute(query="SearchClass", top_k=5)
            tool_results["code_search"] = r.success
            assert r.success

            # 9. create_skill
            skills_dir = tmp_path / "skills"
            skills_dir.mkdir()
            from agent.tools.skill_manager import SkillManager

            mgr = SkillManager(skills_dir=skills_dir)
            tool = registry.get("create_skill")
            tool.manager = mgr
            r = await tool.execute(
                name="test-skill", description="A test skill", content="do this", tags=["test"]
            )
            tool_results["create_skill"] = r.success
            assert r.success

            # 10. list_skills
            tool = registry.get("list_skills")
            tool.manager = mgr
            r = await tool.execute()
            tool_results["list_skills"] = r.success
            assert r.success
            assert "test-skill" in r.content

            # 11. search_skills
            tool = registry.get("search_skills")
            tool.manager = mgr
            r = await tool.execute(query="test")
            tool_results["search_skills"] = r.success
            assert r.success
            assert "test-skill" in r.content

            # 12. spawn_sub_agent (needs mock env for its own AgentConfig)
            import os as _os2

            _os2.environ["DEFAULT_MODEL"] = "mock"
            _os2.environ["DEFAULT_PROVIDER"] = "mock"
            _os2.environ["OPENAI_API_KEY"] = "sk-test"
            tool = registry.get("spawn_sub_agent")
            r = await tool.execute(task="say hello")
            tool_results["spawn_sub_agent"] = r.success
            assert r.success
            # Restore
            _os2.environ.pop("DEFAULT_MODEL", None)
            _os2.environ.pop("DEFAULT_PROVIDER", None)

            # 13. sandbox_execute
            tool = registry.get("sandbox_execute")
            r = await tool.execute(command="echo sandbox")
            tool_results["sandbox_execute"] = r.success
            # May fail if docker not available - that's ok for integration

            # 14. snapshot
            tool = registry.get("snapshot")
            r = await tool.execute(path=str(tmp_path), name="test-snap")
            tool_results["snapshot"] = r.success
            assert r.success

            # 15. rollback
            tool = registry.get("rollback")
            r = await tool.execute(name="test-snap", target=str(tmp_path))
            tool_results["rollback"] = r.success
            assert r.success

            # 16. git
            import subprocess

            subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
            tool = registry.get("git")
            r = await tool.execute(command="status", cwd=str(tmp_path))
            tool_results["git"] = r.success
            assert r.success
            assert "On branch" in r.content or "nothing to commit" in r.content

            # 17. grep
            (tmp_path / "grep_test.py").write_text("search_target = 123\n")
            tool = registry.get("grep")
            r = await tool.execute(pattern="search_target", path=str(tmp_path), glob="*.py")
            tool_results["grep"] = r.success
            assert r.success
            assert "search_target" in r.content

            # 18. web_fetch
            tool = registry.get("web_fetch")
            r = await tool.execute(url="https://example.com", max_length=500)
            tool_results["web_fetch"] = r.success
            assert r.success
            assert "Example Domain" in r.content

            # 19. web_search
            tool = registry.get("web_search")
            r = await tool.execute(query="Python programming", max_results=3)
            tool_results["web_search"] = r.success
            # May fail due to network - verify no crash

            # 20. list_sub_agents
            tool = registry.get("list_sub_agents")
            r = await tool.execute()
            tool_results["list_sub_agents"] = r.success

            # 21. kill_sub_agent
            tool = registry.get("kill_sub_agent")
            r = await tool.execute(run_id="nonexistent")
            tool_results["kill_sub_agent"] = r.success  # Should return False but not crash

            # 22. run_tests
            tool = registry.get("run_tests")
            r = await tool.execute(path=str(tmp_path), marker="")
            tool_results["run_tests"] = r.success  # May not find tests, but shouldn't crash

            # 23. spawn_parallel
            tool = registry.get("spawn_parallel")
            r = await tool.execute(tasks='[{"task": "write a hello() function", "label": "test1"}]')
            tool_results["spawn_parallel"] = r.success  # May fail without LLM, but shouldn't crash

            # 24-26. spec tools (need CODING_AGENT_WORKSPACE env)
            import os as _os

            _os.environ["CODING_AGENT_WORKSPACE"] = str(tmp_path)
            # Reload workspace path in spec_verifier
            from agent.tools import spec_verifier as _sv

            _sv.WORKSPACE = tmp_path

            (tmp_path / "SPECS.md").write_text("## Phase 1: Test\n\n- [ ] Task A\n- [x] Task B\n")
            tool = registry.get("get_spec_status")
            r = await tool.execute()
            tool_results["get_spec_status"] = r.success
            assert r.success
            assert "Task A" in r.content

            # 25. mark_spec_task_done
            tool = registry.get("mark_spec_task_done")
            r = await tool.execute(phase_number=1, task_description="Task A")
            tool_results["mark_spec_task_done"] = r.success
            assert r.success

            # 26. verify_against_spec
            tool = registry.get("verify_against_spec")
            r = await tool.execute(implementation_summary="Implemented task A")
            tool_results["verify_against_spec"] = r.success
            assert r.success

            # 27. find_references
            (tmp_path / "ref_test.py").write_text(
                "def calculate(): pass\ndef main(): calculate()\n"
            )
            tool = registry.get("find_references")
            r = await tool.execute(symbol="calculate")
            tool_results["find_references"] = r.success

            # 28. get_call_graph
            tool = registry.get("get_call_graph")
            r = await tool.execute(function="main")
            tool_results["get_call_graph"] = r.success

            # 29. smart_branch
            import subprocess as _subprocess

            _subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
            _subprocess.run(
                ["git", "config", "user.email", "test@test.com"],
                cwd=str(tmp_path),
                capture_output=True,
                check=True,
            )
            _subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=str(tmp_path),
                capture_output=True,
                check=True,
            )
            _subprocess.run(
                ["git", "add", "-A"], cwd=str(tmp_path), capture_output=True, check=True
            )
            _subprocess.run(
                ["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True, check=True
            )

            tool = registry.get("smart_branch")
            r = await tool.execute(task="fix auth bug", cwd=str(tmp_path))
            tool_results["smart_branch"] = r.success

            # 30. smart_commit
            (tmp_path / "commit_test.py").write_text("x = 1\n")
            tool = registry.get("smart_commit")
            r = await tool.execute(message="test commit", cwd=str(tmp_path))
            tool_results["smart_commit"] = r.success

            # 31. create_pr (gh may not be available, just verify no crash)
            tool = registry.get("create_pr")
            r = await tool.execute(title="Test PR", cwd=str(tmp_path))
            tool_results["create_pr"] = r.success  # May fail if gh not installed or no remote

            # 32. safe_rename
            (tmp_path / "rename_test.py").write_text("def old_name(): pass\nold_name()\n")
            tool = registry.get("safe_rename")
            r = await tool.execute(symbol="old_name", new_name="new_name", dry_run=True)
            tool_results["safe_rename"] = r.success  # dry_run should succeed

            # 33. get_refactor_preview
            tool = registry.get("get_refactor_preview")
            r = await tool.execute(symbol="old_name", new_name="new_name")
            tool_results["get_refactor_preview"] = r.success  # Should return preview

            # 34. install_package (mock _run_install)
            from unittest.mock import patch

            with patch("agent.tools.install._run_install") as mock_run:
                mock_run.return_value = ("installed test-pkg\n", "", 0)
                tool = registry.get("install_package")
                r = await tool.execute(package="test-pkg", manager="pip install")
                tool_results["install_package"] = r.success
                assert r.success

            # 35. uninstall_package (mock _run_install)
            with patch("agent.tools.install._run_install") as mock_run:
                mock_run.return_value = ("uninstalled test-pkg\n", "", 0)
                tool = registry.get("uninstall_package")
                r = await tool.execute(package="test-pkg")
                tool_results["uninstall_package"] = r.success
                assert r.success

            # 36. enter_plan_mode (no args, just switches mode)
            tool = registry.get("enter_plan_mode")
            r = await tool.execute()
            tool_results["enter_plan_mode"] = r.success
            assert r.success

            # 37. exit_plan_mode (requires plan text)
            tool = registry.get("exit_plan_mode")
            r = await tool.execute(
                plan="## Plan: test\n\n- [ ] Step 1", allowed_prompts="edit files"
            )
            tool_results["exit_plan_mode"] = r.success
            assert r.success

            # 38. lsp — try an operation; gracefully fails without LSP server
            (tmp_path / "lsp_test.py").write_text("def hello(): pass\n")
            tool = registry.get("lsp")
            r = await tool.execute(
                operation="documentSymbol",
                file_path=str(tmp_path / "lsp_test.py"),
            )
            tool_results["lsp"] = True
            assert r is not None

            # 39. glob — file pattern matching
            (tmp_path / "subdir").mkdir(exist_ok=True)
            (tmp_path / "subdir" / "a.py").write_text("x=1\n")
            (tmp_path / "subdir" / "b.js").write_text("let x=1;\n")
            tool = registry.get("glob")
            r = await tool.execute(pattern="**/*.py", path=str(tmp_path))
            tool_results["glob"] = r.success
            assert r.success
            assert "a.py" in r.content

            # 40. todo_write — task list
            tool = registry.get("todo_write")
            r = await tool.execute(todos='[{"id":"1","status":"completed","content":"test"}]')
            tool_results["todo_write"] = r.success
            assert r.success

            # 41. notebook_read — read .ipynb
            import json as _json

            nb = _json.dumps(
                {
                    "cells": [{"cell_type": "code", "source": ["print(1)"], "outputs": []}],
                    "metadata": {},
                    "nbformat": 4,
                    "nbformat_minor": 5,
                }
            )
            (tmp_path / "test.ipynb").write_text(nb)
            tool = registry.get("notebook_read")
            r = await tool.execute(path=str(tmp_path / "test.ipynb"))
            tool_results["notebook_read"] = r.success
            assert r.success

            # 42. notebook_edit — edit .ipynb
            tool = registry.get("notebook_edit")
            r = await tool.execute(
                path=str(tmp_path / "test.ipynb"),
                cell_index=0,
                operation="replace",
                new_source="print(42)",
            )
            tool_results["notebook_edit"] = r.success
            assert r.success

            # 43. structured_output
            tool = registry.get("structured_output")
            r = await tool.execute(
                schema='{"type":"object","properties":{"name":{"type":"string"}}}',
                description="test schema",
            )
            tool_results["structured_output"] = r.success
            assert r.success

            # 44. write_failing_test (PR-02)
            tool = registry.get("write_failing_test")
            r = await tool.execute(
                path=str(tmp_path / "test_failing.py"),
                test_code="def test_x():\n    assert False\n",
                feature="x",
            )
            tool_results["write_failing_test"] = r.success
            assert r.success

            # 45. semantic_search (PR-04) — uses long-term memory singleton
            from agent.core.vector_memory import get_vector_memory, reset_vector_memory

            reset_vector_memory()
            vm = get_vector_memory()
            vm.add("python", "def hello(): return 42")
            tool = registry.get("semantic_search")
            r = await tool.execute(query="python function", k=1)
            tool_results["semantic_search"] = r.success
            assert r.success
            reset_vector_memory()

            # 46. spec_status (PR-06) — AC-aware spec status
            tool = registry.get("spec_status")
            r = await tool.execute()
            tool_results["spec_status"] = r.success
            assert r.success

            # 47. verify_acs (PR-06)
            tool = registry.get("verify_acs")
            r = await tool.execute(phase_id="P1")
            tool_results["verify_acs"] = r.success
            assert r.success

            # 48. mark_ac_done (PR-06) — mark an AC and check persistence
            tool = registry.get("mark_ac_done")
            r = await tool.execute(ac_id="P1-1")
            tool_results["mark_ac_done"] = r.success
            assert r.success

            # 49. audit_query (PR-08) — read-only query of the audit log
            tool = registry.get("audit_query")
            r = await tool.execute(limit=5)
            tool_results["audit_query"] = r.success
            assert r.success

            # 50. metrics_query (PR-10) — in-process metrics introspection
            tool = registry.get("metrics_query")
            r = await tool.execute()
            tool_results["metrics_query"] = r.success
            assert r.success

            # 51. logs_query (PR-10) — recent log entries
            tool = registry.get("logs_query")
            r = await tool.execute(limit=5, path=str(tmp_path / "no.log"))
            tool_results["logs_query"] = r.success
            assert r.success

        # Run the async test
        asyncio.run(run_test())

        # Verify all tools were tested
        all_tools = set(registry._tools.keys())
        tested = set(tool_results.keys())
        missing = all_tools - tested
        assert not missing, f"Tools not tested: {missing}"
