"""Slash command system — Claude Code-style /commands for the coding agent."""

from .base import CommandRegistry, SlashCommand, registry
from . import builtin  # noqa: F401 — triggers command registration
from . import user_commands  # noqa: F401 — PR-14: /whoami /remember /forget /profile
