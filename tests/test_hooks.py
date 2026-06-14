"""Tests for the HookRegistry lifecycle-extension primitive."""

import pytest

from agent.core.hooks import (
    AFTER_TOOL_EXECUTION,
    BEFORE_LLM_CALL,
    ON_ERROR,
    ON_SESSION_END,
    ON_SESSION_START,
    ON_TOKEN,
    STANDARD_HOOKS,
    HookRegistry,
)


class TestRegisterAndExecute:
    @pytest.mark.asyncio
    async def test_register_and_execute_sync(self):
        reg = HookRegistry()
        reg.register("x", lambda p: p + 1)
        result = await reg.execute("x", payload=10)
        assert result == 11

    @pytest.mark.asyncio
    async def test_register_and_execute_async(self):
        reg = HookRegistry()

        async def hook(p):
            return p * 2

        reg.register("x", hook)
        result = await reg.execute("x", payload=5)
        assert result == 10

    @pytest.mark.asyncio
    async def test_execute_no_hooks_returns_payload_unchanged(self):
        reg = HookRegistry()
        result = await reg.execute("nobody_home", payload={"k": 1})
        assert result == {"k": 1}

    def test_register_returns_fn_for_decorator_use(self):
        reg = HookRegistry()

        @reg.register("decorated")
        def my_hook(p):
            return p

        assert reg.count("decorated") == 1
        assert my_hook.__name__ == "my_hook"


class TestOrdering:
    @pytest.mark.asyncio
    async def test_hooks_fire_in_registration_order(self):
        reg = HookRegistry()
        log = []
        reg.register("x", lambda p: log.append("a") or p)
        reg.register("x", lambda p: log.append("b") or p)
        reg.register("x", lambda p: log.append("c") or p)
        await reg.execute("x", payload=None)
        assert log == ["a", "b", "c"]


class TestPayloadTransformation:
    @pytest.mark.asyncio
    async def test_hook_returning_none_keeps_payload(self):
        reg = HookRegistry()
        reg.register("x", lambda p: None)  # observer — no transform
        result = await reg.execute("x", payload={"v": 1})
        assert result == {"v": 1}

    @pytest.mark.asyncio
    async def test_hook_return_replaces_payload(self):
        reg = HookRegistry()
        reg.register("x", lambda p: {"replaced": True})
        result = await reg.execute("x", payload={"original": True})
        assert result == {"replaced": True}

    @pytest.mark.asyncio
    async def test_chained_hooks_transform_in_order(self):
        reg = HookRegistry()
        reg.register("x", lambda p: p + [1])
        reg.register("x", lambda p: p + [2])
        reg.register("x", lambda p: p + [3])
        result = await reg.execute("x", payload=[])
        assert result == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_mixed_sync_async_chained(self):
        reg = HookRegistry()
        reg.register("x", lambda p: p + 1)

        async def double(p):
            return p * 2

        reg.register("x", double)
        result = await reg.execute("x", payload=3)
        # First hook: 3+1=4, then second hook: 4*2=8
        assert result == 8


class TestErrorPropagation:
    @pytest.mark.asyncio
    async def test_sync_hook_exception_propagates(self):
        reg = HookRegistry()

        def bad(p):
            raise ValueError("oops")

        reg.register("x", bad)
        with pytest.raises(ValueError, match="oops"):
            await reg.execute("x", payload=None)

    @pytest.mark.asyncio
    async def test_async_hook_exception_propagates(self):
        reg = HookRegistry()

        async def bad(p):
            raise RuntimeError("async bad")

        reg.register("x", bad)
        with pytest.raises(RuntimeError, match="async bad"):
            await reg.execute("x", payload=None)

    @pytest.mark.asyncio
    async def test_exception_stops_subsequent_hooks(self):
        """A failing hook should halt the chain (fail-loud)."""
        reg = HookRegistry()
        calls = []
        reg.register("x", lambda p: calls.append("a") or p)

        def bad(p):
            calls.append("bad")
            raise ValueError("stop")

        reg.register("x", bad)
        reg.register("x", lambda p: calls.append("c") or p)
        with pytest.raises(ValueError):
            await reg.execute("x", payload=None)
        assert "c" not in calls


class TestUnregister:
    def test_unregister_removes_hook(self):
        reg = HookRegistry()

        def h(p):
            return p

        reg.register("x", h)
        assert reg.count("x") == 1
        reg.unregister("x", h)
        assert reg.count("x") == 0

    def test_unregister_missing_name_raises(self):
        reg = HookRegistry()
        with pytest.raises(KeyError):
            reg.unregister("never_registered", lambda p: p)

    def test_unregister_specific_hook_keeps_others(self):
        reg = HookRegistry()

        def h1(p):
            return p

        def h2(p):
            return p

        reg.register("x", h1)
        reg.register("x", h2)
        reg.unregister("x", h1)
        assert reg.count("x") == 1


class TestInspection:
    def test_has_returns_true_when_registered(self):
        reg = HookRegistry()
        reg.register("x", lambda p: p)
        assert reg.has("x") is True

    def test_has_returns_false_when_empty(self):
        reg = HookRegistry()
        assert reg.has("x") is False

    def test_names_lists_registered_hooks(self):
        reg = HookRegistry()
        reg.register("a", lambda p: p)
        reg.register("b", lambda p: p)
        assert set(reg.names()) == {"a", "b"}

    def test_stats_counts(self):
        reg = HookRegistry()
        reg.register("a", lambda p: p)
        reg.register("a", lambda p: p)
        reg.register("b", lambda p: p)
        stats = reg.stats()
        assert stats == {"a": 2, "b": 1}

    def test_clear_all(self):
        reg = HookRegistry()
        reg.register("a", lambda p: p)
        reg.register("b", lambda p: p)
        reg.clear()
        assert reg.names() == []

    def test_clear_specific(self):
        reg = HookRegistry()
        reg.register("a", lambda p: p)
        reg.register("b", lambda p: p)
        reg.clear("a")
        assert reg.names() == ["b"]


class TestStandardHookConstants:
    def test_all_standard_hooks_in_constant(self):
        """All 12 standard hooks should be defined as constants (PR-14: +1)."""
        assert len(STANDARD_HOOKS) == 17

    def test_critical_hooks_present(self):
        """The most commonly used hooks must be exported."""
        assert BEFORE_LLM_CALL in STANDARD_HOOKS
        assert AFTER_TOOL_EXECUTION in STANDARD_HOOKS
        assert ON_TOKEN in STANDARD_HOOKS
        assert ON_ERROR in STANDARD_HOOKS

    def test_session_lifecycle_hooks_present(self):
        """PR-14: ON_SESSION_START must be exported alongside ON_SESSION_END."""
        assert ON_SESSION_START in STANDARD_HOOKS
        assert ON_SESSION_END in STANDARD_HOOKS
