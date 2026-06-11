"""Default ON_SESSION_START handler — loads user profile into context.

This is the "memory auto-load" half of the root-cause fix for session
amnesia. When a session starts, this handler reads the user profile
from disk and surfaces it in the hook payload, so downstream hooks
or the engine can inject it into the system prompt.

Hook contract (per agent/core/hooks.py):
  - Input payload: dict (passed to the hook)
  - Returns: dict or None. If a dict is returned, it REPLACES the
    payload for subsequent hooks and the caller. None means "no change".
  - Can be sync or async.

This handler is async to match the pattern of other engine hooks,
even though its work is synchronous (file read + render).
"""

from typing import Any

from .user_profile import UserProfile


async def load_user_profile_on_start(payload: Any) -> Any:
    """Default ON_SESSION_START handler.

    Reads ~/.coding-agent/user_profile.json and injects its rendered
    <user_profile> XML into payload["user_profile"].

    Idempotent: if the profile is empty or unreadable, returns the
    payload unchanged. If user_profile is already in the payload,
    does not overwrite (lets a prior hook win).
    """
    if not isinstance(payload, dict):
        return payload  # pass-through for non-dict payloads

    # Respect prior hook output
    if payload.get("user_profile"):
        return payload

    try:
        profile = UserProfile.load()
    except Exception:
        # If load fails for any reason, don't break the session
        return payload

    if profile.is_empty():
        return payload

    payload["user_profile"] = profile.to_prompt()
    payload["user_profile_loaded"] = True
    return payload
