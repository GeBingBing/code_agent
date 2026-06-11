"""User-profile slash commands — /whoami, /remember, /forget, /profile.

These are the explicit-control counterpart to the implicit auto-extractor:
users can directly inspect, add, and remove facts about themselves
without relying on regex.

Commands:
  /whoami                       — show the user profile
  /remember <key> <value>       — store a fact (mapped to a known field
                                  if possible, else appended to important_facts)
  /forget <key>                 — remove a fact, field, or preference
  /profile                      — interactive-style profile summary
  /profile clear                — wipe profile (requires explicit "clear")

Auto-register: import this module from the command registry
(see agent/commands/__init__.py) to make these commands available.
"""

import os
from .base import SlashCommand, registry
from ..core.user_profile import UserProfile


# ── Helpers ─────────────────────────────────────────────────────────

def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"

def _cyan(text: str) -> str:
    return f"\033[36m{text}\033[0m"

def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m"

def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m"

def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m"


def _get_profile_from_ctx(ctx: dict):
    """Get the active UserProfile, preferring engine's instance if present."""
    engine = ctx.get("engine")
    if engine is not None and getattr(engine, "user_profile", None) is not None:
        return engine.user_profile
    # Fall back to loading from disk (e.g., if command is run before engine init)
    return UserProfile.load()


# ── /whoami ────────────────────────────────────────────────────────


async def _handle_whoami(args: str, ctx: dict) -> str:
    """Show what the agent knows about the user."""
    profile = _get_profile_from_ctx(ctx)

    if profile.is_empty():
        return (
            f"{_yellow('No profile data yet.')}\n"
            f"  Tell me about yourself — e.g. {_cyan('\"I am hay, prefer Chinese\"')}\n"
            f"  Or use {_cyan('/remember <key> <value>')} to save a fact explicitly."
        )

    lines = [f"{_bold('User profile:')}", ""]
    if profile.name:
        lines.append(f"  {_cyan('Name:')}          {profile.name}")
    if profile.preferred_name and profile.preferred_name != profile.name:
        lines.append(f"  {_cyan('Preferred:')}     {profile.preferred_name}")
    if profile.pronouns:
        lines.append(f"  {_cyan('Pronouns:')}      {profile.pronouns}")
    if profile.language:
        lines.append(f"  {_cyan('Language:')}      {profile.language}")
    if profile.timezone:
        lines.append(f"  {_cyan('Timezone:')}      {profile.timezone}")
    if profile.expertise_level:
        lines.append(f"  {_cyan('Expertise:')}     {profile.expertise_level}")

    if profile.preferences:
        lines.append("")
        lines.append(f"  {_bold('Preferences:')}")
        for k, v in profile.preferences.items():
            lines.append(f"    - {k}: {v}")

    if profile.important_facts:
        lines.append("")
        lines.append(f"  {_bold(f'Known facts ({len(profile.important_facts)}):')}")
        for fact in profile.important_facts[-10:]:
            lines.append(f"    - {fact}")
        if len(profile.important_facts) > 10:
            lines.append(f"    {_dim(f'... and {len(profile.important_facts) - 10} more')}")

    if profile.custom_instructions:
        lines.append("")
        lines.append(f"  {_bold('Custom instructions:')}")
        lines.append(f"    {profile.custom_instructions}")

    if profile.updated_at:
        lines.append("")
        lines.append(f"  {_dim(f'Updated: {profile.updated_at}')}")

    return "\n".join(lines)


# ── /remember ──────────────────────────────────────────────────────


async def _handle_remember(args: str, ctx: dict) -> str:
    """Store a fact about the user. Maps to known fields where possible.

    Examples:
      /remember name hay
      /remember user.name hay              (alias for "name")
      /remember language Chinese
      /remember code_style "PEP 8 + type hints"
      /remember favorite_editor vim
    """
    profile = _get_profile_from_ctx(ctx)
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        return f"{_yellow('usage:')} /remember <key> <value>"
    key, value = parts[0].strip(), parts[1].strip()
    if not key or not value:
        return f"{_yellow('usage:')} /remember <key> <value>"

    before_summary = profile.summary()
    profile.remember_fact(key, value)
    after_summary = profile.summary()

    # If key mapped to a field, mention that
    norm_key = key.lower().lstrip("user.").strip()
    if norm_key in profile.FIELD_ALIASES:
        field_name = profile.FIELD_ALIASES[norm_key]
        return (
            f"{_green('✓')} Saved to profile.{field_name}: {value}\n"
            f"  Profile now: {after_summary}"
        )
    return (
        f"{_green('✓')} Saved to important_facts: {key} = {value[:60]}\n"
        f"  Profile now: {after_summary}"
    )


# ── /forget ────────────────────────────────────────────────────────


async def _handle_forget(args: str, ctx: dict) -> str:
    """Remove a fact, field, or preference from the profile.

    Tries in order: known field, important_facts entry, preference.
    """
    profile = _get_profile_from_ctx(ctx)
    key = args.strip()
    if not key:
        return f"{_yellow('usage:')} /forget <key>"

    if profile.forget(key):
        return f"{_green('✓')} Forgot '{key}'.\n  Profile now: {profile.summary()}"
    return f"{_yellow('No matching key:')} '{key}'"


# ── /profile ───────────────────────────────────────────────────────


async def _handle_profile(args: str, ctx: dict) -> str:
    """Show or clear the user profile.

    Subcommands:
      (no args)     — show profile fields (alias for /whoami)
      clear         — wipe all profile data
    """
    profile = _get_profile_from_ctx(ctx)
    args = args.strip()

    if args == "clear":
        profile.clear()
        return f"{_green('✓')} Profile cleared."

    # Default: show (same as /whoami but slightly different framing)
    return await _handle_whoami("", ctx)


# ── Registration ───────────────────────────────────────────────────

registry.register(SlashCommand(
    name="whoami",
    handler=_handle_whoami,
    description="Show what the agent knows about you",
    usage="/whoami",
))

registry.register(SlashCommand(
    name="remember",
    handler=_handle_remember,
    description="Remember a fact about yourself (e.g. /remember name hay)",
    usage="/remember <key> <value>",
))

registry.register(SlashCommand(
    name="forget",
    handler=_handle_forget,
    description="Forget a fact about yourself (e.g. /forget name)",
    usage="/forget <key>",
))

registry.register(SlashCommand(
    name="profile",
    handler=_handle_profile,
    description="Show or clear the user profile (alias of /whoami)",
    usage="/profile [clear]",
))
