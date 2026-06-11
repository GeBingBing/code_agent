"""Tests for the EventBus pub/sub primitive."""

import asyncio
import pytest

from agent.core.event_bus import Event, EventBus


class TestEventBusBasic:
    def test_subscribe_returns_queue(self):
        bus = EventBus()
        q = bus.subscribe("test")
        assert isinstance(q, asyncio.Queue)

    def test_subscribe_adds_to_stats(self):
        bus = EventBus()
        bus.subscribe("a")
        bus.subscribe("a")
        bus.subscribe("b")
        stats = bus.stats()
        assert stats["a"] == 2
        assert stats["b"] == 1


class TestEventDelivery:
    @pytest.mark.asyncio
    async def test_emit_to_subscriber(self):
        bus = EventBus()
        q = bus.subscribe("foo")
        await bus.emit("foo", {"x": 1})
        event = q.get_nowait()
        assert event.type == "foo"
        assert event.payload == {"x": 1}
        assert isinstance(event.ts, float)

    @pytest.mark.asyncio
    async def test_emit_with_no_payload(self):
        bus = EventBus()
        q = bus.subscribe("ping")
        await bus.emit("ping")
        event = q.get_nowait()
        assert event.payload == {}

    @pytest.mark.asyncio
    async def test_only_matching_type_delivered(self):
        bus = EventBus()
        q_foo = bus.subscribe("foo")
        q_bar = bus.subscribe("bar")
        await bus.emit("foo", {"v": 1})
        # foo got it
        assert q_foo.get_nowait().payload == {"v": 1}
        # bar did not
        assert q_bar.empty()

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_receive(self):
        bus = EventBus()
        q1 = bus.subscribe("x")
        q2 = bus.subscribe("x")
        q3 = bus.subscribe("x")
        await bus.emit("x", {"hello": "world"})
        for q in (q1, q2, q3):
            assert q.get_nowait().payload == {"hello": "world"}


class TestWildcardSubscription:
    @pytest.mark.asyncio
    async def test_wildcard_receives_all_types(self):
        bus = EventBus()
        q = bus.subscribe("*")
        await bus.emit("foo", {})
        await bus.emit("bar", {"k": 2})
        await bus.emit("baz", {})
        types = [q.get_nowait().type for _ in range(3)]
        assert types == ["foo", "bar", "baz"]

    @pytest.mark.asyncio
    async def test_wildcard_also_in_targeted_subscribers(self):
        """An emit delivers to both targeted and wildcard subscribers."""
        bus = EventBus()
        q_target = bus.subscribe("foo")
        q_wild = bus.subscribe("*")
        await bus.emit("foo", {"v": 42})
        # Both queues receive
        assert q_target.get_nowait().payload == {"v": 42}
        assert q_wild.get_nowait().payload == {"v": 42}


class TestUnsubscribe:
    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        q = bus.subscribe("x")
        bus.unsubscribe("x", q)
        await bus.emit("x", {})
        assert q.empty()

    def test_unsubscribe_missing_queue_is_noop(self):
        bus = EventBus()
        q = bus.subscribe("x")
        bus.unsubscribe("x", q)
        # Second unsubscribe should be silent
        bus.unsubscribe("x", q)
        assert bus.stats() == {}


class TestEmitNowait:
    @pytest.mark.asyncio
    async def test_emit_nowait_basic(self):
        bus = EventBus()
        q = bus.subscribe("fast")
        bus.emit_nowait("fast", {"k": 1})
        event = q.get_nowait()
        assert event.payload == {"k": 1}

    def test_emit_nowait_works_without_await(self):
        """emit_nowait is fully synchronous, no need to await."""
        bus = EventBus()
        q = bus.subscribe("sync")
        bus.emit_nowait("sync", {"hello": 1})
        assert q.get_nowait().payload == {"hello": 1}


class TestStatsAndClear:
    def test_stats_empty_bus(self):
        bus = EventBus()
        assert bus.stats() == {}

    def test_clear_removes_all_subscribers(self):
        bus = EventBus()
        bus.subscribe("a")
        bus.subscribe("b")
        bus.clear()
        assert bus.stats() == {}

    def test_clear_specific_type(self):
        bus = EventBus()
        bus.subscribe("a")
        bus.subscribe("b")
        bus.clear("a")
        stats = bus.stats()
        assert "a" not in stats
        assert stats["b"] == 1


class TestEventDataclass:
    def test_event_default_payload_is_dict(self):
        e = Event(type="x")
        assert e.payload == {}

    def test_event_default_ts_is_now(self):
        e = Event(type="x")
        assert e.ts > 0
