"""Ralph TDD supervisor hook (PR-19, extracted from AgentEngine).

Runs on BEFORE_TOOL_EXECUTION. Checks the pending tool call against
the TDD state machine and raises InvalidTDDTransition on violation.
The engine catches the raise and surfaces it as a tool error to the LLM.

Originally `AgentEngine._ralph_check_hook` — extracted to its own
class so the engine doesn't need to know about TDD semantics.
"""

from __future__ import annotations

from typing import Any

from ..core.tdd_state_machine import InvalidTDDTransition


class RalphCheckHook:
    """Validate tool calls against the TDD state machine.

    Constructor takes the TDD supervisor; the engine wires this hook
    into BEFORE_TOOL_EXECUTION. Returning the payload unchanged means
    the tool may proceed; raising short-circuits the call.
    """

    def __init__(self, ralph):
        self._ralph = ralph

    async def __call__(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        tool_name = payload.get("tool", "")
        args = payload.get("args", {})
        violation = await self._ralph.check(tool_name, args)
        if violation:
            raise InvalidTDDTransition(violation)
        return payload
