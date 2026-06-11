"""Hook registry — lifecycle extension points for the agent engine.

Hooks are registered callbacks that fire at well-defined points in the agent
loop. The engine itself contains zero business logic — observers, logging,
permission checks, audit, telemetry all live in hooks.

Hook contract:
- A hook is a callable that takes a single payload and returns either:
  * None — payload unchanged, no effect
  * A new payload — substituted for downstream hooks and the engine
- Hooks can be sync or async; the registry awaits async ones
- Hooks run in registration order
- If a hook raises, the exception propagates to the engine (fail-loud)
- Returning a non-None value REPLACES the payload for subsequent hooks

Standard hook names (constants below) — engine is expected to fire each at
its semantic lifecycle point. Hooks can be added by users without modifying
engine code.
"""

import asyncio
from collections import defaultdict
from typing import Any, Callable

# ── Standard hook names ──────────────────────────────────────────────
# Fired by the engine at well-defined lifecycle points. Plugins register
# against these strings; the engine does not import any hook implementation.

# Task perception
BEFORE_PERCEIVE = "before_perceive"  # payload: {"task": str}

# LLM interaction
BEFORE_LLM_CALL = "before_llm_call"  # payload: {"messages": list, "system": str}
AFTER_LLM_CALL = "after_llm_call"  # payload: {"response": Any, "usage": dict|None}

# Decision
BEFORE_DECIDE = "before_decide"  # payload: {"response": Any}

# Tool execution
BEFORE_TOOL_EXECUTION = "before_tool_execution"  # payload: {"tool": str, "args": dict}
AFTER_TOOL_EXECUTION = (
    "after_tool_execution"  # payload: {"tool": str, "args": dict, "result": Any, "error": Any}
)

# Error handling
ON_ERROR = "on_error"  # payload: {"exception": Exception, "context": dict}

# Streaming
ON_TOKEN = "on_token"  # payload: {"chunk": str}

# Memory management
BEFORE_COMPACT = "before_compact"  # payload: {"messages": list}
AFTER_COMPACT = "after_compact"  # payload: {"summary": str}

# Session lifecycle
ON_SESSION_START = "on_session_start"  # payload: {"session_id": str, "task": str|None}
ON_SESSION_END = "on_session_end"  # payload: {"final_state": dict, "result": Any}


# All standard hook names — useful for listing in /status or docs
STANDARD_HOOKS = frozenset(
    {
        BEFORE_PERCEIVE,
        BEFORE_LLM_CALL,
        AFTER_LLM_CALL,
        BEFORE_DECIDE,
        BEFORE_TOOL_EXECUTION,
        AFTER_TOOL_EXECUTION,
        ON_ERROR,
        ON_TOKEN,
        BEFORE_COMPACT,
        AFTER_COMPACT,
        ON_SESSION_START,
        ON_SESSION_END,
    }
)


HookFn = Callable[[Any], Any]


class HookRegistry:
    """Plugin-style hook registry. Fire-and-replace, fail-loud semantics."""

    def __init__(self):
        self._hooks: dict[str, list[HookFn]] = defaultdict(list)

    def register(self, name_or_fn=None, fn: HookFn = None) -> HookFn:
        """Register a hook for `name`. Two call styles:

        Direct:    reg.register("before_llm_call", my_fn)
        Decorator: @reg.register
                   def my_fn(payload): ...

        Hooks fire in registration order.
        """
        # Decorator form: register("name") returns a decorator
        if name_or_fn is not None and fn is None and isinstance(name_or_fn, str):

            def decorator(real_fn):
                self._hooks[name_or_fn].append(real_fn)
                return real_fn

            return decorator
        # Decorator form: @reg.register (no parens) — name_or_fn is the function
        if name_or_fn is not None and fn is None and callable(name_or_fn):
            self._hooks[""].append(name_or_fn) if False else None  # not used
            # Treat as decorator with default name
            raise TypeError("Use @reg.register('hook_name') or reg.register('hook_name', fn)")
        # Direct form
        if not isinstance(name_or_fn, str) or fn is None:
            raise TypeError("register() requires (name, fn) or (name) as decorator factory")
        self._hooks[name_or_fn].append(fn)
        return fn

    def unregister(self, name: str, fn: HookFn) -> None:
        """Remove a specific hook. Raises ValueError if not registered."""
        if name not in self._hooks:
            raise KeyError(f"No hooks registered for {name!r}")
        self._hooks[name].remove(fn)
        if not self._hooks[name]:
            del self._hooks[name]

    async def execute(self, name: str, payload: Any = None) -> Any:
        """Run all hooks for `name` in order. Sync hooks run inline.

        If any hook returns non-None, that value becomes the new payload
        for subsequent hooks and the return value. A hook raising will
        propagate to the caller (the engine decides whether to catch).

        Coroutine detection:
          - `asyncio.iscoroutinefunction(fn)` catches `async def` and async lambdas
          - We also check `fn.__call__` for callable instances whose class
            defines `async def __call__` (e.g. our hook classes); the
            instance itself isn't a coroutine function, but its `__call__` is.
          - Defense in depth: if a "sync" call still returns a coroutine
            (e.g. a wrapped/partial'd async function), we await it. This
            prevents the silent "coroutine was never awaited" warning.
        """
        for fn in self._hooks.get(name, []):
            # Using __call__ to detect async, not to test callability — B004 false-positive.
            call_attr = getattr(fn, "__call__", None)  # noqa: B004
            if asyncio.iscoroutinefunction(fn) or asyncio.iscoroutinefunction(call_attr):
                result = await fn(payload)
            else:
                result = fn(payload)
                if asyncio.iscoroutine(result):
                    # Safety net: a registered "sync" hook actually returned
                    # a coroutine. Await it instead of leaking it as payload.
                    result = await result
            if result is not None:
                payload = result
        return payload

    def has(self, name: str) -> bool:
        """True if at least one hook is registered for `name`."""
        return name in self._hooks

    def count(self, name: str) -> int:
        """Number of hooks registered for `name`."""
        return len(self._hooks.get(name, []))

    def names(self) -> list[str]:
        """All registered hook names (not necessarily standard ones)."""
        return list(self._hooks.keys())

    def clear(self, name: str = None) -> None:
        """Remove hooks for a specific name, or all hooks if name is None."""
        if name is None:
            self._hooks.clear()
        else:
            self._hooks.pop(name, None)

    def stats(self) -> dict:
        """Snapshot of hook counts per name. Useful for /status."""
        return {name: len(fns) for name, fns in self._hooks.items()}
