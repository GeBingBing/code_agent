"""Tests for PlanToolFilter — the dispatcher-level read-only gate for plan mode.

These tests pin down the hard, fail-closed behaviour that PlanToolFilter
introduces: in plan mode, only tools in PLAN_ONLY_TOOLS are allowed. Anything
else returns a ToolResult with ``plan_blocked: True`` in metadata.

Why a hard dispatcher-level gate:
  * The RiskLevel-based filter in PermissionManager is too coarse (grep/glob/
    web_search default to MEDIUM and would be wrongly blocked).
  * Docstring-level "you cannot write" instructions are honoured only by LLM
    goodwill. The dispatcher is the fail-closed enforcement point.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.core.permissions import PermissionManager, PermissionMode
from agent.core.tool_dispatcher import (
    PLAN_ONLY_TOOLS,
    PlanModeViolation,
    ToolDispatcher,
)

# ── Lightweight stubs ───────────────────────────────────────────────────────


class _NoHooks:
    async def execute(self, _event: str, payload: dict) -> dict:
        return payload


class _NoEventBus:
    async def emit(self, _event: str, _payload: dict) -> None:
        return None


class _NoMemory:
    def add(self, *_args, **_kwargs) -> None:
        return None

    def remember(self, *_args, **_kwargs) -> None:
        return None


def _make_dispatcher(mode: str) -> ToolDispatcher:
    """Build a minimal ToolDispatcher for filter tests.

    We only exercise stage 0 (PlanToolFilter) and stage 1 (hooks), so the
    later callbacks can be no-op lambdas.
    """
    return ToolDispatcher(
        hooks=_NoHooks(),
        event_bus=_NoEventBus(),
        permissions=PermissionManager(mode=mode),
        memory=_NoMemory(),
        trace_id="t",
        workspace=Path("."),
        get_current_project_dir=lambda: None,
        set_current_project_dir=lambda v: None,
        get_pre_plan_mode=lambda: None,
        set_pre_plan_mode=lambda v: None,
        get_confirm_handler=lambda: None,
        log_event=lambda *a, **k: None,
    )


# ── _check_plan_whitelist (sync helper) ─────────────────────────────────────


class TestCheckPlanWhitelist:
    def test_noop_in_default_mode(self):
        d = _make_dispatcher("default")
        # write_file is NOT in whitelist, but we're in default mode → no raise
        d._check_plan_whitelist("write_file")

    def test_noop_in_auto_mode(self):
        d = _make_dispatcher("auto")
        d._check_plan_whitelist("execute_command")

    def test_noop_in_bypass_mode(self):
        d = _make_dispatcher("bypass")
        d._check_plan_whitelist("delete_file")

    def test_blocks_write_file_in_plan_mode(self):
        d = _make_dispatcher("plan")
        try:
            d._check_plan_whitelist("write_file")
        except PlanModeViolation as exc:
            assert exc.tool_name == "write_file"
            assert exc.mode == "plan"
            assert "write_file" in str(exc)
        else:
            raise AssertionError("PlanModeViolation not raised")

    def test_blocks_apply_diff_in_plan_mode(self):
        d = _make_dispatcher("plan")
        try:
            d._check_plan_whitelist("apply_diff")
        except PlanModeViolation:
            pass
        else:
            raise AssertionError("apply_diff should be blocked in plan mode")

    def test_blocks_execute_command_in_plan_mode(self):
        d = _make_dispatcher("plan")
        try:
            d._check_plan_whitelist("execute_command")
        except PlanModeViolation:
            pass
        else:
            raise AssertionError("execute_command should be blocked in plan mode")

    def test_blocks_install_package_in_plan_mode(self):
        d = _make_dispatcher("plan")
        try:
            d._check_plan_whitelist("install_package")
        except PlanModeViolation:
            pass
        else:
            raise AssertionError("install_package should be blocked in plan mode")

    def test_blocks_spawn_sub_agent_in_plan_mode(self):
        d = _make_dispatcher("plan")
        try:
            d._check_plan_whitelist("spawn_sub_agent")
        except PlanModeViolation:
            pass
        else:
            raise AssertionError("spawn_sub_agent should be blocked in plan mode")

    def test_allows_read_file_in_plan_mode(self):
        d = _make_dispatcher("plan")
        d._check_plan_whitelist("read_file")  # should not raise

    def test_allows_grep_in_plan_mode(self):
        """Regression: grep defaults to MEDIUM in assess_risk and would be
        wrongly blocked by the legacy PLAN-mode RiskLevel filter. The
        dispatcher-level whitelist fixes this."""
        d = _make_dispatcher("plan")
        d._check_plan_whitelist("grep")  # should not raise

    def test_allows_glob_in_plan_mode(self):
        d = _make_dispatcher("plan")
        d._check_plan_whitelist("glob")  # should not raise

    def test_allows_web_search_and_web_fetch_in_plan_mode(self):
        d = _make_dispatcher("plan")
        d._check_plan_whitelist("web_search")
        d._check_plan_whitelist("web_fetch")

    def test_allows_enter_plan_mode_in_plan_mode(self):
        """enter_plan_mode is idempotent — calling it again while in plan
        mode should not be blocked (defensive: the tool is a no-op)."""
        d = _make_dispatcher("plan")
        d._check_plan_whitelist("enter_plan_mode")  # should not raise

    def test_allows_exit_plan_mode_in_plan_mode(self):
        d = _make_dispatcher("plan")
        d._check_plan_whitelist("exit_plan_mode")  # should not raise


# ── PLAN_ONLY_TOOLS constant ────────────────────────────────────────────────


class TestWhitelistContract:
    def test_whitelist_is_frozenset(self):
        assert isinstance(PLAN_ONLY_TOOLS, frozenset)

    def test_whitelist_contains_core_read_tools(self):
        for tool in ("read_file", "list_files", "grep", "glob", "code_search"):
            assert tool in PLAN_ONLY_TOOLS, f"{tool} missing from whitelist"

    def test_whitelist_does_not_contain_write_tools(self):
        for tool in (
            "write_file",
            "apply_diff",
            "delete_file",
            "insert_after_line",
            "replace_lines",
        ):
            assert tool not in PLAN_ONLY_TOOLS, f"{tool} should NOT be in plan whitelist"

    def test_whitelist_does_not_contain_execution_tools(self):
        for tool in ("execute_command", "install_package", "uninstall_package", "spawn_sub_agent"):
            assert tool not in PLAN_ONLY_TOOLS, f"{tool} should NOT be in plan whitelist"

    def test_whitelist_does_not_contain_state_mutating_tools(self):
        for tool in ("smart_commit", "create_pr", "create_skill", "todo_write", "run_tests"):
            assert tool not in PLAN_ONLY_TOOLS, f"{tool} should NOT be in plan whitelist"

    def test_whitelist_contains_plan_transitions(self):
        assert "enter_plan_mode" in PLAN_ONLY_TOOLS
        assert "exit_plan_mode" in PLAN_ONLY_TOOLS


# ── Integration: execute() returns PlanModeViolation ToolResult ──────────────


class TestDispatcherExecuteBlocksWritesInPlanMode:
    """End-to-end: the execute() pipeline must convert PlanModeViolation
    into a ToolResult the LLM can adapt to (rather than crashing the run).
    """

    def test_write_file_returns_plan_blocked(self):
        d = _make_dispatcher("plan")
        result = asyncio.run(
            d.execute(
                func_name="write_file",
                args={"path": "x.py", "content": "print(1)"},
                tc_id="tc1",
                func_args_raw="{}",
            )
        )
        assert result.success is False
        assert "Plan mode blocks" in (result.error or "")
        assert result.metadata == {"plan_blocked": True, "tool": "write_file"}

    def test_execute_command_returns_plan_blocked(self):
        d = _make_dispatcher("plan")
        result = asyncio.run(
            d.execute(
                func_name="execute_command",
                args={"command": "ls"},
                tc_id="tc1",
                func_args_raw="{}",
            )
        )
        assert result.success is False
        assert "Plan mode blocks" in (result.error or "")

    def test_read_file_is_not_blocked_by_filter(self):
        """In plan mode, read_file should pass stage 0. It will still hit
        stage 3 (top-level permission check) which also allows it; we just
        verify the filter did not raise.

        We use a real ReadFileTool (registered in the global registry) so
        the full pipeline can complete. If the file doesn't exist, the
        read fails for a different reason — that's fine, we're checking
        that the failure is NOT a plan_blocked one.
        """
        d = _make_dispatcher("plan")
        result = asyncio.run(
            d.execute(
                func_name="read_file",
                args={"path": "tests/test_plan_tool_filter.py"},
                tc_id="tc1",
                func_args_raw="{}",
            )
        )
        # We don't assert success=True (file IO can fail in test env); we
        # assert it's NOT a plan-blocked failure.
        assert not (result.metadata or {}).get(
            "plan_blocked"
        ), f"read_file should pass PlanToolFilter; got {result.error}"


# ── Regression: default / auto / bypass modes are unaffected ────────────────


class TestOtherModesUnaffected:
    def test_default_mode_does_not_invoke_filter(self):
        d = _make_dispatcher("default")
        # write_file should pass stage 0 (filter no-ops in non-PLAN modes)
        try:
            d._check_plan_whitelist("write_file")
        except PlanModeViolation as err:
            raise AssertionError("Filter should no-op in default mode") from err
        assert d._permissions.mode == PermissionMode.DEFAULT

    def test_auto_mode_does_not_invoke_filter(self):
        d = _make_dispatcher("auto")
        try:
            d._check_plan_whitelist("execute_command")
        except PlanModeViolation as err:
            raise AssertionError("Filter should no-op in auto mode") from err

    def test_bypass_mode_does_not_invoke_filter(self):
        d = _make_dispatcher("bypass")
        try:
            d._check_plan_whitelist("delete_file")
        except PlanModeViolation as err:
            raise AssertionError("Filter should no-op in bypass mode") from err
