"""Base command registry — pluggable slash command system."""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Callable, Awaitable


@dataclass
class SlashCommand:
    """A single slash command like /clear or /plan."""

    name: str
    description: str
    usage: str = ""  # e.g. "/model <name>"
    aliases: List[str] = field(default_factory=list)
    handler: Optional[Callable[[str, dict], Awaitable[str]]] = None

    # Handler signature: async def handle(self, args: str, ctx: dict) -> str
    # ctx contains: engine, cli, current_model, current_mode, etc.


class CommandRegistry:
    """Global registry of slash commands."""

    def __init__(self):
        self._commands: Dict[str, SlashCommand] = {}

    def register(self, cmd: SlashCommand):
        """Register a slash command."""
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd

    def get(self, name: str) -> Optional[SlashCommand]:
        """Get a command by name or alias."""
        return self._commands.get(name)

    def list_all(self) -> List[SlashCommand]:
        """Return unique commands (deduplicate aliases)."""
        seen = set()
        result = []
        for cmd in self._commands.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                result.append(cmd)
        return sorted(result, key=lambda c: c.name)

    def matches(self, user_input: str) -> Optional[SlashCommand]:
        """Check if user input starts with a slash command. Returns command if found."""
        if not user_input.startswith("/"):
            return None
        # Strip leading / and split command name
        stripped = user_input[1:]  # remove '/'
        # Handle multi-word input like "/clear" or "/model gpt-4"
        parts = stripped.split(maxsplit=1)
        name = parts[0].lower()
        return self.get(name)


# Global singleton
registry = CommandRegistry()
