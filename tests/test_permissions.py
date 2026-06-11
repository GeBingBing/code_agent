"""Tests for Phase 3: Permission system."""

import pytest

from agent.core.permissions import (
    PermissionManager,
    RiskLevel,
    assess_risk,
)


class TestRiskAssessment:
    """Test risk level assessment."""

    def test_read_file_is_low(self):
        assert assess_risk("read_file", {"path": "test.py"}) == RiskLevel.LOW

    def test_write_file_is_medium(self):
        assert assess_risk("write_file", {"path": "test.py"}) == RiskLevel.MEDIUM

    def test_execute_command_is_high(self):
        assert assess_risk("execute_command", {"command": "ls"}) == RiskLevel.HIGH

    def test_rm_rf_is_critical(self):
        assert assess_risk("execute_command", {"command": "rm -rf /"}) == RiskLevel.CRITICAL

    def test_sudo_is_critical(self):
        assert assess_risk("execute_command", {"command": "sudo apt install x"}) == RiskLevel.CRITICAL


class TestPlanMode:
    """Test plan mode (read-only)."""

    def test_allows_read(self):
        pm = PermissionManager("plan")
        allowed, _ = pm.check("read_file", {"path": "x"})
        assert allowed is True

    def test_blocks_write(self):
        pm = PermissionManager("plan")
        allowed, reason = pm.check("write_file", {"path": "x"})
        assert allowed is False
        assert "Plan mode" in reason

    def test_blocks_execute(self):
        pm = PermissionManager("plan")
        allowed, _ = pm.check("execute_command", {"command": "ls"})
        assert allowed is False


class TestDefaultMode:
    """Test default mode."""

    def test_allows_low_risk(self):
        pm = PermissionManager("default")
        allowed, _ = pm.check("read_file", {"path": "x"})
        assert allowed is True

    def test_allows_medium_with_confirmation(self):
        pm = PermissionManager("default")
        allowed, _ = pm.check("write_file", {"path": "x"})
        assert allowed is True
        assert pm.needs_confirmation("write_file", {"path": "x"}) is True

    def test_blocks_critical(self):
        pm = PermissionManager("default")
        allowed, reason = pm.check("execute_command", {"command": "rm -rf /"})
        assert allowed is False
        assert "CRITICAL" in reason


class TestBypassMode:
    """Test bypass mode."""

    def test_allows_everything(self):
        pm = PermissionManager("bypass")
        # Non-critical operations are auto-approved in bypass mode
        allowed, _ = pm.check("write_file", {"path": "test.py"})
        assert allowed is True
        assert "Bypass" in _

    def test_bypass_still_blocks_critical(self):
        pm = PermissionManager("bypass")
        # CRITICAL operations are blocked even in bypass mode
        allowed, _ = pm.check("execute_command", {"command": "rm -rf /"})
        assert allowed is False
        assert "CRITICAL" in _

    def test_no_confirmation_needed(self):
        pm = PermissionManager("bypass")
        assert pm.needs_confirmation("write_file", {"path": "x"}) is False


class TestAlwaysAllowCache:
    """Test 'always allow' caching like Claude/Cursor."""

    def test_needs_confirmation_by_default(self):
        pm = PermissionManager("default")
        assert pm.needs_confirmation("write_file", {"path": "/tmp/foo.py"}) is True

    def test_after_approval_no_longer_needs_confirmation(self):
        pm = PermissionManager("default")
        # Simulate user pressing 'a' (always) by directly adding to cache
        from agent.core.permissions import _make_signature
        sig = _make_signature("write_file", {"path": "/tmp/foo.py"})
        pm._approved.add(sig)
        assert pm.needs_confirmation("write_file", {"path": "/tmp/foo.py"}) is False

    def test_different_path_still_needs_confirmation(self):
        pm = PermissionManager("default")
        from agent.core.permissions import _make_signature
        sig = _make_signature("write_file", {"path": "/tmp/foo.py"})
        pm._approved.add(sig)
        # Different path should still need confirmation
        assert pm.needs_confirmation("write_file", {"path": "/tmp/bar.py"}) is True

    def test_execute_command_cached(self):
        pm = PermissionManager("default")
        from agent.core.permissions import _make_signature
        sig = _make_signature("execute_command", {"command": "python test.py"})
        pm._approved.add(sig)
        assert pm.needs_confirmation("execute_command", {"command": "python test.py"}) is False
