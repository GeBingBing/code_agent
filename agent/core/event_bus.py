"""Event bus — async pub/sub for agent events.

Decouples producers (engine, hooks) from consumers (UI, logging, metrics).
Use `subscribe(type)` to get a queue that receives matching events, and
`emit(type, payload)` to broadcast.

Wildcards: subscribing to "*" receives every event type.

This is intentionally tiny — no priority, no replay, no filtering beyond type
matching. If you need richer semantics, layer them on top.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Event:
    """A single event flowing through the bus."""

    type: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class EventBus:
    """Async pub/sub. Subscribers receive events via asyncio.Queue.

    Thread-safety: not thread-safe — designed for use within a single asyncio
    event loop. Subscribers are expected to await get() in coroutines.
    """

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, event_type: str) -> asyncio.Queue:
        """Subscribe to events of a given type. Returns the queue.

        The queue is unbounded; consumers should drain it or risk memory growth.
        """
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[event_type].append(q)
        return q

    def unsubscribe(self, event_type: str, queue: asyncio.Queue) -> None:
        """Remove a queue from a type's subscriber list. No-op if missing."""
        if event_type in self._subscribers:
            try:
                self._subscribers[event_type].remove(queue)
            except ValueError:
                pass
            # Clean up empty list to keep stats tidy
            if not self._subscribers[event_type]:
                del self._subscribers[event_type]

    async def emit(self, event_type: str, payload: dict = None) -> None:
        """Broadcast an event to all subscribers of `event_type` and "*".

        Returns immediately; queue puts happen in order. If a queue is full
        (unbounded in this implementation), put would await, but since we
        use unbounded queues, this never blocks.
        """
        event = Event(type=event_type, payload=payload or {})
        # Targeted subscribers
        for q in self._subscribers.get(event_type, []):
            await q.put(event)
        # Wildcard subscribers
        for q in self._subscribers.get("*", []):
            await q.put(event)

    def emit_nowait(self, event_type: str, payload: dict = None) -> None:
        """Synchronous variant. Same as emit() but uses put_nowait.

        Raises asyncio.QueueFull if a queue is bounded (it isn't by default,
        so this is effectively a no-op in this implementation).
        """
        event = Event(type=event_type, payload=payload or {})
        for q in self._subscribers.get(event_type, []):
            q.put_nowait(event)
        for q in self._subscribers.get("*", []):
            q.put_nowait(event)

    def stats(self) -> dict:
        """Snapshot of subscriber counts per event type. Useful for /status."""
        return {event_type: len(qs) for event_type, qs in self._subscribers.items()}

    def clear(self, event_type: str = None) -> None:
        """Remove all subscribers, or all subscribers for a specific type."""
        if event_type is None:
            self._subscribers.clear()
        else:
            self._subscribers.pop(event_type, None)
