"""Tests for engine integration of EventBus + HookRegistry (PR-01).

Engine-level tests focus on:
- Engine initializes with bus + hooks
- Hooks can be registered/inspected via engine.hooks
- EventBus can be subscribed to
- Hook execution in isolation (bypassing full run_stream)
"""

import asyncio

import pytest

from agent.core.engine import AgentConfig, AgentEngine
from agent.core.hooks import (
    AFTER_LLM_CALL,
    BEFORE_LLM_CALL,
    BEFORE_TOOL_EXECUTION,
    ON_TOKEN,
)


class TestEngineHasBusAndHooks:
    def test_engine_initializes_event_bus(self):
        e = AgentEngine()
        assert e.event_bus is not None

    def test_engine_initializes_hook_registry(self):
        e = AgentEngine()
        assert e.hooks is not None

    def test_engine_event_bus_starts_empty(self):
        e = AgentEngine()
        assert e.event_bus.stats() == {}

    def test_engine_hooks_starts_with_ralph_registered(self):
        """Engine pre-registers the Ralph supervisor (PR-02).

        TDD-mode=off means ralph is not registered; default 'guided' means it is.
        """
        e = AgentEngine()  # default tdd_mode='guided'
        # Ralph registers on BEFORE_TOOL_EXECUTION
        assert e.hooks.has(BEFORE_TOOL_EXECUTION)

    def test_engine_hooks_empty_when_tdd_off(self):
        # PR-08 + PR-10 + PR-11 also register on BEFORE_TOOL_EXECUTION, so disable them all
        e = AgentEngine(
            AgentConfig(
                tdd_mode="off",
                audit_enabled=False,
                otel_enabled=False,
                enable_dual_review=False,
            )
        )
        # In off mode with audit + otel + dual review disabled, no BEFORE_TOOL_EXECUTION hooks
        assert not e.hooks.has(BEFORE_TOOL_EXECUTION)


class TestHookRegistrationOnEngine:
    def test_can_register_hook_via_engine(self):
        e = AgentEngine()
        e.hooks.register(BEFORE_LLM_CALL, lambda p: p)
        assert e.hooks.has(BEFORE_LLM_CALL)

    def test_can_subscribe_to_event_bus(self):
        e = AgentEngine()
        q = e.event_bus.subscribe(ON_TOKEN)
        assert e.event_bus.stats()[ON_TOKEN] == 1


class TestHookExecutionIsolated:
    """Test the hook registry via the engine's instance (no full run_stream)."""

    @pytest.mark.asyncio
    async def test_execute_hook_chain(self):
        e = AgentEngine()
        log = []
        e.hooks.register(BEFORE_LLM_CALL, lambda p: log.append("a") or p)
        e.hooks.register(BEFORE_LLM_CALL, lambda p: log.append("b") or p)
        await e.hooks.execute(BEFORE_LLM_CALL, {"messages": []})
        assert log == ["a", "b"]

    @pytest.mark.asyncio
    async def test_async_hook_via_engine(self):
        e = AgentEngine()

        async def slow(p):
            await asyncio.sleep(0)
            return p + 1

        e.hooks.register(BEFORE_LLM_CALL, slow)
        result = await e.hooks.execute(BEFORE_LLM_CALL, payload=10)
        assert result == 11


class TestEventBusEmissionIsolated:
    @pytest.mark.asyncio
    async def test_emit_to_subscriber_via_engine(self):
        e = AgentEngine()
        q = e.event_bus.subscribe(ON_TOKEN)
        await e.event_bus.emit(ON_TOKEN, {"chunk": "hello"})
        ev = q.get_nowait()
        assert ev.payload == {"chunk": "hello"}

    @pytest.mark.asyncio
    async def test_wildcard_subscriber_via_engine(self):
        e = AgentEngine()
        q = e.event_bus.subscribe("*")
        await e.event_bus.emit(BEFORE_LLM_CALL, {})
        await e.event_bus.emit(AFTER_LLM_CALL, {})
        types = [q.get_nowait().type for _ in range(2)]
        assert BEFORE_LLM_CALL in types
        assert AFTER_LLM_CALL in types


class TestStatsFromEngine:
    def test_event_bus_stats(self):
        e = AgentEngine()
        e.event_bus.subscribe(ON_TOKEN)
        e.event_bus.subscribe(ON_TOKEN)
        e.event_bus.subscribe(AFTER_LLM_CALL)
        stats = e.event_bus.stats()
        assert stats[ON_TOKEN] == 2
        assert stats[AFTER_LLM_CALL] == 1

    def test_hook_stats(self):
        e = AgentEngine()
        # Engine auto-registers PR-05 codmap + PR-10 OTel on BEFORE_LLM_CALL
        # and PR-10 OTel on AFTER_LLM_CALL — count deltas, not absolutes.
        before_baseline = e.hooks.stats().get(BEFORE_LLM_CALL, 0)
        after_baseline = e.hooks.stats().get(AFTER_LLM_CALL, 0)
        e.hooks.register(BEFORE_LLM_CALL, lambda p: p)
        e.hooks.register(BEFORE_LLM_CALL, lambda p: p)
        e.hooks.register(AFTER_LLM_CALL, lambda p: p)
        stats = e.hooks.stats()
        assert stats[BEFORE_LLM_CALL] == before_baseline + 2
        assert stats[AFTER_LLM_CALL] == after_baseline + 1


class TestHookConstants:
    def test_12_standard_hooks_listed(self):
        """PR-14: ON_SESSION_START was added → 11 → 12."""
        from agent.core.hooks import STANDARD_HOOKS

        assert len(STANDARD_HOOKS) == 17

    def test_hooks_exported_from_core(self):
        from agent.core import (
            EventBus,
            HookRegistry,
        )

        assert EventBus is not None
        assert HookRegistry is not None
