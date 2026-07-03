"""Tests for cron tools and the cron matcher."""

import time

import pytest

from agent.core.cron_store import CronStore, ScheduledTask
from agent.tools.base import registry


class TestCronStore:
    def test_add_and_list(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        store = CronStore()
        t = ScheduledTask(
            id="abc", cron="0 9 * * *", prompt="review PRs", recurring=True, durable=True
        )
        store.add(t)
        assert len(store.list()) == 1
        assert store.get("abc").prompt == "review PRs"

    def test_remove(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        store = CronStore()
        store.add(ScheduledTask(id="x", cron="* * * * *", prompt="p", recurring=True, durable=True))
        assert store.remove("x") is True
        assert store.list() == []
        assert store.remove("x") is False

    def test_persist_across_instances(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        s1 = CronStore()
        s1.add(
            ScheduledTask(id="p1", cron="0 0 * * *", prompt="daily", recurring=True, durable=True)
        )
        # New instance loads from disk.
        s2 = CronStore()
        assert s2.get("p1") is not None


class TestCronCreateTool:
    def test_registered(self):
        import agent.core.engine  # noqa: F401

        assert "cron_create" in [t.name for t in registry.list()]
        assert "cron_delete" in [t.name for t in registry.list()]

    def test_low_risk(self):
        from agent.core.permissions import RiskLevel, assess_risk

        assert assess_risk("cron_create", {"cron": "* * * * *", "prompt": "x"}) == RiskLevel.LOW

    @pytest.mark.asyncio
    async def test_create_validates_5_fields(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from agent.tools.cron import CronCreateTool

        tool = CronCreateTool()
        result = await tool.execute(cron="0 9", prompt="p")
        assert not result.success
        assert "5 fields" in result.error

    @pytest.mark.asyncio
    async def test_create_adds_to_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Reset the singleton so it picks up the new HOME.
        import agent.core.cron_store as cs
        from agent.tools.cron import CronCreateTool

        cs._store = None
        tool = CronCreateTool()
        result = await tool.execute(cron="*/5 * * * *", prompt="check deploy")
        assert result.success
        assert "check deploy" in result.content
        assert result.metadata["task_id"]


class TestCronMatcher:
    def test_star_matches_anything(self):
        from ui.cli import _cron_matches

        now = time.localtime()
        assert _cron_matches("* * * * *", now) is True

    def test_specific_minute_matches(self):
        from ui.cli import _cron_matches

        # Build a fixed time: 2026-07-01 09:05 Wednesday (tm_wday=2 → Tue=1? no)
        # struct_time: tm_wday 0=Monday. 2026-07-01 is a Wednesday → tm_wday=2.
        now = time.struct_time((2026, 7, 1, 9, 5, 0, 2, 182, 0))  # Wed 09:05
        assert _cron_matches("5 9 * * *", now) is True
        assert _cron_matches("0 9 * * *", now) is False  # minute 5 ≠ 0

    def test_step_matches(self):
        from ui.cli import _cron_matches

        now = time.struct_time((2026, 7, 1, 9, 10, 0, 2, 182, 0))  # minute 10
        assert _cron_matches("*/5 * * * *", now) is True  # 10 % 5 == 0
        assert _cron_matches("*/7 * * * *", now) is False  # 10 % 7 != 0

    def test_range_matches(self):
        from ui.cli import _cron_matches

        now = time.struct_time((2026, 7, 1, 9, 5, 0, 2, 182, 0))  # hour 9
        assert _cron_matches("* 9-17 * * *", now) is True
        assert _cron_matches("* 10-17 * * *", now) is False
