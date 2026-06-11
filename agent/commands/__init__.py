"""Slash command system — Claude Code-style /commands for the coding agent."""

from . import (
    builtin,  # noqa: F401 — triggers command registration
    user_commands,  # noqa: F401 — PR-14: /whoami /remember /forget /profile
)
from .base import CommandRegistry as CommandRegistry  # noqa: F401 — re-export
from .base import SlashCommand as SlashCommand  # noqa: F401 — re-export
from .base import registry as registry  # noqa: F401 — re-export
