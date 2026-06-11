"""Textual TUI for Coding Agent — Rich-enhanced interactive interface.

Requires: pip install textual

Features:
- Syntax highlighting for code blocks (via Rich)
- Diff view panel for file changes
- Progress indicator for long-running tasks
- Auto-detection of code language from markdown fences
- Graceful fallback to CLI if Textual not installed

Usage:
    coding-agent --tui       # Launch Textual TUI
    coding-agent --cli       # Use original raw CLI (default)
"""

import os
import re

try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, ScrollableContainer
    from textual.reactive import reactive
    from textual.widgets import (  # noqa: F401 — Footer reserved for future use
        Footer,
        Header,
        Input,
        Static,
    )

    TEXTUAL_AVAILABLE = True
except ImportError:
    TEXTUAL_AVAILABLE = False

try:
    from rich.console import Group
    from rich.syntax import Syntax
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


def _detect_language(content: str) -> str:
    """Detect programming language from markdown code fence or content heuristic."""
    fence_match = re.match(r"^```(\w+)", content)
    if fence_match:
        return fence_match.group(1)
    first_line = content.split("\n", 1)[0].strip()
    if (
        first_line.startswith("def ")
        or first_line.startswith("import ")
        or first_line.startswith("class ")
    ):
        return "python"
    if (
        first_line.startswith("function ")
        or first_line.startswith("const ")
        or first_line.startswith("let ")
    ):
        return "javascript"
    if first_line.startswith("package ") or first_line.startswith("func "):
        return "go"
    return "python"


if TEXTUAL_AVAILABLE:

    class RichChatMessage(Static):
        """A chat message with optional Rich syntax highlighting for code blocks."""

        def __init__(self, role: str, content: str):
            self.role = role
            self.raw_content = content
            super().__init__("")
            self._render_content()

        def _render_content(self):
            """Render content with syntax highlighting for code blocks."""
            color = {
                "user": "cyan",
                "assistant": "green",
                "tool": "yellow",
                "system": "dim",
                "error": "red",
            }.get(self.role, "white")
            prefix = {
                "user": ">",
                "assistant": "",
                "tool": "  ↳",
                "error": "  ✗",
                "system": "  ℹ",
            }.get(self.role, "")

            if not RICH_AVAILABLE or self.role in ("tool", "system", "error", "user"):
                display = f"[{color}]{prefix} {self.raw_content}[/{color}]"
                self.update(display)
                return

            parts = []
            pattern = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
            last_end = 0

            for match in pattern.finditer(self.raw_content):
                before = self.raw_content[last_end : match.start()]
                if before.strip():
                    parts.append(Text.from_markup(f"[{color}]{prefix} {before.strip()}[/{color}]"))

                lang = match.group(1) or _detect_language(match.group(2))
                code = match.group(2)
                try:
                    syntax = Syntax(code, lang, theme="monokai", line_numbers=False)
                    parts.append(syntax)
                except Exception:
                    parts.append(Text.from_markup(f"[dim]{code}[/dim]"))

                last_end = match.end()

            after = self.raw_content[last_end:]
            if after.strip():
                parts.append(Text.from_markup(f"[{color}]{prefix} {after.strip()}[/{color}]"))

            if not parts:
                display = f"[{color}]{prefix} {self.raw_content}[/{color}]"
                self.update(display)
                return

            group = Group(*parts)
            self.update(group)

        def append_text(self, text: str):
            """Append streaming text and re-render."""
            self.raw_content += text
            self._render_content()

    class DiffView(Static):
        """Panel showing file diff with colored additions/deletions."""

        def show_diff(self, old: str, new: str, path: str = ""):
            """Display a unified diff."""
            import difflib

            old_lines = old.splitlines(keepends=True)
            new_lines = new.splitlines(keepends=True)
            diff = list(
                difflib.unified_diff(old_lines, new_lines, fromfile=path, tofile=path, lineterm="")
            )

            if not diff:
                self.update("[dim]No changes[/dim]")
                return

            if RICH_AVAILABLE:
                lines = []
                for line in diff:
                    if line.startswith("+"):
                        lines.append(Text(line, style="green"))
                    elif line.startswith("-"):
                        lines.append(Text(line, style="red"))
                    elif line.startswith("@@"):
                        lines.append(Text(line, style="cyan"))
                    else:
                        lines.append(Text(line, style="dim"))
                self.update(Group(*lines))
            else:
                self.update("\n".join(diff))

        def clear(self):
            self.update("")

    class InfoPanel(Static):
        """Right-side panel showing context: active files, plan, spec status."""

        def update_info(self, files: list = None, plan: str = "", spec_status: str = ""):
            lines = ["[bold]Context[/bold]"]
            if files:
                lines.append("\n[dim]Files:[/dim]")
                for f in files[-5:]:
                    lines.append(f"  {f}")
            if plan:
                lines.append(f"\n[dim]Plan:[/dim]\n{plan[:200]}")
            if spec_status:
                lines.append(f"\n[dim]Spec:[/dim]\n{spec_status[:200]}")
            self.update("\n".join(lines) if len(lines) > 1 else "[dim]No context[/dim]")

    class StatusBar(Static):
        """Bottom status bar showing model, mode, tokens, and progress."""

        def __init__(self):
            super().__init__("")
            self._model = "unknown"
            self._mode = "default"
            self._tokens = 0
            self._estimated = False
            self._progress = 0
            self._total = 0
            self._label = ""

        def update_status(self, model: str, mode: str, tokens: int = 0, estimated: bool = False):
            self._model = model
            self._mode = mode
            self._tokens = tokens
            self._estimated = estimated
            self._refresh()

        def set_progress(self, current: int, total: int, label: str = ""):
            self._progress = current
            self._total = total
            self._label = label
            self._refresh()

        def clear_progress(self):
            self._progress = 0
            self._total = 0
            self._label = ""
            self._refresh()

        def _refresh(self):
            token_str = f"~{self._tokens} (估计)" if self._estimated else f"{self._tokens}"
            parts = [f"[dim]Model: {self._model}  Mode: {self._mode}  Tokens: {token_str}[/dim]"]
            if self._total > 0:
                pct = int(100 * self._progress / self._total)
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                parts.append(f"  [{bar}] {pct}% {self._label}")
            self.update("  ".join(parts))

    class CodingAgentTUI(App):
        """Textual TUI for the coding agent with Rich-enhanced components."""

        CSS = """
        Screen {
            layout: grid;
            grid-size: 2 1;
            grid-columns: 3fr 1fr;
        }
        #chat {
            height: 100%;
            border: solid $primary;
            padding: 1;
        }
        #info {
            height: 100%;
            border: solid $primary-darken-2;
            padding: 1;
            background: $surface-darken-1;
        }
        #status {
            height: 1;
            dock: bottom;
            background: $surface;
        }
        #input {
            dock: bottom;
            margin: 1 0;
        }
        .dim { color: $text-disabled; }
        """

        TITLE = "Coding Agent"
        SUB_TITLE = "TUI Mode"

        token_count: reactive[int] = reactive(0)
        current_model: reactive[str] = reactive("unknown")
        current_mode: reactive[str] = reactive("default")

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal():
                yield ScrollableContainer(id="chat")
                yield InfoPanel(id="info")
            yield StatusBar(id="status")
            yield Input(placeholder="Type a task or /command...", id="input")

        def on_mount(self):
            self.current_model = os.getenv("DEFAULT_MODEL", "moonshot-v1-8k")
            self.current_mode = os.getenv("AGENT_MODE", "default")
            status = self.query_one("#status", StatusBar)
            status.update_status(self.current_model, self.current_mode)
            self.query_one("#input", Input).focus()

            # PR-14: Fire ON_SESSION_START once per TUI session so the
            # default handler can load the user profile. Async-safe.
            try:
                import asyncio as _asyncio

                from agent.core.hooks import ON_SESSION_START, HookRegistry
                from agent.core.hooks_session import load_user_profile_on_start

                session_hooks = HookRegistry()
                session_hooks.register(ON_SESSION_START, load_user_profile_on_start)
                payload = {"session_id": f"tui-{id(self)}", "task": None}
                try:
                    _asyncio.get_event_loop()
                except RuntimeError:
                    pass
                # Schedule as a task — fires on next event loop tick
                _asyncio.ensure_future(session_hooks.execute(ON_SESSION_START, payload))
            except Exception:
                pass

        def _get_chat(self):
            return self.query_one("#chat", ScrollableContainer)

        def _get_info(self):
            return self.query_one("#info", InfoPanel)

        def _get_status(self):
            return self.query_one("#status", StatusBar)

        def on_input_submitted(self, event: Input.Submitted):
            user_input = event.value.strip()
            if not user_input:
                return

            chat = self._get_chat()
            chat.mount(RichChatMessage("user", user_input))
            event.input.clear()
            chat.scroll_end(animate=False)

            if user_input.startswith("/"):
                self.run_worker(self._handle_command(user_input))
            else:
                self.run_worker(self._handle_task(user_input))

        async def _handle_command(self, cmd: str):
            chat = self._get_chat()
            if cmd == "/help":
                chat.mount(
                    RichChatMessage(
                        "assistant",
                        "Commands: /help /clear /plan /commit /model /mode /memory /status /context /review /undo",
                    )
                )
            elif cmd == "/clear":
                await chat.query("*").remove()
                chat.mount(RichChatMessage("system", "Conversation cleared."))
            elif cmd.startswith("/mode"):
                parts = cmd.split()
                if len(parts) > 1 and parts[1] in ("plan", "default", "auto", "bypass"):
                    self.current_mode = parts[1]
                    os.environ["AGENT_MODE"] = parts[1]
                    chat.mount(RichChatMessage("system", f"Mode switched to {parts[1]}"))
                else:
                    chat.mount(RichChatMessage("system", f"Current mode: {self.current_mode}"))
            elif cmd == "/status":
                chat.mount(
                    RichChatMessage(
                        "system",
                        f"Model: {self.current_model}\nMode: {self.current_mode}\nTokens: {self.token_count}",
                    )
                )
            else:
                chat.mount(RichChatMessage("system", f"Unknown command: {cmd}"))
            chat.scroll_end(animate=False)

        async def _handle_task(self, task: str):
            from agent.core.engine import AgentConfig, AgentEngine

            config = AgentConfig(verbose=False)
            engine = AgentEngine(config)
            chat = self._get_chat()
            info = self._get_info()
            status = self._get_status()

            thinking_msg = RichChatMessage("system", "Thinking...")
            await chat.mount(thinking_msg)
            chat.scroll_end(animate=False)

            current_msg = None
            current_tool = ""

            try:
                async for event in engine.run_stream(task):
                    event_type = event.get("type")

                    if event_type == "step_start":
                        step = event.get("step", 0)
                        max_steps = event.get("max_steps", 20)
                        status.set_progress(step, max_steps, f"Step {step}/{max_steps}")

                    elif event_type == "content":
                        token = event.get("content", "")
                        if thinking_msg:
                            await thinking_msg.remove()
                            thinking_msg = None
                        if current_msg is None:
                            current_msg = RichChatMessage("assistant", "")
                            await chat.mount(current_msg)
                        current_msg.append_text(token)
                        chat.scroll_end(animate=False)

                    elif event_type == "content_end":
                        current_msg = None
                        status.clear_progress()

                    elif event_type == "tool_call":
                        if thinking_msg:
                            await thinking_msg.remove()
                            thinking_msg = None
                        tool_name = event.get("tool_name", "")
                        args = event.get("tool_args", {})
                        current_tool = tool_name
                        path = args.get("path", args.get("command", ""))[:60]
                        tool_msg = RichChatMessage("tool", f"{tool_name}: {path}")
                        await chat.mount(tool_msg)
                        chat.scroll_end(animate=False)

                        if tool_name in ("write_file", "read_file", "edit_file") and "path" in args:
                            info.update_info(files=[args["path"]])

                    elif event_type == "tool_result":
                        if not event.get("success") and event.get("error"):
                            err_msg = RichChatMessage(
                                "error", f"{current_tool}: {event.get('error', '')}"
                            )
                            await chat.mount(err_msg)
                            chat.scroll_end(animate=False)

                        if current_tool == "edit_file" and event.get("success"):
                            args = event.get("tool_args", {})
                            path = args.get("path", "")
                            old_str = args.get("old_string", "")
                            new_str = args.get("new_string", "")
                            if old_str or new_str:
                                diff_view = DiffView()
                                await chat.mount(diff_view)
                                diff_view.show_diff(old_str, new_str, path)
                                chat.scroll_end(animate=False)

                    elif event_type == "error":
                        if thinking_msg:
                            await thinking_msg.remove()
                            thinking_msg = None
                        err_msg = RichChatMessage("error", event.get("error", ""))
                        await chat.mount(err_msg)
                        chat.scroll_end(animate=False)

                    elif event_type == "final":
                        status.clear_progress()

                self.token_count = engine.total_input_tokens + engine.total_output_tokens
                status.update_status(
                    self.current_model,
                    self.current_mode,
                    self.token_count,
                    estimated=engine.last_usage_estimated,
                )

                if engine.current_project_dir:
                    info.update_info(files=[engine.current_project_dir])

            except Exception as e:
                if thinking_msg:
                    await thinking_msg.remove()
                err_msg = RichChatMessage("error", str(e))
                await chat.mount(err_msg)
                chat.scroll_end(animate=False)


def run_tui():
    """Entry point for Textual TUI."""
    if not TEXTUAL_AVAILABLE:
        print("Textual is not installed. Install it with: pip install textual")
        print("Falling back to CLI mode...")
        from ui.cli import main

        main()
        return

    app = CodingAgentTUI()
    app.run()
